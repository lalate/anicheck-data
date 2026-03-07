import os
import json
import re
from openai import OpenAI
from dotenv import load_dotenv

# .envファイルからAPIキーを読み込む
load_dotenv()

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

def fetch_anime_list_index(season_keyword="2026年冬アニメ"):
    print(f"🚀 Grokに「{season_keyword}」の全体リスト（通し番号付き）を問い合わせています...")
    
    system_prompt = "あなたはアニメ情報のリサーチャーです。事実に基づき正確なリストを作成してください。"
    user_prompt = f"""
{season_keyword}の主要な深夜アニメをリストアップし、1から始まる通し番号をつけて列挙してください。
出力は番号とタイトルのみのシンプルなリスト形式にしてください。
例:
1. 葬送のフリーレン
2. 薬屋のひとりごと
"""
    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error fetching index: {e}")
        return ""

def fetch_anime_details_batch(season_keyword, index_text, start_idx, end_idx):
    print(f"📦 Grokに番号 {start_idx}〜{end_idx} の詳細データを要求しています...")
    
    system_prompt = """
あなたはアニメデータ生成のスペシャリストです。
ユーザーの要求に基づき、アニメデータを以下の3層構造（Master, Episode, Broadcast）のJSON形式で出力してください。
純粋なJSONブロック（```json ... ```）のみを出力し、余計な解説は省いてください。

【データ構造の定義】
1. Master (作品基本情報):
   {"anime_id": "一意のID", "title": "作品名", "official_url": "公式サイトURL", "hashtag": "公式ハッシュタグ", "station_master": "主要放送局", "cast": ["声優1"], "staff": {"director": "監督"}}
2. Episode (話数情報):
   {"anime_id": "Masterと同じID", "ep_num": 1, "sub_title": "", "synopsis": ""}
3. Broadcast (放送枠情報):
   {"anime_id": "Masterと同じID", "station_id": "放送局ID", "start_time": "2026-01-01T24:00:00+09:00", "day_of_week": "曜日"}
"""

    user_prompt = f"""
以下の通し番号付きリストの中から、番号 {start_idx} から {end_idx} までの作品について、詳細情報を抽出して指定のJSON形式で出力してください。

【対象リスト】
{index_text}

【要求】
上記リストの {start_idx}番 から {end_idx}番 までの作品について、「公式URL」「公式ハッシュタグ」「主要放送局の基本放送時間」を特定し、Master, Episode, Broadcast の3つのJSONブロックを出力してください。
"""
    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error fetching batch {start_idx}-{end_idx}: {e}")
        return ""

if __name__ == "__main__":
    season = "2026年冬アニメ"
    
    # 1. 全体リスト（インデックス）を取得
    index_text = fetch_anime_list_index(season)
    print("\n=== 取得したリスト ===\n" + index_text + "\n====================\n")
    
    # リストの行数から概算の件数を出す（簡易的）
    lines = [line for line in index_text.split('\n') if re.match(r'^\d+\.', line.strip())]
    total_items = len(lines)
    
    if total_items == 0:
        print("リストの取得に失敗しました。")
        exit(1)
        
    print(f"合計 {total_items} 件のアニメを検出。バッチ処理を開始します。")
    
    all_json_outputs = []
    batch_size = 10
    
    # 2. 指定件数ごとにバッチ処理（テストのため最初の2バッチ程度に制限してもよい）
    for i in range(1, total_items + 1, batch_size):
        start_idx = i
        end_idx = min(i + batch_size - 1, total_items)
        
        batch_output = fetch_anime_details_batch(season, index_text, start_idx, end_idx)
        all_json_outputs.append(f"<!-- Batch {start_idx}-{end_idx} -->\n" + batch_output)
        
    # 3. 結合して保存
    final_output = "\n\n".join(all_json_outputs)
    output_filename = "raw_grok_output_batched.txt"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(final_output)
    
    print(f"\n✅ 全バッチの処理が完了し '{output_filename}' に保存しました。")