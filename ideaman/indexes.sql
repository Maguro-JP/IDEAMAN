-- 索引。テーブルの作成と列のマイグレーションが終わったあとに適用する。
-- （Phase 1 の DB には signature 等の列がまだ無いので、順番が逆だと落ちる）

CREATE INDEX IF NOT EXISTS idx_ideas_state ON ideas (state, id);
CREATE INDEX IF NOT EXISTS idx_ideas_ready ON ideas (state, verdict);
CREATE INDEX IF NOT EXISTS idx_ideas_created ON ideas (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ideas_signature ON ideas (signature);
CREATE INDEX IF NOT EXISTS idx_ideas_chain ON ideas (chain_id);
CREATE INDEX IF NOT EXISTS idx_ratings_idea ON ratings (idea_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON deliveries (channel, state, id);
CREATE INDEX IF NOT EXISTS idx_deliveries_external ON deliveries (channel, external_id);
