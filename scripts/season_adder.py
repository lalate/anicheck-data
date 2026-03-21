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
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv
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
    clean_short_id = re.sub(r'[^a-zA-Z0-9]', '', short_id).lower()
    if not clean_short_id:
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
        clean_short_id = f"gen_{digest}"
    return f"{yyyymm}_{clean_short_id}_c1"


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
    logger.info(f"[LOG: START] Step 3: Grok API 呼び出し (target={season_str})")
    client = Client()
    user_input = (
        f"対象：{season_str}\n"
        f"この時期に日本で放送開始された、あるいは放送中の主要な深夜アニメを 20作品程度リストアップしてください。"
        f"web_search 等で最新情報を必ず取得し、正確な公式URLとスケジュールを記載してください。"
    )
    try:
        chat = client.chat.create(model="grok-4-1-fast-reasoning", tools=[web_search(), x_search()], tool_choice="auto")
        chat.append(system(_GROK_SYSTEM_PROMPT))
        chat.append(user(user_input))
        response = chat.sample()
        return response.content or ""
    except Exception as e:
        logger.error(f"Grok Error: {e}")
        return None

def parse_grok_response(text: str) -> list | None:
    json_blocks = re.findall(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)
    if not json_blocks:
        json_blocks = re.findall(r"(\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*\])", text, re.DOTALL)
    if not json_blocks: return None
    try:
        return json.loads(json_blocks[0])
    except: return None

def is_duplicate(new_title: str, existing_list: list) -> bool:
    new_lower = new_title.lower()
    for entry in existing_list:
        existing_lower = entry.get("title", "").lower()
        if new_lower == existing_lower or new_lower in existing_lower or existing_lower in new_lower:
            return True
    return False

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
        except:
            time.sleep(backoff)
            retries += 1
            backoff *= 2
    return None

def enrich_with_jikan(title: str) -> dict:
    _empty = {"mal_id": None, "image_url": None, "genres": [], "themes": [], "score": None, "studio": None, "title_english": None, "title_japanese": None}
    query = urllib.parse.quote(title)
    res = _fetch_jikan_with_backoff(f"{JIKAN_API_BASE}/anime?q={query}&limit=5")
    if not res or not res.get("data"): return _empty
    anime = res["data"][0]
    images = anime.get("images", {})
    titles = anime.get("titles", [])
    return {
        "mal_id": anime.get("mal_id"),
        "image_url": images.get("webp", {}).get("large_image_url") or images.get("jpg", {}).get("large_image_url"),
        "genres": [g["name"] for g in anime.get("genres", []) if "Explicit" not in g["name"]],
        "themes": [t["name"] for t in anime.get("themes", []) if "Explicit" not in t["name"]],
        "score": anime.get("score") if (anime.get("score") or 0) > 0 else None,
        "studio": anime["studios"][0]["name"] if anime.get("studios") else None,
        "title_english": next((t["title"] for t in titles if t.get("type") == "English"), None),
        "title_japanese": next((t["title"] for t in titles if t.get("type") == "Japanese"), None),
    }

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--season", type=str, help="対象シーズン文字列 (例: '2026年冬アニメ')")
    args = parser.parse_args()

    season_info = get_season_info(args.season)
    logger.info(f"Target Season: {season_info['season_str']}")

    watch_list = load_json_safe(WATCH_LIST_FILE, [])
    broadcast_history = load_json_safe(BROADCAST_HISTORY_FILE, {})

    raw_text = call_grok_for_season(season_info["season_str"])
    if not raw_text: sys.exit(1)

    anime_list = parse_grok_response(raw_text)
    if not anime_list: sys.exit(1)

    new_count = 0
    for anime in anime_list:
        title = (anime.get("title") or "").strip()
        if not title or is_duplicate(title, watch_list): continue

        anime_id = generate_anime_id(title, anime.get("short_id", ""), season_info["yyyymm"])
        jikan = enrich_with_jikan(title)
        
        master = {
            "anime_id": anime_id, "title": title, "official_url": anime.get("official_url"),
            "hashtag": None, "station_master": anime.get("schedules", [{}])[0].get("station", "").upper() if anime.get("schedules") else None,
            "cast": [], "staff": {}, "sources": {"manga_amazon": None, "goods": []},
            **jikan
        }

        if not args.dry_run:
            (DATABASE_MASTER_DIR / f"{anime_id}.json").write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8")
            watch_list.append({"anime_id": anime_id, "mal_id": jikan["mal_id"], "title": title, "official_url": anime.get("official_url"), "last_checked_ep": anime.get("ep_num", 0), "is_active": True, "season": season_info["season_key"], "season_end_date": None})
            
            platforms = {}
            for sch in anime.get("schedules", []):
                platforms[sch.get("station", "unknown")] = {"last_ep_num": anime.get("ep_num", 0), "remarks": f"{sch.get('day_of_week', '')} {sch.get('time', '')}".strip()}
            broadcast_history[anime_id] = {"title": title, "overall_latest_ep": anime.get("ep_num", 0), "platforms": platforms}
        
        new_count += 1
        logger.info(f"Added: {title} ({anime_id})")

    if not args.dry_run and new_count > 0:
        WATCH_LIST_FILE.write_text(json.dumps(watch_list, ensure_ascii=False, indent=2), encoding="utf-8")
        BROADCAST_HISTORY_FILE.write_text(json.dumps(broadcast_history, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(f"Finished. Added {new_count} items.")

def load_json_safe(path: Path, default):
    if not path.exists(): return default
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except: return default

if __name__ == "__main__":
    main()
