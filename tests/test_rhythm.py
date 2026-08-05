"""思考のリズムのテスト。

見ているのは「多様性が高いか」ではなく「連鎖して、行き詰まって、離れて、
戻ってくる」が起きているか。
"""

from datetime import datetime, timedelta, timezone

import pytest

from ideaman import service
from ideaman.engine import rhythm
from ideaman.engine.combinator import combine
from ideaman.store import Store

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


# --- モードの切り替え ------------------------------------------------


def test_starts_by_wandering():
    assert rhythm.ThoughtState().mode == rhythm.WANDER


def test_associating_inherits_terms():
    state = rhythm.ThoughtState(mode=rhythm.ASSOCIATE, chain_id=1, chain_length=1)
    step = rhythm.plan_step(state)
    assert step.mode == rhythm.ASSOCIATE
    assert step.inherit >= 1


def test_inheritance_narrows_as_the_chain_grows():
    """連鎖が進むほど引き継ぎを減らし、少しずつ話題をずらす。"""
    early = rhythm.plan_step(rhythm.ThoughtState(rhythm.ASSOCIATE, 1, 1)).inherit
    late = rhythm.plan_step(rhythm.ThoughtState(rhythm.ASSOCIATE, 1, 5)).inherit
    assert early > late


def test_chain_ends_at_the_length_limit():
    state = rhythm.ThoughtState(
        mode=rhythm.ASSOCIATE, chain_id=1, chain_length=rhythm.MAX_CHAIN_LENGTH
    )
    assert rhythm.plan_step(state).mode == rhythm.WANDER


def test_a_good_draw_starts_a_chain():
    state = rhythm.after_hit(
        rhythm.ThoughtState(), chain_id=9, best_weight=3.0, novelty=1.0, roll=0.0
    )
    assert state.mode == rhythm.ASSOCIATE
    assert state.chain_id == 9


def test_a_dull_draw_keeps_wandering():
    state = rhythm.after_hit(
        rhythm.ThoughtState(), chain_id=9, best_weight=1.0, novelty=0.0, roll=0.99
    )
    assert state.mode == rhythm.WANDER


def test_chains_can_start_before_any_rating_exists():
    """立ち上げ直後（重み全部 1.0）でも連鎖が始まりうる。

    ここがゼロだと、評価が付くまで永久に放浪し続けてしまう。
    """
    assert rhythm.spark_chance(best_weight=1.0, novelty=0.0) > 0


def test_striking_draws_are_more_likely_to_spark():
    dull = rhythm.spark_chance(best_weight=1.0, novelty=0.0)
    striking = rhythm.spark_chance(best_weight=3.0, novelty=1.0)
    assert striking > dull


# --- 行き詰まりと休眠 ------------------------------------------------


def test_a_single_duplicate_does_not_break_the_chain():
    """一度の重複で連鎖を切らない。粘る。"""
    state = rhythm.ThoughtState(mode=rhythm.ASSOCIATE, chain_id=1, chain_length=2)
    state = rhythm.after_duplicate(state)
    assert not rhythm.is_exhausted(state)


def test_repeated_duplicates_exhaust_the_chain():
    state = rhythm.ThoughtState(mode=rhythm.ASSOCIATE, chain_id=1, chain_length=2)
    for _ in range(rhythm.STALE_LIMIT):
        state = rhythm.after_duplicate(state)
    assert rhythm.is_exhausted(state)


def test_wandering_is_never_exhausted():
    """放浪中は「尽きる」概念がない（掘っていないので）。"""
    state = rhythm.ThoughtState(mode=rhythm.WANDER)
    for _ in range(10):
        state = rhythm.after_duplicate(state)
    assert not rhythm.is_exhausted(state)


def test_a_new_hit_resets_the_stall_count():
    state = rhythm.ThoughtState(mode=rhythm.ASSOCIATE, chain_id=1, chain_length=1)
    state = rhythm.after_duplicate(state)
    state = rhythm.after_hit(state, chain_id=1, best_weight=1.0)
    assert state.stale_hits == 0


def test_leaving_a_chain_returns_to_wandering():
    state = rhythm.leave_chain(
        rhythm.ThoughtState(mode=rhythm.ASSOCIATE, chain_id=1, chain_length=5)
    )
    assert state.mode == rhythm.WANDER
    assert state.chain_id is None


# --- 休眠と復帰 ------------------------------------------------------


