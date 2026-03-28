# -*- coding: utf-8 -*-
"""anicheck_daily.py — v3.4 (Syoboi-first + Grok-summary/preview, 動的クールダウン)

フロー:
  1. Syoboi Calendar API から向こう3日間の放送データを一括取得
  2. watch_list.json の監視対象とタイトルマッチ（部分一致）
  3. Syoboi から ep_num / station_id / start_time / status を直接確定
  4. 不足情報（summary, preview等）のみ Grok に問い合わせ（上限 MAX_GROK_CALLS_PER_DAY 回/日）
  5. broadcast_history / daily_schedule / episodes / watch_list を更新
  6. 既存 V2 データ構造（ファイルパス・JSONキー）との互換性を完全維持
"""
import argparse
import json
import re
import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from xai_sdk import Client
from xai_sdk.chat import user, system
from xai_sdk.tools import web_search

load_dotenv()  # ローカル開発用。GitHub Actionsでは不要（secretsで直接環境変数）

# =================================================================
# ロギング設定（Actionsのログでも見やすいように）
# =================================================================
log_dir = Path("fetch_logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "daily_fetch.log"

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

# =================================================================
# デバッグ用 JSONL フォーマッター
# =================================================================

class JsonlFormatter(logging.Formatter):
    """各ログレコードを1行のJSON（JSONL）として出力するフォーマッター。

    extra={"data": <任意のdict>} で渡したデータは "data" キーに含まれる。
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data is not None:
            entry["data"] = data
        return json.dumps(entry, ensure_ascii=False)


def setup_debug_logger(log_path: Path) -> logging.Logger:
    """デバッグ用構造化ログを JSONL ファイルに出力する Logger を生成して返す。

    Args:
        log_path: 出力先ファイルパス（例: logs/daily_fetch_debug.jsonl）

    Returns:
        設定済みの logging.Logger。
    """
    logger = logging.getLogger("anicheck_debug")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(JsonlFormatter())
        logger.addHandler(handler)
    logger.propagate = False  # ルートロガーへの伝播を防ぎ二重出力を避ける
    return logger


# =================================================================
# 定数・設定
# =================================================================

# 1日あたりの Grok API 呼び出し上限
MAX_GROK_CALLS_PER_DAY: int = 5
# Grokクールダウン: TV放送（地上波/BS/CS）は短め、配信は長め
GROK_COOLDOWN_TV_DAYS: int = 3      # TV放送作品のGrokクールダウン日数
GROK_COOLDOWN_STREAM_DAYS: int = 14 # 配信限定作品のGrokクールダウン日数
GROK_SHORT_COOLDOWN_DAYS: int = 2   # syoboi_tid設定済み作品の短期連続呼出抑制日数

# Syoboi Calendar API エンドポイント
SYOBOI_API_URL: str = "http://cal.syoboi.jp/json.php"
# Syoboi チャンネル一覧 HTML エンドポイント（JSON APIが廃止されたためHTMLスクレイピングへ移行）
SYOBOI_CHLIST_HTML_URL: str = "https://cal.syoboi.jp/mng?Action=ShowChList"
SYOBOI_REQUEST_TIMEOUT: int = 15  # seconds

# =================================================================
# 局名正規化マップ（Syoboi の ChName / Grok の局名 → 内部正規化キー）
# broadcast_history.json のキー散乱を防ぐ。
# =================================================================
STATION_NORMALIZE_MAP: Dict[str, str] = {
    # ── Tokyo MX ──────────────────────────────────────────────
    "tokyo mx": "mx",
    "tokyo mx 1": "mx",
    "東京mx": "mx",
    "東京mx1": "mx",
    "mxtv": "mx",
    "mx": "mx",
    # ── TBS ───────────────────────────────────────────────────
    "tbs": "tbs",
    "tbsテレビ": "tbs",
    # ── MBS ───────────────────────────────────────────────────
    "mbs": "mbs",
    "毎日放送": "mbs",
    "mbsテレビ": "mbs",
    # ── Fuji TV ───────────────────────────────────────────────
    "fujitv": "fujitv",
    "cx": "fujitv",
    "フジテレビ": "fujitv",
    "fuji tv": "fujitv",
    # ── TV Tokyo ──────────────────────────────────────────────
    "tx": "tx",
    "テレビ東京": "tx",
    "tv tokyo": "tx",
    "tvtokyo": "tx",
    # ── TV Asahi ──────────────────────────────────────────────
    "tv_asahi": "tv_asahi",
    "ex": "tv_asahi",
    "テレビ朝日": "tv_asahi",
    "tv asahi": "tv_asahi",
    # ── NTV ───────────────────────────────────────────────────
    "ntv": "ntv",
    "日本テレビ": "ntv",
    "日テレ": "ntv",
    "nippon tv": "ntv",
    # ── NHK ───────────────────────────────────────────────────
    "nhk": "nhk",
    "nhk総合": "nhk",
    "nhk bs": "nhk-bs",
    "nhk bs1": "nhk-bs1",
    "nhk bs2": "nhk-bs2",
    "nhk bsp": "nhk-bsp",
    "nhk bsプレミアム": "nhk-bsp",
    "nhk bs premium": "nhk-bsp",
    "nhk bs4k": "nhk-bs4k",
    "nhk 4k": "nhk-bs4k",
    "nhk eテレ": "nhk-e",
    "nhk教育": "nhk-e",
    # ── CBC ───────────────────────────────────────────────────
    "cbc": "cbc",
    "cbcテレビ": "cbc",
    # ── 地方キー局系 ─────────────────────────────────────────
    "tva": "tva",
    "テレビ愛知": "tva",
    "tvh": "tvh",
    "テレビ北海道": "tvh",
    "tvo": "tvo",
    "テレビ大阪": "tvo",
    "tvq": "tvq",
    "tvq九州放送": "tvq",
    "テレビ西日本": "tni",
    "tni": "tni",
    # ── BS channels ───────────────────────────────────────────
    "bs11": "bs11",
    "bs11デジタル": "bs11",
    "bs11!": "bs11",
    "at-x": "at-x",
    "atx": "at-x",
    "bs日テレ": "bs_ntv",
    "bs日本": "bs_ntv",
    "bs朝日": "bs_asahi",
    "bsフジ": "bs_fuji",
    "bs-tbs": "bs_tbs",
    "bstbs": "bs_tbs",
    "bs-tbsテレビ": "bs_tbs",
    "bstbsテレビ": "bs_tbs",
    "wowow": "wowow",
    "wowowライブ": "wowow",
    "wowowプライム": "wowow",
    "wowow prime": "wowow",
    "wowow live": "wowow",
    "animax": "animax",
    "アニマックス": "animax",
    "kids station": "kids-station",
    "キッズステーション": "kids-station",
    "bs anime saimai": "bs-anime",
    "bsアニメ": "bs-anime",
    # ── Streaming / Digital ───────────────────────────────────
    "abema": "abema",
    "abematv": "abema",
    "dアニメストア": "d-anime",
    "d-anime store": "d-anime",
    "dアニメ": "d-anime",
    "d anime store": "d-anime",
    "netflix": "netflix",
    "prime_video": "prime_video",
    "amazon prime": "prime_video",
    "amazon prime video": "prime_video",
    "prime video": "prime_video",
    "amazon": "prime_video",
    "hulu": "hulu",
    "disney+": "disney-plus",
    "u-next": "u-next",
    "crunchyroll": "crunchyroll",
    "funimation": "funimation",
}

# TV放送局（地上波・BS・CS）の正規化後 station_id セット。
# このセットに含まれない局 ID は「配信サービス」として扱う。
TV_BROADCAST_STATION_IDS: frozenset = frozenset({
    # 地上波
    "mx", "tbs", "mbs", "fujitv", "tx", "tv_asahi", "ntv",
    "nhk", "nhk-e", "cbc", "tva", "tvh", "tvo", "tvq", "tni",
    # BS / CS
    "nhk-bs", "nhk-bs1", "nhk-bs2", "nhk-bsp", "nhk-bs4k",
    "bs11", "at-x",
    "bs_ntv", "bs_asahi", "bs_fuji", "bs_tbs",
    "wowow", "animax", "kids-station", "bs-anime",
})


def normalize_station(raw_name: str) -> str:
    """局名を正規化キーに変換する。

    マッチング優先順: ① 完全一致 → ② 小文字変換後の完全一致 → ③ 部分一致
    いずれもヒットしない場合は小文字化して返す（フォールバック）。
    """
    if not raw_name:
        return raw_name
    stripped = raw_name.strip()
    # ① 完全一致
    if stripped in STATION_NORMALIZE_MAP:
        return STATION_NORMALIZE_MAP[stripped]
    # ② 大文字小文字無視の完全一致
    lower = stripped.lower()
    if lower in STATION_NORMALIZE_MAP:
        return STATION_NORMALIZE_MAP[lower]
    # ③ キーが入力に含まれる、または入力がキーに含まれる（最長マッチを優先）
    best_key = ""
    best_val = ""
    for key, val in STATION_NORMALIZE_MAP.items():
        if (key in lower or lower in key) and len(key) > len(best_key):
            best_key = key
            best_val = val
    if best_val:
        return best_val
    return lower  # フォールバック: そのまま小文字化


def is_tv_broadcast(station_id: str) -> bool:
    """station_id が TV放送局（地上波/BS/CS）かどうかを判定する。

    Args:
        station_id: normalize_station() が返す正規化後の局 ID。

    Returns:
        TV放送局なら True、配信サービスなら False。
    """
    return station_id in TV_BROADCAST_STATION_IDS


# =================================================================
# Pydantic モデル（Grok 応答バリデーション）
# =================================================================

class BroadcastEntry(BaseModel):
    station_id: str
    start_time: str
    status: str = "normal"

    @field_validator("station_id", mode="before")
    @classmethod
    def normalize_sid(cls, v: str) -> str:
        return normalize_station(v)


class EpisodeSchedule(BaseModel):
    ep_num: int
    title: Optional[str] = None
    summary: Optional[str] = None
    preview_youtube_id: Optional[str] = None
    broadcasts: List[BroadcastEntry] = Field(default_factory=list)


# =================================================================
# Grok API
# =================================================================

SYSTEM_PROMPT = """# 役割
あなたは日本のアニメのエピソードあらすじと予告編情報を収集する専門の調査員です。
放送スケジュール・放送局・配信サービスの情報は**扱いません**。あらすじと予告編YouTube IDのみを調査してください。

# 探索フェーズ（情報の海を泳ぐ）
指定された作品の「最新エピソードのあらすじ要約」と「予告編の YouTube ID」を、全方位から幅広く収集してください。
- ツール（web_search, x_search）を必ず駆使し、公式サイト、公式Twitter/X、動画配信サービス、ニュースサイト（ANN、Natalie等）から情報を集めてください。
- 検索時は作品名・略称・エピソード番号などを組み合わせて検索し、公式YouTubeチャンネルも確認してください。

# 抽出フェーズ（情報の構造化）
集めた情報から、以下の1つのJSONブロック（```json ... ```）を作成してください。

Episode_Summary_And_Preview JSONブロック
「最新エピソードのあらすじ要約」と「予告編の YouTube ID」。放送進捗や放送予定に関する情報は不要です。
{
  "ep_num": 整数 (必ず最新話の番号。情報がない場合は 0),
  "summary": "あらすじ要約（3行以内）。情報がない場合は null",
  "preview_youtube_id": "予告のYouTube 動画IDのみ（11文字）。例: https://www.youtube.com/watch?v=dQw4w9WgXcQ の場合は 'dQw4w9WgXcQ'。URLは絶対に入れないこと。11文字のIDのみを格納すること。情報がない場合は null"
}

# 検証フェーズ（厳格な制約とハルシネーション排除）
最後に、集めた情報を厳しく精査します。以下のルールに違反する情報は捨ててください。
- 【重要】出力に含める情報は、公式サイト、放送局公式、信頼できるニュースソースで裏付けが取れたもののみとしてください。
- ソースのない噂や推測による捏造は絶対に行わないでください。確認できない項目は `null` にしてください。
- `preview_youtube_id` はURLではなく、'watch?v=' の後に続く**11文字の動画IDのみ**を格納してください。フルURLや不完全な文字列が混入した場合は、直ちにID部分のみを抽出して整形してください。
- 出力は上記の1つのJSONブロックと、最後に【ソース確認】（参照したURL）のみとし、余計な解説は省いてください。"""


def call_grok_for_anime(
    title: str,
    official_url: Optional[str] = None,
    current_history: Optional[Dict[str, Any]] = None,  # 後方互換のため保持（未使用）
    ep_num: Optional[int] = None,
) -> str:
    """Grokに作品の最新エピソードのあらすじと予告編YouTube IDを問い合わせる。

    Args:
        title: 作品名。
        official_url: 公式サイトURL（参考情報として提示）。
        current_history: 後方互換のためシグネチャに保持（本関数内では未使用）。
        ep_num: Syoboi で確認済みのエピソード番号。指定時はそのエピソードに焦点を絞る。

    Returns:
        Grok の応答テキスト（失敗時は空文字）。
    """
    client = Client()  # XAI_API_KEY が環境変数にある場合、api_key=不要
    url_hint = f"\n公式URL（参考）：{official_url}" if official_url else ""

    if ep_num is not None and ep_num > 0:
        # Syoboi でエピソード確認済み: そのエピソードのあらすじと予告 YouTube ID のみ依頼
        user_input = (
            f"作品名：{title}{url_hint}\n\n"
            f"【対象エピソード】第 {ep_num} 話 のあらすじ（3行以内）と予告編のYouTube IDのみを取得してください。\n"
            f"他のエピソードの情報は不要です。必ず第 {ep_num} 話に絞って検索してください。\n"
            f"YouTube IDはURLではなく動画IDのみ（'watch?v='の後の11文字）を格納してください。\n"
            f"放送日時・放送局・放送スケジュール等の情報は不要です。\n"
            f"システムプロンプトで指定した Episode_Summary_And_Preview JSONブロックを1つだけ出力してください。"
        )
    else:
        # エピソード不明: 最新エピソードのあらすじと予告 YouTube ID を依頼
        user_input = (
            f"作品名：{title}{url_hint}\n\n"
            f"この作品の最新エピソードのあらすじ（3行以内）と予告編のYouTube IDのみを取得してください。\n"
            f"YouTube IDはURLではなく動画IDのみ（'watch?v='の後の11文字）を格納してください。\n"
            f"放送日時・放送局・放送スケジュール等の情報は不要です。\n"
            f"システムプロンプトで指定した Episode_Summary_And_Preview JSONブロックを1つだけ出力してください。"
        )

    chat = client.chat.create(
        model="grok-4-1-fast-reasoning",  # 安価でツール対応良好
        tools=[web_search()],             # サーバー側で自動検索実行
        tool_choice="auto",
    )

    chat.append(system(SYSTEM_PROMPT))
    chat.append(user(user_input))

    response = chat.sample()
    return response.content or ""


def parse_grok_output(text: str, title: str) -> Optional[Dict[str, Any]]:
    """Grokの応答テキストから Episode_Summary_And_Preview JSONブロックを Pydantic でバリデーションしつつ抽出する。

    Returns:
        {"today": EpisodeSchedule | None, "sources": str}
        テキストが空/None の場合も {"today": None, "sources": "..."} を返す。
        JSONパース・バリデーション失敗時も {"today": None, ...} を返し、None は返さない。
    """
    # 空レスポンス・None の早期リターン（GrokがAPIエラー等で空を返したケース）
    if not text or not text.strip():
        logging.warning(f"  ⚠️ Grokレスポンスが空です（空文字またはNone）: {title}")
        return {"today": None, "sources": "Grokレスポンスなし（空文字）"}

    json_blocks = re.findall(r"```json\s*([\s\S]*?)\s*```", text)
    if not json_blocks:
        # フェンスなしの生JSONフォールバック（先頭1つを採用）
        json_blocks = re.findall(
            r"(\{[\s\S]*?\})(?=\s*(?:\【ソース確認】|$))", text, re.DOTALL
        )
        if not json_blocks:
            logging.error(f"  JSONブロックが見つかりません: {title}")
            return {"today": None, "sources": "JSONブロック抽出失敗"}

    try:
        raw_today = json_blocks[0].strip()
        # 空オブジェクト（情報なし）は None として扱う
        today_data: Optional[EpisodeSchedule] = (
            None if raw_today in ("{}", "{ }") else EpisodeSchedule.model_validate_json(raw_today)
        )
        # summary と preview_youtube_id が空文字の場合は明示的に None に正規化
        if today_data is not None:
            if today_data.summary == "":
                today_data.summary = None
            if today_data.preview_youtube_id == "":
                today_data.preview_youtube_id = None
            # YouTube ID の長さ検証: 11文字でない場合はフルURLが混入している可能性あり
            if today_data.preview_youtube_id and len(today_data.preview_youtube_id) != 11:
                # URLが混入した場合は watch?v= 以降の11文字を抽出を試みる
                yt_match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", today_data.preview_youtube_id)
                if yt_match:
                    logging.warning(
                        f"  ⚠️ preview_youtube_id にURLが混入 → IDを抽出: {today_data.preview_youtube_id[:60]}"
                    )
                    today_data.preview_youtube_id = yt_match.group(1)
                else:
                    logging.warning(
                        f"  ⚠️ preview_youtube_id が不正（11文字でなく、URLからも抽出不可） → None にリセット: "
                        f"'{today_data.preview_youtube_id[:30]}'"
                    )
                    today_data.preview_youtube_id = None
    except Exception as e:
        logging.warning(f"  EpisodeSchedule バリデーション失敗（スキップ）: {e}")
        today_data = None

    source_match = re.search(r"【ソース確認】([\s\S]*)", text, re.DOTALL)
    sources = source_match.group(1).strip() if source_match else "ソース抽出失敗"

    return {"today": today_data, "sources": sources}


# =================================================================
# Syoboi Calendar API
# =================================================================

def fetch_syoboi_channels() -> Dict[str, str]:
    """Syoboi チャンネル一覧 HTML から {ChID: ChName} マップを取得する。

    HTMLテーブル（class="tframe output"）をBeautifulSoupで解析し、
    各行の2列目(ChID)と4列目(ChName)を抽出する。

    Returns:
        局 ID（文字列）→ 局名（文字列）の辞書。失敗時は空辞書。
    """
    logging.info("[LOG: START] Syoboi ChList HTML 取得")
    logging.info(f"  [INPUT] URL: {SYOBOI_CHLIST_HTML_URL}")
    try:
        resp = requests.get(
            SYOBOI_CHLIST_HTML_URL,
            timeout=SYOBOI_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        logging.info(f"  [THOUGHT] HTTP {resp.status_code} 取得成功。BeautifulSoupでlxmlパース開始")

        soup = BeautifulSoup(resp.content, "lxml")

        # class="tframe output" のテーブルを探す
        table = soup.find("table", class_="output")
        if table is None:
            logging.warning("  ⚠️ チャンネル一覧テーブルが見つかりません — 空マップで継続")
            return {}

        ch_map: Dict[str, str] = {}
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            # ヘッダ行（<th>のみ）はスキップ。データ行は <td> が4列以上
            if len(tds) < 4:
                continue
            ch_id = tds[1].get_text(strip=True)
            ch_name = tds[3].get_text(strip=True)
            if ch_id and ch_name:
                ch_map[ch_id] = ch_name

        logging.info(f"  [OUTPUT] Syoboi Channel (HTML): {len(ch_map)} 局取得")
        return ch_map
    except requests.exceptions.Timeout:
        logging.warning(f"  ⚠️ Syoboi ChList HTML タイムアウト ({SYOBOI_REQUEST_TIMEOUT}s) — 空マップで継続")
        return {}
    except requests.exceptions.ConnectionError as e:
        logging.warning(f"  ⚠️ Syoboi ChList HTML 接続エラー: {e} — 空マップで継続")
        return {}
    except requests.exceptions.HTTPError as e:
        logging.warning(f"  ⚠️ Syoboi ChList HTML HTTPエラー: {e} — 空マップで継続")
        return {}
    except Exception as e:
        logging.warning(f"  ⚠️ Syoboi ChList HTML 取得/解析失敗: {e} — 空マップで継続")
        return {}


def epoch_to_jst_iso(epoch_str: Any) -> Optional[str]:
    """Syoboi の StTime（Unix epoch 文字列）を ISO 8601 JST 形式に変換する。"""
    try:
        ts = int(epoch_str)
        jst = datetime.timezone(datetime.timedelta(hours=9))
        dt = datetime.datetime.fromtimestamp(ts, tz=jst)
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


def fetch_syoboi_proglist(start_date: datetime.date, days: int = 3) -> List[Dict[str, Any]]:
    """Syoboi ProgList から start_date から {days} 日間の放送予定を取得する。

    Returns:
        Syoboi の Items 値のリスト。失敗時は空リスト。
    """
    logging.info(
        f"[LOG: START] Syoboi ProgramByDate 取得 (Start={start_date.strftime('%Y%m%d')}, Days={days})"
    )
    try:
        resp = requests.get(
            SYOBOI_API_URL,
            params={
                "Req": "ProgramByDate",
                "Start": start_date.strftime("%Y%m%d"),
                "Days": str(days),
            },
            timeout=SYOBOI_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        items_raw = data.get("Programs", {})
        # Items は辞書形式（数値キー）または配列形式どちらでも対応
        items: List[Dict[str, Any]] = (
            list(items_raw.values()) if isinstance(items_raw, dict) else items_raw
        )
        logging.info(f"  [OUTPUT] Syoboi ProgramByDate: {len(items)} 件取得")
        return items
    except requests.exceptions.Timeout:
        logging.error(
            f"  ❌ Syoboi ProgramByDate タイムアウト ({SYOBOI_REQUEST_TIMEOUT}s) — 空リストで継続"
        )
        return []
    except requests.exceptions.ConnectionError as e:
        logging.error(f"  ❌ Syoboi ProgramByDate 接続エラー: {e} — 空リストで継続")
        return []
    except requests.exceptions.HTTPError as e:
        logging.error(f"  ❌ Syoboi ProgramByDate HTTPエラー: {e} — 空リストで継続")
        return []
    except ValueError as e:
        logging.error(f"  ❌ Syoboi ProgramByDate JSONパース失敗: {e} — 空リストで継続")
        return []
    except Exception as e:
        logging.error(f"  ❌ Syoboi ProgramByDate 取得失敗: {e}")
        return []


def map_syoboi_to_watchlist_by_tid(
    prog_items: List[Dict[str, Any]],
    active_animes: List[Dict[str, Any]],
    ch_map: Dict[str, str],
    debug_logger: Optional[logging.Logger] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Syoboi の放送アイテムを watch_list の各作品に TID ベースでマッチングする。

    マッチング戦略:
      ① watch_list エントリに syoboi_tid フィールドがある場合は Syoboi TID で直接マッチ（高精度・決定論的）
      ② syoboi_tid がない作品（または TID 未マッチ）はタイトル完全一致 → 部分一致にフォールバック（後方互換）

    Args:
        prog_items: Syoboi ProgramByDate のアイテムリスト。
        active_animes: watch_list の is_active 作品リスト。
        ch_map: Syoboi ChID → ChName マップ（本関数では参照しないが API 統一のために受け取る）。
        debug_logger: 指定時に詳細な構造化デバッグログを JSONL で出力する Logger。

    Returns:
        {anime_id: [matched_prog_items]} の辞書。anime_id は watch_list.json の anime_id キー。
    """
    logging.info("[LOG: START] Syoboi × watch_list TIDベース マッチング")
    logging.info(
        "[THOUGHT: syoboi_tid フィールドがある作品はTIDで直接マッチ（誤爆なし）。"
        "未設定の作品はタイトルマッチにフォールバック（後方互換）]"
    )

    # ── Step A: prog_items を TID でグループ化 ────────────────────────
    tid_to_items: Dict[str, List[Dict[str, Any]]] = {}
    for item in prog_items:
        tid = str(item.get("TID") or "").strip()
        if not tid:
            continue
        if tid not in tid_to_items:
            tid_to_items[tid] = []
        tid_to_items[tid].append(item)

    if debug_logger:
        debug_logger.debug(
            "tid_groups_generated",
            extra={"data": {"tid_count": len(tid_to_items), "tids": list(tid_to_items.keys())}},
        )

    results: Dict[str, List[Dict[str, Any]]] = {}
    tid_matched_anime_ids: set = set()
    tid_matched_tids: set = set()

    # ── Step B: syoboi_tid を持つ作品を TID で直接マッチ ─────────────
    for anime in active_animes:
        anime_id = anime["anime_id"]
        syoboi_tid = str(anime.get("syoboi_tid") or "").strip()
        if not syoboi_tid:
            continue
        if syoboi_tid in tid_to_items:
            results[anime_id] = tid_to_items[syoboi_tid]
            tid_matched_anime_ids.add(anime_id)
            tid_matched_tids.add(syoboi_tid)
            if debug_logger:
                debug_logger.debug(
                    "tid_direct_match",
                    extra={
                        "data": {
                            "anime_id": anime_id,
                            "syoboi_tid": syoboi_tid,
                            "hit_count": len(results[anime_id]),
                        }
                    },
                )
        else:
            if debug_logger:
                debug_logger.debug(
                    "tid_miss",
                    extra={"data": {"anime_id": anime_id, "syoboi_tid": syoboi_tid}},
                )

    logging.info(
        f"  [OUTPUT] TIDマッチ: {len(tid_matched_anime_ids)}/{len(active_animes)} 作品"
    )

    # ── Step C: syoboi_tid 未設定の作品はタイトルマッチにフォールバック ──
    remaining_animes = [a for a in active_animes if a["anime_id"] not in tid_matched_anime_ids]
    if remaining_animes:
        logging.info(
            f"  [THOUGHT: {len(remaining_animes)} 作品が syoboi_tid 未設定 → タイトルフォールバック開始]"
        )

        # タイトル索引を作成（残りの作品のみ）
        # key = 小文字化タイトル、value = anime_id
        watch_index: Dict[str, str] = {}
        # anime_id から anime オブジェクトへの逆引きマップ（TID保存用）
        anime_id_to_anime: Dict[str, Dict[str, Any]] = {}
        for anime in remaining_animes:
            anime_id = anime["anime_id"]
            anime_id_to_anime[anime_id] = anime
            title = anime.get("title", "")
            # シーズン表記（第2期・Season 2・2nd Season 等）の前までをベースタイトルとして抽出
            base_title = re.split(
                r"[\s　]+(?:第\d+期|Season\s*\d+|\d+nd\s+Season|\d+rd\s+Season|\d+th\s+Season|S\d+|シーズン\d+)",
                title,
                maxsplit=1,
            )[0].strip()
            for t in [title, base_title]:
                key = t.lower().replace("　", " ").strip()
                if key and key not in watch_index:
                    watch_index[key] = anime_id

        if debug_logger:
            debug_logger.debug(
                "fallback_watch_index_generated",
                extra={"data": {"watch_index": watch_index}},
            )

        # TIDマッチ済みの TID に属する prog_items は除外して誤爆を防ぐ
        fallback_items = [
            item for item in prog_items
            if str(item.get("TID") or "").strip() not in tid_matched_tids
        ]

        # タイトルフォールバックで syoboi_tid が確定した anime_id を追跡（重複代入防止）
        fallback_tid_assigned: set = set()

        unmatched_tids: set = set()
        for item in fallback_items:
            prog_title = (item.get("Title") or "").strip()
            if not prog_title:
                continue
            prog_lower = prog_title.lower()
            matched_id: Optional[str] = None
            match_type: str = "none"

            # ① 完全一致
            if prog_lower in watch_index:
                matched_id = watch_index[prog_lower]
                match_type = "exact"
            else:
                # ② 部分一致（4文字以上のキーのみ検索して誤爆を抑制）
                for key, aid in watch_index.items():
                    if len(key) >= 4 and (key in prog_lower or prog_lower in key):
                        matched_id = aid
                        match_type = "partial"
                        break

            if debug_logger:
                debug_logger.debug(
                    "title_fallback_match",
                    extra={
                        "data": {
                            "syoboi_title": prog_title,
                            "normalized_syoboi_title": prog_lower,
                            "match_type": match_type,
                            "matched_id": matched_id,
                        }
                    },
                )

            if matched_id:
                if matched_id not in results:
                    results[matched_id] = []
                results[matched_id].append(item)

                # タイトルフォールバックでマッチした場合、syoboi_tid を anime オブジェクトに保存する。
                # 次回以降は TID 直接マッチに昇格し、誤爆リスクを低減する。
                # 初回マッチ時のみ代入し、同一 anime に異なる TID が混在するのを防ぐ。
                if matched_id not in fallback_tid_assigned:
                    prog_tid = str(item.get("TID") or "").strip()
                    if prog_tid and not anime_id_to_anime.get(matched_id, {}).get("syoboi_tid"):
                        anime_id_to_anime[matched_id]["syoboi_tid"] = prog_tid
                        fallback_tid_assigned.add(matched_id)
                        logging.info(
                            f"  [THOUGHT: タイトルフォールバックでsyoboi_tid={prog_tid}を"
                            f"{matched_id}に設定 (match_type={match_type})]"
                        )
            else:
                unmatched_tids.add(item.get("TID", "?"))

        fallback_hit_count = len([a for a in remaining_animes if a["anime_id"] in results])
        logging.info(
            f"  [OUTPUT] タイトルフォールバック: {fallback_hit_count}/{len(remaining_animes)} 作品がヒット"
            f" (未マッチ TID 数={len(unmatched_tids)})"
        )

    logging.info(
        f"  [OUTPUT] マッチング合計: {len(results)}/{len(active_animes)} 作品が Syoboi にヒット"
        f" (TIDマッチ={len(tid_matched_anime_ids)}, タイトルフォールバック={len(results) - len(tid_matched_anime_ids)})"
    )
    return results


def build_broadcasts_from_syoboi(
    anime_id: str,
    title: str,
    syoboi_items: List[Dict[str, Any]],
    ch_map: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Syoboi の放送アイテムから daily_schedule エントリを生成する。

    Returns:
        daily_schedule 互換の broadcast 辞書リスト。
    """
    broadcasts: List[Dict[str, Any]] = []
    for item in syoboi_items:
        start_time = epoch_to_jst_iso(item.get("StTime"))
        if not start_time:
            continue  # 開始時刻が不明なものは除外

        # 局名を正規化
        raw_ch_id = str(item.get("ChID", ""))
        raw_ch_name = item.get("ChName") or ch_map.get(raw_ch_id) or raw_ch_id
        station_id = normalize_station(raw_ch_name) if raw_ch_name else raw_ch_id

        # Count（話数）は "1", "1.5" 等の文字列の場合あり → int に丸める
        try:
            ep_num = int(float(item.get("Count") or 0))
        except (ValueError, TypeError):
            ep_num = 0

        # Rank: 0=通常, 8=休止
        try:
            rank = int(item.get("Rank", 0))
        except (ValueError, TypeError):
            rank = 0
        status = "normal" if rank == 0 else ("suspension" if rank == 8 else "delayed")

        broadcasts.append(
            {
                "anime_id": anime_id,
                "title": title,
                "ep_num": ep_num,
                "_subtitle": (item.get("SubTitle") or "").strip() or None,
                "station_id": station_id,
                "start_time": start_time,
                "status": status,
                "_syoboi_tid": str(item.get("TID", "")),
            }
        )
    return broadcasts


# =================================================================
# ユーティリティ
# =================================================================

def load_json_file(path: Path, default: Any = None) -> Any:
    """UTF-8 JSON ファイルを読み込んで返す。

    Args:
        path: 読み込むファイルのパス。
        default: ファイル不在または JSON パース失敗時に返す値（デフォルト: None）。

    Returns:
        JSON デシリアライズ結果。失敗時は default を返す。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.error(f"  ❌ ファイルが見つかりません: {path}")
        return default
    except json.JSONDecodeError as e:
        logging.error(f"  ❌ JSONパース失敗 ({path}): {e}")
        return default


def save_json_file(path: Path, data: Any) -> None:
    """データを UTF-8 JSON ファイルとして保存する。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def needs_grok_enrichment(anime_id: str, ep_num: int) -> bool:
    """対象エピソードの summary が未取得かどうかを判定する。

    エピソードファイルが存在しない、または summary が空のとき True を返す。
    """
    ep_file = Path("database") / "episodes" / anime_id / f"ep{ep_num:03d}.json"
    if not ep_file.exists():
        return True
    try:
        ep_data = load_json_file(ep_file)
        return not ep_data.get("summary")
    except Exception:
        return True


def should_call_grok_with_cooldown(
    anime: Dict[str, Any],
    grok_call_count: int,
    max_grok_calls: int,
    today_str: str,
    broadcast_history: Optional[Dict[str, Any]] = None,
) -> bool:
    """Grokを呼び出すべきかクールダウンを考慮して判定する。

    - Grok呼び出し上限に達している場合は False
    - syoboi_tid が設定されている作品は基本 True だが、直近 GROK_SHORT_COOLDOWN_DAYS 日以内に
      Grokを呼んでいた場合は短期連続呼出を抑制して False を返す。
    - syoboi_tid がない作品（配信限定等）は last_grok_date を参照し、
      broadcast_history に TV局がある場合は GROK_COOLDOWN_TV_DAYS 日、
      それ以外（配信のみ）は GROK_COOLDOWN_STREAM_DAYS 日のクールダウンを適用する。
    """
    if grok_call_count >= max_grok_calls:
        logging.info(
            f"    [THOUGHT: {anime['anime_id']}] Grok上限 ({max_grok_calls}回/日) 到達のためスキップ"
        )
        return False

    anime_id = anime["anime_id"]
    syoboi_tid_val = str(anime.get("syoboi_tid") or "").strip()
    last_grok_date_str = anime.get("last_grok_date")

    # Syoboi にヒットする作品 (syoboi_tid がある) の場合:
    # 直近 GROK_SHORT_COOLDOWN_DAYS 日以内の再呼出を抑制する
    if syoboi_tid_val:
        if last_grok_date_str:
            last_grok_date = datetime.date.fromisoformat(last_grok_date_str)
            today = datetime.date.fromisoformat(today_str)
            elapsed_days = (today - last_grok_date).days
            if elapsed_days < GROK_SHORT_COOLDOWN_DAYS:
                logging.info(
                    f"    [THOUGHT: {anime_id}] syoboi_tid設定済みだが直近{elapsed_days}日以内にGrok呼出済 "
                    f"(<{GROK_SHORT_COOLDOWN_DAYS}日) → 短期連続抑制スキップ"
                )
                return False
        return True

    # Syoboi にヒットしない作品 (syoboi_tid がない) の場合: 動的クールダウンを適用
    if not last_grok_date_str:
        logging.info(f"    [THOUGHT: {anime_id}] last_grok_date がないため初回Grok呼出を許可")
        return True

    # broadcast_history から放送局タイプを判定し、クールダウン日数を動的に決定
    cooldown_days = GROK_COOLDOWN_STREAM_DAYS  # デフォルト: 配信サービス
    if broadcast_history is not None:
        anime_hist = broadcast_history.get(anime_id, {})
        platforms = anime_hist.get("platforms", {})
        if any(is_tv_broadcast(sid) for sid in platforms):
            cooldown_days = GROK_COOLDOWN_TV_DAYS
            logging.info(
                f"    [THOUGHT: {anime_id}] TV放送局を検出 → クールダウン {cooldown_days}日 を適用"
            )
        else:
            logging.info(
                f"    [THOUGHT: {anime_id}] TV局なし（配信のみ） → クールダウン {cooldown_days}日 を適用"
            )

    last_grok_date = datetime.date.fromisoformat(last_grok_date_str)
    today = datetime.date.fromisoformat(today_str)
    elapsed_days = (today - last_grok_date).days
    if elapsed_days >= cooldown_days:
        logging.info(
            f"    [THOUGHT: {anime_id}] Grokクールダウン経過 ({elapsed_days}日 >= {cooldown_days}日) → Grok呼出を許可"
        )
        return True
    else:
        logging.info(
            f"    [THOUGHT: {anime_id}] Grokクールダウン期間中 ({elapsed_days}日 < {cooldown_days}日, "
            f"前回: {last_grok_date_str}) → Grok呼出をスキップ"
        )
        return False


def update_history_from_syoboi(
    broadcast_history: Dict[str, Any],
    anime_id: str,
    title: str,
    syoboi_broadcasts: List[Dict[str, Any]],
    today_str: str,
) -> None:
    """Syoboi 放送データで broadcast_history を更新する（破壊的更新）。"""
    if anime_id not in broadcast_history:
        broadcast_history[anime_id] = {"title": title, "overall_latest_ep": 0, "platforms": {}}

    broadcast_history[anime_id]["title"] = title

    if "platforms" not in broadcast_history[anime_id]:
        broadcast_history[anime_id]["platforms"] = {}

    for bc in syoboi_broadcasts:
        ep_num = bc["ep_num"]
        sid = bc["station_id"]

        # overall_latest_ep を更新（既存より大きい場合のみ）
        prev_latest = broadcast_history[anime_id].get("overall_latest_ep") or 0
        if ep_num > prev_latest:
            broadcast_history[anime_id]["overall_latest_ep"] = ep_num

        # プラットフォームの進捗を更新
        if sid not in broadcast_history[anime_id]["platforms"]:
            broadcast_history[anime_id]["platforms"][sid] = {
                "last_ep_num": 0,
                "last_broadcast_date": None,
                "last_updated_at": today_str,
                "remarks": None,
            }
        plat = broadcast_history[anime_id]["platforms"][sid]
        if ep_num >= (plat.get("last_ep_num") or 0):
            plat["last_ep_num"] = ep_num
            plat["last_broadcast_date"] = bc["start_time"][:10]
            plat["last_updated_at"] = today_str


def save_episode_file(
    anime_id: str,
    ep_num: int,
    today_data: Optional[EpisodeSchedule],
    subtitle_from_syoboi: Optional[str] = None,
) -> None:
    """エピソードファイルを保存・マージする。

    既存ファイルがある場合は新データとマージし、summary や title が空の場合のみ上書きする。
    """
    ep_dir = Path("database") / "episodes" / anime_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    ep_file = ep_dir / f"ep{ep_num:03d}.json"

    # 既存ファイルを読み込む（なければ空辞書）
    existing: Dict[str, Any] = {}
    if ep_file.exists():
        try:
            existing = load_json_file(ep_file)
        except Exception:
            existing = {}

    existing.setdefault("anime_id", anime_id)
    existing.setdefault("ep_num", ep_num)

    # Syoboi のサブタイトルは title が未設定の場合のみ補完
    if not existing.get("title") and subtitle_from_syoboi:
        existing["title"] = subtitle_from_syoboi

    # Grok のエピソードデータで空フィールドを補完
    if today_data is not None:
        if not existing.get("title") and today_data.title:
            existing["title"] = today_data.title
        if not existing.get("summary") and today_data.summary:
            existing["summary"] = today_data.summary
        if not existing.get("preview_youtube_id") and today_data.preview_youtube_id:
            existing["preview_youtube_id"] = today_data.preview_youtube_id

    save_json_file(ep_file, existing)
    logging.info(f"    📄 エピソードファイル更新: {ep_file}")


# =================================================================
# メイン実行
# =================================================================

def main() -> None:
    """エントリーポイント。argparse で引数を処理し、メイン処理を実行する。"""
    parser = argparse.ArgumentParser(description="anicheck_daily — Syoboi-first daily fetch")
    parser.add_argument(
        "--debug-log",
        action="store_true",
        help="詳細なデバッグログを logs/daily_fetch_debug.jsonl に JSONL 形式で出力する",
    )
    args = parser.parse_args()

    # --debug-log フラグが立っている場合のみデバッグロガーをセットアップ
    debug_logger: Optional[logging.Logger] = None
    if args.debug_log:
        debug_log_path = log_dir / "daily_fetch_debug.jsonl"
        debug_logger = setup_debug_logger(debug_log_path)
        logging.info(f"[LOG: DEBUG-MODE] デバッグログ出力先: {debug_log_path}")

    today_date = datetime.date.today()
    today_str = today_date.strftime("%Y-%m-%d")

    logging.info(f"[LOG: START] anicheck_daily.py v3.4 — {today_str}")
    logging.info(
        "[THOUGHT: Syoboi-first戦略: Syoboi APIで7日分を一括取得し、"
        f"不足分のみGrok呼出（上限{MAX_GROK_CALLS_PER_DAY}回/日）]"
    )

    current_dir = Path("current")
    current_dir.mkdir(parents=True, exist_ok=True)

    # ── watch_list.json 読み込み ──────────────────────────────
    watch_list_file = current_dir / "watch_list.json"
    if not watch_list_file.exists():
        logging.critical(f"❌ {watch_list_file} が見つかりません")
        exit(1)

    ANIMES_TO_CHECK: List[Dict[str, Any]] = load_json_file(watch_list_file, default=[])

    # last_grok_date フィールドがない既存エントリに None で初期化（後方互換）
    for _entry in ANIMES_TO_CHECK:
        _entry.setdefault("last_grok_date", None)

    active_animes = [a for a in ANIMES_TO_CHECK if a.get("is_active", True)]
    logging.info(
        f"[INPUT] watch_list: 全{len(ANIMES_TO_CHECK)}件中 is_active={len(active_animes)}件"
    )

    # ── broadcast_history.json 読み込み ─────────────────────
    broadcast_history_file = current_dir / "broadcast_history.json"
    if broadcast_history_file.exists():
        broadcast_history: Dict[str, Any] = load_json_file(broadcast_history_file)
    else:
        broadcast_history = {}
        logging.info("broadcast_history.json が存在しないため空辞書で初期化します")

    # =========================================================
    # Step 1: Syoboi から7日分の放送データを一括取得
    # =========================================================
    logging.info("[LOG: START] Step 1: Syoboi ProgramByDate & Channel 取得")
    ch_map = fetch_syoboi_channels()
    prog_items = fetch_syoboi_proglist(today_date, days=7)
    logging.info(
        f"[OUTPUT] Syoboi 取得: ProgramByDate={len(prog_items)}件, Channel={len(ch_map)}局"
    )
    if debug_logger:
        debug_logger.debug(
            "syoboi_data_fetched",
            extra={
                "data": {
                    "ch_map": ch_map,
                    "prog_items": prog_items,
                }
            },
        )

    # =========================================================
    # Step 2: watch_list × Syoboi マッチング
    # =========================================================
    logging.info("[LOG: START] Step 2: watch_list × Syoboi マッチング")
    syoboi_matches = map_syoboi_to_watchlist_by_tid(prog_items, active_animes, ch_map, debug_logger=debug_logger)
    syoboi_hit_count = len(syoboi_matches)
    logging.info(
        f"[OUTPUT] Syoboiマッチング完了: {syoboi_hit_count}/{len(active_animes)} 作品がヒット"
    )

    # =========================================================
    # Step 3: 各作品の処理
    # =========================================================
    all_broadcasts: List[Dict[str, Any]] = []
    # grok_call_count = 今日の Grok API 呼び出し回数（grok_calls_today と同義）
    grok_call_count: int = 0
    syoboi_confirmed_count: int = 0

    # [THOUGHT: Syoboi にヒットした作品を先頭に並べることで、Grokの1日上限枠を
    # 「番組表に掲載されていてあらすじが未取得の作品」に優先消費させる。
    # syoboi_tid未設定の配信限定作品などに先に枠を奪われないようにするための並び替え。]
    syoboi_hit_ids = set(syoboi_matches.keys())
    syoboi_hit_animes = [a for a in active_animes if (a.get("anime_id") or a.get("title", "")) in syoboi_hit_ids]
    non_syoboi_animes = [a for a in active_animes if (a.get("anime_id") or a.get("title", "")) not in syoboi_hit_ids]
    prioritized_animes = syoboi_hit_animes + non_syoboi_animes
    logging.info(
        f"[THOUGHT: 処理順序を優先化 — Syoboiヒット:{len(syoboi_hit_animes)}件 → 非ヒット:{len(non_syoboi_animes)}件]"
    )

    total = len(prioritized_animes)

    logging.info(f"[LOG: START] Step 3: 全{total}作品の処理開始")

    for i, anime in enumerate(prioritized_animes):
        anime_id: str = anime.get("anime_id") or anime.get("title", f"unknown_{i}")
        title: str = anime.get("title", "不明")
        logging.info(f"  [{i+1}/{total}] {title} (id={anime_id})")

        try:
            syoboi_items_for_anime = syoboi_matches.get(anime_id, [])
            syoboi_broadcasts = build_broadcasts_from_syoboi(
                anime_id, title, syoboi_items_for_anime, ch_map
            )

            if syoboi_broadcasts:
                # ── Syoboi にヒットした作品 ─────────────────
                syoboi_confirmed_count += 1
                logging.info(
                    f"    [THOUGHT: Syoboiにヒット — {len(syoboi_broadcasts)}件の放送予定を確認]"
                )

                # ep_num を今回の最大値で確定
                ep_nums = [b["ep_num"] for b in syoboi_broadcasts if b["ep_num"] > 0]
                ep_num: int = max(ep_nums) if ep_nums else 0

                # broadcast_history を Syoboi データで更新
                update_history_from_syoboi(
                    broadcast_history, anime_id, title, syoboi_broadcasts, today_str
                )

                # daily_schedule 用に追加（内部管理フィールド _ prefix を除去）
                for bc in syoboi_broadcasts:
                    all_broadcasts.append(
                        {
                            "anime_id": bc["anime_id"],
                            "title": bc["title"],
                            "ep_num": bc["ep_num"],
                            "station_id": bc["station_id"],
                            "start_time": bc["start_time"],
                            "status": bc["status"],
                        }
                    )

                # watch_list の last_checked_ep を更新
                if ep_num > (anime.get("last_checked_ep") or 0):
                    anime["last_checked_ep"] = ep_num
                    logging.info(f"    last_checked_ep 更新 → {ep_num}")

                # エピソードファイルを Syoboi データで初期保存
                if ep_num > 0:
                    subtitle = next(
                        (b["_subtitle"] for b in syoboi_broadcasts if b.get("_subtitle")),
                        None,
                    )
                    save_episode_file(anime_id, ep_num, None, subtitle_from_syoboi=subtitle)

                # ── Grok 補完が必要か判断 ───────────────────
                should_call_grok = (
                    ep_num > 0
                    and should_call_grok_with_cooldown(
                        anime, grok_call_count, MAX_GROK_CALLS_PER_DAY, today_str,
                        broadcast_history=broadcast_history,
                    )
                    and needs_grok_enrichment(anime_id, ep_num)
                )
                if should_call_grok:
                    logging.info(
                        f"    [THOUGHT: summary未取得のためGrok呼出 "
                        f"(今日の残り呼出: {MAX_GROK_CALLS_PER_DAY - grok_call_count}回)]"
                    )
                    current_history = broadcast_history.get(anime_id, {"platforms": {}})
                    try:
                        raw_text = call_grok_for_anime(
                            title, anime.get("official_url"), current_history,
                            ep_num=ep_num,
                        )
                        grok_call_count += 1
                        anime["last_grok_date"] = today_str
                        logging.info(
                            f"    [Grok呼出 {grok_call_count}/{MAX_GROK_CALLS_PER_DAY}]"
                            f" last_grok_date → {today_str}"
                        )

                        grok_result = parse_grok_output(raw_text, title)
                        if grok_result:
                            # エピソードファイルに summary / preview を補完
                            save_episode_file(
                                anime_id, ep_num, grok_result["today"],
                                subtitle_from_syoboi=subtitle,
                            )
                        else:
                            logging.warning(f"    ⚠️ Grokレスポンスのパース失敗: {title}")
                    except Exception as grok_err:
                        logging.error(
                            f"    🔥 Grokエラー: {title} — {grok_err}", exc_info=True
                        )
                elif ep_num > 0 and not needs_grok_enrichment(anime_id, ep_num):
                    logging.info(f"    ✅ Grok不要 (summary取得済み)")
                elif grok_call_count >= MAX_GROK_CALLS_PER_DAY:
                    logging.info(
                        f"    ⚠️ Grok上限到達 ({MAX_GROK_CALLS_PER_DAY}回/日) — スキップ"
                    )

            else:
                # ── Syoboi にヒットしない作品 ──────────────────────────────────────────────────
                syoboi_tid_val = str(anime.get("syoboi_tid") or "").strip()
                if syoboi_tid_val:
                    # syoboi_tid が設定されているのに向こう7日間の番組表に載っていない
                    # → 放送前 / 放送終了 / 特番等による休止 のいずれかと判断。Grok呼出不要。
                    logging.info(
                        f"    [THOUGHT: syoboi_tid={syoboi_tid_val} 設定済みだが向こう7日間の番組表にヒットなし "
                        f"→ 放送前・放送終了・特番休止のいずれかと判断。Grokをスキップします]"
                    )
                    logging.info(
                        f"    ⏭️ Grokスキップ (syoboi_tid={syoboi_tid_val} 設定済み: "
                        f"今週は休みまたは放送前/終了のためSyoboi番組表に掲載なし)"
                    )
                else:
                    # syoboi_tid 未設定 → 配信限定またはTID未特定の作品のみGrokへ問い合わせ
                    logging.info(
                        f"    [THOUGHT: syoboi_tidなし — 配信限定作品またはTID未特定。Grok候補]"
                    )
                    if should_call_grok_with_cooldown(
                        anime, grok_call_count, MAX_GROK_CALLS_PER_DAY, today_str,
                        broadcast_history=broadcast_history,
                    ):
                        logging.info(
                            f"    → Grok問い合わせ "
                            f"(今日の残り呼出: {MAX_GROK_CALLS_PER_DAY - grok_call_count}回)"
                        )
                        current_history = broadcast_history.get(anime_id, {"platforms": {}})
                        try:
                            raw_text = call_grok_for_anime(
                                title, anime.get("official_url"), current_history
                            )
                            grok_call_count += 1
                            anime["last_grok_date"] = today_str
                            logging.info(
                                f"    [Grok呼出 {grok_call_count}/{MAX_GROK_CALLS_PER_DAY}]"
                                f" last_grok_date → {today_str}"
                            )

                            grok_result = parse_grok_output(raw_text, title)
                            if grok_result:
                                today_ep: Optional[EpisodeSchedule] = grok_result["today"]
                                if today_ep is not None:
                                    ep_num_g: int = today_ep.ep_num
                                    save_episode_file(anime_id, ep_num_g, today_ep)

                                    # broadcast_history の overall_latest_ep を ep_num から更新
                                    if anime_id not in broadcast_history:
                                        broadcast_history[anime_id] = {
                                            "title": title,
                                            "overall_latest_ep": 0,
                                            "platforms": {},
                                        }
                                    prev_latest = broadcast_history[anime_id].get("overall_latest_ep") or 0
                                    if ep_num_g > prev_latest:
                                        broadcast_history[anime_id]["overall_latest_ep"] = ep_num_g
                                        logging.info(
                                            f"    broadcast_history overall_latest_ep 更新 → {ep_num_g}"
                                        )

                                    # watch_list の last_checked_ep を更新
                                    if ep_num_g > (anime.get("last_checked_ep") or 0):
                                        anime["last_checked_ep"] = ep_num_g
                                        logging.info(f"    last_checked_ep 更新 → {ep_num_g}")

                                    # daily_schedule に追加（Grok が局情報を持つ場合）
                                    for bc in today_ep.broadcasts:
                                        all_broadcasts.append(
                                            {
                                                "anime_id": anime_id,
                                                "title": title,
                                                "ep_num": ep_num_g,
                                                "station_id": bc.station_id,
                                                "start_time": bc.start_time,
                                                "status": bc.status,
                                            }
                                        )
                            else:
                                logging.warning(f"    ⚠️ Grokレスポンスのパース失敗: {title}")
                        except Exception as grok_err:
                            logging.error(
                                f"    🔥 Grokエラー: {title} — {grok_err}", exc_info=True
                            )

            logging.info(f"    ✅ {anime_id} 処理完了")

        except Exception as e:
            logging.error(f"    🔥 予期せぬエラー: {title} ({anime_id}) — {e}", exc_info=True)

    # =========================================================
    # Step 4: ファイル保存
    # =========================================================
    logging.info("[LOG: START] Step 4: ファイル保存")

    # daily_schedule.json を start_time 昇順でソートして保存
    all_broadcasts.sort(key=lambda x: x.get("start_time") or "")
    save_json_file(current_dir / "daily_schedule.json", all_broadcasts)
    logging.info(f"  📅 daily_schedule.json 保存 ({len(all_broadcasts)} 件)")

    # watch_list.json（last_checked_ep 更新済み）を保存
    save_json_file(watch_list_file, ANIMES_TO_CHECK)
    logging.info(f"  📋 watch_list.json 保存 ({len(ANIMES_TO_CHECK)} 件)")

    # broadcast_history.json を保存
    save_json_file(broadcast_history_file, broadcast_history)
    logging.info("  📊 broadcast_history.json 保存")

    logging.info(
        f"[LOG: END] 処理完了 — "
        f"Syoboi確認={syoboi_confirmed_count}/{total}作品, "
        f"Grok呼出={grok_call_count}/{MAX_GROK_CALLS_PER_DAY}回, "
        f"daily_schedule={len(all_broadcasts)}件"
    )
    logging.info(f"ログ: {log_file.absolute()}")


if __name__ == "__main__":
    main()
