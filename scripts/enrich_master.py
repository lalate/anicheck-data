#!/usr/bin/env python3
import json
import glob
import time
import urllib.parse
from pathlib import Path
import requests

JIKAN_API_BASE = "https://api.jikan.moe/v4"

def load_json(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def fetch_with_backoff(url, max_retries=5):
    """Jikan APIをRate Limitに配慮して叩く（指数バックオフ）"""
    retries = 0
    backoff = 2
    while retries < max_retries:
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                time.sleep(0.5) # Rate Limit: 3 requests/second
                return response.json()
            elif response.status_code == 429:
                print(f"[Rate Limit] 429 Too Many Requests. Retrying in {backoff} seconds...")
                time.sleep(backoff)
                retries += 1
                backoff *= 2
            else:
                print(f"[Error] HTTP {response.status_code} for URL: {url}")
                time.sleep(1)
                return None
        except requests.exceptions.RequestException as e:
            print(f"[Network Error] {e}. Retrying in {backoff} seconds...")
            time.sleep(backoff)
            retries += 1
            backoff *= 2
    
    print(f"[Fatal] Max retries reached for {url}")
    return None

def process_master_file(filepath):
    data = load_json(filepath)
    title = data.get("title")
    mal_id = data.get("mal_id")

    if mal_id:
        # 既に mal_id がある場合はスキップ、または詳細取得のみ行うことも可能
        # 今回は null のものを優先して処理する
        return False

    print(f"--- Processing: {title} ---")
    
    # 1. Search by title
    query = urllib.parse.quote(title)
    search_url = f"{JIKAN_API_BASE}/anime?q={query}&limit=5"
    search_result = fetch_with_backoff(search_url)

    if not search_result or not search_result.get("data"):
        print(f"[Skip] No results found for '{title}'")
        return False

    # 先頭の結果を正とする（精度を高めるなら放送時期などでフィルタリングが必要だが、今回は先頭を採用）
    anime_data = search_result["data"][0]
    
    # 2. Extract and Validate
    new_mal_id = anime_data.get("mal_id")
    if not new_mal_id:
        print(f"[Skip] mal_id could not be resolved for '{title}'")
        return False

    data["mal_id"] = new_mal_id

    # Title variations
    titles = anime_data.get("titles", [])
    data["title_english"] = next((t["title"] for t in titles if t["type"] == "English"), None)
    data["title_japanese"] = next((t["title"] for t in titles if t["type"] == "Japanese"), None)

    # Images (WebP Large > JPG Large)
    images = anime_data.get("images", {})
    img_url = images.get("webp", {}).get("large_image_url") or images.get("jpg", {}).get("large_image_url")
    data["image_url"] = img_url

    # Score (0.0 is null)
    score = anime_data.get("score")
    data["score"] = score if score and score > 0 else None

    # Genres & Themes (Filter Explicit)
    def extract_names(item_list):
        return [item["name"] for item in item_list if "Explicit" not in item["name"]]
    
    data["genres"] = extract_names(anime_data.get("genres", []))
    data["themes"] = extract_names(anime_data.get("themes", []))

    # Studio
    studios = anime_data.get("studios", [])
    data["studio"] = studios[0]["name"] if studios else data.get("studio")

    # Trailer
    trailer_id = anime_data.get("trailer", {}).get("youtube_id")
    if trailer_id and len(trailer_id) == 11:
        data["jikan_trailer_id"] = trailer_id

    # 3. Save
    save_json(filepath, data)
    print(f"[Success] Enriched '{title}' (mal_id: {new_mal_id})")
    return True

def main():
    master_files = glob.glob("../database/master/*.json")
    if not master_files:
        master_files = glob.glob("database/master/*.json")

    print(f"Found {len(master_files)} master files.")
    
    enriched_count = 0
    for idx, filepath in enumerate(master_files):
        if process_master_file(filepath):
            enriched_count += 1
            
        # 60 requests per minute limit check
        if (idx + 1) % 20 == 0:
            print("Taking a short break to respect rate limits (20s)...")
            time.sleep(20)

    print(f"Enrichment completed. Updated {enriched_count} files.")

if __name__ == "__main__":
    main()
