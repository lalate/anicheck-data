#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
season_adder.py

次期シーズンのアニメを watch_list.json / broadcast_history.json へ追記するスクリプト。
既存エントリは一切削除・変更しない（追記専用）。

使い方 (AniJson/ ディレクトリから実行):
  python scripts/season_adder.py           # 通常実行（自動判定）
  python scripts/season_adder.py --dry-run # 書き込みなしで動作確認
  python scripts/season_adder.py --season "2026年冬アニメ" # シーズンを手動指定
"""

import argparse
import datetime
import hashlib
import json
import logging
import re
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, ValidationError
from xai_sdk import Client
from xai_sdk.chat import system, user
from xai_sdk.tools import web_search, x_search

load_dotenv()

# =================================================================
# パス設定
# =================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CURRENT_DIR = BASE_DIR / "current"
DATABASE_MASTER_DIR = BASE_DIR / "database" / "master"
WATCH_LIST_FILE = CURRENT_DIR / "watch_list.json"
BROADCAST_HISTORY_FILE = CURRENT_DIR / "broadcast_history.json"
LOG_DIR = BASE_DIR / "logs"

# =================================================================
# ロギング設定
# =================================================================
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "season_adder.log"

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
logger = logging.getLogger(__name__)

# =================================================================
# 定数
# =================================================================
JIKAN_API_BASE = "https://api.jikan.moe/v4"
JIKAN_SLEEP = 0.5
GROK_MAX_RETRIES = 3
# 正規化後の文字列で部分一致を重複とみなす最小文字数
DUPLICATE_MIN_LEN = 8
# ARM（Anime Relations Map）APIのベースURL
# GET /api/ids?service=<service>&id=<id>
# 例: ?service=mal&id=5114 → {"mal_id":5114,"anilist_id":5114,"annict_id":1745,"syobocal_tid":1575}
ARM_API_URL = "https://arm.kawaiioverflow.com/api/ids"

# =================================================================
# Pydanticスキーマ（Grok出力のバリデーション用）
# =================================================================

class GrokSchedule(BaseModel):
    station: str = ""
    day_of_week: str = ""
    time: str = ""


class GrokAnimeItem(BaseModel):
    title: str
    short_id: str = ""
    official_url: Optional[str] = None
    ep_num: int = 1
    schedules: list[GrokSchedule] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def title_must_not_be_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title is empty")
        return v

    @field_validator("ep_num", mode="before")
    @classmethod
    def ep_num_coerce(cls, v) -> int:
        # 文字列で渡された場合も安全にintに変換
        try:
            return int(v)
        except (TypeError, ValueError):
            return 1


class GrokAnimeList(BaseModel):
    items: list[GrokAnimeItem]


# watch_list.json の各エントリを表すスキーマ（syoboi_tid フィールド含む）
class AnimeSeasonEntry(BaseModel):
    anime_id: str
    mal_id: Optional[int] = None
    title: str
    official_url: Optional[str] = None
    last_checked_ep: int = 1
    is_active: bool = True
    season: str = ""
    season_end_date: Optional[str] = None
    syoboi_tid: Optional[int] = None


# =================================================================
# シーズン判定ロジック
# =================================================================
_CURRENT_SEASON_MAP: dict[int, tuple[str, str]] = {
    1: ("冬", "winter"), 2: ("冬", "winter"), 3: ("冬", "winter"),
    4: ("春", "spring"), 5: ("春", "spring"), 6: ("春", "spring"),
    7: ("夏", "summer"), 8: ("夏", "summer"), 9: ("夏", "summer"),
    10: ("秋", "autumn"), 11: ("秋", "autumn"), 12: ("秋", "autumn"),
}

_NEXT_SEASON_MAP: dict[str, tuple[str, str, int]] = {
    "冬": ("春", "spring", 4),
    "春": ("夏", "summer", 7),
    "夏": ("秋", "autumn", 10),
    "秋": ("冬", "winter", 1),
}


def get_season_info(target_season_str: str | None = None) -> dict:
    """指定された文字列、または現在日時からシーズン情報を算出して返す。"""
    now = datetime.datetime.now()
    year, month = now.year, now.month

    if target_season_str:
        # 手動指定時 (例: "2026年冬アニメ")
        match = re.search(r"(\d{4})年(冬|春|夏|秋)", target_season_str)
        if match:
            s_year = int(match.group(1))
            s_jp = match.group(2)
            en_map = {"冬": "winter", "春": "spring", "夏": "summer", "秋": "autumn"}
            mm_map = {"冬": "01", "春": "04", "夏": "07", "秋": "10"}
            return {
                "season_str": target_season_str,
                "season_key": f"{s_year}_{en_map[s_jp]}",
                "yyyymm": f"{s_year}{mm_map[s_jp]}",
                "season_jp": s_jp,
                "season_en": en_map[s_jp],
            }

    # 自動判定
    current_season_jp, _ = _CURRENT_SEASON_MAP[month]
    next_season_jp, next_season_en, next_month = _NEXT_SEASON_MAP[current_season_jp]
    next_year = year + 1 if current_season_jp == "秋" else year

    return {
        "season_str": f"{next_year}年{next_season_jp}アニメ",
        "season_key": f"{next_year}_{next_season_en}",
        "yyyymm": f"{next_year}{next_month:02d}",
        "season_jp": next_season_jp,
        "season_en": next_season_en,
    }


# =================================================================
# anime_id 生成
# =================================================================
def generate_anime_id(title: str, short_id: str, yyyymm: str) -> str:
    """
    short_idから英数字のみを抽出してanime_idを生成する。
    short_idが空や記号だらけの場合はタイトルのSHA256ハッシュでフォールバックする。
    """
    clean_short_id = re.sub(r"[^a-zA-Z0-9]", "", short_id).lower()
    if not clean_short_id:
        # [LOG] short_idが使えないため、タイトルハッシュでフォールバック
        logger.warning(f"[THOUGHT: short_id='{short_id}' が無効。タイトルハッシュでフォールバック。title='{title}']")
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
        clean_short_id = f"gen_{digest}"
    return f"{yyyymm}_{clean_short_id}_c1"


# =================================================================
# タイトル正規化
# =================================================================
def normalize_title(title: str) -> str:
    """
    重複判定用にタイトルを正規化する。
    - 括弧・記号を除去（【】[]()（）「」『』）
    - NFKC正規化（全角英数→半角）
    - ×・・-_スペースを除去
    - 小文字化
    """
    # 括弧と括弧内の内容は除去せず、括弧記号のみ除去（「第3期」などの情報を保持）
    title = re.sub(r"[【】\[\]()（）「」『』]", "", title)
    # NFKC正規化: 全角英数字を半角に、カタカナ正規化
    title = unicodedata.normalize("NFKC", title)
    # 記号・空白を除去
    title = re.sub(r"[×·・\s\-_☆♪♥★◆◇▼▽△▲●○◎♦]", "", title)
    return title.lower().strip()


def is_duplicate(new_title: str, existing_list: list) -> bool:
    """
    正規化後のタイトルで重複チェックを行う。
    - 完全一致: 常に重複
    - 部分一致（包含）: 短い方が DUPLICATE_MIN_LEN 文字以上の場合のみ重複
    これにより「推しの子」(4文字)と「推しの子 第3期」は別物として扱い、
    「SPY×FAMILY」と「SPYFAMILY」(9文字)は同一とみなす。
    """
    norm_new = normalize_title(new_title)
    for entry in existing_list:
        norm_existing = normalize_title(entry.get("title", ""))
        if norm_new == norm_existing:
            logger.debug(f"[THOUGHT: 重複検出(完全一致): '{new_title}' == '{entry.get('title')}'")
            return True
        shorter = norm_new if len(norm_new) <= len(norm_existing) else norm_existing
        longer = norm_existing if shorter == norm_new else norm_new
        if len(shorter) >= DUPLICATE_MIN_LEN and shorter in longer:
            logger.debug(
                f"[THOUGHT: 重複検出(部分一致 {len(shorter)}文字): '{new_title}' ≈ '{entry.get('title')}']"
            )
            return True
    return False


# =================================================================
# Grok プロンプト
# =================================================================
_GROK_SYSTEM_PROMPT = """# 役割
あなたは日本のアニメ放送情報に精通した、ハルシネーションを絶対に行わない厳格な調査員です。

