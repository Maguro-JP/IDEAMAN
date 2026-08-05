"""リズムの健康度の計測が正しく数えられているか。

指標そのものの「良し悪し」は測らない（重複ゼロが正解ではないので）。
数え方が合っているかだけを見る。
"""

from ideaman import service
from ideaman.diversity import measure
from ideaman.engine import rhythm
from ideaman.engine.combinator import combine


def test_empty_store_is_measurable(store):
    health = measure(store)
    assert health.total == 0
    assert health.report()  # 落ちずにレポートが出る


def test_counts_ideas_and_chains(store):
    for _ in range(20):
        service.generate_idea(store, use_llm=False)

    health = measure(store)
    assert health.total == 20
    assert health.chains >= 1
    assert sum(
        length * count for length, count in health.chain_lengths.items()
    ) == 20


def test_chain_lengths_are_not_all_one(store):
    """全部1なら連想が働いていない。実際に回して確かめる。"""
    for _ in range(40):
        service.generate_idea(store, use_llm=False)

    health = measure(store)
    assert health.longest_chain > 1
    assert health.mean_chain_length > 1.0


def test_modes_are_both_used(store):
    for _ in range(50):
        service.generate_idea(store, use_llm=False)

    health = measure(store)
    assert health.mode_counts["associate"] > 0, "連想が一度も起きていない"
    assert health.mode_counts["wander"] > 0, "放浪が一度も起きていない"


def test_coverage_grows_with_more_ideas(store):
    for _ in range(5):
        service.generate_idea(store, use_llm=False)
    few = measure(store).coverage

    for _ in range(45):
        service.generate_idea(store, use_llm=False)
    many = measure(store).coverage

    assert many > few
    assert 0.0 < many <= 1.0


def test_duplicate_counter_is_reported(store):
    store.bump_counter("duplicate_hits", 3)
    assert measure(store).duplicate_hits == 3


def test_dormant_terms_are_counted(store):
    service.generate_idea(store, use_llm=False)
    seed = combine(seed=11)
    store.sleep_terms(seed.terms)

    health = measure(store)
    assert health.dormant_now > 0


def test_aspiration_terms_are_not_counted_as_dormant(store):
    """休眠中でも通る語（十分に良いもの）は「眠っている」に数えない。"""
    service.generate_idea(store, use_llm=False)
    seed = combine(seed=12)
    store.sleep_terms(seed.terms)

    pool, term = seed.terms[0]
    store.conn.execute(
        "UPDATE weights SET weight = ? WHERE pool = ? AND term = ?",
        (rhythm.ASPIRATION_WEIGHT + 1, pool, term),
    )
    store.conn.commit()

    assert term not in store.dormant_terms()


def test_report_is_readable(store):
    for _ in range(10):
        service.generate_idea(store, use_llm=False)
    report = measure(store).report()
    assert "連鎖の長さ" in report
    assert "読み方" in report
