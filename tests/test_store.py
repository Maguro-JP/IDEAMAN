import pytest

from ideaman import service
from ideaman.engine.combinator import combine
from ideaman.store import MAX_WEIGHT, MIN_WEIGHT, Store


def _save(store: Store, seed: int = 1):
    idea_seed = combine(seed=seed)
    return idea_seed, store.save(idea_seed, "てすと", "engine")


def test_saved_idea_starts_unseen(store):
    _, idea = _save(store)
    assert idea.state == "unseen"
    assert store.stock_count() == 1
    # 未閲覧のものは一覧に出ない
    assert store.list_seen() == []


def test_upvote_raises_weights_of_its_terms(store):
    idea_seed, idea = _save(store)
    store.rate(idea.id, 1)

    weights = store.weights()
    for _, term in idea_seed.terms:
        assert weights[term] > 1.0


def test_downvote_lowers_weights(store):
    idea_seed, idea = _save(store)
    store.rate(idea.id, -1)

    weights = store.weights()
    for _, term in idea_seed.terms:
        assert weights[term] < 1.0


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
    assert store.stock_count() == 2
