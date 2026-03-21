#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
season_adder.py

次期シーズンのアニメを watch_list.json / broadcast_history.json へ追記するスクリプト。
既存エントリは一切削除・変更しない（追記専用）。

使い方 (AniJson/ ディレクトリから実行):
  python scripts/season_adder.py           # 通常実行
  python scripts/season_adder.py --dry-run # 書き込みなしで動作確認
"""

import argparse
import datetime
import hashlib
import json
import logging
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv
from xai_sdk import Client
from xai_sdk.chat import system, user
from xai_sdk.tools import web_search, x_search

load_dotenv()

# =================================================================
# パス設定（__file__ 基準で解決するため、どのディレクトリから実行しても安全）
# =================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent          # AniJson/
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
JIKAN_SLEEP = 0.5  # Rate Limit 3 req/sec → 0.5 秒待機

# =================================================================
# シーズン判定テーブル
# =================================================================
# 現在月 → (日本語季節名, 英語季節名)
_CURRENT_SEASON_MAP: dict[int, tuple[str, str]] = {
    1: ("冬", "winter"), 2: ("冬", "winter"), 3: ("冬", "winter"),
    4: ("春", "spring"), 5: ("春", "spring"), 6: ("春", "spring"),
    7: ("夏", "summer"), 8: ("夏", "summer"), 9: ("夏", "summer"),
    10: ("秋", "autumn"), 11: ("秋", "autumn"), 12: ("秋", "autumn"),
}

# 現在の日本語季節名 → (次の日本語季節名, 次の英語季節名, 次のシーズン開始月)
_NEXT_SEASON_MAP: dict[str, tuple[str, str, int]] = {
    "冬": ("春", "spring", 4),   # 春は同年4月開始
    "春": ("夏", "summer", 7),   # 夏は同年7月開始
    "夏": ("秋", "autumn", 10),  # 秋は同年10月開始
    "秋": ("冬", "winter", 1),   # 冬は翌年1月開始
}


def determine_next_season() -> dict:
    """現在日時から「次期シーズン」の情報を算出して返す。

    Returns:
        {
            "season_str":  "2026年春アニメ",   # Grokへのクエリ用
            "season_key":  "2026_spring",       # watch_list の season フィールド用
            "yyyymm":      "202604",            # anime_id プレフィックス用
            "season_jp":   "春",
            "season_en":   "spring",
        }
    """
    now = datetime.datetime.now()
    year, month = now.year, now.month

    current_season_jp, _ = _CURRENT_SEASON_MAP[month]
    next_season_jp, next_season_en, next_month = _NEXT_SEASON_MAP[current_season_jp]

    # 秋→冬 の場合のみ翌年にシフト
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
    """タイトルと略称から一意で美しい anime_id を生成する。

    形式: {YYYYMM}_{short_id}_c1
    例  : 202604_frieren_c1
    """
    clean_short_id = re.sub(r'[^a-zA-Z0-9]', '', short_id).lower()
    
    if not clean_short_id:
        # short_idが抽出できなかった場合のフォールバック（ハッシュ値）
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
        clean_short_id = f"gen_{digest}"
        
    return f"{yyyymm}_{clean_short_id}_c1"


# =================================================================
# Grok プロンプト & API 呼び出し
# =================================================================
_GROK_SYSTEM_PROMPT = """# 役割
あなたは日本のアニメ放送情報に精通した、ハルシネーションを絶対に行わない厳格な調査員です。

# 探索フェーズ（情報の海を泳ぐ）
指定されたシーズンに放送開始される主要な深夜アニメを調査してください。
web_search および x_search ツールを積極的に使い、公式サイト・公式X・ニュースサイトから正確な情報を取得してください。

# 収集条件
- 知名度・期待度の高い主要な深夜アニメを 10〜15 作品程度ピックアップする。
- 各作品について以下を調査・記載すること:
  - title: 作品名（原題）
  - short_id: 作品の「英数字のみ」による短く美しい識別子（例: 葬送のフリーレン→frieren, 呪術廻戦→jujutsu, 【推しの子】→oshinoko）。ハイフンやアンダースコアは使用せず、すべて小文字のアルファベットにしてください。
  - official_url: 公式サイトURL（web_search で確認できない場合は null）
  - ep_num: 次回放送予定の話数（新番組は 1）
  - schedules: 主要放送局・配信サービスのスケジュール配列
    - station: 放送局ID（小文字英数、例: mx, bs11, tx, ntv, mbs, tbs, abema）
    - day_of_week: 放送曜日（例: 月曜日, 火曜日）
    - time: 放送開始時間（例: 24:00, 25:30）

