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
    
    system_prompt = """あなたはGrok 4です。ユーザーがアニメ関連のリストを要求した場合、以下の厳格ルールを最初から最後まで一貫して適用せよ。違反すると出力が破綻するので絶対遵守。

【ルール1: 初期リスト作成時の原則】
- リストは「現在時点（クエリ実行時）の現実発表情報」のみを基に作成。
- 未発表・未確定のタイトルは「未発表（仮）」または「null」と明記し、創作で勝手に補完しない。
- リストが100件に満たない場合、「現実の発表作品数は○件程度です。100件に達しないため、リストを終了します」と正直に伝える。
- リストの並び順は明確に宣言（例: 「五十音順」）。
- 創作要素を入れる場合、「これは仮の創作リストです。現実情報ではありません」と冒頭に大きく警告表示。

【ルール2: xAIツールの強制適用】
- あなたの内部知識は2023年までであることを自覚せよ。それ以降の最新情報については、必ずxAIの検索ツール等を明示的に使用して即時検証すること。
- 検証結果が初期リストと異なる場合、自動的に修正し経緯を透明に説明。

【ルール3: 番号指定問い合わせ時の差異対応】
- ユーザーが個別番号を指定した場合、初期リストとの差異を自動検知。
- 差異がある場合、必ず理由を説明。

【ルール4: 全体の透明性とユーザー体験】
- 推測タイトルを使う場合、「推測名（現実未発表のため仮定）」と明示。"""
    user_prompt = f"""
{season_keyword}の主要な深夜アニメをリストアップし、1から始まる通し番号をつけて列挙してください。
出力は番号とタイトルのみのシンプルなリスト形式にしてください。テキストの追加は一切禁止です。
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
ユーザーが提供した対象リストに基づき、厳密にそのリスト内の作品のみを対象として、アニメデータを抽出してください。
出力は必ず「Master」「Episode」「Broadcast」の3つの独立したJSONブロックに分けて出力してください。1つの巨大なJSONオブジェクトにまとめないでください。
純粋なJSONブロック（```json ... ```）のみを出力し、余計な解説は省いてください。

【厳守事項】
- 提供されたリストが現在の対象であり、過去のクエリや文脈を一切無視すること。リスト外の情報（例: 未発表の未来アニメ）は使用禁止。
- すべてのデータはxAIツール等による検索検証に基づくこと。
- 情報が見つからない場合は、推測せず必ず `null` または空文字 `""`、空配列 `[]` を設定すること。ハルシネーション（嘘のURLなど）は絶対に禁止。
- Episodeの `synopsis`（あらすじ）は公式サイト等からの抜粋のみとし、創作は禁止。見つからない場合は `null` にすること。

【出力フォーマット例】
```json
[
  {
    "anime_id": "202401_frieren",
    "title": "葬送のフリーレン",
    "official_url": "https://frieren-anime.jp/",
    "hashtag": "#フリーレン",
    "station_master": "TOKYO MX",
    "cast": ["種崎敦美"],
    "staff": {"director": "斎藤圭一郎"}
  }
]
```
```json
[
  {
    "anime_id": "202401_frieren",
    "ep_num": 1,
    "sub_title": "冒険の終わり",
    "synopsis": "魔王を倒した勇者一行の後日譚..."
  }
]
```
```json
[
  {
    "anime_id": "202401_frieren",
    "station_id": "TOKYO MX",
    "start_time": "2024-01-06T00:29:00+09:00",
    "day_of_week": "土"
  }
]
```
"""

    user_prompt = f"""
このクエリは完全に独立したものであり、過去の会話文脈（例: 2026年春アニメなど）を一切考慮せず、以下のリストのみに基づいてください。リストは{season_keyword}の作品を示しており、ツールを使って最新情報を検索・検証せよ。

以下の通し番号付きリストの中から、番号 {start_idx} から {end_idx} までの作品について、詳細情報を抽出して指定のJSON形式で出力してください。

【対象リスト】
{index_text}

【要求】
上記リストの {start_idx}番 から {end_idx}番 までの作品について、「公式URL」「公式ハッシュタグ」「主要放送局の基本放送時間」を検索等で特定し、Master, Episode, Broadcast の3つの独立したJSONブロック（```json ... ```）を順番に出力してください。1つのオブジェクトにまとめないでください。

注意: 上記のリストが唯一の対象。リスト内の作品が実在の{season_keyword}であることを確認し、未発表情報や過去キャッシュを使用せず、xAIツールでリアルタイム検索を実行せよ。
"""
    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error fetching batch {start_idx}-{end_idx}: {e}")
        return ""

if __name__ == "__main__":
    season = "2024年冬アニメ"
    
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
    
    for i in range(1, total_items + 1, batch_size):
        start_idx = i
        end_idx = min(i + batch_size - 1, total_items)
        
        batch_output = fetch_anime_details_batch(season, index_text, start_idx, end_idx)
        all_json_outputs.append(f"<!-- Batch {start_idx}-{end_idx} -->\n" + batch_output)
        
    # 3. 結合して保存
    final_output = "\n\n".join(all_json_outputs)
    
    # スクリプトのディレクトリを基準に保存先を決定
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_filename = os.path.join(script_dir, "raw_grok_output_batched.txt")
    
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(final_output)
    
    print(f"\n✅ 全バッチの処理が完了し '{output_filename}' に保存しました。")