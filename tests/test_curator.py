"""選別のテスト。

考えている間は独り言。連鎖が閉じてから、良さそうなものを一つ二つだけ出す。
選ばれなかったものは捨てない。
"""

import pytest

from ideaman import curator, service
from ideaman.engine.combinator import combine


def _chain(store, texts, chain_id=None):
    """同じ連鎖に属するアイデアを作る。"""
    ideas = []
    parent = None
    for i, text in enumerate(texts):
        idea = store.save(
            combine(seed=100 + i),
            text,
            "engine",
            parent_id=parent.id if parent else None,
            chain_id=chain_id or (parent.chain_id if parent else None),
            mode="associate" if parent else "wander",
        )
        ideas.append(idea)
        parent = idea
    return ideas


# --- 選ぶ数 ----------------------------------------------------------


def test_short_chains_yield_one():
    assert curator.pick_count(1) == 1
    assert curator.pick_count(3) == 1


def test_long_chains_yield_two():
    assert curator.pick_count(4) == 2
    assert curator.pick_count(7) == 2


def test_curate_picks_two_from_a_long_chain(store):
    chain = _chain(store, [f"考え{i}" for i in range(7)])
    chosen = curator.curate(store, chain, use_llm=False)
    assert len(chosen) == 2


def test_everything_gets_a_verdict(store):
    """判定漏れがあると、そのアイデアは永久に配信されない。"""
    chain = _chain(store, [f"考え{i}" for i in range(7)])
    curator.curate(store, chain, use_llm=False)

    for idea in chain:
        assert store.get(idea.id).verdict in ("chosen", "kept")
    assert store.unjudged() == []


def test_unchosen_ideas_are_kept_not_deleted(store):
    """選ばれなくても捨てない。"""
    chain = _chain(store, [f"考え{i}" for i in range(7)])
    curator.curate(store, chain, use_llm=False)

    kept = [i for i in chain if store.get(i.id).verdict == "kept"]
    assert len(kept) == 5
    for idea in kept:
        assert store.get(idea.id).text  # 残っている


def test_curating_twice_is_a_no_op(store):
    chain = _chain(store, [f"考え{i}" for i in range(5)])
    assert curator.curate(store, chain, use_llm=False)
    # 既に判定済みなら何も選ばない（二重配信を防ぐ）
    assert curator.curate(store, store.chain(chain[0].id), use_llm=False) == []


# --- 単発の思いつき --------------------------------------------------


def test_a_lone_thought_must_clear_the_bar(store):
    """連鎖にならなかった1件は、何とも比べていないので基準を課す。"""
    # 履歴を作る（平均 0.8）
    for i in range(10):
        idea = store.save(combine(seed=200 + i), f"過去{i}", "engine")
        store.set_verdict(idea.id, "kept", 0.8)

    weak = _chain(store, ["弱い思いつき"])
    monkey = curator.score
    curator.score = lambda s, i: 0.1  # noqa: E731
    try:
        assert curator.curate(store, weak, use_llm=False) == []
        assert store.get(weak[0].id).verdict == "kept"
    finally:
        curator.score = monkey


def test_a_strong_lone_thought_gets_through(store):
    for i in range(10):
        idea = store.save(combine(seed=300 + i), f"過去{i}", "engine")
        store.set_verdict(idea.id, "kept", 0.2)

    strong = _chain(store, ["強い思いつき"])
    monkey = curator.score
    curator.score = lambda s, i: 0.9  # noqa: E731
    try:
        assert len(curator.curate(store, strong, use_llm=False)) == 1
    finally:
        curator.score = monkey


def test_solo_bar_uses_recent_average(store):
    assert curator.solo_bar(store) == curator.SOLO_COLD_START

    for i, value in enumerate([0.2, 0.4, 0.6]):
        idea = store.save(combine(seed=400 + i), f"過去{i}", "engine")
        store.set_verdict(idea.id, "kept", value)

    assert curator.solo_bar(store) == pytest.approx(0.4)


# --- スコア ----------------------------------------------------------


