# アニちぇっく 新データ構造・移行定義書 (v2.0 DB型フラット構造)

## 1. 物理ディレクトリ構造

`current/` への依存を脱却し、全作品データを永久保存する「真のDB」として `database/` ディレクトリを新設します。シーズンを跨いでもファイルの移動は発生しません。

```text
AniJson/
├── database/                # 全アニメのデータを永久保存する真のDB
│   ├── master/              # 全作品の不変データ (1作品1ファイル)
│   │   ├── 202601_frieren_c1.json
│   │   └── 202604_newanimeA_c1.json
│   └── episodes/            # 全作品のエピソードアーカイブ (1話1ファイル)
│       ├── 202601_frieren_c1/
│       │   ├── ep001.json
│       │   └── ep028.json
│       └── 202604_newanimeA_c1/
│           └── ep001.json
│
├── current/                 # 「今」動いている状態管理（ステート）
│   ├── watch_list.json          # 現在監視中のリスト (DBでいう WHERE is_active = true)
│   ├── broadcast_history.json   # 現在進行中の局別進捗
│   └── daily_schedule.json      # 今日の放送まとめ (アプリ表示用)
│
└── archive/                 # 過去のインデックスのみを保存
    └── 2025_winter_watch_list.json
```

## 2. 詳細JSONスキーマ定義 (TypeScript Interface形式)

### 2.1. `current/watch_list.json`
監視対象の絞り込みと、作品全体の最新進捗（Grokが最後に確認した話数）を管理します。
```typescript
interface WatchListItem {
  anime_id: string;          // アニちぇっく内部ID (例: "202601_frieren_c1")
  mal_id: number;            // Jikan API 主キー
  title: string;
  official_url: string;
  last_checked_ep: number;   // Grokが最後に確認した「作品全体の最新話数」
  is_active: boolean;        // 監視継続フラグ (シーズン終了等で false になる)
  season: string;            // 放送クール (例: "2026冬")
  season_end_date: string;   // ISO 8601形式。自動終了判定の目安。
}
```

### 2.2. `current/broadcast_history.json`
各作品の「局・配信プラットフォームごと」の進捗を管理します。
```typescript
type BroadcastHistory = Record<string, {
  title: string;
  overall_latest_ep: number;
  platforms: Record<string, { // キー例: "mx", "bs11", "netflix"
    last_ep_num: number;     // その局/配信での最終放送話数
    last_broadcast_date: string | null; // "YYYY-MM-DD"
    last_updated_at: string; // ISO 8601
    remarks: string | null;  // 「Netflix独占先行」「1話遅れ」等のメモ
  }>;
}>;
```

### 2.3. `database/master/{anime_id}.json`
作品不変の基本情報、スタッフ、キャスト、外部リンクを保持します。
```typescript
interface AnimeMasterData {
  anime_id: string;
  mal_id: number;
  title: string;
  title_english: string | null;
  title_japanese: string | null;
  image_url: string | null;  // Jikan API (images.webp.large_image_url)
  score: number | null;      // Jikan API (score)
  genres: string[];          // Jikan API (genres.name)
  themes: string[];          // Jikan API (themes.name)
  studio: string | null;     // Jikan API優先、無ければGrok推測
  official_url: string;
  hashtag: string;
  station_master: string;    // キーステーション
  base_op_youtube_id: string | null;
  jikan_trailer_id: string | null; 
  sources: {
    manga_amazon: string | null;
    light_novel_amazon: string | null;
    web_novel: string | null;
  };
  staff: Record<string, string>;
  cast: string[];
}
```

### 2.4. `database/episodes/{anime_id}/ep{nnn}.json`
各話固有のメタデータを蓄積します。
```typescript
interface EpisodeData {
  anime_id: string;
  mal_id: number;
  ep_num: number;            // 話数 (整数)
  title: string;             // サブタイトル
  summary: string;           // あらすじ
  preview_youtube_id: string | null; // 次回予告YouTube ID
  original_vol: number | null;       // 原作対応巻数
  confirmed_at: string;      // Grokによる情報確認日時 (ISO 8601)
  source_priority: string;   // 取得元 ("official", "x_search" 等)
}
```

## 3. 日々の運用フロー（オーバーラップ対応）

1. **毎日実行 (`anicheck_daily.py`)**:
   - `current/watch_list.json` から `is_active: true` の作品を抽出。
   - `current/broadcast_history.json` を参照し、各局の進捗状況を把握。
   - 局の進捗と `watch_list` の `last_checked_ep` を基に、Grokへ「最新話および各局の放送実績」を問い合わせる。
   - Grokの回答を基に、以下を更新：
     - `watch_list.json` の `last_checked_ep` を更新。
     - `broadcast_history.json` の局別進捗を更新。
     - 未取得のエピソードがあれば `database/episodes/{anime_id}/ep{nnn}.json` を新規作成。
     - 今日の放送予定を `current/daily_schedule.json` として再生成。

2. **新シーズンの追加**:
   - 新規作品の `master` を `database/master/` に作成。
   - `watch_list.json` に `is_active: true` で追加。
   - ※ 旧シーズンの作品は `is_active` が `false` になるまで監視が継続され、新旧が自然に共存します。