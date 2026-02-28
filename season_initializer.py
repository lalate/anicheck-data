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
あなたは日本のアニメ放送情報に精通したアシスタントです。

# 目的
指定されたシーズン（例：2026年春）に日本で放送される主要な深夜アニメのタイトルを収集し、監視用のJSONリストとして出力してください。

# 条件
- 知名度や期待度の高い主要な深夜アニメを10〜15作品程度ピックアップしてください。
- 各作品の話数（ep_num）は、新シーズンの始まりなので全て `1` に設定してください。
- 継続放送の作品（2クール目など）が含まれる場合は、その時点での最新予想話数、分からなければ適当な継続話数（例: 13など）にしても構いませんが、基本は新作の `1` を優先してください。
- 出力は必ず以下のJSON形式のみとし、Markdownのコードブロック（```json ... ```）で囲んでください。余計な解説は不要です。

# 出力形式
```json
[
  {
    "title": "作品名1",
    "ep_num": 1
  },
  {
    "title": "作品名2",
    "ep_num": 1
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
    # JSONブロック（```json ... ```）を抽出
    json_blocks = re.findall(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
    
    if not json_blocks:
        # フォールバック: []で囲まれた部分を探す
        json_blocks = re.findall(r'(\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*\])', text, re.DOTALL)
        if not json_blocks:
            print("❌ エラー: Grokの応答からJSONリストを抽出できませんでした。")
            print("--- 生の応答 ---")
            print(text)
            return False

    try:
        anime_list = json.loads(json_blocks[0])
        
        # 簡易バリデーション
        if not isinstance(anime_list, list) or len(anime_list) == 0:
             print("❌ エラー: 抽出されたJSONが空のリスト、またはリスト形式ではありません。")
             return False
             
        if "title" not in anime_list[0] or "ep_num" not in anime_list[0]:
             print("❌ エラー: JSONの構造が期待される形式（title, ep_num）と異なります。")
             return False

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(anime_list, f, ensure_ascii=False, indent=2)
            
        print(f"✅ 成功: {len(anime_list)}件のアニメを {output_file.name} に保存しました！")
        for anime in anime_list:
            print(f"  - {anime.get('title')} (第{anime.get('ep_num')}話)")
        return True
        
    except json.JSONDecodeError as e:
        print(f"❌ JSONパースエラー: {e}")
        return False

if __name__ == "__main__":
    # 現在の月を元に、直近のシーズンを自動判定するか、手動で指定する
    # ここでは2026年4月（春アニメ）をターゲットとする
    # today = datetime.date.today()
    # year = today.year
    # month = today.month
    # season = "春" if 3 <= month <= 5 else "夏" if 6 <= month <= 8 else "秋" if 9 <= month <= 11 else "冬"
    
    # ユーザーが指定しやすいように変数化
    TARGET_SEASON = "2026年春（4月期）"
    
    raw_text = fetch_season_anime(TARGET_SEASON)
    
    watch_list_path = Path("watch_list.json")
    
    # バックアップを取る
    if watch_list_path.exists():
        backup_path = Path("watch_list_backup.json")
        watch_list_path.rename(backup_path)
        print(f"ℹ️ 既存のリストを {backup_path.name} にバックアップしました。")
        
    success = parse_and_save(raw_text, watch_list_path)
    
    if not success and Path("watch_list_backup.json").exists():
        print("⚠️ 失敗したため、バックアップからリストを復元します。")
        Path("watch_list_backup.json").rename(watch_list_path)
