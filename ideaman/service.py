"""生成の唯一の入口。

Web からも、Bot からも、常駐ワーカーからも、MCP からもここを呼ぶ。ここを
通らない生成経路を作らないことが、後のフェーズを安く済ませる唯一のコツ。

思考のリズム（連想 → 行き詰まり → 休眠 → 放浪 → 復帰）の判断は
`engine/rhythm.py` にあり、ここはその判断と DB を繋ぐだけ。
"""

from __future__ import annotations

import json
import logging
import random

from . import curator
from .engine import rhythm, templates
from .engine.combinator import IdeaSeed, combine
from .engine.lexicon import Lexicon, default_lexicon
from .engine.refiner import refine
from .store import DUPLICATE_FACTOR, Idea, Store

log = logging.getLogger(__name__)

#: 起動時に確保しておく在庫の数。
DEFAULT_STOCK = 5

#: 重複を引いたときに引き直す回数の上限。
#: ここを大きくしても、鉱脈が尽きていれば結局重複するので粘りすぎない。
MAX_REDRAWS = 6

#: 在庫を1件得るために考える回数の上限。連鎖が閉じるまで選別は走らないので
#: 空回りしうる。無限に考え込まないための歯止め。
MAX_THINKING_STEPS = 20


def _draw(
    store: Store,
    *,
    lexicon: Lexicon,
    domain: str | None,
    tone: str | None,
    seed: int | None,
) -> tuple[IdeaSeed, rhythm.Step, Idea | None, int]:
    """リズムに従って種を引く。重複したら引き直す。

    Returns:
        `(種, 一手, 親アイデア, 重複した回数)`。重複しか出せなかった場合でも
        最後に引いた種を返す（呼び出し側が「尽きた」と判断できるように）。
    """
    state = store.thought_state()
    step = rhythm.plan_step(state)
    weights = store.draw_weights()

    parent: Idea | None = None
    inherit: tuple[tuple[str, str], ...] = ()
    if step.mode == rhythm.ASSOCIATE and state.chain_id is not None:
        chain = store.chain(state.chain_id)
        if chain:
            parent = chain[-1]
            inherit = store.terms_of(parent.id)

    if not inherit:
        # 連想したかったが親が見つからない（初回など）。素直に放浪する。
        step = rhythm.Step(mode=rhythm.WANDER, inherit=0, state=step.state)

    duplicates = 0
    idea_seed = None
    for attempt in range(MAX_REDRAWS + 1):
        idea_seed = combine(
            lexicon=lexicon,
            weights=weights,
            domain=domain,
            tone=tone,
            seed=seed if attempt == 0 else None,
            inherit=inherit,
            inherit_count=step.inherit,
        )
        if not store.signature_exists(idea_seed.signature):
            break
        # 同じ場所に戻ってきた。出力はしないが、信号としては拾う。
        store.reinforce(idea_seed.terms, DUPLICATE_FACTOR)
        store.bump_counter("duplicate_hits")
        duplicates += 1

    assert idea_seed is not None
    return idea_seed, step, parent, duplicates


def generate_idea(
    store: Store,
    *,
    domain: str | None = None,
    tone: str | None = None,
    seed: int | None = None,
    use_llm: bool = True,
    lexicon: Lexicon | None = None,
) -> Idea:
    """アイデアを1つ考えて蓄える。

    保存された時点では `unseen`（＝溜まっている考え）。誰かが取り出すまで
    表には出ない。

    連想中なら直前のアイデアの語を引き継ぎ、鉱脈が尽きていればその語を
    休眠させて放浪に戻る。
    """
    lexicon = lexicon or default_lexicon()
    idea_seed, step, parent, duplicates = _draw(
        store, lexicon=lexicon, domain=domain, tone=tone, seed=seed
    )

    # ここでは磨かない。連鎖7件のうち5〜6件は表に出ないので、出すと決まって
    # から磨く（`_close_chain`）。API 呼び出しが 1/4 以下になる。
    text = templates.render(idea_seed, lexicon)

    state = store.thought_state()
    for _ in range(duplicates):
        state = rhythm.after_duplicate(state)

    chain_id = state.chain_id if step.mode == rhythm.ASSOCIATE else None
    idea = store.save(
        idea_seed,
        text,
        "engine",
        parent_id=parent.id if step.mode == rhythm.ASSOCIATE and parent else None,
        chain_id=chain_id,
        mode=step.mode,
    )

    weights = store.weights()
    best = max((weights.get(term, 1.0) for _, term in idea_seed.terms), default=1.0)
    state = rhythm.after_hit(
        state,
        chain_id=idea.chain_id or idea.id,
        best_weight=best,
        novelty=idea_seed.novelty,
        roll=random.random(),
    )

    if rhythm.is_exhausted(state):
        # 掘り尽くした。中心にあった語をしばらく寝かせて、別のことを考える。
        # 消すのではなく、時間が来れば戻ってくる。
        store.sleep_terms(idea_seed.terms)
        log.info(
            "連鎖 %s が尽きたので休眠に入る（長さ %d, 重複 %d）",
            state.chain_id,
            state.chain_length,
            state.stale_hits,
        )
        _close_chain(store, idea.id, use_llm=use_llm)
        state = rhythm.leave_chain(state)
    elif state.mode == rhythm.WANDER:
        # 引きが spark せず、この1件で完結した。連想の連鎖にはならなかった
        # ので、ここで閉じる。**これを忘れると永久に判定されず、一生
        # 配信されないアイデアが溜まっていく。**
        _close_chain(store, idea.id, use_llm=use_llm)

    store.save_thought_state(state)
    return idea


