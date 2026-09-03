import sqlite3

import pytest

from ideaman import service
from ideaman.engine.combinator import combine
from ideaman.store import MAX_WEIGHT, MIN_WEIGHT, Store


def _save(store: Store, seed: int = 1):
    idea_seed = combine(seed=seed)
    return idea_seed, store.save(idea_seed, "てすと", "engine")


def test_saved_idea_starts_unseen_and_unjudged(store):
    """保存直後は「まだ判定していない独り言」。在庫には数えない。"""
    _, idea = _save(store)
    assert idea.state == "unseen"
    assert idea.verdict is None
    assert store.stock_count() == 0, "判定前のものが在庫に数えられている"
    assert len(store.unjudged()) == 1
    # 未閲覧のものは一覧に出ない
    assert store.list_seen() == []


def test_only_chosen_counts_as_stock(store):
    _, first = _save(store)
    _, second = _save(store, seed=2)
    store.set_verdict(first.id, "chosen", 0.8)
    store.set_verdict(second.id, "kept", 0.1)

    assert store.stock_count() == 1
    assert store.take_ready().id == first.id


def test_kept_ideas_are_not_deleted(store):
    """選ばれなくても捨てない。連鎖を辿れば読める。"""
    _, idea = _save(store)
    store.set_verdict(idea.id, "kept", 0.1)

    assert store.get(idea.id).verdict == "kept"
    assert idea.id in {i.id for i in store.search(limit=10)}
    assert idea.id in {i.id for i in store.chain(idea.id)}


def test_ready_comes_out_best_first(store):
    """FIFO ではなくスコア順。溜まった chosen は質のプールになる。"""
    _, low = _save(store, seed=1)
    _, high = _save(store, seed=2)
    store.set_verdict(low.id, "chosen", 0.2)
    store.set_verdict(high.id, "chosen", 0.9)

    assert store.take_ready().id == high.id
    assert store.take_ready().id == low.id
    assert store.take_ready() is None


def _idea_terms(idea_seed):
    """発想を担う語だけ。言い方（probes）は評価で動かさない。"""
    return [
        (pool, term)
        for pool, term in idea_seed.terms
        if pool not in idea_seed.SIGNATURE_IGNORED_POOLS
    ]


def test_upvote_raises_weights_of_its_terms(store):
    idea_seed, idea = _save(store)
    store.rate(idea.id, 1)

    weights = store.weights()
    for _, term in _idea_terms(idea_seed):
        assert weights[term] > 1.0


def test_downvote_lowers_weights(store):
    idea_seed, idea = _save(store)
    store.rate(idea.id, -1)

    weights = store.weights()
    for _, term in _idea_terms(idea_seed):
        assert weights[term] < 1.0


def test_rating_does_not_move_the_phrasing(store):
    """締めの一文（probes）は評価で動かさないこと。

    「まず15分だけ試す」は言い方であって思いつきではない。👍 をこれの
    手柄にすると、当たった発想の道連れで太る。しかも probes は署名から
    外してあるので反復抑制にも見えず、止めるものが無い。稼働中の DB で
    重みが 4.64（他は約1.0、上限5.0）まで走り、選ばれた案の probe の
    49% が同じ一文になっていた。
    """
    idea_seed, idea = _save(store)
    phrasing = [
        term
        for pool, term in idea_seed.terms
        if pool in idea_seed.SIGNATURE_IGNORED_POOLS
    ]
    if not phrasing:  # そのテンプレートが probe を使っていない
        return

    store.rate(idea.id, 1)
    weights = store.weights()
    for term in phrasing:
        assert weights.get(term, 1.0) == 1.0


def test_neutral_rating_leaves_weights_alone(store):
    _, idea = _save(store)
    store.rate(idea.id, 0)
    assert store.weights() == {}


def test_weights_are_clamped(store):
    idea_seed, idea = _save(store)
    for _ in range(200):
        store.rate(idea.id, 1)
    assert all(w <= MAX_WEIGHT for w in store.weights().values())

    for _ in range(400):
        store.rate(idea.id, -1)
    assert all(w >= MIN_WEIGHT for w in store.weights().values())


def test_score_accumulates(store):
    _, idea = _save(store)
    store.rate(idea.id, 1)
    store.rate(idea.id, 1)
    assert store.get(idea.id).score == 2
    assert store.rate(idea.id, -1).score == 1


def test_rate_rejects_bad_input(store):
    _, idea = _save(store)
    with pytest.raises(ValueError):
        store.rate(idea.id, 5)
    with pytest.raises(KeyError):
        store.rate(9999, 1)


def test_note_is_optional_and_stored(store):
    _, idea = _save(store)
    store.rate(idea.id, 1, "これは使えそう")
    row = store.conn.execute(
        "SELECT note FROM ratings WHERE idea_id = ?", (idea.id,)
    ).fetchone()
    assert row["note"] == "これは使えそう"


def test_list_seen_is_newest_first(store):
    for i in range(3):
        _, idea = _save(store, seed=i)
        store.mark_seen(idea.id)
    ideas = store.list_seen()
    assert [i.id for i in ideas] == sorted((i.id for i in ideas), reverse=True)


def test_weights_survive_reopen(tmp_path):
    """育った重みはプロセスをまたいで残る。"""
    path = tmp_path / "grow.db"
    store = Store(path)
    idea_seed, idea = _save(store)
    store.rate(idea.id, 1)
    grown = store.weights()
    store.close()

    reopened = Store(path)
    assert reopened.weights() == grown
    reopened.close()


