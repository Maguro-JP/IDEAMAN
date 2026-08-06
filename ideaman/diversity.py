"""思考のリズムが健康かどうかを見る。

**多様性が高いほど良い、ではない。** 重複率ゼロは連想が効いていない証拠だし、
連鎖の長さが全部1なら、それは思考ではなくただの乱数。見たいのは「連鎖して、
行き詰まって、離れて、戻ってくる」が起きているかどうか。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from .engine.lexicon import default_lexicon
from .store import Store


@dataclass
class Health:
    """リズムの健康度。"""

    total: int = 0
    chains: int = 0
    chain_lengths: Counter = field(default_factory=Counter)
    longest_chain: int = 0
    mean_chain_length: float = 0.0
    mode_counts: Counter = field(default_factory=Counter)
    coverage: float = 0.0
    top_share: float = 0.0
    duplicate_hits: int = 0
    dormant_now: int = 0
    revived: int = 0
    chosen: int = 0
    kept: int = 0
    unjudged: int = 0

    def report(self) -> str:
        """人が読む形。指標だけ見ても分からないので、判断の目安も書く。"""
        lines = [
            f"アイデア          {self.total} 件 / 連鎖 {self.chains} 本",
            f"連鎖の長さ        平均 {self.mean_chain_length:.1f} / 最長 {self.longest_chain}",
            f"  分布            "
            + ", ".join(
                f"{length}→{count}本"
                for length, count in sorted(self.chain_lengths.items())
            ),
            f"モード            "
            + ", ".join(f"{mode} {n}" for mode, n in self.mode_counts.most_common()),
            f"語の被覆率        {self.coverage:.0%}（語彙のうち実際に使われた割合）",
            f"上位集中度        {self.top_share:.0%}（最頻出5語が占める割合）",
            f"重複到達          {self.duplicate_hits} 回",
            f"いま休眠中        {self.dormant_now} 語 / 復帰済み {self.revived} 語",
            f"選別              出す {self.chosen} / 残す {self.kept} "
            f"/ 判定前 {self.unjudged}"
            + (f"（{self.chosen / self.total:.0%} を出す）" if self.total else ""),
            "",
            "読み方:",
            "  連鎖の長さが全部1  → 連想が働いていない（ただの乱数になっている）",
            "  重複到達が0        → 連想が浅い。ゼロが正解ではない",
            "  上位集中度が高い   → 同じ語ばかり。休眠が効いていない",
            "  休眠が0            → 行き詰まりを検知できていない",
            "  出す割合が高い     → 選別が甘い。連鎖7件なら2〜3割が目安",
            "  判定前が多い       → 連鎖が閉じていない（考え込んで出てこない）",
        ]
        return "\n".join(lines)


def measure(store: Store, limit: int = 500) -> Health:
    """直近 `limit` 件からリズムを測る。"""
    rows = store.conn.execute(
        "SELECT id, chain_id, mode, verdict, seed_json FROM ideas "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()

    health = Health(total=len(rows))
    # 語に紐づく数はアイデアが1件も無くても意味を持つので、先に埋める。
    health.duplicate_hits = store.counter("duplicate_hits")
    health.dormant_now = len(store.dormant_terms())
    health.revived = store.revived_count()
    if not rows:
        return health

    chain_lengths = Counter(row["chain_id"] for row in rows if row["chain_id"])
    health.chains = len(chain_lengths)
    health.chain_lengths = Counter(chain_lengths.values())
    health.longest_chain = max(chain_lengths.values(), default=0)
    health.mean_chain_length = (
        sum(chain_lengths.values()) / len(chain_lengths) if chain_lengths else 0.0
    )
    health.mode_counts = Counter(row["mode"] or "unknown" for row in rows)

    verdicts = Counter(row["verdict"] for row in rows)
    health.chosen = verdicts["chosen"]
    health.kept = verdicts["kept"]
    health.unjudged = verdicts[None]

    term_counts: Counter = Counter()
    for row in rows:
        for _, term in json.loads(row["seed_json"])["terms"]:
            term_counts[term] += 1

    lexicon = default_lexicon()
    vocabulary = sum(len(terms) for terms in lexicon.pools.values())
    health.coverage = len(term_counts) / vocabulary if vocabulary else 0.0
    drawn = sum(term_counts.values())
    health.top_share = (
        sum(n for _, n in term_counts.most_common(5)) / drawn if drawn else 0.0
    )

    return health
