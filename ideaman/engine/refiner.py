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
            # 短い創作なので思考は不要。effort high 以下でのみ disabled が許される。
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": _prompt(seed, text)}],
        )
        if response.stop_reason == "refusal":
            log.warning("refiner: モデルが応答を拒否したためテンプレート出力を使います")
            return text, "engine"

        refined = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not refined:
            return text, "engine"
        return refined, "llm"
    except Exception:  # noqa: BLE001 - 仕上げは失敗しても生成を止めない
        log.warning("refiner: 仕上げに失敗したためテンプレート出力を使います", exc_info=True)
        return text, "engine"
