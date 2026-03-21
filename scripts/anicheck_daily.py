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
あなたは日本のアニメ放送・配信情報に精通した調査員です。

# 探索フェーズ（情報の海を泳ぐ）
指定された作品の「現在最も新しい配信・放送済みエピソード」および「直近3日間（本日〜明後日）の放送予定」に関する情報を、全方位から幅広く収集してください。
- ツール（web_search, x_search）を必ず駆使し、公式発表、ニュースサイト（ANN、Natalie等）、番組表サイト、公式Twitter、一般ユーザーの実況や噂まで、まずは広く情報を集めてください。
- 検索時は広範な情報収集を意識し、作品名や略称、局名などを組み合わせて検索してください。
- ユーザーから提供される「前回の局別進捗（履歴）」をヒントに、それ以降の新しい情報（最新話は第何話か、向こう3日間で放送されるのは第何話か）を探してください。

# 抽出フェーズ（情報の構造化）
集めた情報から、以下の2つのJSONブロック（```json ... ```）を作成してください。

1. Broadcast_Update JSONブロック
各局・配信プラットフォームごとの最新の放送/配信済み話数と、作品全体の最新話数。
{
  "overall_latest_ep": 整数,
  "platforms": {
    "局名A": { "last_ep_num": 整数, "remarks": "遅れ放送など" }
  }
}

2. Upcoming_Schedule_And_Episode JSONブロック
「直近3日間（本日、明日、明後日）」に放送・配信される予定のエピソード情報。期間内に放送がない場合は `{}` を出力。
{
  "ep_num": 整数,
  "title": "サブタイトル",
  "summary": "あらすじ要約（3行以内）",
  "preview_youtube_id": "予告のYouTube ID または null",
  "broadcasts": [
    { "station_id": "局名", "start_time": "YYYY-MM-DDTHH:MM:SS+09:00", "status": "normal/delayed" }
  ]
}

