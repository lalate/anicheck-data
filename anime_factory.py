import json
import os
import re

# --- 設定: 工場のライン構成 ---
INPUT_FILE = 'grok_anime_2026冬アニメ.txt'
OUTPUT_FILES = {
    'master': 'master.json',
    'episode': 'episode.json',
    'broadcast': 'broadcast.json'
}

# ラッパーキー → デフォルトカテゴリ (None = 中身のフィールドで自動判定)
WRAPPER_HINTS = {
    'works':      None,
    'master':     'master',
    'masters':    'master',
    'episode':    'episode',
    'episodes':   'episode',
    'broadcast':  'broadcast',
    'broadcasts': 'broadcast',
}

def clean_text(text):
    """スマートクォートやマークダウンブロック記号などノイズを除去する。"""
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = re.sub(r'^```\w*\n|```$', '', text.strip(), flags=re.MULTILINE)
    return text.strip()

def parse_json_stream(text):
    """会話テキストに埋もれたJSONオブジェクト/リストを全て抽出して返す。"""
    decoder = json.JSONDecoder()
    pos = 0
    items = []
    while pos < len(text):
        match = re.search(r'[\[\{]', text[pos:])
        if not match:
            break
        start_index = pos + match.start()
        try:
            obj, index = decoder.raw_decode(text[start_index:])
            if isinstance(obj, list):
                items.extend(obj)
            elif isinstance(obj, dict):
                items.append(obj)
            pos = start_index + index
        except json.JSONDecodeError:
            pos = start_index + 1
    return items

def classify_item(item):
    """
    1件のdictをフィールド内容から master / episode / broadcast に分類する。
    判定不能な場合は None を返す。
    """
    keys = set(item.keys())

    # Episode: 話数・あらすじフィールドがある
    if keys & {'ep_num', 'episode_num', 'episode_number', 'synopsis'}:
        return 'episode'

    # Broadcast (IDリンク型): master_id/anime_id + 放送枠情報
    if 'station_id' in keys and 'start_time' in keys:
        return 'broadcast'
    if ('master_id' in keys or 'anime_id' in keys) and ('day' in keys or 'time' in keys):
        return 'broadcast'

    # Broadcast (タイトル直接型): 放送曜日+時間+局名があり、作品マスター的フィールドがない
    is_broadcast_like = (
        ('broadcast_day' in keys or 'day' in keys)
        and 'time' in keys
        and ('station' in keys or 'channel' in keys)
        and 'official_url' not in keys
        and 'start_date' not in keys
    )
    if is_broadcast_like:
        return 'broadcast'

    # Master: 公式URL・放送開始日・制作会社・原作などのフィールドがある
    if keys & {'official_url', 'start_date', 'studio', 'source', 'cast', 'staff'}:
        return 'master'
    if 'title' in keys:
        return 'master'

    return None

def process_top_level(item, classified):
    """
    トップレベルの dict を解析して classified に振り分ける。
    {"works": [...]}, {"master": [...], "broadcast": [...]} などの
    ラッパー構造も正しく解体する。
    """
    keys = set(item.keys())
    wrapper_keys_found = keys & set(WRAPPER_HINTS.keys())

    if wrapper_keys_found:
        for wkey in wrapper_keys_found:
            default_cat = WRAPPER_HINTS[wkey]
            val = item[wkey]
            if not isinstance(val, list):
                continue  # null や非リストは無視
            for sub in val:
                if not isinstance(sub, dict):
                    continue
                # デフォルトカテゴリが None (works など) の場合は中身で判断
                cat = default_cat if default_cat is not None else classify_item(sub)
                if cat is None:
                    cat = 'master'  # フォールバック
                classified[cat].append(sub)
    else:
        cat = classify_item(item)
        if cat:
            classified[cat].append(item)
        else:
            print(f'Warning: 分類不能 -> {sorted(keys)}')

def deduplicate_by_title(records):
    """title をキーに重複排除。フィールド数が多い方（情報量が多い）を優先する。"""
    seen = {}
    no_title = []
    for rec in records:
        t = rec.get('title')
        if t is None:
            no_title.append(rec)
            continue
        if t not in seen or len(rec) > len(seen[t]):
            seen[t] = rec
    return list(seen.values()) + no_title

def deduplicate_exact(records):
    """完全一致の重複のみ除去する（放送局違いなど有効な重複は残す）。"""
    unique = {}
    for rec in records:
        key = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        unique[key] = rec
    return list(unique.values())

def main():
    print('🏭 Anime Data Factory 稼働開始...')

    if not os.path.exists(INPUT_FILE):
        print(f'❌ Error: 入力ファイル \'{INPUT_FILE}\' が見つかりません。')
        return

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        raw_content = f.read()

    cleaned_content = clean_text(raw_content)
    items = parse_json_stream(cleaned_content)

    classified = {'master': [], 'episode': [], 'broadcast': []}
    for item in items:
        process_top_level(item, classified)

    # 重複排除
    classified['master']    = deduplicate_by_title(classified['master'])
    classified['broadcast'] = deduplicate_exact(classified['broadcast'])
    classified['episode']   = deduplicate_exact(classified['episode'])

    for category, data in classified.items():
        filename = OUTPUT_FILES[category]
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f'✅ {category.upper().ljust(9)}: {len(data):3d} 件 → {filename}')

if __name__ == '__main__':
    main()
