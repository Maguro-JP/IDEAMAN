-- IDEAMAN のスキーマ。起動時に毎回そのまま適用される。
--
-- state:
--   unseen … 生成済みだがまだ誰も見ていない「溜まっている考え」
--   seen   … 一度取り出された
-- mood / parent_id は今は表示に使っていないが、Phase 5 の思考の連鎖のために
-- 最初から持たせておく（後からのスキーマ変更を避ける）。

CREATE TABLE IF NOT EXISTS ideas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT    NOT NULL,
    seed_json   TEXT    NOT NULL,
    domain      TEXT,
    template_id TEXT    NOT NULL,
    tone        TEXT    NOT NULL,
    mood        TEXT    NOT NULL,
    source      TEXT    NOT NULL CHECK (source IN ('engine', 'llm')),
    state       TEXT    NOT NULL DEFAULT 'unseen' CHECK (state IN ('unseen', 'seen')),
    parent_id   INTEGER REFERENCES ideas (id),
    created_at  TEXT    NOT NULL,
    seen_at     TEXT,
    -- 同じ組み合わせに再到達したかの判定に使う（テンプレID + ソート済みの語）
    signature   TEXT,
    -- 連鎖の識別子。同じ chain_id を持つものは一つの思考の流れ
    chain_id    INTEGER,
    -- associate（連想）か wander（放浪）か
    mode        TEXT
);


CREATE TABLE IF NOT EXISTS ratings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id    INTEGER NOT NULL REFERENCES ideas (id) ON DELETE CASCADE,
    score      INTEGER NOT NULL CHECK (score IN (-1, 0, 1)),
    note       TEXT,
    created_at TEXT    NOT NULL
);


-- 語ごとの重み。評価が付くたびに増減し、次の抽選に反映される。
-- これが「蓄積して育つ」の実体。
--
-- dormant_until は「頭の片隅に置いておく」ための休眠。捨てるのでも重みを
-- 下げるのでもなく、しばらく抽選から外すだけ。時間が来れば戻ってくる。
CREATE TABLE IF NOT EXISTS weights (
    pool          TEXT NOT NULL,
    term          TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    dormant_until TEXT,
    stall_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pool, term)
);

-- いま何を考えているかの状態。プロセスが落ちても続きから再開する。
CREATE TABLE IF NOT EXISTS thought_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    mode         TEXT    NOT NULL DEFAULT 'wander' CHECK (mode IN ('associate', 'wander')),
    chain_id     INTEGER,
    chain_length INTEGER NOT NULL DEFAULT 0,
    stale_hits   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL
);

-- 全体のカウンタ。重複到達の回数など、行に紐づかない数を置く。
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

-- 配信の記録。ワーカーが pending を書き、Bot が拾って投稿する
-- プロセス間キューでもある（Redis 等を持ち込まないため）。
CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id     INTEGER NOT NULL REFERENCES ideas (id),
    channel     TEXT    NOT NULL,
    external_id TEXT,
    state       TEXT    NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'sent', 'failed')),
    created_at  TEXT    NOT NULL,
    sent_at     TEXT
);