def test_score_is_bounded(store):
    for i in range(20):
        idea = service.generate_idea(store, use_llm=False)
        assert 0.0 <= curator.score(store, idea) <= 1.0


def test_developed_ideas_score_higher_than_bare_ones(store):
    """何かの上に乗っているものを、単なる思いつきより優先する。"""
    chain = _chain(store, ["親", "子"])
    bare, developed = chain[0], chain[1]
    # 語の違いを消して、親の有無だけを比べる
    monkey = store.novelty_of
    store.novelty_of = lambda _id: 0.5  # noqa: E731
    try:
        assert curator.score(store, developed) > curator.score(store, bare)
    finally:
        store.novelty_of = monkey


def test_rated_terms_raise_the_score(store):
    idea = service.generate_idea(store, use_llm=False)
    before = curator.score(store, idea)
    for _ in range(10):
        store.rate(idea.id, 1)
    assert curator.score(store, store.get(idea.id)) > before


# --- 生成との繋がり --------------------------------------------------


def test_generation_closes_chains_so_nothing_is_stranded(store):
    """考えたものが未判定のまま溜まり続けないこと。

    放浪1件（連鎖にならなかったもの）を閉じ忘れると、永久に配信されない
    アイデアが積み上がる。
    """
    for _ in range(50):
        service.generate_idea(store, use_llm=False)

    # 未判定は「いま考えている最中の連鎖」だけのはず
    assert len(store.unjudged()) <= 7


def test_only_a_minority_gets_published(store):
    """出すのは一部だけ。溢れるアイデアでウザくならないこと。"""
    for _ in range(100):
        service.generate_idea(store, use_llm=False)

    total = store.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]
    chosen = store.conn.execute(
        "SELECT COUNT(*) FROM ideas WHERE verdict = 'chosen'"
    ).fetchone()[0]
    assert 0.1 < chosen / total < 0.45, f"出す割合が {chosen / total:.0%}"


def test_curation_works_without_an_api_key(store):
    """LLM 無しでも選べる。"""
    chain = _chain(store, [f"考え{i}" for i in range(6)])
    assert curator.curate(store, chain, use_llm=True)


# --- 同じ連鎖から選ぶ2件が似ないこと ------------------------------------
# 連鎖は同じ主題を掘るので、兄弟が似るのは当たり前。だからこそ出す側で
# 似ていないものを選ぶ。一度に採点して上位を取ると、「いちばんヘビーに
# 使う一人のためだけに語彙を作り直す」が側面違いで2件並んだ。


def test_two_picks_from_one_chain_are_not_the_same_shape(store):
    from ideaman import service

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫", "評価", "休眠"])
    for _ in range(250):
        service.generate_idea(store, use_llm=False)

    chains: dict[int, list] = {}
    for idea in sorted(store.search(None, limit=800), key=lambda i: i.id):
        if idea.verdict == "chosen" and idea.chain_id:
            chains.setdefault(idea.chain_id, []).append(idea)

    pairs = [v for v in chains.values() if len(v) >= 2]
    assert len(pairs) >= 5, "2件選ばれた連鎖が少なすぎて判定できない"
    same = sum(1 for v in pairs if v[0].template_id == v[1].template_id)
    assert same / len(pairs) < 0.25, f"同じ文型が {same}/{len(pairs)} 本で並んでいる"


def test_picking_prefers_something_different_from_what_it_already_took(store):
    """1件目に似たものは、点数が高くても2件目に選ばれにくいこと。"""
    from ideaman import curator, service

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙"])
    for _ in range(40):
        service.generate_idea(store, use_llm=False)

    pending = [i for i in store.search(None, limit=200)][:6]
    assert len(pending) >= 3
    scores = {i.id: 0.5 for i in pending}
    # 同じ文型のものを2件用意し、点数を同じにしても両方は選ばれないこと
    twins = [i for i in pending if i.template_id == pending[0].template_id]
    if len(twins) >= 2:
        picked = curator._pick_distinct(store, pending, scores, 2)
        assert not (twins[0].id in picked and twins[1].id in picked)