def test_dormancy_grows_with_repeated_stalls():
    once = rhythm.dormancy_for(1, now=NOW)
    twice = rhythm.dormancy_for(2, now=NOW)
    assert twice > once


def test_dormancy_is_capped():
    """何度行き詰まっても、永久に消えることはない。"""
    forever = rhythm.dormancy_for(99, now=NOW)
    assert forever <= NOW + rhythm.MAX_DORMANCY


def test_dormant_terms_drop_out_of_the_draw():
    assert rhythm.draw_weight(1.0, NOW + timedelta(hours=1), now=NOW) == 0.0


def test_aspiration_lets_strong_terms_through_while_dormant():
    """休眠中でも十分に良いものは通す（連続がダメなのではない）。"""
    strong = rhythm.ASPIRATION_WEIGHT + 0.5
    assert rhythm.draw_weight(strong, NOW + timedelta(hours=1), now=NOW) == strong


def test_terms_come_back_with_a_boost():
    """休眠明け直後は少し引きやすい — 「ふとしたときに思いつく」。"""
    woke = NOW - timedelta(minutes=5)
    assert rhythm.draw_weight(1.0, woke, now=NOW) > 1.0


def test_the_boost_fades():
    long_ago = NOW - rhythm.REVIVAL_WINDOW - timedelta(hours=1)
    assert rhythm.draw_weight(1.0, long_ago, now=NOW) == 1.0


def test_awake_terms_are_unaffected():
    assert rhythm.draw_weight(1.7, None, now=NOW) == 1.7


# --- 実際に回したときの挙動 ------------------------------------------


def test_generation_actually_forms_chains(store):
    """40件生成して、連鎖が実際に伸びること。

    全部が長さ1なら連想が働いていない＝ただの乱数。
    """
    for _ in range(40):
        service.generate_idea(store, use_llm=False)

    lengths = [len(store.chain(row["id"])) for row in
               store.conn.execute("SELECT id FROM ideas").fetchall()]
    assert max(lengths) > 1, "連鎖が一度も伸びていない"


def test_chained_ideas_share_vocabulary(store):
    """連鎖しているアイデア同士は語を共有している（＝繋がっている）。"""
    for _ in range(40):
        service.generate_idea(store, use_llm=False)

    rows = store.conn.execute(
        "SELECT id, parent_id FROM ideas WHERE parent_id IS NOT NULL"
    ).fetchall()
    assert rows, "連想モードのアイデアが一件も無い"

    for row in rows:
        child = {t for _, t in store.terms_of(row["id"])}
        parent = {t for _, t in store.terms_of(row["parent_id"])}
        assert child & parent, f"{row['id']} は親と語を共有していない"


def test_chain_records_its_parent(store):
    for _ in range(30):
        service.generate_idea(store, use_llm=False)
    for idea in store.search(limit=100):
        if idea.mode == "associate":
            assert idea.parent_id is not None
            break
    else:
        pytest.skip("この試行では連想が起きなかった")


def test_duplicates_reinforce_instead_of_being_discarded(store):
    """同じ組み合わせに再到達したら、出力はしないが重みは上がる。"""
    seed = combine(seed=4242)
    store.save(seed, "既にあるもの", "engine")

    before = store.weights()
    store.reinforce(seed.terms, 1.03)
    after = store.weights()

    for _, term in seed.terms:
        assert after[term] > before.get(term, 1.0)


def test_thought_state_survives_a_restart(tmp_path):
    """プロセスが落ちても、続きから考えられる。"""
    path = tmp_path / "state.db"
    store = Store(path)
    store.save_thought_state(
        rhythm.ThoughtState(mode=rhythm.ASSOCIATE, chain_id=7, chain_length=3)
    )
    store.close()

    reopened = Store(path)
    state = reopened.thought_state()
    assert state.mode == rhythm.ASSOCIATE
    assert state.chain_id == 7
    assert state.chain_length == 3
    reopened.close()


def test_exhausted_chains_put_terms_to_sleep(store):
    """行き詰まったら語が休眠する。ただし DB からは消えない。"""
    seed = combine(seed=1)
    store.sleep_terms(seed.terms)

    dormant = set(store.dormant_terms())
    assert dormant, "休眠した語が無い"
    # 消えてはいない
    assert set(store.weights()) >= {t for _, t in seed.terms}


def test_sleeping_removes_terms_from_the_draw(store):
    seed = combine(seed=1)
    store.sleep_terms(seed.terms)
    weights = store.draw_weights()
    for _, term in seed.terms:
        assert weights[term] == 0.0
