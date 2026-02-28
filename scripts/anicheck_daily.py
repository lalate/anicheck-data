# -*- coding: utf-8 -*-
import os
import json
import re
import datetime
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

# =================================================================

SYSTEM_PROMPT = """# 役割

あなたはアニメ番組表「アニちぇっく」の正確なデータ作成を行う専属エディターです。

# 目的

指定された作品の最新話情報を、アプリ用JSONと、検証用のソースURLのセットで出力してください。

# 入力

作品名：[作品名]
話数：[話数]

# 出力形式

以下の3つのJSONブロックと、その後に【ソース確認】セクションを出力してください。解説は不要です。

## 1. Master_data

```json
{
  "anime_id": "YYYYMM_title_c2",
  "title": "作品名",
  "official_url": "公式サイトURL",
  "hashtag": "公式ハッシュタグ",
  "station_master": "主要放送局名",
  "cast": ["主要声優1", "主要声優2"],
  "staff": { "director": "監督名", "studio": "制作会社" }
}
```

## 2. Episode_Content

```json
{
  "anime_id": "YYYYMM_title_c2",
  "ep_num": [話数],
  "title": "サブタイトル",
  "prev_summary": "視聴直前用の前回のあらすじ(3行)",
  "next_preview_youtube_id": "公式予告動画ID"
}
```

## 3. Broadcast_Schedule

```json
{
  "anime_id": "YYYYMM_title_c2",
  "ep_num": [話数],
  "station_id": "ntv",
  "start_time": "YYYY-MM-DDTHH:MM:00+09:00",
  "status": "normal"
}
```

## anime_idについて

- YYYYMM:放送開始年月
- title:アニメが判別出来る10文字までの英数字
- c2:第一期ならc1、二期ならc2

【ソース確認】
- 公式サイト確認用URL:
- 放送スケジュール根拠URL:
- 備考: (放送休止や時間変更がある場合はここに記述)"""

def call_grok_for_anime(title: str, ep_num: int, official_url: str = None):
    url_hint = f"\\n公式サイトURL（参考）：{official_url}" if official_url else ""
    user_input = f"作品名：{title}\\n話数：{ep_num}{url_hint}"
    
    # 嘘（ハルシネーション）を強力に抑制するシステムメッセージの追加
    prompt_with_strictness = SYSTEM_PROMPT + "\\n\\n【重要：事実確認の徹底】\\n必ず提供された公式サイトURLやWeb上の最新情報を確認し、架空のサブタイトルや放送時間を捏造しないでください。不明な場合は捏造せず、ソース確認の備考欄にその旨を記述してください。"

    response = client.chat.completions.create(
        model="grok-4-1-fast-reasoning", # ツール対応・高速・安い
        messages=[
            {"role": "system", "content": prompt_with_strictness},
            {"role": "user", "content": user_input}
        ],
        # tools=[{"type": "live_search"}], # ← これでリアルタイム検索が有効
        temperature=0.1, # 創造性を抑えて事実に基づかせる
        max_tokens=1500,
    )
    return response.choices[0].message.content

def parse_output(text: str, title: str, ep_num: int):
    # JSONブロック（```json ... ```）をすべて抽出する
    json_blocks = re.findall(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    
    if len(json_blocks) < 3:
        # ヘッダーがない場合のフォールバックとして波括弧のブロックを探す
        json_blocks = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', text, some_text = text, flags=re.DOTALL)
        # 上記の正規表現を修正
        json_blocks = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', text, re.DOTALL)
        if len(json_blocks) < 3:
            return None # パース失敗

    try:
        master = json.loads(json_blocks[0])
        episode = json.loads(json_blocks[1])
        broadcast = json.loads(json_blocks[2])
        
        # 配列に入ってしまっている可能性があるフィールドを修正
        if isinstance(episode.get("ep_num"), list) and len(episode["ep_num"]) > 0:
            episode["ep_num"] = episode["ep_num"][0]
        if isinstance(broadcast.get("ep_num"), list) and len(broadcast["ep_num"]) > 0:
            broadcast["ep_num"] = broadcast["ep_num"][0]
            
    except json.JSONDecodeError as e:
        print(f"JSON Decode Error: {e}")
        return None

    # ソース確認部分
    source_section = re.search(r'【ソース確認】(.*)', text, re.DOTALL)
    sources = source_section.group(1).strip() if source_section else "取得失敗"

    return {
        "master": master,
        "episode": episode,
        "broadcast": broadcast,
        "sources": sources
    }

# ====================== メイン実行 ======================
if __name__ == "__main__":
    today = datetime.date.today().strftime("%Y-%m-%d")
    output_dir = Path("current")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    watch_list_file = Path("current/watch_list.json")
    if watch_list_file.exists():
        with open(watch_list_file, "r", encoding="utf-8") as f:
            ANIMES_TO_CHECK = json.load(f)
    else:
        print(f"❌ Error: {watch_list_file} が見つかりません。")
        exit(1)

    all_broadcasts = []

    print(f"🚀 {today} アニちぇっく データ取得開始...")

    for anime in ANIMES_TO_CHECK:
        title = anime['title']
        ep_num = anime['ep_num']
        official_url = anime.get('official_url')
        
        print(f"  📺 {title} 第{ep_num}話 取得中...")
        raw_text = call_grok_for_anime(title, ep_num, official_url)
        
        data = parse_output(raw_text, title, ep_num)
        
        if data:
            anime_id = data["master"]["anime_id"]
            
            # 個別保存
            (output_dir / f"{anime_id}_master.json").write_text(
                json.dumps(data["master"], ensure_ascii=False, indent=2), encoding="utf-8")
            (output_dir / f"{anime_id}_episode.json").write_text(
                json.dumps(data["episode"], ensure_ascii=False, indent=2), encoding="utf-8")
            (output_dir / f"{anime_id}_broadcast.json").write_text(
                json.dumps(data["broadcast"], ensure_ascii=False, indent=2), encoding="utf-8")
                
            all_broadcasts.append(data["broadcast"])
            
            # ソースログ
            (output_dir / f"{anime_id}_sources.txt").write_text(data["sources"], encoding="utf-8")
            
            print(f"  ✅ {anime_id} 完了 (次回取得話を自動更新します)")
            # 成功したので次回用に話数をインクリメント
            anime["ep_num"] += 1
        else:
            print(f"  ❌ パース失敗: {title}")

    # その日の全番組表（時間順）
    all_broadcasts.sort(key=lambda x: x["start_time"])
    (output_dir / "daily_schedule.json").write_text(
        json.dumps(all_broadcasts, ensure_ascii=False, indent=2), encoding="utf-8")
        
    # 更新された監視リストを保存
    with open(watch_list_file, "w", encoding="utf-8") as f:
        json.dump(ANIMES_TO_CHECK, f, ensure_ascii=False, indent=2)

    print(f"\\n🎉 完了！データは current/ に保存されました")
    print(f"  📱 アプリ用：daily_schedule.json をご利用ください")
    print(f"  📝 watch_list.json も最新話数に自動更新されました。")