# 探索フェーズ
指定されたシーズンに日本で放送されている主要な深夜アニメを調査してください。
web_search および x_search を駆使し、公式サイト・ニュースサイトから正確な情報を取得してください。

# 収集条件
- 知名度・期待度の高い主要な深夜アニメを 15〜20 作品程度ピックアップする。
- 各作品について以下を記載すること:
  - title: 作品名（原題）
  - short_id: 英数字のみの短く美しい識別子（例: frieren, jujutsu, oshinoko）
  - official_url: 公式サイトURL
  - ep_num: 次回放送予定の話数（新番組は1、放送中なら現在の最新話）
  - schedules: 主要放送局のスケジュール（mx, bs11, tx 等、曜日、時間）

# 検証・出力
- 推測禁止。出力は JSON コードブロックのみ。余計な解説は不要。
```json
[
  {
    "title": "作品名",
    "short_id": "shortid",
    "official_url": "URL",
    "ep_num": 1,
    "schedules": [{"station": "mx", "day_of_week": "曜日", "time": "24:00"}]
  }
]
```"""


def call_grok_for_season(season_str: str) -> str | None:
    """
    Grok APIを呼び出してシーズン情報を取得する。
    失敗した場合は最大GROK_MAX_RETRIES回リトライする（指数バックオフ）。
    全リトライ失敗時はNoneを返す。
    """
    logger.info(f"[LOG: START] Step 3: Grok API 呼び出し (target={season_str})")
    client = Client()
    user_input = (
        f"対象：{season_str}\n"
        f"この時期に日本で放送開始された、あるいは放送中の主要な深夜アニメを 20作品程度リストアップしてください。"
        f"web_search 等で最新情報を必ず取得し、正確な公式URLとスケジュールを記載してください。"
    )

    backoff = 2
    for attempt in range(1, GROK_MAX_RETRIES + 1):
        try:
            logger.info(f"[THOUGHT: Grok呼び出し試行 {attempt}/{GROK_MAX_RETRIES}]")
            chat = client.chat.create(
                model="grok-4-1-fast-reasoning",
                tools=[web_search(), x_search()],
                tool_choice="auto",
            )
            chat.append(system(_GROK_SYSTEM_PROMPT))
            chat.append(user(user_input))
            response = chat.sample()
            content = response.content or ""
            if content:
                logger.info(f"[LOG: END] Grok呼び出し成功 (attempt={attempt}, len={len(content)})")
                return content
            logger.warning(f"[THOUGHT: Grok応答が空。リトライ {attempt}/{GROK_MAX_RETRIES}]")
        except Exception as e:
            logger.error(f"[THOUGHT: Grok例外 attempt={attempt}: {e}]")

        if attempt < GROK_MAX_RETRIES:
            logger.info(f"[THOUGHT: {backoff}秒待機後リトライ]")
            time.sleep(backoff)
            backoff *= 2

    logger.error(f"[LOG: END] Grok全リトライ失敗 (season={season_str})")
    return None


# =================================================================
# Grokレスポンスのパース（Pydanticバリデーション付き）
# =================================================================
def _extract_json_candidate(text: str) -> str | None:
    """
    テキストからJSONらしき文字列（配列）を抽出する。
    コードブロック優先、なければ裸のJSON配列を探す。
    """
    # ```json ... ``` ブロックを優先抽出（バッククォートの乱れに対応）
    block_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if block_match:
        return block_match.group(1)

    # コードブロックがない場合、最外側の配列を抽出（ネスト対応）
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    return None


def parse_grok_response(text: str) -> list[GrokAnimeItem] | None:
    """
    GrokのテキストレスポンスからJSONを抽出し、Pydanticでバリデーションする。
    失敗した場合はログを出力してNoneを返す（即死しない）。
    """
    logger.info("[LOG: START] Step 4: Grokレスポンスのパース")
    logger.debug(f"[INPUT] raw_text length={len(text)}, preview={text[:200]!r}")

    candidate = _extract_json_candidate(text)
    if not candidate:
        logger.error("[THOUGHT: JSON候補が見つからなかった。テキスト全体を確認してください。]")
        logger.debug(f"[OUTPUT] parse_grok_response → None (no JSON found)")
        return None

    logger.debug(f"[THOUGHT: JSON候補抽出成功 length={len(candidate)}]")

    try:
        raw_list = json.loads(candidate)
        if not isinstance(raw_list, list):
            logger.error(f"[THOUGHT: JSONがリストでない: {type(raw_list)}]")
            return None
    except json.JSONDecodeError as e:
        logger.error(f"[THOUGHT: JSONDecodeError: {e}]")
        logger.debug(f"[INPUT] 問題のある候補: {candidate[:300]!r}")
        return None

    validated: list[GrokAnimeItem] = []
    for idx, item in enumerate(raw_list):
        try:
            validated.append(GrokAnimeItem.model_validate(item))
        except ValidationError as e:
            logger.warning(f"[THOUGHT: アイテム{idx}バリデーション失敗（スキップ）: {e.errors()}]")

    if not validated:
        logger.error("[THOUGHT: バリデーション通過アイテムが0件]")
        return None

    logger.info(f"[LOG: END] parse_grok_response → {len(validated)}件 validated")
    return validated


# =================================================================
# Jikan検索（最良候補選択ロジック付き）
# =================================================================
def _fetch_jikan_with_backoff(url: str, max_retries: int = 5) -> dict | None:
    retries = 0
    backoff = 2
    while retries < max_retries:
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                time.sleep(JIKAN_SLEEP)
                return response.json()
            elif response.status_code == 429:
                time.sleep(backoff)
                retries += 1
                backoff *= 2
            else:
                time.sleep(1)
                return None
        except Exception:
            time.sleep(backoff)
            retries += 1
            backoff *= 2
    return None


def _pick_best_jikan_candidate(candidates: list[dict], query_title: str) -> dict | None:
    """
    Jikan検索結果の候補リストから最も正しい1件を選ぶ。

    優先順位:
    1. titles配列の type="Japanese" が検索クエリと完全一致するもの
    2. scoreが最も高いもの（同点ならaired.from年が現在年に近いもの）
    3. 上記で選べなかった場合は先頭要素（フォールバック）
    """
    current_year = datetime.datetime.now().year
    norm_query = normalize_title(query_title)

    # 優先1: Japanese titleの完全一致
    for c in candidates:
        for t in c.get("titles", []):
            if t.get("type") == "Japanese" and normalize_title(t.get("title", "")) == norm_query:
                logger.debug(f"[THOUGHT: Jikan最良候補 → Japanese title完全一致: {t.get('title')}]")
                return c

    # 優先2: スコア最大 or 放送年が現在年に最も近い
    def sort_key(c: dict) -> tuple:
        score = c.get("score") or 0.0
        aired_from = (c.get("aired") or {}).get("from") or ""
        try:
            aired_year = int(aired_from[:4]) if aired_from else 0
        except (ValueError, TypeError):
            aired_year = 0
        year_diff = abs(current_year - aired_year) if aired_year else 9999
        return (-score, year_diff)

    sorted_candidates = sorted(candidates, key=sort_key)
    best = sorted_candidates[0] if sorted_candidates else None

    if best:
        # 完全に別物チェック: 候補のどのタイトルにも正規化後クエリが含まれない場合は_emptyを返す
        all_titles = [normalize_title(t.get("title", "")) for t in best.get("titles", [])]
        if not any(norm_query in t or t in norm_query for t in all_titles if t):
            logger.warning(
                f"[THOUGHT: Jikan最良候補がクエリと無関係と判断: query='{query_title}', "
                f"best_titles={[t.get('title') for t in best.get('titles', [])[:3]]}]"
            )
            return None

        logger.debug(
            f"[THOUGHT: Jikan最良候補 → score/year優先: {best.get('title')} (score={best.get('score')})]"
        )

    return best


def enrich_with_jikan(title: str) -> dict:
    """
    Jikan APIでアニメ情報を補完する。
    5件候補を取得し、最も正しい1件を選択する。
    候補が完全に別物の場合は空辞書を返す（誤情報汚染を防ぐ）。
    """
    _empty: dict = {
        "mal_id": None,
        "image_url": None,
        "genres": [],
        "themes": [],
        "score": None,
        "studio": None,
        "title_english": None,
        "title_japanese": None,
    }
    logger.debug(f"[LOG: START] enrich_with_jikan: title='{title}'")

    query = urllib.parse.quote(title)
    res = _fetch_jikan_with_backoff(f"{JIKAN_API_BASE}/anime?q={query}&limit=5")
    if not res or not res.get("data"):
        logger.warning(f"[THOUGHT: Jikanから結果なし: title='{title}']")
        return _empty

    candidates = res["data"]
    anime = _pick_best_jikan_candidate(candidates, title)
    if anime is None:
        logger.warning(f"[THOUGHT: Jikan候補なし（全て別物）: title='{title}' → _emptyを返す]")
        return _empty

    images = anime.get("images", {})
    titles = anime.get("titles", [])
    result = {
        "mal_id": anime.get("mal_id"),
        "image_url": (
            images.get("webp", {}).get("large_image_url")
            or images.get("jpg", {}).get("large_image_url")
        ),
        "genres": [g["name"] for g in anime.get("genres", []) if "Explicit" not in g["name"]],
        "themes": [t["name"] for t in anime.get("themes", []) if "Explicit" not in t["name"]],
        "score": anime.get("score") if (anime.get("score") or 0) > 0 else None,
        "studio": anime["studios"][0]["name"] if anime.get("studios") else None,
        "title_english": next(
            (t["title"] for t in titles if t.get("type") == "English"), None
        ),
        "title_japanese": next(
            (t["title"] for t in titles if t.get("type") == "Japanese"), None
        ),
    }
    logger.debug(f"[LOG: END] enrich_with_jikan: mal_id={result['mal_id']}, score={result['score']}")
    return result


# =================================================================
# ARM API（Anime Relations Map）ID取得
# =================================================================
def fetch_arm_ids(id_type: str, id_value: str) -> dict | None:
    """
    arm.kawaiioverflow.com の /api/ids を呼び出し、各DBのID対応表を取得する。

    引数:
        id_type:  "mal", "anilist", "anidb" など（APIの service パラメータ）
        id_value: 対応するID文字列

    戻り値:
        成功時: {"mal_id": int, "anilist_id": int, "syobocal_tid": int, ...} の辞書
        失敗時: None（スクリプト全体を停止しない）
    """
    logger.info(f"[LOG: START] fetch_arm_ids: id_type={id_type}, id_value={id_value}")
    try:
        url = f"{ARM_API_URL}?service={id_type}&id={id_value}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            # レスポンスはオブジェクト 1件 または 配列の場合を両方考慮
            if isinstance(data, list):
                if not data:
                    logger.warning(
                        f"[THOUGHT: ARM API 空配列レスポンス: id_type={id_type}, id_value={id_value}]"
                    )
                    return None
                result = data[0]
            elif isinstance(data, dict):
                result = data
            else:
                logger.warning(f"[THOUGHT: ARM API 予期しないレスポンス型: {type(data)}]")
                return None
            logger.info(f"[LOG: END] fetch_arm_ids → {result}")
            return result
        elif response.status_code == 404:
            logger.info(
                f"[THOUGHT: ARM API 404 Not Found（未登録）: id_type={id_type}, id_value={id_value}]"
            )
            return None
        else:
            logger.warning(
                f"[THOUGHT: ARM API ステータスエラー: {response.status_code} "
                f"id_type={id_type}, id_value={id_value}]"
            )
            return None
    except Exception as e:
        logger.error(f"[THOUGHT: fetch_arm_ids 例外: {e} (id_type={id_type}, id_value={id_value})]")
        return None


# =================================================================
# メイン処理
# =================================================================
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--season", type=str, help="対象シーズン文字列 (例: '2026年冬アニメ')")
    args = parser.parse_args()

    logger.info("[LOG: START] season_adder.py 開始")

    season_info = get_season_info(args.season)
    logger.info(f"[THOUGHT: Target Season: {season_info['season_str']}, yyyymm={season_info['yyyymm']}]")

    watch_list = load_json_safe(WATCH_LIST_FILE, [])
    broadcast_history = load_json_safe(BROADCAST_HISTORY_FILE, {})
    logger.info(f"[THOUGHT: 既存watch_list={len(watch_list)}件読み込み]")

    # --- Grok呼び出し ---
    raw_text = call_grok_for_season(season_info["season_str"])
    if not raw_text:
        logger.error("[LOG: END] Grok応答が取得できなかったため終了。")
        sys.exit(1)

    # --- Grokレスポンスパース ---
    anime_list = parse_grok_response(raw_text)
    if not anime_list:
        logger.error("[LOG: END] Grokレスポンスのパースに失敗したため終了。")
        sys.exit(1)

    logger.info(f"[THOUGHT: Grokから{len(anime_list)}件のアニメ情報を取得]")

    new_count = 0
    skip_count = 0
    error_count = 0

    for anime in anime_list:
        title = anime.title
        try:
            # --- 重複チェック ---
            if is_duplicate(title, watch_list):
                logger.info(f"[THOUGHT: スキップ（重複）: '{title}']")
                skip_count += 1
                continue

            anime_id = generate_anime_id(title, anime.short_id, season_info["yyyymm"])
            logger.info(f"[THOUGHT: 処理中: '{title}' → anime_id='{anime_id}']")

            # --- Jikan補完 ---
            jikan = enrich_with_jikan(title)

            # --- ARM APIで syoboi_tid を取得 ---
            syoboi_tid: Optional[int] = None
            if jikan.get("mal_id"):
                arm_ids = fetch_arm_ids("mal", str(jikan["mal_id"]))
                if arm_ids and arm_ids.get("syobocal_tid"):
                    syoboi_tid = int(arm_ids["syobocal_tid"])
                    logger.info(
                        f"[OUTPUT] syoboi_tid取得成功: '{title}' "
                        f"mal_id={jikan['mal_id']} → syoboi_tid={syoboi_tid}"
                    )
                else:
                    logger.info(
                        f"[THOUGHT: syoboi_tid取得不可: '{title}' mal_id={jikan['mal_id']}]"
                    )
            else:
                logger.debug(f"[THOUGHT: mal_idなしのためARM APIスキップ: '{title}']")

            # --- masterデータ構築 ---
            station_master = (
                anime.schedules[0].station.upper() if anime.schedules else None
            )
            master = {
                "anime_id": anime_id,
                "title": title,
                "official_url": anime.official_url,
                "hashtag": None,
                "station_master": station_master,
                "cast": [],
                "staff": {},
                "sources": {"manga_amazon": None, "goods": []},
                "syoboi_tid": syoboi_tid,
                **jikan,
            }

            if not args.dry_run:
                DATABASE_MASTER_DIR.mkdir(parents=True, exist_ok=True)
                (DATABASE_MASTER_DIR / f"{anime_id}.json").write_text(
                    json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                watch_list.append({
                    "anime_id": anime_id,
                    "mal_id": jikan["mal_id"],
                    "title": title,
                    "official_url": anime.official_url,
                    "last_checked_ep": anime.ep_num,
                    "is_active": True,
                    "season": season_info["season_key"],
                    "season_end_date": None,
                    "syoboi_tid": syoboi_tid,
                })
                platforms = {}
                for sch in anime.schedules:
                    station_key = sch.station or "unknown"
                    platforms[station_key] = {
                        "last_ep_num": anime.ep_num,
                        "remarks": f"{sch.day_of_week} {sch.time}".strip(),
                    }
                broadcast_history[anime_id] = {
                    "title": title,
                    "overall_latest_ep": anime.ep_num,
                    "platforms": platforms,
                }

            new_count += 1
            logger.info(f"[OUTPUT] Added: '{title}' ({anime_id})")

        except Exception as e:
            # 1件の失敗で全体を止めない（部分成功を許容）
            error_count += 1
            logger.error(f"[THOUGHT: '{title}' の処理中に予期しないエラー（スキップして継続）: {e}]")
            continue

    # --- ファイル書き込み ---
    if not args.dry_run and new_count > 0:
        WATCH_LIST_FILE.write_text(
            json.dumps(watch_list, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        BROADCAST_HISTORY_FILE.write_text(
            json.dumps(broadcast_history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[THOUGHT: watch_list.json / broadcast_history.json を更新しました]")

    # --- 既存エントリの syoboi_tid 遡及補完 ---
    # mal_id はあるが syoboi_tid が未設定のエントリを ARM API で補完する。
    # 新規追加分も含め watch_list 全体を対象とする。
    logger.info("[LOG: START] Step 既存watch_listの syoboi_tid 遡及補完")
    arm_updated = 0
    for entry in watch_list:
        if entry.get("mal_id") and entry.get("syoboi_tid") is None:
            arm_ids = fetch_arm_ids("mal", str(entry["mal_id"]))
            if arm_ids and arm_ids.get("syobocal_tid"):
                entry["syoboi_tid"] = int(arm_ids["syobocal_tid"])
                arm_updated += 1
                logger.info(
                    f"[OUTPUT] syoboi_tid遡及補完: '{entry.get('title')}' "
                    f"mal_id={entry['mal_id']} → syoboi_tid={entry['syoboi_tid']}"
                )
            else:
                logger.debug(
                    f"[THOUGHT: syoboi_tid遡及補完不可: '{entry.get('title')}' "
                    f"mal_id={entry.get('mal_id')}]"
                )

    if not args.dry_run and arm_updated > 0:
        WATCH_LIST_FILE.write_text(
            json.dumps(watch_list, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            f"[THOUGHT: syoboi_tid遡及補完により {arm_updated}件 watch_list.json を更新しました]"
        )
    logger.info(
        f"[LOG: END] syoboi_tid遡及補完完了。arm_updated={arm_updated}件"
    )

    logger.info(
        f"[LOG: END] season_adder.py 終了。"
        f" added={new_count}, skipped(重複)={skip_count}, errors={error_count}"
    )


def load_json_safe(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[THOUGHT: {path} の読み込みに失敗: {e}。デフォルト値を使用]")
        return default


if __name__ == "__main__":
    main()
