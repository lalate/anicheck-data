# 🛡️ アニちぇっく・データ要塞 (AniJson v2.1)

このリポジトリは、アニメ番組表アプリ「アニちぇっく」の心臓部であり、AIとJikan APIを統合した次世代のアニメ情報データベースです。
放送データの民主化を目指し、すべてのデータはJSON形式でオープンに管理されています。

## 🏰 V2 データ構造（DB型フラット構造）

旧来のシーズンごとのディレクトリ管理を廃止し、永続的な蓄積を可能にするフラットな設計を採用しています。

- **`database/`**: 全アニメデータの永久保存庫（真のDB）
  - **`master/`**: 作品不変データ。`mal_id` (MyAnimeList ID) を主キーとし、高画質ポスターURL、ジャンル、制作スタジオ等を保持。
  - **`episodes/`**: 全話アーカイブ。`{anime_id}/ep{nnn}.json` 形式で、各話のあらすじや予告編IDを蓄積。
- **`current/`**: 現在の状態（ステート）管理
  - **`watch_list.json`**: 現在監視中（放送中）の作品インデックス。
  - **`broadcast_history.json`**: 作品ごとの局別・配信別進捗状況（最新放送話数）を一元管理。
  - **`daily_schedule.json`**: 直近3日間（本日〜明後日）の統合放送スケジュール。

## 🤖 AI兵站パイプライン

データは以下の高度な自動化プロセスによって常に最新に保たれています。

1.  **Grok AI 偵察**: `scripts/anicheck_daily.py` が「探索・抽出・検証」の3フェーズプロンプトを用いてWebを巡回。向こう3日間のスケジュールと局別進捗を抽出。
2.  **Jikan API 武装**: `scripts/enrich_master.py` がMyAnimeListと同期し、マスターデータを公式アセットでエンリッチ化。
3.  **新作自動検知**: `scripts/season_adder.py` が次期シーズンの新作を検知し、既存リストを破壊することなく安全に追記。

## 🛠️ データの修正・貢献方法

放送休止や時間変更など、AIが捉えきれない「現場の真実」を発見した場合は、ぜひ以下の修正をお願いします。

1.  `database/master/` または `database/episodes/` 配下の該当JSONを編集。
2.  内容を記載して **Pull Request** を送信。
3.  GitHub Actionsがバリデーションを行い、承認後にマージされます。

---
Produced by **Project AniCheck Lead Architect (AI)**
