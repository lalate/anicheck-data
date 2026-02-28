import os
import datetime
from openai import OpenAI
from dotenv import load_dotenv

# .envファイルからAPIキーを読み込む
load_dotenv()

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

def fetch_anime_schedule():
    # 日付範囲の設定（今日から3日間）
    today = datetime.date.today()
    target_dates = [today + datetime.timedelta(days=i) for i in range(3)]
    date_str = "、".join([d.strftime("%Y年%m月%d日") for d in target_dates])
    
    print(f"🚀 Grokに {date_str} のアニメ放送予定を問い合わせています...")

    # システムプロンプト: 役割と出力フォーマットの定義
    system_prompt = """
あなたはアニメデータ生成のスペシャリストです。
ユーザーの要求に基づき、アニメ放送データを以下の3つのJSON構造（Master, Episode, Broadcast）で出力してください。
各データはMarkdownのコードブロック（```json ... ```）内に記述してください。

【データ構造の定義】
1. Master (作品基本情報):
   {"anime_id": "一意のID", "title": "作品名", "official_url": "公式サイト", "cast": ["声優1"], "staff": {"director": "監督"}}
2. Episode (話数情報):
   {"anime_id": "Masterと同じID", "ep_num": 話数(int), "sub_title": "サブタイトル", "synopsis": "あらすじ"}
3. Broadcast (放送枠情報):
   {"anime_id": "Masterと同じID", "station_id": "放送局ID", "start_time": "ISO8601形式の日時", "day_of_week": "曜日"}
"""

    # ユーザープロンプト: 具体的な期間と内容の指示
    user_prompt = f"""
今日（{today.strftime("%Y-%m-%d")}）から向こう3日間に日本で放送される、主な深夜アニメの放送予定を教えてください。
特に人気のある作品をいくつかピックアップし、上記の「Master」「Episode」「Broadcast」の3層構造のJSON形式で出力してください。
データ間の `anime_id` は必ず一致させてください。
"""

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",  # 安くて速いモデル
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3, # データ生成なので創造性より正確性を重視
        )
        
        return response.choices[0].message.content.strip()

    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    # 1. Grokからデータを取得
    grok_output = fetch_anime_schedule()
    
    # 2. 結果をファイルに保存（anime_factory.py の入力となる）
    output_filename = "raw_grok_output.txt"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(grok_output)
    
    print(f"\n✅ Grokからの応答を '{output_filename}' に保存しました。")
    print("👉 続けて 'python3 anime_factory.py' を実行してJSONに変換してください。")