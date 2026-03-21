# anicheck_daily.py リファクタリング計画 (V2対応)

## 目的
`AniJson/scripts/anicheck_daily.py` を改修し、新データ構造（V2）に適合させる。
Grokへのリクエストにおいて、マスターデータの生成を廃止し、「履歴を考慮した進捗確認」と「今日の放送予定の取得」に特化させる。

## 対象ファイル
`AniJson/scripts/anicheck_daily.py`

## 改修ステップ

### 1. SYSTEM_PROMPTの完全置換
既存の `SYSTEM_PROMPT` を以下の内容に置き換えてください。

```text
# 役割
あなたは日本のアニメ放送・配信情報に精通した調査員です。

# 探索フェーズ（情報の海を泳ぐ）
指定された作品の「現在最も新しい配信・放送済みエピソード」および「本日の放送予定」に関する情報を、全方位から幅広く収集してください。
- ツール（web_search, x_search）を必ず駆使し、公式発表、ニュースサイト（ANN、Natalie等）、番組表サイト、公式Twitter、一般ユーザーの実況や噂まで、まずは広く情報を集めてください。
- 検索時は広範な情報収集を意識し、作品名や略称、局名などを組み合わせて検索してください。
- ユーザーから提供される「前回の局別進捗（履歴）」をヒントに、それ以降の新しい情報（最新話は第何話か、今日放送されるのは第何話か）を探してください。

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

2. Today_Schedule_And_Episode JSONブロック
「今日（実行日）」に放送・配信される予定のエピソード情報。今日放送がない場合は `{}` を出力。
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
- 出力は上記の2つのJSONブロックと、最後に【ソース確認】（参照したURL）のみとし、余計な解説は省いてください。
```

### 2. `call_grok_for_anime` 関数の引数と処理の修正
- 引数に `current_history: dict` を追加。
- `user_input` の構築部分で、`current_history` の内容（JSON文字列等）を埋め込み、Grokに「前回の局別進捗」として提示する。
  ```python
  history_str = json.dumps(current_history, ensure_ascii=False) if current_history else "{}"
  user_input = f"作品名：{title}\n公式URL（参考）：{official_url}\n前回の局別進捗：{history_str}\n\n上記を踏まえ、進捗の更新と本日の放送予定を出力せよ。"
  ```

### 3. `parse_output` 関数の修正
- 期待するJSONブロックを「3つ」から「2つ（`Broadcast_Update`, `Today_Schedule_And_Episode`）」に変更。
- 戻り値を `{"update": update_data, "today": today_data, "sources": sources}` の形式に変更。

### 4. メイン実行ループの修正
- スクリプト実行の冒頭で `current/broadcast_history.json` を読み込む（なければ空辞書 `{}`）。
- ループ内で各作品の `anime_id` を用いて、現在の履歴（`history.get(anime_id, {"platforms": {}})`）を取り出し `call_grok_for_anime` に渡す。
- パース結果を受け取ったら：
  1. `broadcast_history` の当該アニメの情報を `update_data` で更新する。
  2. `today_data` が空 `{}` でなければ、`database/episodes/{anime_id}/ep{ep_num:03d}.json` としてエピソード情報を保存する。同時に、`all_broadcasts` にスケジュール情報を追加する。
- 最後に `watch_list.json`、`daily_schedule.json` に加えて、`broadcast_history.json` も保存する。

## 注意事項
- ディレクトリパスは新構造（`database/episodes/...`）を前提とする。`database/episodes` や `current` ディレクトリがない場合は `Path.mkdir` で作成すること。