"""語の衝突を作る抽選エンジン。

「AI が質問に答える」のではなく「普段結びつかないものが偶然ぶつかる」ことで
きっかけを生む。そのため素直な重み付きランダムではなく、候補を複数回引いて
**互いの分野タグが遠い組**を選ぶ。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .lexicon import Lexicon, Template, Term, default_lexicon

#: 1回の抽選で試す候補の数。多いほど「遠い組」が選ばれやすくなるが、
#: 大きすぎると毎回似たような最遠ペアに収束するので控えめにする。
CANDIDATE_TRIES = 6

#: テンプレの needs にある名前 -> 引くべきプール名
SLOT_TO_POOL = {
    "concept_a": "concepts",
    "concept_b": "concepts",
    "operation": "operations",
    "constraint": "constraints",
    "provocation": "provocations",
}


@dataclass(frozen=True)
class IdeaSeed:
    """アイデアの構造化された種。

    同じ seed と同じ語彙なら同じ文章が再現できる。
    """

    template_id: str
    tone: str
    slots: dict[str, str]
    domain: str | None
    mood: str
    seed: int
    #: 抽選に使われた語を (プール名, 語) の形で持つ。評価による重み更新で使う。
    terms: tuple[tuple[str, str], ...] = field(default=())


def _weighted_choice(
    rng: random.Random, terms: tuple[Term, ...], weights: dict[str, float]
) -> Term:
    ws = [max(weights.get(t.term, 1.0), 0.0) for t in terms]
    if sum(ws) <= 0:
        return rng.choice(terms)
    return rng.choices(terms, weights=ws, k=1)[0]


def _distance(a: Term, b: Term) -> float:
    """2語の「遠さ」。分野タグの重なりが少ないほど大きい。

    Jaccard 距離。どちらもタグなしのときは中間値を返す（極端に優遇しない）。
    """
    da, db = set(a.domains), set(b.domains)
    if not da and not db:
        return 0.5
    union = da | db
    if not union:
        return 0.5
    return 1.0 - len(da & db) / len(union)


def _pick_far_pair(
    rng: random.Random, terms: tuple[Term, ...], weights: dict[str, float]
) -> tuple[Term, Term]:
    """互いに遠い2語を選ぶ。"""
    if len(terms) < 2:
        raise ValueError("ペアを作るには2語以上必要です")

    best: tuple[Term, Term] | None = None
    best_score = -1.0
    for _ in range(CANDIDATE_TRIES):
        a = _weighted_choice(rng, terms, weights)
        b = _weighted_choice(rng, terms, weights)
        if a.term == b.term:
            continue
        score = _distance(a, b)
        if score > best_score:
            best, best_score = (a, b), score

    if best is None:
        # 全試行が同じ語を引いた場合のフォールバック
        a, b = rng.sample(terms, 2)
        best = (a, b)
    return best


def _filter_by_domain(terms: tuple[Term, ...], domain: str | None) -> tuple[Term, ...]:
    """分野指定があれば、そのタグを持つ語に絞る。

    絞り込んだ結果が2語未満になる場合は、偏りすぎを避けるため絞らない。
    """
    if domain is None:
        return terms
    narrowed = tuple(t for t in terms if domain in t.domains)
    return narrowed if len(narrowed) >= 2 else terms


def combine(
    *,
    lexicon: Lexicon | None = None,
    weights: dict[str, float] | None = None,
    domain: str | None = None,
    tone: str | None = None,
    seed: int | None = None,
) -> IdeaSeed:
    """語彙を衝突させて `IdeaSeed` を1つ作る。

    Args:
        lexicon: 使う語彙。省略時は既定の語彙。
        weights: 語 -> 重み。評価によって育った値を渡す。未知の語は 1.0 扱い。
        domain: 分野を指定すると concepts をその分野に寄せる（Phase 3 の下地）。
        tone: `oracle` / `proposal` / `question` のいずれか。省略時はランダム。
        seed: 乱数シード。指定すると結果が決定的になる。

    Returns:
        文章化前の構造化された種。
    """
    lexicon = lexicon or default_lexicon()
    weights = weights or {}
    if seed is None:
        seed = random.randrange(2**31)
    rng = random.Random(seed)

    candidates = lexicon.templates_for(tone)
    if not candidates:
        candidates = lexicon.templates
    template: Template = rng.choice(candidates)

    slots: dict[str, str] = {}
    used: list[tuple[str, str]] = []

    # concepts は「遠い2語」をまとめて引く必要があるので先に処理する。
    concept_slots = [s for s in template.needs if SLOT_TO_POOL.get(s) == "concepts"]
    if concept_slots:
        pool = _filter_by_domain(lexicon.pool("concepts"), domain)
        if len(concept_slots) >= 2:
            a, b = _pick_far_pair(rng, pool, weights)
            slots["concept_a"], slots["concept_b"] = a.term, b.term
            used += [("concepts", a.term), ("concepts", b.term)]
        else:
            a = _weighted_choice(rng, pool, weights)
            slots[concept_slots[0]] = a.term
            used.append(("concepts", a.term))

    for slot in template.needs:
        if slot in slots:
            continue
        pool_name = SLOT_TO_POOL.get(slot)
        if pool_name is None:
            raise ValueError(f"未知のスロット {slot!r} (template={template.id})")
        term = _weighted_choice(rng, lexicon.pool(pool_name), weights)
        slots[slot] = term.term
        used.append((pool_name, term.term))

    return IdeaSeed(
        template_id=template.id,
        tone=template.tone,
        slots=slots,
        domain=domain,
        mood=rng.choice(lexicon.moods),
        seed=seed,
        terms=tuple(used),
    )