# 検証フェーズ（厳格な制約とハルシネーション排除）
最後に、集めた情報を厳しく精査します。以下のルールに違反する情報は捨ててください。
- 【重要】出力に含める情報は、公式サイト、放送局公式、信頼できるニュースソースで裏付けが取れたもののみとしてください。
- ソースのない噂や推測による捏造は絶対に行わないでください。確認できない項目は `null` にしてください。
- 出力は上記の2つのJSONブロックと、最後に【ソース確認】（参照したURL）のみとし、余計な解説は省いてください。"""


def call_grok_for_anime(
    title: str,
    official_url: Optional[str] = None,
    current_history: Optional[Dict[str, Any]] = None
) -> str:
    """Grokに作品の最新放送進捗と直近3日間の放送予定を問い合わせる。

    Args:
        title: 作品名。
        official_url: 公式サイトURL（参考情報として提示）。
        current_history: broadcast_history.jsonから取り出した局別進捗の辞書。
    """
    history_str = json.dumps(current_history, ensure_ascii=False) if current_history else "{}"
    url_hint = f"\n公式URL（参考）：{official_url}" if official_url else ""
    user_input = (
        f"作品名：{title}{url_hint}\n"
        f"前回の局別進捗：{history_str}\n\n"
        f"上記を踏まえ、進捗の更新と直近3日間（本日〜明後日）の放送予定を出力せよ。"
    )

    chat = client.chat.create(
        model="grok-4-1-fast-reasoning",  # 安価でツール対応良好
        tools=[web_search()],             # サーバー側で自動検索実行
        tool_choice="auto"
    )

    chat.append(system(SYSTEM_PROMPT))
    chat.append(user(user_input))

    response = chat.sample()
    return response.content or ""


def parse_output(text: str, title: str) -> Optional[Dict[str, Any]]:
    """Grokの応答テキストから2つのJSONブロックと【ソース確認】を抽出する。

    Returns:
        {"update": Broadcast_Update dict, "today": Today_Schedule_And_Episode dict, "sources": str}
        パース失敗時は None を返す。
    """
    json_blocks = re.findall(r'```json\s*([\s\S]*?)\s*```', text)
    if len(json_blocks) < 2:
        # フェンスなしの生JSONフォールバック（先頭2つを採用）
        json_blocks = re.findall(r'(\{[\s\S]*?\})(?=\s*(?:\{|\【ソース確認】|$))', text, re.DOTALL)
        if len(json_blocks) < 2:
            logging.error(f"JSONブロック不足({len(json_blocks)}個): {title}")
            return None

    try:
        update_data = json.loads(json_blocks[0].strip())
        today_data = json.loads(json_blocks[1].strip())
    except json.JSONDecodeError as e:
        logging.error(f"JSONパース失敗: {e}\nRaw: {text[:500]}...")
        return None

    source_match = re.search(r'【ソース確認】([\s\S]*)', text, re.DOTALL)
    sources = source_match.group(1).strip() if source_match else "ソース抽出失敗"

    return {
        "update": update_data,
        "today": today_data,
        "sources": sources
    }


# ====================== メイン実行 ======================
if __name__ == "__main__":
    today = datetime.date.today().strftime("%Y-%m-%d")

    current_dir = Path("current")
    current_dir.mkdir(parents=True, exist_ok=True)

    watch_list_file = current_dir / "watch_list.json"
    if not watch_list_file.exists():
        logging.critical(f"❌ {watch_list_file} が見つかりません")
        exit(1)

    with open(watch_list_file, "r", encoding="utf-8") as f:
        ANIMES_TO_CHECK = json.load(f)

    # broadcast_history.json を読み込む（存在しなければ空辞書）
    broadcast_history_file = current_dir / "broadcast_history.json"
    if broadcast_history_file.exists():
        with open(broadcast_history_file, "r", encoding="utf-8") as f:
            broadcast_history: Dict[str, Any] = json.load(f)
    else:
        broadcast_history = {}
        logging.info("broadcast_history.json が存在しないため空辞書で初期化します")

    all_broadcasts = []
    processed_count = 0
    total_count = len(ANIMES_TO_CHECK)

    logging.info(f"🚀 {today} データ取得開始 ({total_count}件)")

    for i, anime in enumerate(ANIMES_TO_CHECK):
        title = anime.get('title', '不明')
        # anime_id が未設定の場合はタイトルをフォールバックキーとして使う
        anime_id: str = anime.get('anime_id') or title

        logging.info(f"[{i+1}/{total_count}] {title} (id={anime_id})")

        # 現在の局別進捗を履歴から取り出す
        current_history = broadcast_history.get(anime_id, {"platforms": {}})

        try:
            raw_text = call_grok_for_anime(
                title,
                anime.get('official_url'),
                current_history
            )

            data = parse_output(raw_text, title)

            if data is None:
                logging.error(f"  ❌ パース失敗: {title}")
                continue

            update_data: Dict[str, Any] = data["update"]
            today_data: Dict[str, Any] = data["today"]

            # broadcast_history を update_data で更新
            if anime_id not in broadcast_history:
                broadcast_history[anime_id] = {}
            broadcast_history[anime_id]["title"] = title
            broadcast_history[anime_id]["overall_latest_ep"] = update_data.get("overall_latest_ep")
            broadcast_history[anime_id]["platforms"] = update_data.get("platforms", {})

            # 今日の放送がある場合はエピソードファイルを保存
            if today_data:
                ep_num: int = today_data.get("ep_num", 0)

                episodes_dir = Path("database") / "episodes" / anime_id
                episodes_dir.mkdir(parents=True, exist_ok=True)

                ep_file = episodes_dir / f"ep{ep_num:03d}.json"
                ep_file.write_text(
                    json.dumps(today_data, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                logging.info(f"  📄 エピソード保存: {ep_file}")

                # daily_schedule 用にブロードキャスト情報を追加
                for bc in today_data.get("broadcasts", []):
                    all_broadcasts.append({
                        "anime_id": anime_id,
                        "title": title,
                        "ep_num": ep_num,
                        **bc
                    })

            processed_count += 1
            logging.info(f"  ✅ {anime_id} 完了")

        except Exception as e:
            logging.error(f"  🔥 エラー: {title} - {e}", exc_info=True)

    if processed_count > 0:
        # daily_schedule.json を start_time でソートして保存
        all_broadcasts.sort(key=lambda x: x.get("start_time") or "")
        (current_dir / "daily_schedule.json").write_text(
            json.dumps(all_broadcasts, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        with open(watch_list_file, "w", encoding="utf-8") as f:
            json.dump(ANIMES_TO_CHECK, f, ensure_ascii=False, indent=2)

        broadcast_history_file.write_text(
            json.dumps(broadcast_history, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        logging.info("📝 watch_list / daily_schedule / broadcast_history 更新完了")

    logging.info(f"🎉 処理完了！成功: {processed_count}/{total_count}")
    logging.info(f"ログ: {log_file.absolute()}")