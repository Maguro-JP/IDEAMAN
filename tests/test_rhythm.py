"""思考のリズムのテスト。

見ているのは「多様性が高いか」ではなく「連鎖して、行き詰まって、離れて、
戻ってくる」が起きているか。
"""

from datetime import UTC, datetime, timedelta

import pytest

from ideaman import service
from ideaman.engine import rhythm
from ideaman.engine.combinator import combine
from ideaman.store import Store

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


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
    # 眠ったばかり（まだ1本も連鎖を挟んでいない）
    assert rhythm.draw_weight(1.0, NOW + timedelta(hours=1), chains_since=0, now=NOW) == 0.0


def test_aspiration_lets_strong_terms_through_while_dormant():
    """休眠中でも十分に良いものは通す（連続がダメなのではない）。"""
    strong = rhythm.ASPIRATION_WEIGHT + 0.5
    assert (
        rhythm.draw_weight(strong, NOW + timedelta(hours=1), chains_since=0, now=NOW)
        == strong
    )


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


# --- 気分は連鎖に属する --------------------------------------------------
# 気分はもともとアイデアごとにランダムに引かれ、文章化には一切使われて
# いなかった。連鎖の中で 82% の確率で変わっていて、一手ごとに提案と問いが
# 入れ替わるので、連鎖ではなく交替として読めていた。


def test_tone_for_pulls_towards_the_moods_register():
    assert rhythm.tone_for("挑発", 0.0) == "question"
    assert rhythm.tone_for("実務", 0.0) == "proposal"


def test_tone_for_does_not_force():
    """毎回従うと連鎖が一本調子になる。"""
    assert rhythm.tone_for("挑発", 0.99) is None


def test_moods_without_a_register_leave_the_tone_free():
    assert rhythm.tone_for("静穏", 0.0) is None
    assert rhythm.tone_for("", 0.0) is None
    assert rhythm.tone_for("知らない気分", 0.0) is None


def test_thought_state_remembers_the_mood(store):
    state = rhythm.ThoughtState(mode=rhythm.ASSOCIATE, chain_id=3, mood="実務")
    store.save_thought_state(state)
    assert store.thought_state().mood == "実務"


def test_a_chain_keeps_one_mood(store):
    """一つの思考の流れは一つの調子で続くこと。"""
    from ideaman import service

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫", "評価"])
    for _ in range(150):
        service.generate_idea(store, use_llm=False)

    chains: dict[int, list] = {}
    for idea in sorted(store.search(None, limit=500), key=lambda i: i.id):
        if idea.chain_id:
            chains.setdefault(idea.chain_id, []).append(idea)

    long = [v for v in chains.values() if len(v) >= 4]
    assert long, "長い連鎖が一本もできていない"
    steps = sum(len(v) - 1 for v in long)
    flips = sum(sum(1 for a, b in zip(v, v[1:]) if a.mood != b.mood) for v in long)
    assert flips / steps < 0.35, f"連鎖の中で気分が変わりすぎ: {flips / steps:.0%}"


def test_thinking_hard_does_not_put_the_whole_vocabulary_to_sleep(tmp_path):
    """回し続けても、引ける語が残っていること。

    休眠を時間だけで測っていたときは、150 件生成した時点で**材料が
    10語中10語とも眠っていた**。「あなたの材料 × 遠い概念」がこの
    システムの有益さなので、材料が全部眠るのは考えていないのと同じ。
    実際に動かして初めて出た（ワーカーは在庫を埋めるために数分で
    何十本も連鎖を終わらせる）。

    **最初はここで「半分以上が起きていること」を見ていて、CI で落ちた。**
    手元で 60 回まわすと起きている語は 4〜8（8語中）に散らばり、CI が
    引いたのは 3。分布の端に線を引いていた。守りたいのは「枯れない」
    ことであって「毎回半分」ではないので、複数回まわして下限を見る。
    """
    from ideaman import service
    from ideaman.store import Store

    materials = ["通知", "語彙", "連鎖", "在庫", "休眠", "評価", "材料", "配信"]
    awake_counts = []
    for trial in range(3):
        store = Store(tmp_path / f"sleep{trial}.db")
        try:
            theme = store.add_theme("t")
            store.add_materials(theme.id, materials)
            for _ in range(150):
                service.generate_idea(store, use_llm=False)
            drawable = store.draw_weights()
            awake_counts.append(
                sum(1 for term in materials if drawable.get(term, 1.0) > 0.0)
            )
        finally:
            store.close()

    assert min(awake_counts) >= 2, f"材料が枯れた: {awake_counts}"
    assert sum(awake_counts) / len(awake_counts) >= 3, awake_counts


