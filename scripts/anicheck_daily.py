# -*- coding: utf-8 -*-
"""anicheck_daily.py — v3.0 (Syoboi-first + Grok-supplement)

フロー:
  1. Syoboi Calendar API から向こう3日間の放送データを一括取得
  2. watch_list.json の監視対象とタイトルマッチ（部分一致）
  3. Syoboi から ep_num / station_id / start_time / status を直接確定
  4. 不足情報（summary, preview等）のみ Grok に問い合わせ（上限 MAX_GROK_CALLS_PER_DAY 回/日）
  5. broadcast_history / daily_schedule / episodes / watch_list を更新
  6. 既存 V2 データ構造（ファイルパス・JSONキー）との互換性を完全維持
"""
import json
import re
import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from xai_sdk import Client
from xai_sdk.chat import user, system
from xai_sdk.tools import web_search

load_dotenv()  # ローカル開発用。GitHub Actionsでは不要（secretsで直接環境変数）

# =================================================================
# ロギング設定（Actionsのログでも見やすいように）
# =================================================================
log_dir = Path("logs")
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
# 定数・設定
# =================================================================

# 1日あたりの Grok API 呼び出し上限
MAX_GROK_CALLS_PER_DAY: int = 5

# Syoboi Calendar API エンドポイント
SYOBOI_API_URL: str = "http://cal.syoboi.jp/json.php"
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
    "cx": "cx",
    "フジテレビ": "cx",
    "fuji tv": "cx",
    # ── TV Tokyo ──────────────────────────────────────────────
    "tx": "tx",
    "テレビ東京": "tx",
    "tv tokyo": "tx",
    "tvtokyo": "tx",
    # ── TV Asahi ──────────────────────────────────────────────
    "ex": "ex",
    "テレビ朝日": "ex",
    "tv asahi": "ex",
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
    "nhk eテレ": "nhk-e",
    "nhk教育": "nhk-e",
    # ── CBC ───────────────────────────────────────────────────
    "cbc": "cbc",
    "cbcテレビ": "cbc",
    # ── BS channels ───────────────────────────────────────────
    "bs11": "bs11",
    "bs11デジタル": "bs11",
    "at-x": "at-x",
    "atx": "at-x",
    "bs日テレ": "bs-ntv",
    "bs日本": "bs-ntv",
    "bs朝日": "bs-ex",
    "bsフジ": "bs-cx",
    "bs-tbs": "bs-tbs",
    "bstbs": "bs-tbs",
    "bs-tbsテレビ": "bs-tbs",
    "bstbsテレビ": "bs-tbs",
    # ── 地方キー局系 ─────────────────────────────────────────
    "tva": "tva",
    "テレビ愛知": "tva",
    "tvh": "tvh",
    "テレビ北海道": "tvh",
    "tvo": "tvo",
    "テレビ大阪": "tvo",
    # ── Streaming / Digital ───────────────────────────────────
    "abema": "abema",
    "abematv": "abema",
    "dアニメストア": "d-anime",
    "d-anime store": "d-anime",
    "dアニメ": "d-anime",
    "d anime store": "d-anime",
    "netflix": "netflix",
    "amazon prime": "amazon",
    "amazon prime video": "amazon",
    "prime video": "amazon",
    "amazon": "amazon",
    "hulu": "hulu",
    "disney+": "disney-plus",
    "u-next": "u-next",
    "crunchyroll": "crunchyroll",
    "funimation": "funimation",
}


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


# =================================================================
# Pydantic モデル（Grok 応答バリデーション）
# =================================================================

class PlatformStatus(BaseModel):
    last_ep_num: int
    remarks: Optional[str] = None


class BroadcastUpdate(BaseModel):
    overall_latest_ep: int
    platforms: Dict[str, PlatformStatus] = Field(default_factory=dict)


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
あなたは日本のアニメ放送・配信情報に精通した調査員です。

# 探索フェーズ（情報の海を泳ぐ）
指定された作品の「現在最も新しい配信・放送済みエピソード」および「直近3日間（本日〜明後日）の放送予定」に関する情報を、全方位から幅広く収集してください。
- ツール（web_search, x_search）を必ず駆使し、公式発表、ニュースサイト（ANN、Natalie等）、番組表サイト、公式Twitter、一般ユーザーの実況や噂まで、まずは広く情報を集めてください。
- 検索時は広範な情報収集を意識し、作品名や略称、局名などを組み合わせて検索してください。
- ユーザーから提供される「前回の局別進捗（履歴）」をヒントに、それ以降の新しい情報（最新話は第何話か、向こう3日間で放送されるのは第何話か）を探してください。

