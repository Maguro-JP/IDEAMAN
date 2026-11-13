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
    """語彙プールの1語。

    `kind` は語の文法的な形。`aspects` でだけ意味を持つ:

    - `noun`（既定）— 名詞句。「{aspect}を{操作}」に入れられる
    - `clause` — 疑問節（「誰が決めるか」）。**「〜を取り除く」に入れると
      日本語が壊れる**ので、問いの位置にしか置けない

    この区別が無かったせいで「連鎖の誰が決めるかを自動で忘れさせる」の
    ような文が出ていた。区別したことで、逆に疑問節系の語を増やせる。
    """

    term: str
    domains: tuple[str, ...] = ()
    kind: str = "noun"
    #: `mechanisms` でだけ使う。何をどうしているかの一文と、その出典。
    #: 裸の語（改札）ではなく仕組み（脇へ出して個別に捌く）を運ぶための場所。
    how: str = ""
    source: str = ""


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


def load_mechanisms(path: Path | None = None) -> tuple[Term, ...]:
    """外の世界から採った「仕組みの一文」を読む。

    **無くてもよい。** 採取がまだなら空で、仕組みを使うテンプレートは
    選ばれないだけ。語彙プールと違って、空でも起動を止めない。
    """
    path = path or DATA_DIR / "mechanisms.yaml"
    if not path.is_file():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return tuple(
        Term(
            term=item["subject"],
            domains=tuple(item.get("domains") or ()),
            how=str(item["how"]).strip().rstrip("。"),
            source=item.get("source", ""),
        )
        for item in raw
        if item.get("subject") and item.get("how")
    )


def _load_terms(raw: list[dict]) -> tuple[Term, ...]:
    return tuple(
        Term(
            term=item["term"],
            domains=tuple(item.get("domains") or ()),
            kind=item.get("kind", "noun"),
        )
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
    pools["mechanisms"] = load_mechanisms()

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
