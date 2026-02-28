# -*- coding: utf-8 -*-
import os
import json
import glob
from pathlib import Path

def validate_json_file(file_path: Path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data, None
    except json.JSONDecodeError as e:
        return None, f"Syntax Error: {e}"
    except Exception as e:
        return None, f"Error: {e}"

def validate_schema(file_path: Path, data: dict):
    # 基本的なスキーマ検証
    filename = file_path.name
    
    if filename == "watch_list.json":
        if not isinstance(data, list):
            return "watch_list.json は配列である必要があります。"
        for item in data:
            if "title" not in item or "ep_num" not in item:
                return f"watch_list.json の要素に必要なキー (title, ep_num) が不足しています: {item}"
        return None
        
    if filename == "daily_schedule.json":
        if not isinstance(data, list):
            return "daily_schedule.json は配列である必要があります。"
        for item in data:
            if "anime_id" not in item or "start_time" not in item:
                return f"daily_schedule.json の要素に必要なキー (anime_id, start_time) が不足しています: {item}"
        return None

    if filename.endswith("_master.json"):
        if "anime_id" not in data or "title" not in data:
             return f"{filename} に必要なキー (anime_id, title) が不足しています。"
    
    if filename.endswith("_episode.json"):
        if "anime_id" not in data or "ep_num" not in data:
             return f"{filename} に必要なキー (anime_id, ep_num) が不足しています。"
             
    if filename.endswith("_broadcast.json"):
        if "anime_id" not in data or "start_time" not in data:
             return f"{filename} に必要なキー (anime_id, start_time) が不足しています。"
             
    return None

if __name__ == "__main__":
    current_dir = Path("current")
    if not current_dir.exists():
        print("current directory not found.")
        exit(0)

    json_files = list(current_dir.glob("*.json"))
    has_error = False
    
    print(f"Validating {len(json_files)} JSON files in 'current/'...")
    
    for file_path in json_files:
        data, error = validate_json_file(file_path)
        if error:
            print(f"❌ {file_path.name}: {error}")
            has_error = True
            continue
            
        schema_error = validate_schema(file_path, data)
        if schema_error:
            print(f"❌ {file_path.name}: {schema_error}")
            has_error = True
        else:
            print(f"✅ {file_path.name}: OK")
            
    if has_error:
        print("\\n❌ 1つ以上のファイルで検証エラーが発生しました。")
        exit(1)
    else:
        print("\\n🎉 全てのJSONファイルが正常です。")
        exit(0)
