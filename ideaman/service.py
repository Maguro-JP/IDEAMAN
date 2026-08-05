"""生成の唯一の入口。

Web からも、将来の Bot からも、常駐ワーカーからもここを呼ぶ。ここを通らない
生成経路を作らないことが、Phase 2 以降を安く済ませる唯一のコツ。
"""

from __future__ import annotations

from .engine import templates
from .engine.combinator import combine
from .engine.lexicon import Lexicon, default_lexicon
from .engine.refiner import refine
from .store import Idea, Store

#: 起動時に確保しておく在庫の数。
DEFAULT_STOCK = 5


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

    Args:
        store: 保管庫。重みの読み込みと保存に使う。
        domain: 分野の指定。concepts をその分野に寄せる。
        tone: `oracle` / `proposal` / `question`。省略時はランダム。
        seed: 乱数シード。指定すると決定的になる。
        use_llm: False なら LLM 仕上げを試みない。
        lexicon: 使う語彙。省略時は既定。
    """
    lexicon = lexicon or default_lexicon()
    idea_seed = combine(
        lexicon=lexicon,
        weights=store.weights(),
        domain=domain,
        tone=tone,
        seed=seed,
    )
    text = templates.render(idea_seed, lexicon)
    if use_llm:
        text, source = refine(idea_seed, text)
    else:
        source = "engine"
    return store.save(idea_seed, text, source)


def receive_idea(
    store: Store,
    *,
    domain: str | None = None,
    use_llm: bool = True,
) -> Idea:
    """溜まっている考えを1つ受け取る。

    在庫があればそれを返し（＝すでに考えてあったもの）、空のときだけその場で
    考える。Phase 5 で在庫の作られ方が賢くなっても、この関数の形は変わらない。
    """
    idea = store.take_unseen(domain=domain)
    if idea is not None:
        return idea
    fresh = generate_idea(store, domain=domain, use_llm=use_llm)
    return store.mark_seen(fresh.id)


def prefill(store: Store, count: int = DEFAULT_STOCK, *, use_llm: bool = True) -> int:
    """在庫を `count` 件まで補充し、新たに考えた数を返す。

    バックグラウンド思考の最小形。今は起動時に一度だけ走る。
    """
    missing = max(count - store.stock_count(), 0)
    for _ in range(missing):
        generate_idea(store, use_llm=use_llm)
    return missing