def _close_chain(store: Store, idea_id: int, *, use_llm: bool = True) -> list[Idea]:
    """閉じた連鎖を判定し、選ばれたものだけを磨く。

    ここが「独り言」と「発言」の境目。判定されるまで、どのアイデアも
    外には出ない。
    """
    chain = store.chain(idea_id)
    chosen = curator.curate(store, chain, use_llm=use_llm)

    if use_llm:
        lexicon = default_lexicon()
        for idea in chosen:
            seed = _seed_of(store, idea, lexicon)
            text, source = refine(seed, idea.text)
            if source == "llm":
                store.update_text(idea.id, text, source)
    return chosen


def _seed_of(store: Store, idea: Idea, lexicon: Lexicon) -> IdeaSeed:
    """保存されたアイデアから、磨くのに必要な種を組み立て直す。"""
    raw = json.loads(
        store.conn.execute(
            "SELECT seed_json FROM ideas WHERE id = ?", (idea.id,)
        ).fetchone()["seed_json"]
    )
    return IdeaSeed(
        template_id="",
        tone=idea.tone,
        slots=raw["slots"],
        domain=idea.domain,
        mood=idea.mood,
        seed=raw["seed"],
        terms=tuple(tuple(t) for t in raw["terms"]),  # type: ignore[misc]
        novelty=raw.get("novelty", 0.0),
    )


def receive_idea(
    store: Store,
    *,
    domain: str | None = None,
    use_llm: bool = True,
) -> Idea:
    """溜まっている考えを1つ受け取る。

    在庫があればそれを返し（＝すでに考えてあったもの）、空のときだけその場で
    考える。返るのは**選ばれたものだけ**なので、空のときは連鎖が閉じるまで
    考え続ける必要がある（1件生成しただけでは選別が走らない）。
    """
    idea = store.take_ready(domain=domain)
    if idea is not None:
        return idea

    for _ in range(MAX_THINKING_STEPS):
        fresh = generate_idea(store, domain=domain, use_llm=use_llm)
        if fresh.verdict == "chosen":
            return store.mark_seen(fresh.id)
        ready = store.take_ready(domain=domain)
        if ready is not None:
            return ready

    # ここまで来るのは連鎖が閉じないほど回した場合。考えたものを無駄に
    # しないよう、直近のものをそのまま出す。
    log.warning("連鎖が閉じないまま上限に達したので、直近の考えを出す")
    return store.mark_seen(fresh.id)


def prefill(store: Store, count: int = DEFAULT_STOCK, *, use_llm: bool = True) -> int:
    """出せる在庫を `count` 件まで補充し、考えた回数を返す。

    在庫として数えるのは**選ばれたもの**だけ。連鎖が閉じるまで選別は走らない
    ので、1件確保するのに数回〜10回ほど考えることになる。

    ワーカーがこれを定期的に呼ぶことで「裏で考え続けている」状態になる。
    """
    if store.stock_count() >= count:
        return 0

    thought = 0
    limit = max(count, 1) * MAX_THINKING_STEPS
    while store.stock_count() < count and thought < limit:
        generate_idea(store, use_llm=use_llm)
        thought += 1

    if store.stock_count() < count:
        log.warning(
            "%d 回考えたが在庫が %d 件までしか埋まらなかった",
            thought,
            store.stock_count(),
        )
    return thought