def test_generated_weights_feed_back_into_generation(store):
    """評価 → 重み → 次の生成、の一巡が通ること。"""
    first = service.generate_idea(store, use_llm=False, seed=1)
    store.rate(first.id, 1)
    second = service.generate_idea(store, use_llm=False)
    assert second.id != first.id
    assert store.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 2


# --- Phase 2 で足したもの --------------------------------------------


def test_phase1_database_is_migrated(tmp_path):
    """Phase 1 の DB を開いても壊れず、新しい列が足される。

    ユーザーが既に溜めたアイデアを捨てさせないための保証。
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL, seed_json TEXT NOT NULL, domain TEXT,
            template_id TEXT NOT NULL, tone TEXT NOT NULL, mood TEXT NOT NULL,
            source TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'unseen',
            parent_id INTEGER, created_at TEXT NOT NULL, seen_at TEXT
        );
        CREATE TABLE ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id INTEGER NOT NULL,
            score INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE weights (
            pool TEXT NOT NULL, term TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0, PRIMARY KEY (pool, term)
        );
        INSERT INTO ideas
            (text, seed_json, domain, template_id, tone, mood, source, created_at)
        VALUES ('昔のアイデア', '{"slots":{},"terms":[["concepts","地図"]],"seed":1}',
                NULL, 'oracle_collision', 'oracle', '静穏', 'engine', '2026-01-01');
        INSERT INTO weights (pool, term, weight) VALUES ('concepts', '地図', 1.5);
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        # 昔のアイデアも重みも残っている
        assert store.get(1).text == "昔のアイデア"
        assert store.weights()["地図"] == 1.5
        # 新しい列と表が使える
        assert store.thought_state().mode == "wander"
        assert store.terms_of(1) == (("concepts", "地図"),)
        service.generate_idea(store, use_llm=False)
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path):
    """二度開いても ALTER TABLE で落ちない。"""
    path = tmp_path / "twice.db"
    for _ in range(3):
        s = Store(path)
        s.close()


def test_chain_returns_the_whole_flow(store):
    parent = store.save(combine(seed=1), "親", "engine")
    child = store.save(
        combine(seed=2), "子", "engine", parent_id=parent.id, chain_id=parent.chain_id
    )
    chain = store.chain(child.id)
    assert [i.id for i in chain] == [parent.id, child.id]


def test_chain_of_unknown_idea_raises(store):
    with pytest.raises(KeyError):
        store.chain(9999)


def test_search_prefers_high_scores(store):
    low = store.save(combine(seed=1), "普通のもの", "engine")
    high = store.save(combine(seed=2), "良いもの", "engine")
    store.rate(high.id, 1)
    store.rate(high.id, 1)

    assert store.search(limit=2)[0].id == high.id
    assert [i.id for i in store.search("良い")] == [high.id]
    assert store.search("存在しない語") == []
    assert low.id in {i.id for i in store.search(limit=10)}


def test_counters(store):
    assert store.counter("nothing") == 0
    store.bump_counter("duplicate_hits")
    store.bump_counter("duplicate_hits", 2)
    assert store.counter("duplicate_hits") == 3


# --- スレッドごとの接続 --------------------------------------------------
# IDEAMAN_EMBED_WORKER=1 は Web のリクエスト処理とワーカーを同じプロセスで
# 動かす。接続を共有するとトランザクションまで共有してしまう。


def test_a_commit_in_another_thread_does_not_finish_my_write(store):
    """別スレッドの commit が、こちらの書きかけを確定させないこと。

    `rate()` は「評価を記録して重みを動かす」までで一つ。途中で確定される
    と、評価だけ残って重みが動いていない状態がディスクに残りうる。
    """
    import sqlite3
    import threading

    from ideaman import service

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙"])
    idea = service.generate_idea(store, use_llm=False)

    # 評価の途中まで（ratings は入れたが重みはまだ、commit もまだ）
    store.conn.execute(
        "INSERT INTO ratings (idea_id, score, created_at) "
        "VALUES (?, 1, datetime('now'))",
        (idea.id,),
    )

    def other_thread() -> None:
        store.conn.execute("SELECT 1")
        store.conn.commit()

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()

    outside = sqlite3.connect(store.path)
    try:
        count = outside.execute(
            "SELECT COUNT(*) FROM ratings WHERE idea_id = ?", (idea.id,)
        ).fetchone()[0]
    finally:
        outside.close()
    assert count == 0, "commit していない書き込みが確定している"


def test_each_thread_gets_its_own_connection(store):
    import threading

    seen = []
    seen.append(id(store.conn))

    def grab() -> None:
        seen.append(id(store.conn))

    thread = threading.Thread(target=grab)
    thread.start()
    thread.join()
    assert seen[0] != seen[1]


def test_threads_can_write_at_the_same_time(store):
    """Web とワーカーの同居で、書き込みがぶつかっても落ちないこと。"""
    import threading

    from ideaman import service

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖"])
    errors: list[BaseException] = []

    def churn() -> None:
        try:
            for _ in range(15):
                service.generate_idea(store, use_llm=False)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=churn) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert store.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] >= 45


def test_an_old_thought_state_gains_the_mood_column(tmp_path):
    """気分の列が無い DB を開いても壊れないこと。"""
    import sqlite3

    from ideaman.store import Store

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE thought_state ("
        " id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT NOT NULL DEFAULT 'wander',"
        " chain_id INTEGER, chain_length INTEGER NOT NULL DEFAULT 0,"
        " stale_hits INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);"
        "INSERT INTO thought_state (id, mode, updated_at)"
        " VALUES (1, 'associate', '2026-01-01T00:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        state = store.thought_state()
        assert state.mode == "associate"
        assert state.mood == ""
    finally:
        store.close()
