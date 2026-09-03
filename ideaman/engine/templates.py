"""`IdeaSeed` を日本語の文章にする。"""

from __future__ import annotations

from .combinator import IdeaSeed
from .lexicon import Lexicon, default_lexicon


def render(seed: IdeaSeed, lexicon: Lexicon | None = None) -> str:
    """種を文章化する。

    Raises:
        KeyError: 種に対応するテンプレートが語彙に無い場合。
    """
    lexicon = lexicon or default_lexicon()
    for template in lexicon.templates:
        if template.id == seed.template_id:
            return template.text.format(**seed.slots)
    raise KeyError(f"テンプレート {seed.template_id!r} が見つかりません")
