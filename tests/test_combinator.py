from ideaman.engine import templates
from ideaman.engine.combinator import SLOT_TO_POOL, _distance, combine
from ideaman.engine.lexicon import Term, default_lexicon


def test_same_seed_is_deterministic():
    a = combine(seed=12345)
    b = combine(seed=12345)
    assert a.slots == b.slots
    assert a.template_id == b.template_id
    assert a.mood == b.mood


def test_different_seeds_diverge():
    seeds = {tuple(sorted(combine(seed=i).slots.items())) for i in range(30)}
    assert len(seeds) > 20, "同じ組ばかり出ている"


def test_every_template_slot_is_filled():
    lexicon = default_lexicon()
    for i in range(100):
        seed = combine(seed=i, lexicon=lexicon)
        template = next(t for t in lexicon.templates if t.id == seed.template_id)
        assert set(seed.slots) == set(template.needs)
        # 文章化がプレースホルダ不足で落ちないこと
        assert templates.render(seed, lexicon)


def test_two_concepts_are_never_identical():
    for i in range(100):
        seed = combine(seed=i)
        if "concept_a" in seed.slots and "concept_b" in seed.slots:
            assert seed.slots["concept_a"] != seed.slots["concept_b"]


def test_terms_record_their_pool():
    seed = combine(seed=7)
    for pool, term in seed.terms:
        assert pool in SLOT_TO_POOL.values()
        assert term in {t.term for t in default_lexicon().pool(pool)}


def test_distance_prefers_unrelated_domains():
    near = _distance(Term("A", ("city",)), Term("B", ("city",)))
    far = _distance(Term("A", ("city",)), Term("B", ("nature",)))
    assert far > near


def test_weights_shift_the_draw():
    """重みを極端に振ると、その語が明確に出やすくなる。"""
    lexicon = default_lexicon()
    favored = lexicon.pool("operations")[0].term
    weights = {t.term: 0.01 for t in lexicon.pool("operations")}
    weights[favored] = 100.0

    hits = 0
    total = 0
    for i in range(200):
        seed = combine(seed=i, weights=weights, lexicon=lexicon)
        if "operation" in seed.slots:
            total += 1
            hits += seed.slots["operation"] == favored

    assert total > 0, "operation を使うテンプレが一度も選ばれていない"
    assert hits / total > 0.8


def test_domain_filter_biases_concepts():
    lexicon = default_lexicon()
    tagged = {t.term for t in lexicon.pool("concepts") if "nature" in t.domains}

    used = set()
    for i in range(60):
        seed = combine(seed=i, domain="nature", lexicon=lexicon)
        used |= {v for k, v in seed.slots.items() if k.startswith("concept")}

    assert used, "concept が一つも使われていない"
    assert used <= tagged, f"分野外の語が混ざった: {used - tagged}"


def test_unknown_domain_falls_back_instead_of_erroring():
    # 該当語が2語未満なら絞らない、という設計どおりの挙動
    seed = combine(seed=1, domain="この分野は存在しない")
    assert seed.slots
    assert seed.domain == "この分野は存在しない"


def test_tone_filter():
    for i in range(20):
        assert combine(seed=i, tone="question").tone == "question"
