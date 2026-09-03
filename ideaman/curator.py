"""連鎖が閉じたときに、出すものを選ぶ。

思考は連鎖する。だから考えている間はずっと独り言で、外に出すのは連鎖が
閉じてから、良さそうなものを一つ二つだけ。そうしないとアイデアが溢れ続ける
ウザい Bot になる。

選ばれなかったものは**捨てない**。DB に残り、連鎖を辿れば読める。
"""

from __future__ import annotations

import logging
import os

from .store import Idea, Store

log = logging.getLogger(__name__)

#: 連鎖からいくつ出すか。
PICK_SHORT = 1  # 長さ3以下の連鎖から
PICK_LONG = 2  # それより長い連鎖から
SHORT_CHAIN = 3

#: スコアの重み。
#: 立ち上げ直後は評価が無いので実質 novelty しか効かない。評価が溜まると
#: 重みが意味を持ち始める、という性質のもの。動かしてから調整する前提。
W_NOVELTY = 0.40
W_WEIGHT = 0.28
W_DEVELOP = 0.12
#: 直近と同じ文型を避ける重み。**内容ではなく見え方の指標**だが、
#: 毎日届く Bot では効き目が大きい。同じ骨格の文が続くと、違う話を
#: していても壁紙になる。
W_FRESH = 0.20

#: 何件前までの文型を「最近使った」とみなすか。
FRESHNESS_WINDOW = 8

#: 重みの正規化に使う上限（store.MAX_WEIGHT と揃える）。
WEIGHT_CEILING = 5.0

#: 単発の思いつき（連鎖にならなかった1件）に課す基準。
#: 連鎖なら「7件中の上位2件」という比較を勝ち抜いているが、1件しかない
#: ものは何とも比べていない。何もしないと全部素通りするので、**直近の
#: 平均より上**であることを求める。絶対値の閾値を置かないのは、評価が
#: 溜まるにつれてスコアの分布そのものが動くため。
SOLO_HISTORY = 50

#: 履歴がまだ無いときだけ使う暫定の基準。
SOLO_COLD_START = 0.5


def score(store: Store, idea: Idea) -> float:
    """そのアイデアの「良さそう度」。0〜1 に収める。

    使えるのは手持ちの信号だけ:
    - 語同士がどれだけ離れていたか（＝どれだけ意外な組か）
    - その語が過去に評価されて育っているか
    - 何かの上に乗っているか（連想で生まれたものは単なる思いつきより一歩先）
    """
    novelty = store.novelty_of(idea.id)

    weights = store.weights()
    terms = store.terms_of(idea.id)
    if terms:
        mean = sum(weights.get(term, 1.0) for _, term in terms) / len(terms)
    else:
        mean = 1.0
    # 1.0 を基準に 0〜1 へ。育っていない語ばかりなら 0 付近になる。
    grown = min(max((mean - 1.0) / (WEIGHT_CEILING - 1.0), 0.0), 1.0)

    developed = 1.0 if idea.parent_id is not None else 0.0

    # 直近に使った文型ほど減点。すぐ前に使ったものが最も重い。
    recent = store.recent_chosen_templates(FRESHNESS_WINDOW)
    if idea.template_id in recent:
        # 0 件前（直前）なら 0.0、窓の端なら 1.0 に近づく。
        fresh = recent.index(idea.template_id) / len(recent)
    else:
        fresh = 1.0

    return (
        W_NOVELTY * novelty
        + W_WEIGHT * grown
        + W_DEVELOP * developed
        + W_FRESH * fresh
    )


def pick_count(chain_length: int) -> int:
    """連鎖の長さに応じて、いくつ出すか。"""
    return PICK_SHORT if chain_length <= SHORT_CHAIN else PICK_LONG


def solo_bar(store: Store) -> float:
    """単発の思いつきに課す基準。直近の平均スコア。

    絶対値の定数を置かないのは、評価が溜まるとスコアの分布そのものが
    上に動くため。「ふつうより良ければ出す」という相対の基準にしておく。
    """
    history = store.recent_scores(SOLO_HISTORY)
    if not history:
        return SOLO_COLD_START
    return sum(history) / len(history)


def _llm_pick(chain: list[Idea], want: int) -> list[int] | None:
    """Claude に選ばせる。使えない・失敗したら None を返して指標に譲る。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None

    listing = "\n".join(f"{i + 1}. {idea.text}" for i, idea in enumerate(chain))
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=256,
            system=(
                "あなたは発想の目利きです。与えられたのは一つの思考の連鎖です。"
                "この中から、行き詰まった人に見せたときに一番きっかけになりそうな"
                f"ものを {want} つ選んでください。\n"
                "基準: 意外さ、具体的に動き出せること、既視感がないこと。\n"
                "出力は選んだ番号だけをカンマ区切りで。説明は不要です（例: 3,5）。"
            ),
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": listing}],
        )
        if response.stop_reason == "refusal":
            return None
        text = "".join(
            b.text for b in response.content if b.type == "text"
        ).strip()
        picked = []
        for chunk in text.replace(" ", "").split(","):
            if chunk.isdigit() and 1 <= int(chunk) <= len(chain):
                picked.append(chain[int(chunk) - 1].id)
        return picked[:want] or None
    except Exception:  # noqa: BLE001 - 選別の失敗で思考を止めない
        log.warning("LLM による選別に失敗したので指標で選ぶ", exc_info=True)
        return None


def curate(store: Store, chain: list[Idea], *, use_llm: bool = True) -> list[Idea]:
    """閉じた連鎖を判定し、選ばれたものを返す。

    全件に `verdict` が付く。選ばれなかったものは `kept` として残り、
    連鎖を辿れば読める（捨てない）。

    Args:
        chain: 一つの連鎖に属するアイデア（古い順）。
        use_llm: False なら指標だけで選ぶ。

    Returns:
        選ばれたアイデア。既に判定済みの連鎖を渡した場合は空。
    """
    pending = [idea for idea in chain if idea.verdict is None]
    if not pending:
        return []

    scores = {idea.id: score(store, idea) for idea in pending}

    if len(chain) == 1:
        # 連鎖にならなかった単発。比較する相手がいないので、直近の平均を
        # 超えていなければ出さない（残しはする）。
        only = pending[0]
        bar = solo_bar(store)
        chosen_ids = [only.id] if scores[only.id] >= bar else []
    else:
        want = min(pick_count(len(chain)), len(pending))
        chosen_ids = []
        if use_llm:
            chosen_ids = _llm_pick(pending, want) or []
        if not chosen_ids:
            ranked = sorted(pending, key=lambda i: scores[i.id], reverse=True)
            chosen_ids = [idea.id for idea in ranked[:want]]

    chosen: list[Idea] = []
    for idea in pending:
        verdict = "chosen" if idea.id in chosen_ids else "kept"
        store.set_verdict(idea.id, verdict, scores[idea.id])
        if verdict == "chosen":
            chosen.append(store.get(idea.id))

    log.info(
        "連鎖 %s を判定: %d件中 %d件を選んだ",
        chain[0].chain_id if chain else "?",
        len(pending),
        len(chosen),
    )
    return chosen
