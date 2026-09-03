"""語の衝突を作る抽選エンジン。

「AI が質問に答える」のではなく「普段結びつかないものが偶然ぶつかる」ことで
きっかけを生む。そのため素直な重み付きランダムではなく、候補を複数回引いて
**互いの分野タグが遠い組**を選ぶ。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
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
    "aspect": "aspects",
    "probe": "probes",
    # material はプールから引かない。呼び出し側が渡すテーマの材料を使う。
    "material": "materials",
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
    #: 引いた概念同士の遠さ（0〜1）。連想を始めるかの判断に使う。
    novelty: float = 0.0

    @property
    def signature(self) -> str:
        """同じ組み合わせに再到達したかを判定するための署名。

        テンプレートと語の集合だけで決まる。語順やシードは含めない
        （「星座と手紙」と「手紙と星座」は同じ発想）。
        """
        terms = sorted(f"{pool}:{term}" for pool, term in self.terms)
        return f"{self.template_id}|" + "|".join(terms)


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


def _pick_inherited(
    rng: random.Random,
    inherit: Sequence[tuple[str, str]],
    count: int,
    usable_pools: set[str],
) -> dict[str, list[str]]:
    """引き継ぐ語を、プール名ごとに分けて返す。

    **今回のテンプレートが実際に使うプールの語だけを候補にする。** これを
    しないと、引き継いだつもりの語が使われず、連鎖が名ばかりになる
    （親子として記録されているのに一語も共有していない状態）。
    """
    if count <= 0 or not inherit:
        return {}
    usable = [(pool, term) for pool, term in inherit if pool in usable_pools]
    if not usable:
        return {}
    chosen = rng.sample(usable, min(count, len(usable)))
    carried: dict[str, list[str]] = {}
    for pool, term in chosen:
        carried.setdefault(pool, []).append(term)
    return carried


def combine(
    *,
    lexicon: Lexicon | None = None,
    weights: dict[str, float] | None = None,
    domain: str | None = None,
    tone: str | None = None,
    seed: int | None = None,
    inherit: Sequence[tuple[str, str]] = (),
    inherit_count: int = 0,
    materials: Sequence[str] = (),
) -> IdeaSeed:
    """語彙を衝突させて `IdeaSeed` を1つ作る。

    Args:
        lexicon: 使う語彙。省略時は既定の語彙。
        weights: 語 -> 重み。評価によって育った値を渡す。未知の語は 1.0 扱い。
            0.0 の語は抽選から外れる（休眠している語）。
        domain: 分野を指定すると concepts をその分野に寄せる（Phase 3 の下地）。
        tone: `oracle` / `proposal` / `question` のいずれか。省略時はランダム。
        seed: 乱数シード。指定すると結果が決定的になる。
        inherit: 直前のアイデアが使った (プール名, 語)。連想モードで持ち越す候補。
        inherit_count: このうち何語を引き継ぐか。0 なら引き継がない（放浪）。
        materials: いま取り組んでいるテーマの材料（例: 通知, 会議）。
            **これが有益さの源**。汎用の概念だけを衝突させても宙に浮いた
            警句にしかならないので、片方をユーザー自身の語にする。
            空のときは material を使わないテンプレートに限定される。

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
    if materials:
        # 材料があるなら、それを使うテンプレートを優先する。汎用の概念だけの
        # フォールバックは、テーマが無いときのためのもの。
        grounded = [t for t in candidates if "material" in t.needs]
        candidates = grounded or candidates
    else:
        # 材料が無いなら material を使うテンプレートは埋められない。
        usable = [t for t in candidates if "material" not in t.needs]
        candidates = usable or [
            t for t in lexicon.templates if "material" not in t.needs
        ]
    template: Template = rng.choice(candidates)

    usable_pools = {
        SLOT_TO_POOL[slot] for slot in template.needs if slot in SLOT_TO_POOL
    }
    carried = _pick_inherited(rng, inherit, inherit_count, usable_pools)
    slots: dict[str, str] = {}
    used: list[tuple[str, str]] = []
    # 概念を1つしか引かないテンプレートでは衝突の距離が測れない。0 にすると
    # 「つまらない」と誤解されるので、測れない＝中立として 0.5 を既定にする
    # （`_distance` がタグ無し同士に 0.5 を返すのと同じ考え方）。
    novelty = 0.5

    def take_carried(pool_name: str) -> str | None:
        """そのプールに引き継ぎ語があれば1つ取り出す。"""
        pending = carried.get(pool_name)
        return pending.pop() if pending else None

    # concepts は「遠い2語」をまとめて引く必要があるので先に処理する。
    concept_slots = [s for s in template.needs if SLOT_TO_POOL.get(s) == "concepts"]
    if concept_slots:
        pool = _filter_by_domain(lexicon.pool("concepts"), domain)
        if len(concept_slots) >= 2:
            # 片方だけ引き継ぐと「同じ題材を別の角度から見る」形になる。
            # 両方引き継ぐと元と同じ組になるので、必ず片方は引き直す。
            kept = take_carried("concepts")
            if kept is not None:
                other = _weighted_choice(rng, pool, weights)
                for _ in range(CANDIDATE_TRIES):
                    if other.term != kept:
                        break
                    other = _weighted_choice(rng, pool, weights)
                if other.term == kept:
                    other = next(t for t in pool if t.term != kept)
                a_term, b_term = kept, other.term
            else:
                a, b = _pick_far_pair(rng, pool, weights)
                a_term, b_term = a.term, b.term
            slots["concept_a"], slots["concept_b"] = a_term, b_term
            used += [("concepts", a_term), ("concepts", b_term)]
            by_term = {t.term: t for t in pool}
            if a_term in by_term and b_term in by_term:
                novelty = _distance(by_term[a_term], by_term[b_term])
        else:
            kept = take_carried("concepts")
            term = kept or _weighted_choice(rng, pool, weights).term
            slots[concept_slots[0]] = term
            used.append(("concepts", term))

    if "material" in template.needs:
        # 材料はプールではなくテーマから来る。連想では引き継ぎを優先する。
        kept = take_carried("materials")
        chosen = kept or _weighted_choice(
            rng, tuple(Term(term=m) for m in materials), weights
        ).term
        slots["material"] = chosen
        used.append(("materials", chosen))

    for slot in template.needs:
        if slot in slots:
            continue
        pool_name = SLOT_TO_POOL.get(slot)
        if pool_name is None:
            raise ValueError(f"未知のスロット {slot!r} (template={template.id})")
        kept = take_carried(pool_name)
        term_text = kept or _weighted_choice(rng, lexicon.pool(pool_name), weights).term
        slots[slot] = term_text
        used.append((pool_name, term_text))

    return IdeaSeed(
        template_id=template.id,
        tone=template.tone,
        slots=slots,
        domain=domain,
        mood=rng.choice(lexicon.moods),
        seed=seed,
        terms=tuple(used),
        novelty=novelty,
    )
