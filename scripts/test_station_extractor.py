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

def test_extract_stations(title: str, official_url: str):
    user_input = f"作品名：{title}\\n公式サイトURL：{official_url}\\n\\nこのアニメの主要な放送局（例：TOKYO MX、MBS、BS11、AbemaTVなど）をリストアップしてください。配信サイトも含めて構いません。JSONの配列形式（文字列のリスト）で出力してください。"
    
    response = client.chat.completions.create(
        model="grok-4-1-fast-reasoning",
        messages=[
            {"role": "system", "content": "あなたはアニメ情報の抽出に特化したAIです。指定された作品の放送局・配信サイトを正確に抽出し、JSONの文字列配列のみを出力してください。Markdownのコードブロックは使用しても構いません。"},
            {"role": "user", "content": user_input}
        ],
        temperature=0.1,
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    print("🧪 テスト1: 薬屋のひとりごと")
    print(test_extract_stations("薬屋のひとりごと", "https://kusuriyanohitorigoto.jp/"))
    
    print("\\n🧪 テスト2: ダンダダン")
    print(test_extract_stations("ダンダダン", "https://anime-dandadan.com/"))
