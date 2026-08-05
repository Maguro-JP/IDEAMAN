"""SQLite への蓄積と、評価による重みの更新。

ORM は使わない。テーブルは3つしかなく、SQL のほうが何が起きているか読みやすい。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .engine import rhythm
from .engine.combinator import IdeaSeed

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
INDEXES_PATH = Path(__file__).resolve().parent / "indexes.sql"


def default_db_path() -> Path:
    """既定の DB パス。

    import 時ではなく呼び出し時に環境変数を読む。import 時に固定すると、
    起動後に `IDEAMAN_DB` を設定しても効かない。
    """
    return Path(os.environ.get("IDEAMAN_DB", "ideaman.db"))


#: 評価が語の重みに与える倍率。
UPVOTE_FACTOR = 1.15
DOWNVOTE_FACTOR = 0.85

#: 同じ組み合わせに再到達したときの倍率。
#: 出力はしないが、何度も辿り着くならそこに何かがある、という弱い信号。
#: 👍 よりずっと小さいのは、これが人の評価ではなく偶然の再来だから。
DUPLICATE_FACTOR = 1.03

#: 重みの下限・上限。これが無いと一度嫌われた語が二度と出てこなくなる。
MIN_WEIGHT = 0.2
MAX_WEIGHT = 5.0


@dataclass(frozen=True)
class Idea:
    """保存されたアイデア。"""

    id: int
    text: str
    domain: str | None
    tone: str
    mood: str
    source: str
    state: str
    created_at: str
    score: int = 0
    chain_id: int | None = None
    parent_id: int | None = None
    mode: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "domain": self.domain,
            "tone": self.tone,
            "mood": self.mood,
            "source": self.source,
            "state": self.state,
            "created_at": self.created_at,
            "score": self.score,
            "chain_id": self.chain_id,
            "parent_id": self.parent_id,
            "mode": self.mode,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_idea(row: sqlite3.Row) -> Idea:
    keys = row.keys()
    return Idea(
        id=row["id"],
        text=row["text"],
        domain=row["domain"],
        tone=row["tone"],
        mood=row["mood"],
        source=row["source"],
        state=row["state"],
        created_at=row["created_at"],
        score=row["score"] if "score" in keys and row["score"] is not None else 0,
        chain_id=row["chain_id"] if "chain_id" in keys else None,
        parent_id=row["parent_id"] if "parent_id" in keys else None,
        mode=row["mode"] if "mode" in keys else None,
    )


class Store:
    """アイデアの保管庫。

    `sqlite3` の接続は1本を使い回す。FastAPI から複数スレッドで触られるので
    `check_same_thread=False` を指定し、書き込みは短いトランザクションで済ませる。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Web・ワーカー・Bot が同じ DB を同時に触るため。
        # WAL なら読み手が書き手を待たない。
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        # 表 → 列の追加 → 索引 の順。Phase 1 の DB には新しい列がまだ無いので、
        # 索引を先に張ろうとすると落ちる。
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        self.conn.executescript(INDEXES_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def _migrate(self) -> None:
        """Phase 1 の DB を開いたときに足りない列を足す。

        `CREATE TABLE IF NOT EXISTS` は既存テーブルには何もしないので、
        列の追加だけは自前でやる。マイグレーション機構は導入しない。
        """
        additions = {
            "ideas": [
                ("signature", "TEXT"),
                ("chain_id", "INTEGER"),
                ("mode", "TEXT"),
            ],
            "weights": [
                ("dormant_until", "TEXT"),
                ("stall_count", "INTEGER NOT NULL DEFAULT 0"),
            ],
        }
        for table, columns in additions.items():
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    # --- 重み ---------------------------------------------------------

    def weights(self) -> dict[str, float]:
        """語 -> 保存された重み。休眠は考慮しない生の値。"""
        rows = self.conn.execute("SELECT term, weight FROM weights").fetchall()
        return {row["term"]: row["weight"] for row in rows}

    def draw_weights(self, now: datetime | None = None) -> dict[str, float]:
        """抽選に使う実効的な重み。

        休眠中の語は 0.0（＝抽選から外れる）。ただし十分に重い語は通り、
        休眠明け直後の語には下駄が乗る。判断は `rhythm.draw_weight` に任せ、
        ここは DB から読んで渡すだけ。
        """
        rows = self.conn.execute(
            "SELECT term, weight, dormant_until FROM weights"
        ).fetchall()
        out: dict[str, float] = {}
        for row in rows:
            dormant = (
                datetime.fromisoformat(row["dormant_until"])
                if row["dormant_until"]
                else None
            )
            out[row["term"]] = rhythm.draw_weight(row["weight"], dormant, now=now)
        return out

    def _bump(self, pool: str, term: str, factor: float) -> None:
        row = self.conn.execute(
            "SELECT weight FROM weights WHERE pool = ? AND term = ?", (pool, term)
        ).fetchone()
        current = row["weight"] if row else 1.0
        updated = min(max(current * factor, MIN_WEIGHT), MAX_WEIGHT)
        self.conn.execute(
            "INSERT INTO weights (pool, term, weight) VALUES (?, ?, ?) "
            "ON CONFLICT (pool, term) DO UPDATE SET weight = excluded.weight",
            (pool, term, updated),
        )

    def reinforce(self, terms: Iterable[tuple[str, str]], factor: float) -> None:
        """語の重みをまとめて動かす。重複を信号として拾うときに使う。"""
        for pool, term in terms:
            self._bump(pool, term, factor)
        self.conn.commit()

    # --- カウンタ -----------------------------------------------------

    def bump_counter(self, name: str, delta: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT (name) DO UPDATE SET value = value + excluded.value",
            (name, delta),
        )
        self.conn.commit()

    def counter(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT value FROM counters WHERE name = ?", (name,)
        ).fetchone()
        return row["value"] if row else 0

    def revived_count(self, now: datetime | None = None) -> int:
        """休眠が明けて戻ってきた語の数。"""
        now = now or datetime.now(timezone.utc)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM weights "
            "WHERE dormant_until IS NOT NULL AND dormant_until <= ?",
            (now.isoformat(),),
        ).fetchone()
        return row["n"]

    # --- 休眠と復帰 ---------------------------------------------------

    def sleep_terms(
        self, terms: Iterable[tuple[str, str]], now: datetime | None = None
    ) -> None:
        """語をしばらく寝かせる。

        捨てるのでも重みを下げるのでもない。抽選から一時的に外すだけで、
        時間が来れば戻ってくる（頭の片隅に置いておく）。
        """
        now = now or datetime.now(timezone.utc)
        for pool, term in terms:
            row = self.conn.execute(
                "SELECT weight, stall_count FROM weights WHERE pool = ? AND term = ?",
                (pool, term),
            ).fetchone()
            weight = row["weight"] if row else 1.0
            stalls = (row["stall_count"] if row else 0) + 1
            until = rhythm.dormancy_for(stalls, now=now)
            self.conn.execute(
                "INSERT INTO weights (pool, term, weight, dormant_until, stall_count) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (pool, term) DO UPDATE SET "
                "dormant_until = excluded.dormant_until, "
                "stall_count = excluded.stall_count",
                (pool, term, weight, until.isoformat(), stalls),
            )
        self.conn.commit()

    def dormant_terms(self, now: datetime | None = None) -> list[str]:
        """いま眠っている語（aspiration で通るものは除く）。"""
        now = now or datetime.now(timezone.utc)
        rows = self.conn.execute(
            "SELECT term, weight, dormant_until FROM weights "
            "WHERE dormant_until IS NOT NULL AND dormant_until > ?",
            (now.isoformat(),),
        ).fetchall()
        return [
            row["term"]
            for row in rows
            if row["weight"] < rhythm.ASPIRATION_WEIGHT
        ]

    # --- 思考の状態 ---------------------------------------------------

    def thought_state(self) -> rhythm.ThoughtState:
        """いま何を考えているか。無ければ放浪から始める。"""
        row = self.conn.execute("SELECT * FROM thought_state WHERE id = 1").fetchone()
        if row is None:
            return rhythm.ThoughtState()
        return rhythm.ThoughtState(
            mode=row["mode"],
            chain_id=row["chain_id"],
            chain_length=row["chain_length"],
            stale_hits=row["stale_hits"],
        )

    def save_thought_state(self, state: rhythm.ThoughtState) -> None:
        self.conn.execute(
            "INSERT INTO thought_state "
            "(id, mode, chain_id, chain_length, stale_hits, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "mode = excluded.mode, chain_id = excluded.chain_id, "
            "chain_length = excluded.chain_length, stale_hits = excluded.stale_hits, "
            "updated_at = excluded.updated_at",
            (state.mode, state.chain_id, state.chain_length, state.stale_hits, _now()),
        )
        self.conn.commit()

    # --- 保存と取り出し -----------------------------------------------

    def save(
        self,
        seed: IdeaSeed,
        text: str,
        source: str,
        parent_id: int | None = None,
        chain_id: int | None = None,
        mode: str | None = None,
    ) -> Idea:
        """生成されたアイデアを `unseen` として蓄える。

        `chain_id` を省略すると、自分自身の ID が連鎖の識別子になる
        （＝新しい連鎖の起点）。
        """
        cursor = self.conn.execute(
            "INSERT INTO ideas "
            "(text, seed_json, domain, template_id, tone, mood, source, state,"
            " parent_id, created_at, signature, chain_id, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'unseen', ?, ?, ?, ?, ?)",
            (
                text,
                json.dumps(
                    {
                        "slots": seed.slots,
                        "terms": [list(t) for t in seed.terms],
                        "seed": seed.seed,
                    },
                    ensure_ascii=False,
                ),
                seed.domain,
                seed.template_id,
                seed.tone,
                seed.mood,
                source,
                parent_id,
                _now(),
                seed.signature,
                chain_id,
                mode,
            ),
        )
        idea_id = cursor.lastrowid
        if chain_id is None:
            self.conn.execute(
                "UPDATE ideas SET chain_id = ? WHERE id = ?", (idea_id, idea_id)
            )
        self.conn.commit()
        return self.get(idea_id)  # type: ignore[arg-type]

    def terms_of(self, idea_id: int) -> tuple[tuple[str, str], ...]:
        """そのアイデアが使った (プール名, 語)。連想で引き継ぐ候補になる。"""
        row = self.conn.execute(
            "SELECT seed_json FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if row is None:
            return ()
        return tuple(tuple(t) for t in json.loads(row["seed_json"])["terms"])  # type: ignore[misc]

    def signature_exists(self, signature: str) -> bool:
        """同じ組み合わせに既に到達しているか。"""
        row = self.conn.execute(
            "SELECT 1 FROM ideas WHERE signature = ? LIMIT 1", (signature,)
        ).fetchone()
        return row is not None

    def chain(self, idea_id: int) -> list[Idea]:
        """そのアイデアが属する連鎖を、古い順に返す。

        一つの発想がどう広がったかを読むためのもの。
        """
        row = self.conn.execute(
            "SELECT chain_id FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"アイデア {idea_id} は存在しません")
        chain_id = row["chain_id"] or idea_id
        rows = self.conn.execute(
            "SELECT i.*, COALESCE(SUM(r.score), 0) AS score "
            "FROM ideas i LEFT JOIN ratings r ON r.idea_id = i.id "
            "WHERE i.chain_id = ? GROUP BY i.id ORDER BY i.id",
            (chain_id,),
        ).fetchall()
        return [_row_to_idea(r) for r in rows]

    def take_unseen(self, domain: str | None = None) -> Idea | None:
        """在庫から1件取り出して `seen` にする。無ければ None。

        「すでに考えてあったもの」を返すのがこのメソッドの役目。呼び出し側は
        None が返ってきたときだけその場で生成する。
        """
        if domain is None:
            row = self.conn.execute(
                "SELECT * FROM ideas WHERE state = 'unseen' ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM ideas WHERE state = 'unseen' AND domain = ? "
                "ORDER BY id LIMIT 1",
                (domain,),
            ).fetchone()
        if row is None:
            return None

        self.conn.execute(
            "UPDATE ideas SET state = 'seen', seen_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        self.conn.commit()
        return self.get(row["id"])

    def mark_seen(self, idea_id: int) -> Idea:
        self.conn.execute(
            "UPDATE ideas SET state = 'seen', seen_at = ? WHERE id = ?",
            (_now(), idea_id),
        )
        self.conn.commit()
        return self.get(idea_id)

    def stock_count(self, domain: str | None = None) -> int:
        """在庫（未閲覧）の件数。"""
        if domain is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM ideas WHERE state = 'unseen'"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM ideas WHERE state = 'unseen' AND domain = ?",
                (domain,),
            ).fetchone()
        return row["n"]

    def get(self, idea_id: int) -> Idea:
        row = self.conn.execute(
            "SELECT i.*, COALESCE(SUM(r.score), 0) AS score "
            "FROM ideas i LEFT JOIN ratings r ON r.idea_id = i.id "
            "WHERE i.id = ? GROUP BY i.id",
            (idea_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"アイデア {idea_id} は存在しません")
        return _row_to_idea(row)

    def list_seen(self, limit: int = 50, offset: int = 0) -> list[Idea]:
        """蓄積一覧。一度でも表に出たものだけを新しい順で返す。"""
        rows = self.conn.execute(
            "SELECT i.*, COALESCE(SUM(r.score), 0) AS score "
            "FROM ideas i LEFT JOIN ratings r ON r.idea_id = i.id "
            "WHERE i.state = 'seen' "
            "GROUP BY i.id ORDER BY i.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_idea(row) for row in rows]

    def search(self, query: str | None = None, limit: int = 20) -> list[Idea]:
        """蓄積を見渡す。

        `query` があれば本文で絞る。無ければ高評価のものを優先して返す
        （「何か良いアイデアない?」に応えるため）。
        """
        if query:
            rows = self.conn.execute(
                "SELECT i.*, COALESCE(SUM(r.score), 0) AS score "
                "FROM ideas i LEFT JOIN ratings r ON r.idea_id = i.id "
                "WHERE i.text LIKE ? GROUP BY i.id "
                "ORDER BY score DESC, i.id DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT i.*, COALESCE(SUM(r.score), 0) AS score "
                "FROM ideas i LEFT JOIN ratings r ON r.idea_id = i.id "
                "GROUP BY i.id ORDER BY score DESC, i.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_idea(row) for row in rows]

    # --- 配信 ---------------------------------------------------------

    def queue_delivery(self, idea_id: int, channel: str) -> int:
        """配信予定を積む。Bot が別プロセスなので DB がキューを兼ねる。"""
        cursor = self.conn.execute(
            "INSERT INTO deliveries (idea_id, channel, state, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (idea_id, channel, _now()),
        )
        self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def pending_deliveries(self, channel: str, limit: int = 10) -> list[tuple[int, Idea]]:
        """まだ送られていない配信を `(配信ID, アイデア)` で返す。"""
        rows = self.conn.execute(
            "SELECT d.id AS delivery_id, i.*, COALESCE(SUM(r.score), 0) AS score "
            "FROM deliveries d JOIN ideas i ON i.id = d.idea_id "
            "LEFT JOIN ratings r ON r.idea_id = i.id "
            "WHERE d.channel = ? AND d.state = 'pending' "
            "GROUP BY d.id ORDER BY d.id LIMIT ?",
            (channel, limit),
        ).fetchall()
        return [(row["delivery_id"], _row_to_idea(row)) for row in rows]

    def mark_delivered(
        self, delivery_id: int, external_id: str | None, failed: bool = False
    ) -> None:
        """配信の結果を記録する。`external_id` は評価の逆引きに使う。"""
        self.conn.execute(
            "UPDATE deliveries SET state = ?, external_id = ?, sent_at = ? WHERE id = ?",
            ("failed" if failed else "sent", external_id, _now(), delivery_id),
        )
        self.conn.commit()

    def idea_by_external_id(self, channel: str, external_id: str) -> Idea | None:
        """外部のメッセージ ID からアイデアを引く。

        Discord のリアクションを評価に繋ぐための逆引き。
        """
        row = self.conn.execute(
            "SELECT idea_id FROM deliveries WHERE channel = ? AND external_id = ?",
            (channel, str(external_id)),
        ).fetchone()
        return self.get(row["idea_id"]) if row else None

    def last_delivery_at(self, channel: str) -> datetime | None:
        """最後に配信した時刻。落ちていた間の分をまとめて撃たないために使う。"""
        row = self.conn.execute(
            "SELECT sent_at FROM deliveries "
            "WHERE channel = ? AND sent_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (channel,),
        ).fetchone()
        return datetime.fromisoformat(row["sent_at"]) if row and row["sent_at"] else None

    # --- 評価 ---------------------------------------------------------

    def rate(self, idea_id: int, score: int, note: str | None = None) -> Idea:
        """評価を付け、そのアイデアを構成した語の重みを動かす。

        Raises:
            KeyError: アイデアが存在しない場合。
            ValueError: score が -1 / 0 / 1 以外の場合。
        """
        if score not in (-1, 0, 1):
            raise ValueError(f"score は -1 / 0 / 1 のいずれかです: {score}")

        row = self.conn.execute(
            "SELECT seed_json FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"アイデア {idea_id} は存在しません")

        self.conn.execute(
            "INSERT INTO ratings (idea_id, score, note, created_at) VALUES (?, ?, ?, ?)",
            (idea_id, score, note, _now()),
        )

        if score != 0:
            factor = UPVOTE_FACTOR if score > 0 else DOWNVOTE_FACTOR
            for pool, term in json.loads(row["seed_json"])["terms"]:
                self._bump(pool, term, factor)

        self.conn.commit()
        return self.get(idea_id)
