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

def test_extract_goods(title: str, official_url: str):
    user_input = f"作品名：{title}\\n公式サイトURL：{official_url}\\n\\nこのアニメの公式サイトに掲載されている「グッズ情報（Blu-ray/DVD、フィギュア、関連書籍、または公式オンラインショップのURLなど）」を検索して抽出してください。アフィリエイトの導線として使えるような具体的な商品名や、グッズ紹介ページのURLがあればリストアップしてください。出力は以下のJSON配列形式のみとしてください。\\n\\n[{{\"type\": \"Blu-ray\", \"name\": \"第1巻\", \"url\": \"https://...\"}}]"
    
    response = client.chat.completions.create(
        model="grok-4-1-fast-reasoning",
        messages=[
            {"role": "system", "content": "あなたはアニメの収益化・グッズ情報の抽出に特化したAIです。指定された公式サイトを検索し、物販情報・グッズ情報を抽出してJSONで出力してください。"},
            {"role": "user", "content": user_input}
        ],
        temperature=0.1,
        # tools=[{"type": "live_search"}], # GrokにWeb検索を許可して最新のグッズページを見つけさせる
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    print("🧪 テスト: 葬送のフリーレン 第2期 (公式サイトからグッズ情報を探す)")
    print(test_extract_goods("葬送のフリーレン", "https://frieren-anime.com/"))