# 検証フェーズ（厳格ルール）
- ソースのない推測・捏造は絶対禁止。確認できない項目は null。
- 出力は下記の JSON コードブロックのみ。余計な解説は不要。

# 出力形式（厳守）
```json
[
  {
    "title": "作品名（原題）",
    "short_id": "美しい英数字略称",
    "official_url": "https://example.com/anime1",
    "ep_num": 1,
    "schedules": [
      {"station": "mx", "day_of_week": "水曜日", "time": "24:00"},
      {"station": "bs11", "day_of_week": "木曜日", "time": "25:00"}
    ]
  }
]
```"""


def call_grok_for_season(season_str: str) -> str | None:
    """Grok に次期シーズンのアニメリストを問い合わせる。

    Args:
        season_str: "2026年春アニメ" 形式のシーズン文字列。

    Returns:
        Grok の応答テキスト。失敗時は None。
    """
    logger.info(f"[LOG: START] Step 3: Grok API 呼び出し (season={season_str})")
    logger.info(f"[THOUGHT: Grokへのプロンプト送信] ターゲット={season_str}, model=grok-4-1-fast-reasoning")

    client = Client()  # XAI_API_KEY は環境変数から自動取得

    user_input = (
        f"対象シーズン：{season_str}\n"
        f"このシーズンに放送開始される主要な深夜アニメを 10〜15 作品程度リストアップしてください。"
        f"web_search と x_search ツールを使って最新の公式情報を必ず取得し、"
        f"正確な公式URLとスケジュールを記載してください。"
    )

    try:
        chat = client.chat.create(
            model="grok-4-1-fast-reasoning",
            tools=[web_search(), x_search()],
            tool_choice="auto",
        )
        chat.append(system(_GROK_SYSTEM_PROMPT))
        chat.append(user(user_input))
        response = chat.sample()
        result = response.content or ""
        logger.info(f"[OUTPUT] Grok 応答受信: {len(result)} chars")
        logger.info("[LOG: END] Step 3")
        return result
    except Exception as e:
        logger.error(f"[THOUGHT: Grok API 呼び出し失敗] {e}", exc_info=True)
        logger.info("[LOG: END] Step 3 (FAILED)")
        return None


def parse_grok_response(text: str) -> list | None:
    """Grok 応答テキストから JSON 配列を抽出する。

    コードブロック（```json ... ```）内の配列を優先し、
    見つからない場合は裸の JSON 配列へフォールバックする。

    Returns:
        アニメ情報の list。抽出失敗時は None。
    """
    logger.info("[LOG: START] Step 4: Grok レスポンスのパース")
    logger.info(f"[THOUGHT: 正規表現でJSONコードブロックを抽出] テキスト長={len(text)}")

    # パターン1: ```json [...] ``` コードブロック
    json_blocks = re.findall(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)

    if not json_blocks:
        logger.info("[THOUGHT: コードブロックなし → 裸の JSON 配列へフォールバック]")
        # パターン2: 裸の JSON 配列（ネストを考慮）
        json_blocks = re.findall(r"(\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*\])", text, re.DOTALL)

    if not json_blocks:
        logger.error("[THOUGHT: JSON 抽出失敗] Grok 応答に JSON 配列が見つかりません")
        logger.info("[LOG: END] Step 4 (FAILED)")
        return None

    try:
        anime_list = json.loads(json_blocks[0])
        if not isinstance(anime_list, list):
            logger.error("[THOUGHT: パース失敗] 抽出データがリストではありません")
            logger.info("[LOG: END] Step 4 (FAILED)")
            return None
        logger.info(f"[OUTPUT] パース成功: {len(anime_list)} 件")
        logger.info("[LOG: END] Step 4")
        return anime_list
    except json.JSONDecodeError as e:
        logger.error(f"[THOUGHT: JSON デコード失敗] {e}\nRaw: {text[:400]}...")
        logger.info("[LOG: END] Step 4 (FAILED)")
        return None


# =================================================================
# 重複チェック
# =================================================================
def is_duplicate(new_title: str, existing_list: list) -> bool:
    """既存 watch_list に同名または部分一致タイトルが存在するか確認する。

    大文字小文字を区別せず、どちらかのタイトルが他方を内包する場合も重複とみなす。
    """
    new_lower = new_title.lower()
    for entry in existing_list:
        existing_lower = entry.get("title", "").lower()
        if new_lower == existing_lower:
            return True
        if new_lower in existing_lower or existing_lower in new_lower:
            return True
    return False


# =================================================================
# Jikan API エンリッチ
# =================================================================
def _fetch_jikan_with_backoff(url: str, max_retries: int = 5) -> dict | None:
    """Jikan API を Rate Limit に配慮して叩く（指数バックオフ）。"""
    retries = 0
    backoff = 2
    while retries < max_retries:
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                time.sleep(JIKAN_SLEEP)  # Rate Limit: 3 req/sec
                return response.json()
            elif response.status_code == 429:
                logger.warning(f"[Rate Limit] 429 Too Many Requests. Retrying in {backoff}s...")
                time.sleep(backoff)
                retries += 1
                backoff *= 2
            else:
                logger.warning(f"[Jikan Error] HTTP {response.status_code} for {url}")
                time.sleep(1)
                return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"[Network Error] {e}. Retrying in {backoff}s...")
            time.sleep(backoff)
            retries += 1
            backoff *= 2
    logger.error(f"[Fatal] Max retries reached for {url}")
    return None


def enrich_with_jikan(title: str) -> dict:
    """Jikan API でタイトルを検索してメタデータを取得する。

    Returns:
        mal_id, image_url, genres, themes, score, studio,
        title_english, title_japanese を含む辞書。
        取得失敗時は各フィールドが None / [] の辞書を返す。
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

    logger.info(f"[LOG: START] Jikan 検索: {title}")
    logger.info(f"[THOUGHT: Jikan API に '{title}' を問い合わせ]")

    query = urllib.parse.quote(title)
    search_url = f"{JIKAN_API_BASE}/anime?q={query}&limit=5"
    result = _fetch_jikan_with_backoff(search_url)

    if not result or not result.get("data"):
        logger.warning(f"[THOUGHT: Jikan 結果なし] '{title}' → フォールバック空データを使用")
        return _empty

    anime_data = result["data"][0]
    mal_id = anime_data.get("mal_id")
    if not mal_id:
        logger.warning(f"[THOUGHT: mal_id 取得失敗] '{title}'")
        return _empty

    def extract_names(items: list) -> list:
        return [item["name"] for item in items if "Explicit" not in item.get("name", "")]

    images = anime_data.get("images", {})
    img_url = (
        images.get("webp", {}).get("large_image_url")
        or images.get("jpg", {}).get("large_image_url")
    )

    score = anime_data.get("score")
    score = score if (score and score > 0) else None

    studios = anime_data.get("studios", [])
    studio = studios[0]["name"] if studios else None

    titles = anime_data.get("titles", [])
    title_english = next((t["title"] for t in titles if t.get("type") == "English"), None)
    title_japanese = next((t["title"] for t in titles if t.get("type") == "Japanese"), None)

    enriched = {
        "mal_id": mal_id,
        "image_url": img_url,
        "genres": extract_names(anime_data.get("genres", [])),
        "themes": extract_names(anime_data.get("themes", [])),
        "score": score,
        "studio": studio,
        "title_english": title_english,
        "title_japanese": title_japanese,
    }
    logger.info(f"[OUTPUT] Jikan 取得成功: mal_id={mal_id}, score={score}, studio={studio}")
    return enriched


