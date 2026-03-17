import os
import json
import re
import argparse
from dotenv import load_dotenv

# === 必須インストール ===
# pip install xai-sdk python-dotenv

from xai_sdk import Client
from xai_sdk.chat import system, user
from xai_sdk.tools import web_search, x_search

load_dotenv()

# xAI公式SDKを使用（これが最大の変更点）
client = Client(api_key=os.getenv("XAI_API_KEY"))

def fetch_anime_list_index(season_keyword="2026年冬アニメ"):
    print(f"🚀 Grokに「{season_keyword}」の全体リストを問い合わせ中...（Web/X検索自動実行）")
    
    system_prompt = """あなたはGrok 4です。アニメリスト作成時は以下のルールを厳守。
- 必ずweb_searchとx_searchツールを使って最新情報を取得
- 情報が見つからない場合はリストに含めない（空リストでもOK）
- 出力は番号付きリストのみ。余計な説明は一切なし。"""

    user_prompt = f"""
Web検索とX検索をフル活用して「{season_keyword}」（2026年1〜3月放送）のアニメを可能な限り多くリストアップしてください。
公式・予定・観測・予測・噂すべてOK。必ずソース付きで。
現在は2026年3月11日です。放送中/終了済みの情報も含めてOK。

出力形式（これだけ出力）:
1. タイトル1 [確定] (ソース: ...)
2. タイトル2 [予定: 公式サイト] (ソース: ...)
...
"""

    chat = client.chat.create(
        model="grok-4.20-beta-latest-non-reasoning",  # ツール性能が最も高いモデル
        tools=[web_search(), x_search()],              # ← ここが致命的修正（サーバー側自動実行）
        tool_choice="auto"
    )
    chat.append(system(system_prompt))
    chat.append(user(user_prompt))

    response = chat.sample()
    return response.content.strip()


def parse_index_to_list(index_text: str):
    """Grokの出力から番号付きリストを安全にパース"""
    lines = index_text.strip().split("\n")
    anime_list = []
    for line in lines:
        match = re.match(r'^\s*(\d+)\.\s*(.+?)\s*\[(.+?)\](?:\s*\(ソース:\s*(.+?)\))?', line)
        if match:
            num = int(match.group(1))
            title = match.group(2).strip()
            label = match.group(3).strip()
            source = match.group(4).strip() if match.group(4) else ""
            anime_list.append({"num": num, "title": title, "label": label, "source": source})
    return anime_list


def fetch_anime_details_batch(season_keyword, anime_batch):
    print(f"📦 バッチ処理中... {len(anime_batch)}作品の詳細を取得（検索自動実行）")
    
    titles = "\n".join([f"{item['num']}. {item['title']} [{item['label']}]" for item in anime_batch])
    
    system_prompt = """あなたはアニメ専門データ抽出AIです。
提供されたリスト内の作品のみを対象に、Master / Episode / Broadcast の3つのJSONブロックだけを出力してください。
余計な説明・Markdownは一切なし。情報が見つからなければ null にしてください。
xAIツールで必ず最新情報を検証。"""

    user_prompt = f"""
対象作品（{season_keyword}）:
{titles}

上記作品について、公式URL・ハッシュタグ・放送情報などを検索して以下の3ブロックだけ出力してください。
1. Master JSON
2. Episode JSON（各話1件ずつ）
3. Broadcast JSON
"""

    chat = client.chat.create(
        model="grok-4.20-beta-latest-non-reasoning",
        tools=[web_search(), x_search()],
        tool_choice="auto"
    )
    chat.append(system(system_prompt))
    chat.append(user(user_prompt))

    response = chat.sample()
    return response.content.strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--season', type=str, default='2026年冬アニメ')
    args = parser.parse_args()

    # 1. リスト取得（これで25件以上出るはず）
    index_text = fetch_anime_list_index(args.season)
    print("\n=== 取得したリスト ===\n" + index_text + "\n====================\n")

    anime_list = parse_index_to_list(index_text)
    if not anime_list:
        print("❌ リスト取得失敗（検索結果0件）")
        exit(1)

    print(f"✅ {len(anime_list)}作品を検出。詳細取得を開始します...")

    # 2. バッチ処理（10件ずつ）
    batch_size = 10
    all_outputs = []

    for i in range(0, len(anime_list), batch_size):
        batch = anime_list[i:i + batch_size]
        batch_output = fetch_anime_details_batch(args.season, batch)
        all_outputs.append(f"<!-- Batch {i+1}-{min(i+batch_size, len(anime_list))} -->\n{batch_output}")

    # 3. 保存（見やすい形式に）
    final_output = "\n\n".join(all_outputs)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_file = os.path.join(script_dir, f"grok_anime_{args.season.replace('年', '')}.txt")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_output)

    print(f"\n🎉 完了！ファイル保存: {output_file}")
    print(f"   総作品数: {len(anime_list)}件")