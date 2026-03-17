# -*- coding: utf-8 -*-
import os
import json
import re
import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from xai_sdk import Client
from xai_sdk.chat import user, system
from xai_sdk.tools import web_search  # 必須ツール

load_dotenv()  # ローカル開発用。GitHub Actionsでは不要（secretsで直接環境変数）

# =================================================================
# ロギング設定（Actionsのログでも見やすいように）
# =================================================================
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "daily_fetch.log"

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

# xAI SDKクライアント（APIキーは環境変数から自動取得可能）
client = Client()  # XAI_API_KEY が環境変数にある場合、api_key=不要

# =================================================================

SYSTEM_PROMPT = """# 役割
あなたはアニメ番組表「アニちぇっく」の正確なデータ作成を行う専属エディターです。

# 目的
指定された作品の最新話情報を、アプリ用JSONと、検証用のソースURLのセットで出力してください。

# 厳格な制約（ハルシネーション防止）
- 必ずweb_searchツールを使って最新情報を取得・検証せよ。内部知識や推測は一切禁止。
- 公式サイト、放送局公式、信頼できるニュースソース（ANN, Official Twitterなど）のみ使用。
- 対象話数の情報が見つからない場合、直近の放送済み話（前回話など）のあらすじを`prev_summary`として使用し、備考に「対象話数の情報が未公開のため、前回話を代用」と記載してください。
- 不明な情報は null または空文字/空配列にし、【ソース確認】に「情報未確認」と明記。
- 出力は3つのJSONブロック（```json ... ```） + 【ソース確認】セクションのみ。余計なテキスト・挨拶・解説禁止。

# 処理ステップ
1. 入力の作品名・話数・参考URL・基本スケジュールを基にanime_idを生成（YYYYMM_title_c2形式）。
2. web_searchツールで対象話数の「サブタイトル」「前回あらすじ要約（3行以内）」「時間変更/休止」「関連グッズ」「原作巻数」「公式ハッシュタグ」「YouTube予告ID」を調査。
3. 抽出した情報をJSONにマッピング。変更があれば最新値を優先。

# 出力形式
以下の順で出力：
1. Master_data JSONブロック
2. Episode_Content JSONブロック
3. Broadcast_Schedule JSONブロック
【ソース確認】
- 公式サイト確認用URL: ...
- 放送スケジュール根拠URL: ...
- 備考: ...

anime_idルール: YYYYMM:開始年月, title:10文字以内英数字, c2:クール/期数"""

def call_grok_for_anime(
    title: str,
    ep_num: int,
    official_url: Optional[str] = None,
    schedules: Optional[list] = None
) -> str:
    url_hint = f"\n公式サイトURL（参考）：{official_url}" if official_url else ""
    schedule_hint = ""
    if schedules:
        schedule_str = ", ".join([f"{s.get('station', '')} ({s.get('day_of_week', '')} {s.get('time', '')})" for s in schedules])
        schedule_hint = f"\n基本放送スケジュール：{schedule_str}"

    user_input = f"作品名：{title}\n最新話の情報を取得してください。現在の話数はおおよそ第{ep_num}話前後です。"

    chat = client.chat.create(
        model="grok-4-1-fast-reasoning",  # 安価でツール対応良好
        tools=[web_search()],             # サーバー側で自動検索実行
        tool_choice="auto"
    )

    chat.append(system(SYSTEM_PROMPT))
    chat.append(user(user_input))

    response = chat.sample()
    return response.content or ""

# parse_output関数は前回と同じ（変更なしでOK）
def parse_output(text: str, title: str, ep_num: int) -> Optional[Dict[str, Any]]:
    json_blocks = re.findall(r'```json\s*([\s\S]*?)\s*```', text)
    if len(json_blocks) < 3:
        json_blocks = re.findall(r'(\{[\s\S]*?\})(?=\s*(?:\{|\[ソース確認\]|$))', text, re.DOTALL)
        if len(json_blocks) < 3:
            logging.error(f"JSONブロック不足: {len(json_blocks)}個")
            return None

    try:
        master = json.loads(json_blocks[0].strip())
        episode = json.loads(json_blocks[1].strip())
        broadcast = json.loads(json_blocks[2].strip())

        # ep_numリスト対策
        if isinstance(episode.get("ep_num"), list):
            episode["ep_num"] = episode["ep_num"][0] if episode["ep_num"] else ep_num
        if isinstance(broadcast.get("ep_num"), list):
            broadcast["ep_num"] = broadcast["ep_num"][0] if broadcast["ep_num"] else ep_num

    except json.JSONDecodeError as e:
        logging.error(f"JSONパース失敗: {e}\nRaw: {text[:500]}...")
        return None

    source_match = re.search(r'【ソース確認】([\s\S]*)', text, re.DOTALL)
    sources = source_match.group(1).strip() if source_match else "ソース抽出失敗"

    if "anime_id" not in master:
        logging.error(f"anime_id が見つからない: {title}")
        return None

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
    if not watch_list_file.exists():
        logging.critical(f"❌ {watch_list_file} が見つかりません")
        exit(1)

    with open(watch_list_file, "r", encoding="utf-8") as f:
        ANIMES_TO_CHECK = json.load(f)

    all_broadcasts = []
    processed_count = 0
    total_count = len(ANIMES_TO_CHECK)

    logging.info(f"🚀 {today} データ取得開始 ({total_count}件)")

    for i, anime in enumerate(ANIMES_TO_CHECK):
        title = anime.get('title', '不明')
        ep_num = anime.get('ep_num', 0)

        logging.info(f"[{i+1}/{total_count}] {title} 第{ep_num}話")

        try:
            raw_text = call_grok_for_anime(
                title,
                ep_num,
                anime.get('official_url'),
                anime.get('schedules', [])
            )

            data = parse_output(raw_text, title, ep_num)

            if data and "anime_id" in data["master"]:
                anime_id = data["master"]["anime_id"]

                for key, content in [("master", data["master"]), ("episode", data["episode"]), ("broadcast", data["broadcast"])]:
                    (output_dir / f"{anime_id}_{key}.json").write_text(
                        json.dumps(content, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )

                (output_dir / f"{anime_id}_sources.txt").write_text(data["sources"], encoding="utf-8")

                all_broadcasts.append(data["broadcast"])

                anime["ep_num"] = ep_num + 1
                processed_count += 1
                logging.info(f"  ✅ {anime_id} 完了")

            else:
                logging.error(f"  ❌ パース/検証失敗: {title} 第{ep_num}話")

        except Exception as e:
            logging.error(f"  🔥 エラー: {title} 第{ep_num}話 - {e}", exc_info=True)

    if processed_count > 0:
        all_broadcasts.sort(key=lambda x: x.get("start_time") or "")
        (output_dir / "daily_schedule.json").write_text(
            json.dumps(all_broadcasts, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        with open(watch_list_file, "w", encoding="utf-8") as f:
            json.dump(ANIMES_TO_CHECK, f, ensure_ascii=False, indent=2)

        logging.info("📝 watch_list & daily_schedule 更新完了")

    logging.info(f"🎉 処理完了！成功: {processed_count}/{total_count}")
    logging.info(f"ログ: {log_file.absolute()}")