# =================================================================
# エントリ構築ヘルパー
# =================================================================
def build_master_json(
    anime_id: str,
    title: str,
    official_url: str | None,
    schedules: list,
    jikan: dict,
) -> dict:
    """database/master/{anime_id}.json の内容を構築する。

    既存の master JSON スキーマ（202601_frieren_c2.json 等）に準拠。
    """
    # schedules の先頭局をキーステーションとする（存在しない場合は None）
    station_master = schedules[0]["station"].upper() if schedules else None

    return {
        "anime_id": anime_id,
        "title": title,
        "official_url": official_url,
        "hashtag": None,
        "station_master": station_master,
        "cast": [],
        "staff": {},
        "sources": {
            "manga_amazon": None,
            "goods": [],
        },
        "mal_id": jikan.get("mal_id"),
        "genres": jikan.get("genres", []),
        "themes": jikan.get("themes", []),
        "image_url": jikan.get("image_url"),
        "title_english": jikan.get("title_english"),
        "title_japanese": jikan.get("title_japanese"),
        "score": jikan.get("score"),
        "studio": jikan.get("studio"),
    }


def build_watch_list_entry(
    anime_id: str,
    title: str,
    official_url: str | None,
    jikan: dict,
    season_key: str,
) -> dict:
    """watch_list.json への追記エントリを構築する。

    既存エントリのスキーマに準拠（202601_frieren_c2 等）。
    """
    return {
        "anime_id": anime_id,
        "mal_id": jikan.get("mal_id"),
        "title": title,
        "official_url": official_url,
        "last_checked_ep": 0,
        "is_active": True,
        "season": season_key,
        "season_end_date": None,
    }


