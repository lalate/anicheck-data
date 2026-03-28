# Grok API 呼び出し最適化計画 偵察報告書

## 1. 目的

`anicheck_daily.py` における Grok API 呼び出しの効率を最大化し、API コストを削減しつつ、必要な情報（あらすじ、予告編 YouTube ID）の取得精度を向上させる。特に、Syoboi TID を持たない作品（配信限定など）への無駄な Grok 呼び出しを抑制するためのクールダウン機構の導入を検討する。

## 2. 偵察対象ファイル

-   **`AniJson/scripts/anicheck_daily.py`**: Grok API 呼び出しの主要ロジックとプロンプト、データ保存・更新ロジック。
-   **`AniJson/.github/workflows/daily_fetch.yml`**: GitHub Actions による `anicheck_daily.py` の実行設定。
-   **`AniJson/current/watch_list.json`**: 監視対象アニメのメタデータ。Grok 呼び出しクールダウンの管理フィールド追加を検討。

## 3. 主要な発見

### 3.1. `anicheck_daily.py` の Grok 運用現状

-   **メイン戦略**: Syoboi Calendar API (しょぼいカレンダー) を一次情報源とし、そこで取得できない不足情報（主に `summary` や `preview_youtube_id`）を Grok で補完するハイブリッド戦略を採用している。
-   **Grok 呼び出し上限**: `MAX_GROK_CALLS_PER_DAY: int = 5` と設定されており、1日あたりの API コール回数が厳しく制限されている。
-   **Grok モデル**: `grok-4-1-fast-reasoning` が指定されており、最新かつ効率的なモデルが選定されている。
-   **現在の `SYSTEM_PROMPT` の要求内容**:
    -   「現在最も新しい配信・放送済みエピソード」
    -   「直近3日間（本日〜明後日）の放送予定」
    -   これらを基にした `Broadcast_Update` JSON ブロック（全体の最新話数、プラットフォーム別の最新話数）
    -   `Upcoming_Schedule_And_Episode` JSON ブロック（エピソード話数、サブタイトル、あらすじ要約、予告の YouTube ID、放送局・日時）
    -   **所感**: 放送スケジュールに関する情報は Syoboi で大部分が取得可能であるため、Grok への要求内容がやや広範すぎる可能性がある。
-   **Grok 呼び出し判定**: `needs_grok_enrichment(anime_id: str, ep_num: int)` 関数によって行われる。この関数は、対象エピソードのファイル (`database/episodes/{anime_id}/ep{ep_num:03d}.json`) が存在しない、またはそのファイル内の `summary` フィールドが空の場合に `True` を返す。
-   **`save_episode_file` 関数**: エピソードファイルを保存・マージする。既存ファイルがある場合、Grok からのデータ（`title`, `summary`, `preview_youtube_id`）で空のフィールドのみを上書きするロジックが実装されている。
-   **TIDなし作品への対応**: `syoboi_tid` が設定されていない作品（配信限定や TID 未特定）に対しても、Grok 呼び出しの候補として扱われている。現状、これらの作品に対する Grok 呼び出し頻度を抑制するメカニズムは存在しない。

### 3.2. `daily_fetch.yml` (GitHub Actions)

-   `AniJson` ディレクトリをカレントとして `scripts/anicheck_daily.py` が実行される想定。
-   毎日 UTC 00:00 (JST 09:00) に自動実行される cron ジョブが設定されている。
-   `XAI_API_KEY` を環境変数としてセキュアに渡す設定済み。
-   変更があった場合に `current/` および `logs/` ディレクトリ配下を `git add` し、自動コミット・プッシュする。

### 3.3. `watch_list.json` の構造

-   `AniJson/current/watch_list.json` に配置されている。
-   各アニメエントリは以下の主要なフィールドを持つ：`anime_id`, `mal_id`, `title`, `official_url`, `last_checked_ep`, `is_active`, `season`, `season_end_date`, `syoboi_tid`。
-   **所感**: Grok 呼び出しのクールダウンを管理するため、各アニメエントリに `last_grok_date` (YYYY-MM-DD 形式) フィールドを追加する余地がある。

## 4. 推奨される改善点と今後の計画

1.  **Grok `SYSTEM_PROMPT` の最適化**:
    -   プロンプトの記述を「あらすじ（summary）」と「YouTube 予告編 ID（preview_youtube_id）」の取得に特化・簡素化する。
    -   放送進捗やスケジュールに関する要求は、Syoboi が主であることを強調し、Grok の負荷を軽減する。
2.  **Grok 呼び出しクールダウンの実装**:
    -   `watch_list.json` の各エントリに `last_grok_date` フィールド（最終 Grok 呼び出し日）を追加する。
    -   `GROK_COOLDOWN_DAYS = 7` (例) の定数を定義する。
    -   `anicheck_daily.py` 内の Grok 呼び出しロジックに以下の条件を追加：
        -   **Syoboi TID がない作品**: `last_grok_date` が存在し、かつ現在の日付から `GROK_COOLDOWN_DAYS` 以上経過していない場合は Grok 呼び出しをスキップする。
        -   **全ての Grok 呼び出し後**: 対象アニメの `last_grok_date` を現在の日付で更新する。
3.  **`needs_grok_enrichment` の改修**:
    -   クールダウンロジックの判定をこの関数、または新たに作成する補助関数 (`should_call_grok_with_cooldown` など) に統合し、Grok 呼び出し条件を一元管理する。

これらの改善により、Grok API の利用がさらに戦略的かつ効率的になり、コストと精度の両面で最適化が図られる見込みである。
