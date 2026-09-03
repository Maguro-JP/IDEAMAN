"""語彙プールとテンプレートの読み込み。

YAML はプロセス起動時に一度だけ読み、以降はメモリ上のものを使い回す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: 抽選対象になる語彙プールの名前。テンプレの needs で使う名前とは
#: concept_a / concept_b -> concepts の対応で結びつく。
POOL_NAMES = (
    "concepts",
    "operations",
    "constraints",
    "provocations",
    # 具体性を担う2つ。どの側面をいじるか、まず何で試すか。
    "aspects",
    "probes",
)


@dataclass(frozen=True)
class Term:
    """語彙プールの1語。"""

    term: str
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class Template:
    """アイデアを文章にする型。"""

    id: str
    tone: str
    needs: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Lexicon:
    pools: dict[str, tuple[Term, ...]]
    moods: tuple[str, ...]
    templates: tuple[Template, ...] = field(default=())

    def pool(self, name: str) -> tuple[Term, ...]:
        return self.pools[name]

    def templates_for(self, tone: str | None = None) -> tuple[Template, ...]:
        if tone is None:
            return self.templates
        return tuple(t for t in self.templates if t.tone == tone)


def _load_terms(raw: list[dict]) -> tuple[Term, ...]:
    return tuple(
        Term(term=item["term"], domains=tuple(item.get("domains") or ()))
        for item in raw
    )


def load_lexicon(
    lexicon_path: Path | None = None, templates_path: Path | None = None
) -> Lexicon:
    """YAML から語彙とテンプレートを読み込む。"""
    lexicon_path = lexicon_path or DATA_DIR / "lexicon.yaml"
    templates_path = templates_path or DATA_DIR / "templates.yaml"

    raw = yaml.safe_load(lexicon_path.read_text(encoding="utf-8"))
    pools = {name: _load_terms(raw[name]) for name in POOL_NAMES}
    for name, terms in pools.items():
        if not terms:
            raise ValueError(f"語彙プール {name} が空です: {lexicon_path}")

    raw_templates = yaml.safe_load(templates_path.read_text(encoding="utf-8"))
    templates = tuple(
        Template(
            id=item["id"],
            tone=item["tone"],
            needs=tuple(item["needs"]),
            text=item["text"],
        )
        for item in raw_templates["templates"]
    )
    if not templates:
        raise ValueError(f"テンプレートが空です: {templates_path}")

    return Lexicon(
        pools=pools,
        moods=tuple(raw.get("moods") or ("静穏",)),
        templates=templates,
    )


@lru_cache(maxsize=1)
def default_lexicon() -> Lexicon:
    """既定の語彙。プロセス内でキャッシュされる。"""
    return load_lexicon()


def all_domains(lexicon: Lexicon) -> set[str]:
    """語彙に登場する分野タグの一覧。Phase 3 の分野特化で使う。"""
    return {d for terms in lexicon.pools.values() for t in terms for d in t.domains}