# 抽出フェーズ（情報の構造化）
集めた情報から、以下の2つのJSONブロック（```json ... ```）を作成してください。

1. Broadcast_Update JSONブロック
各局・配信プラットフォームごとの最新の放送/配信済み話数と、作品全体の最新話数。
{
  "overall_latest_ep": 整数,
  "platforms": {
    "局名A": { "last_ep_num": 整数, "remarks": "遅れ放送など" }
  }
}

2. Upcoming_Schedule_And_Episode JSONブロック
「直近3日間（本日、明日、明後日）」に放送・配信される予定のエピソード情報。期間内に放送がない場合は `{}` を出力。
{
  "ep_num": 整数,
  "title": "サブタイトル",
  "summary": "あらすじ要約（3行以内）",
  "preview_youtube_id": "予告のYouTube ID または null",
  "broadcasts": [
    { "station_id": "局名", "start_time": "YYYY-MM-DDTHH:MM:SS+09:00", "status": "normal/delayed" }
  ]
}

# 検証フェーズ（厳格な制約とハルシネーション排除）
最後に、集めた情報を厳しく精査します。以下のルールに違反する情報は捨ててください。
- 【重要】出力に含める情報は、公式サイト、放送局公式、信頼できるニュースソースで裏付けが取れたもののみとしてください。
- ソースのない噂や推測による捏造は絶対に行わないでください。確認できない項目は `null` にしてください。
- 出力は上記の2つのJSONブロックと、最後に【ソース確認】（参照したURL）のみとし、余計な解説は省いてください。"""


def call_grok_for_anime(
    title: str,
    official_url: Optional[str] = None,
    current_history: Optional[Dict[str, Any]] = None,
) -> str:
    """Grokに作品の最新放送進捗と直近3日間の放送予定を問い合わせる。

    Args:
        title: 作品名。
        official_url: 公式サイトURL（参考情報として提示）。
        current_history: broadcast_history.json から取り出した局別進捗の辞書。

    Returns:
        Grok の応答テキスト（失敗時は空文字）。
    """
    client = Client()  # XAI_API_KEY が環境変数にある場合、api_key=不要
    history_str = json.dumps(current_history, ensure_ascii=False) if current_history else "{}"
    url_hint = f"\n公式URL（参考）：{official_url}" if official_url else ""
    user_input = (
        f"作品名：{title}{url_hint}\n"
        f"前回の局別進捗：{history_str}\n\n"
        f"上記を踏まえ、進捗の更新と直近3日間（本日〜明後日）の放送予定を出力せよ。"
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
    """Grokの応答テキストから2つのJSONブロックを Pydantic でバリデーションしつつ抽出する。

    Returns:
        {"update": BroadcastUpdate, "today": EpisodeSchedule | None, "sources": str}
        パース失敗時は None を返す。
    """
    json_blocks = re.findall(r"```json\s*([\s\S]*?)\s*```", text)
    if len(json_blocks) < 2:
        # フェンスなしの生JSONフォールバック（先頭2つを採用）
        json_blocks = re.findall(
            r"(\{[\s\S]*?\})(?=\s*(?:\{|\【ソース確認】|$))", text, re.DOTALL
        )
        if len(json_blocks) < 2:
            logging.error(f"  JSONブロック不足({len(json_blocks)}個): {title}")
            return None

    try:
        update_data = BroadcastUpdate.model_validate_json(json_blocks[0].strip())
    except Exception as e:
        logging.error(f"  BroadcastUpdate バリデーション失敗: {e}\nRaw: {json_blocks[0][:300]}")
        return None

    try:
        raw_today = json_blocks[1].strip()
        # 空オブジェクト（放送なし）は None として扱う
        today_data: Optional[EpisodeSchedule] = (
            None if raw_today in ("{}", "{ }") else EpisodeSchedule.model_validate_json(raw_today)
        )
    except Exception as e:
        logging.warning(f"  EpisodeSchedule バリデーション失敗（スキップ）: {e}")
        today_data = None

    source_match = re.search(r"【ソース確認】([\s\S]*)", text, re.DOTALL)
    sources = source_match.group(1).strip() if source_match else "ソース抽出失敗"

    return {"update": update_data, "today": today_data, "sources": sources}


# =================================================================
# Syoboi Calendar API
# =================================================================

def fetch_syoboi_channels() -> Dict[str, str]:
    """Syoboi ChList から {ChID: 局名} マップを取得する。

    Returns:
        局 ID（文字列）→ 局名（文字列）の辞書。失敗時は空辞書。
    """
    logging.info("[LOG: START] Syoboi ChList 取得")
    try:
        resp = requests.get(
            SYOBOI_API_URL,
            params={"Command": "ChList"},
            timeout=SYOBOI_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        ch_items: Dict[str, Any] = data.get("ChList", {}).get("Items", {})
        ch_map: Dict[str, str] = {}
        for ch_id, ch_info in ch_items.items():
            name = (
                ch_info.get("ChName")
                or ch_info.get("ChShortName")
                or str(ch_id)
            )
            ch_map[str(ch_id)] = name
        logging.info(f"  [OUTPUT] Syoboi ChList: {len(ch_map)} 局取得")
        return ch_map
    except Exception as e:
        logging.warning(f"  ⚠️ Syoboi ChList 取得失敗: {e} — 空マップで継続")
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
        f"[LOG: START] Syoboi ProgList 取得 (Start={start_date.strftime('%Y%m%d')}, Days={days})"
    )
    try:
        resp = requests.get(
            SYOBOI_API_URL,
            params={
                "Command": "ProgList",
                "Start": start_date.strftime("%Y%m%d"),
                "Days": str(days),
            },
            timeout=SYOBOI_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        items_raw = data.get("ProgList", {}).get("Items", {})
        # Items は辞書形式（数値キー）または配列形式どちらでも対応
        items: List[Dict[str, Any]] = (
            list(items_raw.values()) if isinstance(items_raw, dict) else items_raw
        )
        logging.info(f"  [OUTPUT] Syoboi ProgList: {len(items)} 件取得")
        return items
    except Exception as e:
        logging.error(f"  ❌ Syoboi ProgList 取得失敗: {e}")
        return []


def match_syoboi_to_watch(
    prog_items: List[Dict[str, Any]],
    active_animes: List[Dict[str, Any]],
    ch_map: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Syoboi の放送アイテムを watch_list の各作品にタイトルマッチングする。

    マッチング戦略:
      ① prog_item の Title が watch_list title と完全一致
      ② prog_item の Title の小文字版が watch_list base_title（シーズン表記除去後）に含まれる、または逆

    Returns:
        {anime_id: [matched_prog_items]} の辞書。
    """
    logging.info("[LOG: START] Syoboi × watch_list タイトルマッチング")
    logging.info("[THOUGHT: タイトル完全一致 → 部分一致の順でマッチング]")

    # watch_list のタイトルを正規化して索引を作成
    # key = 小文字化タイトル、value = anime_id
    watch_index: Dict[str, str] = {}
    for anime in active_animes:
        anime_id = anime["anime_id"]
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

    results: Dict[str, List[Dict[str, Any]]] = {}
    unmatched_tids: set = set()

    for item in prog_items:
        prog_title = (item.get("Title") or "").strip()
        if not prog_title:
            continue
        prog_lower = prog_title.lower()
        matched_id: Optional[str] = None

        # ① 完全一致
        if prog_lower in watch_index:
            matched_id = watch_index[prog_lower]
        else:
            # ② 部分一致（4文字以上のキーのみ検索して誤爆を抑制）
            for key, aid in watch_index.items():
                if len(key) >= 4 and (key in prog_lower or prog_lower in key):
                    matched_id = aid
                    break

        if matched_id:
            if matched_id not in results:
                results[matched_id] = []
            results[matched_id].append(item)
        else:
            unmatched_tids.add(item.get("TID", "?"))

    logging.info(
        f"  [OUTPUT] マッチング結果: {len(results)}/{len(active_animes)} 作品が Syoboi にヒット"
        f" (未マッチ TID 数={len(unmatched_tids)})"
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

def load_json_file(path: Path) -> Any:
    """UTF-8 JSON ファイルを読み込んで返す。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def update_history_from_grok(
    broadcast_history: Dict[str, Any],
    anime_id: str,
    title: str,
    update_data: BroadcastUpdate,
    today_str: str,
) -> None:
    """Grok の BroadcastUpdate で broadcast_history を更新する（破壊的更新）。"""
    if anime_id not in broadcast_history:
        broadcast_history[anime_id] = {"title": title, "overall_latest_ep": 0, "platforms": {}}

    broadcast_history[anime_id]["title"] = title
    # overall_latest_ep は Grok の値が大きければ採用
    prev_latest = broadcast_history[anime_id].get("overall_latest_ep") or 0
    if update_data.overall_latest_ep > prev_latest:
        broadcast_history[anime_id]["overall_latest_ep"] = update_data.overall_latest_ep

    if "platforms" not in broadcast_history[anime_id]:
        broadcast_history[anime_id]["platforms"] = {}

    for platform_name, plat_status in update_data.platforms.items():
        sid = normalize_station(platform_name)
        if sid not in broadcast_history[anime_id]["platforms"]:
            broadcast_history[anime_id]["platforms"][sid] = {
                "last_ep_num": 0,
                "last_broadcast_date": None,
                "last_updated_at": today_str,
                "remarks": plat_status.remarks,
            }
        plat = broadcast_history[anime_id]["platforms"][sid]
        if plat_status.last_ep_num >= (plat.get("last_ep_num") or 0):
            plat["last_ep_num"] = plat_status.last_ep_num
            plat["last_updated_at"] = today_str
            if plat_status.remarks:
                plat["remarks"] = plat_status.remarks


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

if __name__ == "__main__":
    today_date = datetime.date.today()
    today_str = today_date.strftime("%Y-%m-%d")

    logging.info(f"[LOG: START] anicheck_daily.py v3.0 — {today_str}")
    logging.info(
        "[THOUGHT: Syoboi-first戦略: Syoboi APIで3日分を一括取得し、"
        f"不足分のみGrok呼出（上限{MAX_GROK_CALLS_PER_DAY}回/日）]"
    )

    current_dir = Path("current")
    current_dir.mkdir(parents=True, exist_ok=True)

    # ── watch_list.json 読み込み ──────────────────────────────
    watch_list_file = current_dir / "watch_list.json"
    if not watch_list_file.exists():
        logging.critical(f"❌ {watch_list_file} が見つかりません")
        exit(1)

    ANIMES_TO_CHECK: List[Dict[str, Any]] = load_json_file(watch_list_file)
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
    # Step 1: Syoboi から3日分の放送データを一括取得
    # =========================================================
    logging.info("[LOG: START] Step 1: Syoboi ProgList & ChList 取得")
    ch_map = fetch_syoboi_channels()
    prog_items = fetch_syoboi_proglist(today_date, days=3)
    logging.info(
        f"[OUTPUT] Syoboi 取得: ProgList={len(prog_items)}件, ChList={len(ch_map)}局"
    )

    # =========================================================
    # Step 2: watch_list × Syoboi マッチング
    # =========================================================
    logging.info("[LOG: START] Step 2: watch_list × Syoboi マッチング")
    syoboi_matches = match_syoboi_to_watch(prog_items, active_animes, ch_map)
    syoboi_hit_count = len(syoboi_matches)
    logging.info(
        f"[OUTPUT] Syoboiマッチング完了: {syoboi_hit_count}/{len(active_animes)} 作品がヒット"
    )

    # =========================================================
    # Step 3: 各作品の処理
    # =========================================================
    all_broadcasts: List[Dict[str, Any]] = []
    grok_call_count: int = 0
    syoboi_confirmed_count: int = 0
    total = len(active_animes)

    logging.info(f"[LOG: START] Step 3: 全{total}作品の処理開始")

    for i, anime in enumerate(active_animes):
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
                    and grok_call_count < MAX_GROK_CALLS_PER_DAY
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
                            title, anime.get("official_url"), current_history
                        )
                        grok_call_count += 1
                        logging.info(
                            f"    [Grok呼出 {grok_call_count}/{MAX_GROK_CALLS_PER_DAY}]"
                        )

                        grok_result = parse_grok_output(raw_text, title)
                        if grok_result:
                            # broadcast_history を Grok データでさらに補完
                            update_history_from_grok(
                                broadcast_history,
                                anime_id,
                                title,
                                grok_result["update"],
                                today_str,
                            )
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
                # ── Syoboi にヒットしない作品（配信限定等） ───
                logging.info(
                    f"    [THOUGHT: Syoboiにヒットなし — 配信限定の可能性。Grok候補]"
                )
                if grok_call_count < MAX_GROK_CALLS_PER_DAY:
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
                        logging.info(
                            f"    [Grok呼出 {grok_call_count}/{MAX_GROK_CALLS_PER_DAY}]"
                        )

                        grok_result = parse_grok_output(raw_text, title)
                        if grok_result:
                            update_history_from_grok(
                                broadcast_history,
                                anime_id,
                                title,
                                grok_result["update"],
                                today_str,
                            )
                            today_ep: Optional[EpisodeSchedule] = grok_result["today"]
                            if today_ep is not None:
                                ep_num_g: int = today_ep.ep_num
                                save_episode_file(anime_id, ep_num_g, today_ep)

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
                else:
                    logging.info(
                        f"    ⚠️ Grok上限到達 ({MAX_GROK_CALLS_PER_DAY}回/日) — スキップ"
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