# -*- coding: utf-8 -*-
import os
import json
import re
import datetime
import logging
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# =================================================================
# ロギング設定
# =================================================================
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "daily_fetch.log"

# logging.basicConfigを一度だけ呼び出す
# 既にハンドラが設定されているか確認
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

# =================================================================

SYSTEM_PROMPT = """# 役割
あなたはアニメ番組表「アニちぇっく」の正確なデータ作成を行う専属エディターです。

# 目的
指定された作品の最新話情報を、アプリ用JSONと、検証用のソースURLのセットで出力してください。

# 厳格な制約（ハルシネーション防止）
- 必ず提供された情報（公式サイト、スケジュール、検索結果）のみに基づき抽出してください。推測や外部知識の付加は厳禁です。信頼できないソース（例: ファンWikiや非公式ブログ）は使用せず、公式サイトや放送局の情報に限定してください。
- 不明な情報がある場合は、無理に捏造せず、null または空文字を設定し、ソース確認の備考欄にその旨を記載してください。検索で情報が見つからない場合も、全てnullで備考に「情報未確認」と記述してください。

# 処理ステップ（Chain of Thought）
ステップ1: 入力された作品名、話数、参考URL、基本スケジュールを分析し、anime_idをYYYYMM_title_c2形式で生成・確認してください。
ステップ2: Web検索（live_search）を用いて、対象話数の正確な「サブタイトル」「あらすじ（前回のあらすじとして3行以内に要約）」「特番による時間変更/休止の有無（statusに反映）」「関連グッズ（sources.goodsに追加）」「原作の該当巻数（original_vol）」「公式ハッシュタグ（hashtag）」「YouTube予告ID（next_preview_youtube_id）」を調査・抽出してください。各項目をJSONフィールドにマッピングしてください。
ステップ3: 抽出した情報を元に、指定された3つのJSONブロックを構築してください。JSONは有効な形式で、インデントを統一してください。

# 出力形式
以下の3つのJSONブロックを、独立したコードブロック（例: ```json ... ```）として出力し、その後に【ソース確認】セクションをテキストで追加してください。
【重要】JSONブロックの外に、挨拶、ユーモア、追加の解説などは絶対に含めないでください。JSONパースエラーの原因となります。純粋なデータのみを出力してください。

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
      {"type": "Blu-ray", "name": "第1巻", "url": "商品URL"}
    ]
  }
}
```

## 2. Episode_Content
```json
{
  "anime_id": "YYYYMM_title_c2",
  "ep_num": 5,
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
  "ep_num": 5,
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
- 備考: (放送休止や時間変更がある場合はここに記述、不明情報も記載)"""

def call_grok_for_anime(title: str, ep_num: int, official_url: str = None, schedules: list = None):
    url_hint = f"\\n公式サイトURL（参考）：{official_url}" if official_url else ""
    
    schedule_hint = ""
    if schedules:
        schedule_str = ", ".join([f"{s.get('station', '')} ({s.get('day_of_week', '')} {s.get('time', '')})" for s in schedules])
        schedule_hint = f"\\n基本放送スケジュール：{schedule_str}"

    user_input = f"作品名：{title}\\n話数：{ep_num}{url_hint}{schedule_hint}"
    
    # 嘘（ハルシネーション）を強力に抑制し、基本スケジュールを元に最新情報を確認するよう指示
    prompt_with_strictness = SYSTEM_PROMPT + "\\n\\n【重要：事実確認とポロロッカ戦略】\\n1. 必ず提供された「基本放送スケジュール」をベースにしつつ、Web上の最新情報(live_search)で「特番による時間変更や休止」がないかを確認してください。変更があれば最新の時間を、なければ基本放送時間を出力してください。\\n2. 放送局（station_id）は基本スケジュールにあるものから、最も早い放送時間または主要な放送枠を1つ選んで出力してください。\\n3. **`hashtag` は必ず公式のものを調べて設定してください。**\\n4. **`next_preview_youtube_id` は必ず設定してください。** 最新話の予告動画IDがベストですが、見つからない場合は「作品の公式PV」や「チャンネルの最新動画」のIDでも構いません。空欄（null）は避けてください。\\n5. 対象話数が、原作コミックやライトノベルの「第何巻」に相当するかを推測または検索し、`original_vol` に整数で設定してください。また、その作品のAmazon検索URLを `manga_amazon` に設定してください。\\n6. 公式サイト等から、現在予約・販売中の主要なグッズ（Blu-ray/DVD、フィギュア、書籍等）を最大5件抽出し、`goods` リストに設定してください。URLは可能な限りAmazon等のアフィリエイトに繋げやすい直リンク、または公式サイトの紹介ページにしてください。\\n7. 架空のデータやURLを捏造することは厳禁です。不明な場合は null または空のリストにしてください。（YouTube IDを除く）"

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
        json_blocks = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', text, flags=re.DOTALL)
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
        logging.error(f"JSONデコードエラー: {e}")
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
        logging.critical(f"❌ Error: {watch_list_file} が見つかりません。処理を中断します。")
        exit(1)

    all_broadcasts = []
    processed_count = 0
    total_count = len(ANIMES_TO_CHECK)

    logging.info(f"🚀 {today} アニちぇっく データ取得開始... ({total_count}件)")

    for i, anime in enumerate(ANIMES_TO_CHECK):
        title = anime.get('title', '不明なタイトル')
        ep_num = anime.get('ep_num', 'N/A')
        
        logging.info(f"[{i+1}/{total_count}] 処理中: {title} 第{ep_num}話")

        try:
            official_url = anime.get('official_url')
            schedules = anime.get('schedules', [])
            
            raw_text = call_grok_for_anime(title, ep_num, official_url, schedules)
            
            data = parse_output(raw_text, title, ep_num)
            
            if data and "anime_id" in data.get("master", {}):
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
                
                logging.info(f"  ✅ {anime_id} 正常に処理完了。次回話数をインクリメントします。")
                # 成功したので次回用に話数をインクリメント
                anime["ep_num"] += 1
                processed_count += 1
            else:
                logging.error(f"  ❌ パース失敗: {title} 第{ep_num}話")
                # パース失敗の詳細をログに記録
                logging.debug(f"RAW Response for {title}:\\n---\\n{raw_text}\\n---")

        except Exception as e:
            logging.error(f"  🔥 予期せぬエラー発生: {title} 第{ep_num}話 - {e}", exc_info=True)


    # すべての処理が終わった後で、変更を反映したwatch_listを保存
    if processed_count > 0:
        # 時間順にソート
        all_broadcasts.sort(key=lambda x: x.get("start_time") or "")
        
        # その日の全番組表
        (output_dir / "daily_schedule.json").write_text(
            json.dumps(all_broadcasts, ensure_ascii=False, indent=2), encoding="utf-8")
            
        # 更新された監視リストを保存
        with open(watch_list_file, "w", encoding="utf-8") as f:
            json.dump(ANIMES_TO_CHECK, f, ensure_ascii=False, indent=2)
            
        logging.info("📝 watch_list.json と daily_schedule.json を更新しました。")

    logging.info(f"\\n🎉 {today} の処理完了！ ({processed_count}/{total_count}件成功)")
    logging.info(f"  - ログ: {log_file.absolute()}")
    logging.info(f"  - アプリ用データ: current/daily_schedule.json")