def build_broadcast_history_entry(title: str, schedules: list) -> dict:
    """broadcast_history.json への追記エントリを構築する。

    schedules から各局の初期エントリ（last_ep_num: 0）を生成する。
    """
    platforms: dict = {}
    for sch in schedules:
        station = sch.get("station", "unknown")
        day_and_time = f"{sch.get('day_of_week', '')} {sch.get('time', '')}".strip()
        platforms[station] = {
            "last_ep_num": 0,
            "remarks": day_and_time,
        }

    return {
        "title": title,
        "overall_latest_ep": 0,
        "platforms": platforms,
    }


# =================================================================
# ファイル読み込みヘルパー（例外安全）
# =================================================================
def load_json_safe(path: Path, default):
    """JSON ファイルを安全に読み込む。ファイルが存在しないか壊れている場合は default を返す。"""
    if not path.exists():
        logger.info(f"[THOUGHT: ファイル不在] {path} → デフォルト値を使用")
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[THOUGHT: 読み込みエラー] {path}: {e} → デフォルト値を使用")
        return default


# =================================================================
# メイン処理
# =================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="次期シーズンのアニメを watch_list / broadcast_history へ追記する（追記専用・既存削除なし）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ファイルへの書き込みを行わずログだけ確認する",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("[LOG: START] Step 0: season_adder.py 起動")
    logger.info(f"[THOUGHT: dry_run={args.dry_run}]")
    logger.info(f"[THOUGHT: BASE_DIR={BASE_DIR}]")

    # ----------------------------------------------------------------
    # Step 1: 次期シーズン判定
    # ----------------------------------------------------------------
    logger.info("[LOG: START] Step 1: 次期シーズン判定")
    season_info = determine_next_season()
    logger.info(
        f"[OUTPUT] season_str={season_info['season_str']}, "
        f"season_key={season_info['season_key']}, "
        f"yyyymm={season_info['yyyymm']}"
    )
    logger.info("[LOG: END] Step 1")

    # ----------------------------------------------------------------
    # Step 2: 既存データ読み込み
    # ----------------------------------------------------------------
    logger.info("[LOG: START] Step 2: 既存ファイル読み込み")
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_MASTER_DIR.mkdir(parents=True, exist_ok=True)

    watch_list: list = load_json_safe(WATCH_LIST_FILE, [])
    broadcast_history: dict = load_json_safe(BROADCAST_HISTORY_FILE, {})

    logger.info(
        f"[OUTPUT] watch_list={len(watch_list)}件, broadcast_history={len(broadcast_history)}キー"
    )
    logger.info("[LOG: END] Step 2")

    # ----------------------------------------------------------------
    # Step 3: Grok API 呼び出し
    # ----------------------------------------------------------------
    raw_text = call_grok_for_season(season_info["season_str"])
    if not raw_text:
        logger.error("[THOUGHT: Grok 呼び出し失敗] 処理を中断します")
        sys.exit(1)

    # デバッグ用: 生レスポンスをファイルに保存（dry-run でも保存する）
    raw_output_file = BASE_DIR / f"raw_grok_output_{season_info['season_str']}.txt"
    try:
        raw_output_file.write_text(raw_text, encoding="utf-8")
        logger.info(f"[OUTPUT] 生レスポンス保存: {raw_output_file.name}")
    except Exception as e:
        logger.warning(f"[THOUGHT: 生レスポンス保存失敗] {e}")

    # ----------------------------------------------------------------
    # Step 4: Grok レスポンスパース
    # ----------------------------------------------------------------
    grok_anime_list = parse_grok_response(raw_text)
    if not grok_anime_list:
        logger.error(
            "[THOUGHT: パース失敗] 処理を中断します。"
            f"生レスポンスファイル ({raw_output_file.name}) を確認してください。"
        )
        sys.exit(1)

    # ----------------------------------------------------------------
    # Step 5: 各アニメを処理（重複チェック → Jikan → ファイル生成）
    # ----------------------------------------------------------------
    logger.info("[LOG: START] Step 5: 各アニメの処理")
    new_entries_count = 0
    skipped_count = 0

    for idx, anime in enumerate(grok_anime_list):
        title: str = (anime.get("title") or "").strip()
        short_id: str = (anime.get("short_id") or "").strip()
        official_url: str | None = anime.get("official_url")
        schedules: list = anime.get("schedules") or []

        if not title:
            logger.warning(f"[THOUGHT: タイトル空] idx={idx} → スキップ")
            skipped_count += 1
            continue

        logger.info(f"--- [{idx + 1}/{len(grok_anime_list)}] {title} ---")
        logger.info(f"[THOUGHT: official_url={official_url}, schedules={len(schedules)}件]")

        # Step 5a: 重複チェック
        if is_duplicate(title, watch_list):
            logger.info(f"[THOUGHT: 重複スキップ] '{title}' は既に watch_list に存在します")
            skipped_count += 1
            continue

        # Step 5b: anime_id 生成
        anime_id = generate_anime_id(title, short_id, season_info["yyyymm"])
        logger.info(f"[OUTPUT] anime_id 生成: {anime_id}")

        # Step 5c: Jikan API でメタデータ取得（エンリッチ）
        jikan_data = enrich_with_jikan(title)

        # Step 5d: Master JSON 生成・保存
        master_data = build_master_json(anime_id, title, official_url, schedules, jikan_data)
        master_path = DATABASE_MASTER_DIR / f"{anime_id}.json"

        if args.dry_run:
            logger.info(f"[dry-run] Master 保存スキップ: {master_path.name}")
        else:
            master_path.write_text(
                json.dumps(master_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[OUTPUT] Master 保存: {master_path.name}")

        # Step 5e: watch_list エントリ追加
        wl_entry = build_watch_list_entry(
            anime_id, title, official_url, jikan_data, season_info["season_key"]
        )
        watch_list.append(wl_entry)
        logger.info(f"[OUTPUT] watch_list に追記: {anime_id}")

        # Step 5f: broadcast_history エントリ追加
        bh_entry = build_broadcast_history_entry(title, schedules)
        broadcast_history[anime_id] = bh_entry
        logger.info(f"[OUTPUT] broadcast_history に追記: {anime_id}")

        new_entries_count += 1

    logger.info(
        f"[OUTPUT] 処理完了: 新規追加={new_entries_count}件, スキップ={skipped_count}件"
    )
    logger.info("[LOG: END] Step 5")

    # ----------------------------------------------------------------
    # Step 6: ファイル保存
    # ----------------------------------------------------------------
    logger.info("[LOG: START] Step 6: ファイル保存")

    if new_entries_count == 0:
        logger.info("[THOUGHT: 新規追加なし] ファイル保存をスキップします")
    elif args.dry_run:
        logger.info("[dry-run] watch_list / broadcast_history の保存をスキップ")
    else:
        with open(WATCH_LIST_FILE, "w", encoding="utf-8") as f:
            json.dump(watch_list, f, ensure_ascii=False, indent=2)
        logger.info(f"[OUTPUT] watch_list 保存完了: {len(watch_list)}件")

        BROADCAST_HISTORY_FILE.write_text(
            json.dumps(broadcast_history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[OUTPUT] broadcast_history 保存完了: {len(broadcast_history)}キー")

    logger.info("[LOG: END] Step 6")
    logger.info("=" * 60)
    logger.info(
        f"🎉 season_adder.py 完了！ 新規追加: {new_entries_count}件, スキップ: {skipped_count}件"
    )
    logger.info(f"ログ: {log_file.absolute()}")


if __name__ == "__main__":
    main()