def test_dormancy_ends_by_time_even_without_further_thinking():
    """考えていないあいだは、時計のほうで明けること。

    連鎖の本数だけで測ると、ゆっくり使う人の語は戻ってこない。
    """
    from datetime import UTC, datetime, timedelta

    from ideaman.engine import rhythm

    slept = datetime(2026, 1, 1, tzinfo=UTC)
    until = rhythm.dormancy_for(0, now=slept)
    assert not rhythm.is_awake(until, 0, 0, now=slept + timedelta(minutes=1))
    assert rhythm.is_awake(until, 0, 0, now=until + timedelta(seconds=1))
    # 時計はまだでも、ほかの連鎖を十分に挟めば戻る。
    assert rhythm.is_awake(
        until, 0, rhythm.DORMANCY_CHAINS, now=slept + timedelta(minutes=1)
    )


def test_terms_slept_before_the_column_existed_are_not_held_back():
    """移行した DB を、直したはずの振る舞いのまま数時間動かさない。"""
    from datetime import UTC, datetime, timedelta

    from ideaman.engine import rhythm

    slept = datetime(2026, 1, 1, tzinfo=UTC)
    until = rhythm.dormancy_for(0, now=slept)
    assert rhythm.is_awake(until, 0, None, now=slept + timedelta(minutes=1))


def test_what_total_dormancy_costs(tmp_path):
    """全部眠るとどうなるかを、数字で残しておく。

    「材料が眠る」の何が悪いのかを最初は取り違えていた。材料そのものは
    出力に入り続ける（スロットは必ず埋まる）。壊れるのは**抽選**のほう:

    - 好みが控えめなら、重みが全部 0 になって一様抽選に落ちる。
      評価で育てたものが丸ごと効かなくなる
    - 好みが強い語が1つあると、aspiration でそれだけが生き残り、
      **その語が100%引かれる**。避けたかった「同じ文が並ぶ」状態

    どちらもエラーにならない。だから数字で置いておく。
    """
    import collections
    import random

    from ideaman.engine.combinator import Term, _weighted_choice
    from ideaman.store import Store

    store = Store(tmp_path / "cost.db")
    try:
        theme = store.add_theme("t")
        materials = ["通知", "語彙", "連鎖", "在庫", "休眠", "評価"]
        store.add_materials(theme.id, materials)
        for _ in range(12):
            store._bump("materials", "通知", 1.15)
        store.conn.commit()

        terms = tuple(Term(term=m, domains=()) for m in materials)

        def favourite_share() -> float:
            weights = store.draw_weights()
            rng = random.Random(0)
            drawn = collections.Counter(
                _weighted_choice(rng, terms, weights).term for _ in range(2000)
            )
            return drawn["通知"] / 2000

        awake = favourite_share()
        assert 0.2 < awake < 0.8, awake

        # 連鎖の記録を消して「時間でしか明けない」状態を作る（修正前の姿）。
        store.sleep_terms([("materials", m) for m in materials])
        store.conn.execute("UPDATE weights SET slept_at_chain = 0")
        store.conn.commit()
        assert favourite_share() == 1.0
    finally:
        store.close()


def test_the_revival_boost_does_not_stay_on_forever():
    """連鎖で明けた語の下駄は、明けた直後だけ。

    時計が来るまで乗せ続けると、その語だけが延々と目立つ。実際に
    動かしたとき、一つの側面が14件中6件に出た。
    """
    from datetime import UTC, datetime, timedelta

    from ideaman.engine import rhythm

    slept = datetime(2026, 1, 1, tzinfo=UTC)
    until = rhythm.dormancy_for(0, now=slept)
    soon = slept + timedelta(minutes=1)
    just_woke = rhythm.DORMANCY_CHAINS

    assert rhythm.draw_weight(1.0, until, chains_since=just_woke, now=soon) > 1.0
    later = just_woke + rhythm.REVIVAL_WINDOW_CHAINS + 1
    assert rhythm.draw_weight(1.0, until, chains_since=later, now=soon) == 1.0
