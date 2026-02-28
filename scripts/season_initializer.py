# -*- coding: utf-8 -*-
import os
import json
import re
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv
import datetime

load_dotenv()

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

SYSTEM_PROMPT = """# 役割
あなたは日本のアニメ放送情報に精通した、嘘を許さない厳格な調査員です。

# 目的
指定されたシーズン（例：2026年春）に日本で放送される主要な深夜アニメのタイトル、公式サイトURL、および各放送局の「基本放送スケジュール」を収集し、監視用のJSONリストを出力してください。

# 条件
- 知名度や期待度の高い主要な深夜アニメを10〜15作品程度ピックアップしてください。
- 各作品の「公式サイトURL」を必ず調査し、正確なURLを記載してください。
- 各作品の「主要な放送局・配信サイトの基本スケジュール」を調査し、以下の形式で `schedules` 配列として出力してください。
  - `station`: 放送局ID（例: mx, bs11, tx, ntv, mbs, abema など小文字英数）
  - `day_of_week`: 放送曜日（例: 月曜日, 火曜日）
  - `time`: 基本の放送開始時間（例: 24:00, 25:30）
- 各作品の話数（ep_num）は、新シーズンの始まりなので全て `1` に設定してください。
- 出力は必ず以下のJSON形式のみとし、Markdownのコードブロック（```json ... ```）で囲んでください。余計な解説は不要です。

# 出力形式
```json
[
  {
    "title": "作品名1",
    "official_url": "https://example.com/anime1",
    "ep_num": 1,
    "schedules": [
      {"station": "mx", "day_of_week": "水曜日", "time": "24:00"},
      {"station": "bs11", "day_of_week": "木曜日", "time": "25:00"}
    ]
  }
]
```"""

def fetch_season_anime(season_str: str):
    user_input = f"対象シーズン：{season_str}\\nこのシーズンに放送開始または放送中の主要なアニメをリストアップしてください。"
    
    print(f"🚀 Grokに {season_str} のアニメリストを問い合わせ中...")
    response = client.chat.completions.create(
        model="grok-4-1-fast-reasoning",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input}
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    return response.choices[0].message.content

def parse_and_save(text: str, output_file: Path):
    json_blocks = re.findall(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
    
    if not json_blocks:
        json_blocks = re.findall(r'(\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*\])', text, re.DOTALL)
        if not json_blocks:
            print("❌ エラー: Grokの応答からJSONリストを抽出できませんでした。")
            return False

    try:
        anime_list = json.loads(json_blocks[0])
        
        if not isinstance(anime_list, list) or len(anime_list) == 0:
             return False
             
        if "title" not in anime_list[0] or "ep_num" not in anime_list[0]:
             return False

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(anime_list, f, ensure_ascii=False, indent=2)
            
        print(f"✅ 成功: {len(anime_list)}件のアニメを {output_file.name} に保存しました！")
        return True
        
    except json.JSONDecodeError as e:
        print(f"❌ JSONパースエラー: {e}")
        return False

def archive_current_list(current_list_path: Path, archive_dir: Path):
    """
    現在の watch_list.json を解析し、適切なシーズン名でアーカイブに保存する。
    """
    if not current_list_path.exists():
        return

    try:
        with open(current_list_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # データの最初のアニメからシーズンを推測（または現在日付から）
        # ここではシンプルに「アーカイブ実行時の日付」をベースにする
        now = datetime.datetime.now()
        year = now.year
        month = now.month
        season = "winter" if month in [1, 2, 3] else "spring" if month in [4, 5, 6] else "summer" if month in [7, 8, 9] else "autumn"
        
        archive_name = f"{year}_{season}_list.json"
        archive_path = archive_dir / archive_name
        
        # すでに存在する場合は連番を振る
        counter = 1
        while archive_path.exists():
            archive_name = f"{year}_{season}_list_{counter}.json"
            archive_path = archive_dir / archive_name
            counter += 1
            
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"📦 アーカイブ完了: 現在のリストを {archive_path.name} に保存しました。")
        
        # 元のファイルを削除（後で新しいものが作られるため）
        current_list_path.unlink()
        
    except Exception as e:
        print(f"⚠️ アーカイブ中にエラーが発生しました: {e}")

if __name__ == "__main__":
    # ターゲットシーズンの指定
    TARGET_SEASON = "2025年冬（1月期）または最新の確定情報"
    
    watch_list_path = Path("current/watch_list.json")
    archive_dir = Path("archive")
    archive_dir.mkdir(exist_ok=True)

    # 1. 現在のリストをアーカイブへ「昇華」させる
    archive_current_list(watch_list_path, archive_dir)
    
    # 2. 新しいシーズンのリストを取得
    raw_text = fetch_season_anime(TARGET_SEASON)
    
    # 3. 新しいリストを保存
    success = parse_and_save(raw_text, watch_list_path)
    
    if success:
        print(f"✨ 新シーズン {TARGET_SEASON} の準備が整いました。")
    else:
        print("❌ 新シーズンの取得に失敗しました。")
