# -*- coding: utf-8 -*-
import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

def test_extract_times(title: str, official_url: str):
    user_input = f"作品名：{title}\\n公式サイトURL：{official_url}\\n\\nこのアニメの各放送局および配信サイトにおける「基本放送時間（開始時間と曜日）」をすべてリストアップしてください。出力は以下のJSON配列形式のみとしてください。\\n\\n[{{\"station\": \"TOKYO MX\", \"day\": \"木曜日\", \"time\": \"24:00\"}}]"
    
    response = client.chat.completions.create(
        model="grok-4-1-fast-reasoning",
        messages=[
            {"role": "system", "content": "あなたはアニメ情報の抽出に特化したAIです。指定された作品の各放送局の放送時間を正確に抽出し、指定されたJSON配列形式のみを出力してください。"},
            {"role": "user", "content": user_input}
        ],
        temperature=0.1,
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    print("🧪 テスト: 薬屋のひとりごと 第2期")
    print(test_extract_times("薬屋のひとりごと 第2期", "https://kusuriyanime.jp/2nd/"))
