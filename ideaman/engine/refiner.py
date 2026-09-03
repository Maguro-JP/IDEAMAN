"""任意の LLM 仕上げ。

組み合わせエンジンが作った種はときに硬い。`ANTHROPIC_API_KEY` があれば
Claude に一度だけ通して自然な日本語にする。キーが無い・SDK が無い・API が
失敗した場合はテンプレート出力をそのまま返す。**ここで例外を投げてはいけない。**
"""

from __future__ import annotations

import logging
import os

from .combinator import IdeaSeed

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

SYSTEM = """\
あなたはアイデアの種を短い日本語に整える編集者です。ユーザーの質問に答える\
アシスタントではありません。

守ること:
- 与えられた素材の語をすべて活かす。新しい題材を持ち込まない。
- 出力は1〜2文。前置き・解説・箇条書き・鉤括弧は禁止。
- 説明しない。断定して終わる。読み手が自分で考える余白を残す。
- 元の文の語調（お告げ／提案／問い）を保つ。
整えた文だけを出力してください。"""


def _prompt(seed: IdeaSeed, text: str) -> str:
    slots = "\n".join(f"- {k}: {v}" for k, v in seed.slots.items())
    return (
        f"素材:\n{slots}\n\n語調: {seed.tone}\n気分: {seed.mood}\n\n"
        f"整える前の文:\n{text}"
    )


def is_available() -> bool:
    """LLM 仕上げが使える状態かどうか。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


#: 仕上がりが元の何倍までなら受け入れるか。
#: 「1〜2文」と指示しているので、大きく超えたら前置きや解説が混ざっている。
MAX_GROWTH = 2.5

#: 混ざっていたら捨てる印。タグの漏れ、箇条書き、前置き。
_SUSPICIOUS = ("<thinking", "</thinking", "<", "```", "- ", "* ", "1. ")


def usable(refined: str, original: str) -> bool:
    """仕上がりをそのままアイデアとして出してよいか。

    **ここを通った文字列は、そのままユーザーの目に入る。** モデルが前置きを
    付けたり、思考のタグを漏らしたり、箇条書きにしたりしたら、素の文章の
    ほうがまだよい。判定できないものは受け入れない側に倒す。
    """
    refined = refined.strip()
    if not refined:
        return False
    if len(refined) > max(len(original) * MAX_GROWTH, 40):
        return False
    if any(mark in refined for mark in _SUSPICIOUS):
        return False
    # 1〜2文と指示している。句点が多すぎるなら説明が始まっている。
    return refined.count("。") <= 3


def refine(seed: IdeaSeed, text: str) -> tuple[str, str]:
    """種と素の文章を受け取り、`(文章, source)` を返す。

    `source` は `llm`（磨いた）か `engine`（そのまま）。失敗時は必ず後者。
    """
    if not is_available():
        return text, "engine"

    try:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=SYSTEM,
            # 思考は切らない。**Claude Opus 5 で thinking を disabled にすると、
            # `<thinking>` タグが応答に漏れることがある。** ここは返ってきた
            # 文字列をそのままアイデア本文にするので、漏れるとそのまま表に出る。
            # 思考を残したまま effort を下げれば、それを避けつつ費用も下がる。
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": _prompt(seed, text)}],
        )
        if response.stop_reason == "refusal":
            log.warning("refiner: モデルが応答を拒否したためテンプレート出力を使います")
            return text, "engine"

        refined = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not usable(refined, text):
            return text, "engine"
        return refined, "llm"
    except Exception:
        log.warning("refiner: 仕上げに失敗したためテンプレート出力を使います", exc_info=True)
        return text, "engine"
