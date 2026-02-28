import json
import os
import re

# --- 設定: 工場のライン構成 ---
INPUT_FILE = 'raw_grok_output.txt'
OUTPUT_FILES = {
    'master': 'master.json',
    'episode': 'episode.json',
    'broadcast': 'broadcast.json'
}

def clean_text(text):
    """
    Grok/LLM特有のノイズを除去し、標準的なJSON形式に近づける前処理。
    """
    # スマートクォート（全角）を半角に置換
    text = text.replace('“', '"').replace('”', '"')
    # Markdownのコードブロック記号を除去
    text = re.sub(r'^```\w*\n|```$', '', text.strip(), flags=re.MULTILINE)
    return text.strip()

def parse_json_stream(text):
    """
    Grokの会話テキストに埋もれたJSONオブジェクトやリストを抽出してパースする。
    """
    decoder = json.JSONDecoder()
    pos = 0
    items = []
    
    while pos < len(text):
        # 次のJSON開始記号（{ または [）を探す
        match = re.search(r'[\[\{]', text[pos:])
        if not match:
            break
        
        start_index = pos + match.start()
        
        try:
            # 見つけた位置からパースを試みる
            obj, index = decoder.raw_decode(text[start_index:])
            
            # リストなら展開、辞書なら追加
            if isinstance(obj, list):
                items.extend(obj)
            elif isinstance(obj, dict):
                items.append(obj)
            
            # 読み終わった位置までポインタを進める
            pos = start_index + index
        except json.JSONDecodeError:
            # パース失敗（ただの括弧だった場合など）は1文字進めて再試行
            pos = start_index + 1
            
    return items

def classify_data(items):
    """
    アイテムの特徴に基づいて3つのカテゴリに自動仕分けする。
    """
    classified = {'master': [], 'episode': [], 'broadcast': []}

    for item in items:
        keys = item.keys()
        
        # Broadcast: 放送局IDと開始時間がある
        if 'station_id' in keys and 'start_time' in keys:
            classified['broadcast'].append(item)
        # Episode: 話数があり、かつ放送枠データではない（あらすじ等がある）
        elif 'ep_num' in keys:
            classified['episode'].append(item)
        # Master: キャスト、スタッフ、公式サイトなどの基本情報がある
        elif 'cast' in keys or 'staff' in keys or 'official_url' in keys:
            classified['master'].append(item)
        else:
            # フォールバック: IDとタイトルだけならMaster扱いとする
            if 'anime_id' in keys and 'title' in keys:
                classified['master'].append(item)
            else:
                print(f"⚠️ Warning: 分類不能なデータ -> {item}")

    return classified

def main():
    print(f"🏭 Anime Data Factory 稼働開始...")
    
    if not os.path.exists(INPUT_FILE):
        print(f"❌ Error: 入力ファイル '{INPUT_FILE}' が見つかりません。")
        return

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        raw_content = f.read()

    cleaned_content = clean_text(raw_content)
    items = parse_json_stream(cleaned_content)
    
    classified = classify_data(items)

    for category, data in classified.items():
        filename = OUTPUT_FILES[category]
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"✅ {category.upper().ljust(9)} : {len(data)} 件を {filename} に保存しました。")

if __name__ == "__main__":
    main()