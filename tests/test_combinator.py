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
        # operation / operation_v はどちらも operations プールから引く
        for slot in ("operation", "operation_v"):
            if slot in seed.slots:
                total += 1
                hits += seed.slots[slot] == favored

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
    # 排他ではなく偏り。タグ付きが2〜3語しかない分野で絞り切ると、同じ語が
    # 繰り返されて分野特化ではなく故障に見えるため、足りない分は埋める。
    assert used & tagged, f"分野の語が一つも出ていない: {used}"
    share = len(used & tagged) / len(used)
    assert share > len(tagged) / len(lexicon.pool("concepts")), (
        f"分野を指定しても偏りが出ていない: {share:.0%}"
    )


def test_unknown_domain_falls_back_instead_of_erroring():
    # 該当語が2語未満なら絞らない、という設計どおりの挙動
    seed = combine(seed=1, domain="この分野は存在しない")
    assert seed.slots
    assert seed.domain == "この分野は存在しない"


def test_tone_filter():
    for i in range(20):
        assert combine(seed=i, tone="question").tone == "question"


# --- 分野の指定 ----------------------------------------------------------
# `?domain=` は API・CLI・MCP すべてで宣伝しているが、フィルタが concepts に
# しか掛かっていなかった。概念を使わないテンプレートが大半なので、指定しても
# 60件中16件しか触れず、しかも同じ3語の繰り返しだった。


def _tagged(domain: str) -> set[str]:
    from ideaman.engine.lexicon import POOL_NAMES, default_lexicon

    lexicon = default_lexicon()
    return {
        t.term
        for pool in POOL_NAMES
        for t in lexicon.pool(pool)
        if domain in t.domains
    }


def test_domain_reaches_every_pool_not_just_concepts():
    tagged = _tagged("time")
    assert tagged

    def share(domain):
        hits = 0
        for i in range(120):
            seed = combine(seed=i, domain=domain, materials=["通知", "語彙"])
            if any(term in tagged for _, term in seed.terms):
                hits += 1
        return hits / 120

    assert share("time") > share(None) + 0.15, "分野を指定しても偏りが変わらない"


def test_a_thin_domain_does_not_collapse_to_the_same_word():
    """候補が少ない分野では、絞らずに幅を優先する。

    2語しか残らない分野で同じ語を繰り返すと、分野特化ではなく故障に見える
    （実際「まず1日だけその運用にしてみる」が3件続いた）。
    """
    from ideaman.engine import templates
    from ideaman.engine.lexicon import default_lexicon

    lexicon = default_lexicon()
    probes = set()
    for i in range(120):
        seed = combine(seed=i, domain="analog", materials=["通知"])
        if "probe" in seed.slots:
            probes.add(seed.slots["probe"])
        templates.render(seed, lexicon)
    assert len(probes) >= 4, f"試し方が {probes} に固まっている"


def test_no_doubled_tokoro():
    """「特に一番遅いところのところ」を出さないこと。

    側面に「〜ところ」「〜部分」があるので、テンプレート側で「のところ」を
    付けると重なる。分野を絞ったときに集中して初めて目についた。
    """
    from ideaman.engine import templates
    from ideaman.engine.lexicon import default_lexicon

    lexicon = default_lexicon()
    for i in range(600):
        for materials in (["通知"], []):
            text = templates.render(combine(seed=i, materials=materials), lexicon)
            assert "ところのところ" not in text, text
            assert "部分のところ" not in text, text


# --- 機械的に見つかる文の傷 ----------------------------------------------
# 語彙を足すたびに再発しうるので、目で見るのではなく検出器で守る。
# 「直った」を差分で確認すると、条件が変わったとき静かに戻る。


#: 材料は**ユーザーの語彙**なので、語彙プールと衝突しうる。
#: 「通知」は材料にもなり、concepts にも入っている。
COLLIDING_MATERIALS = ["通知", "語彙", "連鎖", "在庫", "評価", "名前", "待ち時間", "忘却"]


def _samples(materials, count=1500):
    from ideaman.engine import templates
    from ideaman.engine.lexicon import default_lexicon

    lexicon = default_lexicon()
    for seed in range(count):
        idea_seed = combine(materials=materials, seed=seed)
        yield idea_seed, templates.render(idea_seed, lexicon)


def test_a_word_is_never_compared_with_itself():
    """「通知と通知は、例外の扱いだけ見れば同じではないか」を出さないこと。"""
    for seed, text in _samples(COLLIDING_MATERIALS):
        slots = seed.slots
        material = slots.get("material")
        if material:
            assert slots.get("concept_a") != material, text
            assert slots.get("concept_b") != material, text
            assert slots.get("aspect") != material, text
            assert slots.get("aspect_q") != material, text
        if slots.get("concept_a"):
            assert slots["concept_a"] != slots.get("concept_b"), text


def test_no_mechanical_smells_in_the_text():
    """助詞の重なりや句点の重複が無いこと。"""
    for _, text in _samples(COLLIDING_MATERIALS):
        for smell in ("のの", "をを", "はは", "。。", "、、", "ところのところ"):
            assert smell not in text, f"{smell}: {text}"
        assert not text.startswith("。")
        assert text.endswith("。")


def test_the_same_check_holds_without_a_theme():
    """テーマ未登録のフォールバックでも同じこと。"""
    for seed, text in _samples([], count=800):
        assert seed.slots.get("concept_a") != seed.slots.get("concept_b"), text
        for smell in ("のの", "をを", "。。"):
            assert smell not in text, text
