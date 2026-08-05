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
    seen_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ideas_state ON ideas (state, id);
CREATE INDEX IF NOT EXISTS idx_ideas_created ON ideas (created_at DESC);

CREATE TABLE IF NOT EXISTS ratings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id    INTEGER NOT NULL REFERENCES ideas (id) ON DELETE CASCADE,
    score      INTEGER NOT NULL CHECK (score IN (-1, 0, 1)),
    note       TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ratings_idea ON ratings (idea_id);

-- 語ごとの重み。評価が付くたびに増減し、次の抽選に反映される。
-- これが「蓄積して育つ」の実体。
CREATE TABLE IF NOT EXISTS weights (
    pool   TEXT NOT NULL,
    term   TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (pool, term)
);
