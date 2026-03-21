#!/usr/bin/env python3
import json
import glob
import os
import shutil
from pathlib import Path
from datetime import date

def load_json(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def main():
    base_dir = Path("/Volumes/DevSSD/Dev/AniCheck/AniJson")
    backup_dir = base_dir / "archive" / "current_backup_2026-03-10"
    current_dir = base_dir / "current"
    db_master_dir = base_dir / "database" / "master"
    db_episodes_dir = base_dir / "database" / "episodes"

    # Create directories
    db_master_dir.mkdir(parents=True, exist_ok=True)
    db_episodes_dir.mkdir(parents=True, exist_ok=True)
    
    # Load current watch_list
    watch_list_path = current_dir / "watch_list.json"
    if not watch_list_path.exists():
        print("watch_list.json not found!")
        return

    old_watch_list = load_json(watch_list_path)
    new_watch_list = []
    broadcast_history = {}
    today = date.today().isoformat()

    # Load all backed up masters to find matches by title
    backup_masters = list(backup_dir.glob("*_master.json"))
    master_data_map = {} # title -> (anime_id, path, data)
    
    for m_path in backup_masters:
        try:
            m_data = load_json(m_path)
            title = m_data.get('title', '')
            anime_id = m_data.get('anime_id', '')
            if title and anime_id:
                # Also index by partial match for robust matching
                master_data_map[title] = (anime_id, m_path, m_data)
        except Exception as e:
            print(f"Error reading {m_path}: {e}")

    print(f"Loaded {len(master_data_map)} masters from backup.")

    for item in old_watch_list:
        title = item.get('title', '')
        ep_num = item.get('ep_num', 1)
        schedules = item.get('schedules', [])
        
        # Try to match anime_id
        anime_id = None
        matched_m_data = None
        
        # 1. Exact match
        if title in master_data_map:
            anime_id, _, matched_m_data = master_data_map[title]
        else:
            # 2. Partial match (e.g. "葬送のフリーレン 第2期" matches "葬送のフリーレン")
            for m_title, (m_id, m_p, m_d) in master_data_map.items():
                if m_title in title or title in m_title:
                    anime_id = m_id
                    matched_m_data = m_d
                    print(f"Fuzzy matched: '{title}' with '{m_title}' ({anime_id})")
                    break

        if not anime_id:
            # If still no match, generate a pseudo anime_id based on romanized/hashed title
            anime_id = f"gen_{hash(title) % 1000000}"
            print(f"Warning: No backup master found for '{title}'. Generated ID: {anime_id}")
            matched_m_data = {
                "anime_id": anime_id,
                "title": title,
                "official_url": item.get('official_url', ''),
                "hashtag": "",
                "station_master": "",
                "sources": {},
                "staff": {},
                "cast": []
            }

        # --- 1. Save to database/master ---
        # Ensure V2 fields exist in master
        matched_m_data['mal_id'] = matched_m_data.get('mal_id', None)
        matched_m_data['genres'] = matched_m_data.get('genres', [])
        matched_m_data['themes'] = matched_m_data.get('themes', [])
        matched_m_data['image_url'] = matched_m_data.get('image_url', None)
        save_json(db_master_dir / f"{anime_id}.json", matched_m_data)

        # --- 2. Save to database/episodes ---
        ep_dir = db_episodes_dir / anime_id
        ep_dir.mkdir(exist_ok=True)
        
        # Try to find corresponding backed up episode
        old_ep_path = backup_dir / f"{anime_id}_episode.json"
        if old_ep_path.exists():
            try:
                ep_data = load_json(old_ep_path)
                # Save as epXXX.json
                save_json(ep_dir / f"ep{ep_num:03d}.json", ep_data)
            except Exception as e:
                print(f"Failed to copy episode for {anime_id}: {e}")
        else:
            # Create a dummy one for migration
            dummy_ep = {
                "anime_id": anime_id,
                "ep_num": ep_num,
                "title": f"第{ep_num}話",
                "summary": "情報未取得",
                "confirmed_at": today
            }
            save_json(ep_dir / f"ep{ep_num:03d}.json", dummy_ep)

        # --- 3. Build broadcast_history entry ---
        platforms = {}
        for sch in schedules:
            st = sch.get('station', 'unknown')
            platforms[st] = {
                "last_ep_num": ep_num,
                "last_broadcast_date": today,
                "last_updated_at": today,
                "remarks": "Migrated from v1 schedules"
            }
        
        broadcast_history[anime_id] = {
            "title": title,
            "overall_latest_ep": ep_num,
            "platforms": platforms
        }

        # --- 4. Build new watch_list entry ---
        new_watch_list.append({
            "anime_id": anime_id,
            "mal_id": matched_m_data.get('mal_id', None),
            "title": title,
            "official_url": item.get('official_url', ''),
            "last_checked_ep": ep_num,
            "is_active": True,
            "season": "2026_winter", # 仮置き
            "season_end_date": "2026-03-31" # 仮置き
        })

    # Save new configuration
    save_json(current_dir / "watch_list.json", new_watch_list)
    save_json(current_dir / "broadcast_history.json", broadcast_history)
    
    print("Migration to V2 DB structure completed successfully!")

if __name__ == '__main__':
    main()
