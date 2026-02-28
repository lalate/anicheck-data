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
  "staff": { "director": "監督名", "studio": "制作会社" },
  "sources": {
    "manga_amazon": "原作コミックやライトノベルのAmazon検索URL",
    "goods": [
      {"type": "Blu-ray", "name": "第1巻", "url": "商品URL"},
      {"type": "Figure", "name": "フィギュア名", "url": "商品URL"}
    ]
  }
}
```

## 2. Episode_Content

```json
{
  "anime_id": "YYYYMM_title_c2",
  "ep_num": [話数],
  "title": "サブタイトル",
  "prev_summary": "視聴直前用の前回のあらすじ(3行)",
  "next_preview_youtube_id": "公式予告動画ID",
  "original_vol": 5
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

def call_grok_for_anime(title: str, ep_num: int, official_url: str = None, schedules: list = None):
    url_hint = f"\\n公式サイトURL（参考）：{official_url}" if official_url else ""
    
    schedule_hint = ""
    if schedules:
        schedule_str = ", ".join([f"{s.get('station', '')} ({s.get('day_of_week', '')} {s.get('time', '')})" for s in schedules])
        schedule_hint = f"\\n基本放送スケジュール：{schedule_str}"

    user_input = f"作品名：{title}\\n話数：{ep_num}{url_hint}{schedule_hint}"
    
    # 嘘（ハルシネーション）を強力に抑制し、基本スケジュールを元に最新情報を確認するよう指示
    prompt_with_strictness = SYSTEM_PROMPT + "\\n\\n【重要：事実確認とポロロッカ戦略】\\n1. 必ず提供された「基本放送スケジュール」をベースにしつつ、Web上の最新情報(live_search)で「特番による時間変更や休止」がないかを確認してください。変更があれば最新の時間を、なければ基本放送時間を出力してください。\\n2. 放送局（station_id）は基本スケジュールにあるものから、最も早い放送時間または主要な放送枠を1つ選んで出力してください。\\n3. 対象話数が、原作コミックやライトノベルの「第何巻」に相当するかを推測または検索し、`original_vol` に整数で設定してください。また、その作品のAmazon検索URLを `manga_amazon` に設定してください。\\n4. 公式サイト等から、現在予約・販売中の主要なグッズ（Blu-ray/DVD、フィギュア、書籍等）を最大5件抽出し、`goods` リストに設定してください。URLは可能な限りAmazon等のアフィリエイトに繋げやすい直リンク、または公式サイトの紹介ページにしてください。\\n5. 架空のデータやURLを捏造することは厳禁です。不明な場合は null または空のリストにしてください。"

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
        schedules = anime.get('schedules', [])
        
        print(f"  📺 {title} 第{ep_num}話 取得中...")
        raw_text = call_grok_for_anime(title, ep_num, official_url, schedules)
        
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
