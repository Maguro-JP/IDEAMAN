"""SQLite への蓄積と、評価による重みの更新。

ORM は使わない。テーブルは3つしかなく、SQL のほうが何が起きているか読みやすい。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .engine.combinator import IdeaSeed

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def default_db_path() -> Path:
    """既定の DB パス。

    import 時ではなく呼び出し時に環境変数を読む。import 時に固定すると、
    起動後に `IDEAMAN_DB` を設定しても効かない。
    """
    return Path(os.environ.get("IDEAMAN_DB", "ideaman.db"))


#: 評価が語の重みに与える倍率。
UPVOTE_FACTOR = 1.15
DOWNVOTE_FACTOR = 0.85

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
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_idea(row: sqlite3.Row) -> Idea:
    return Idea(
        id=row["id"],
        text=row["text"],
        domain=row["domain"],
        tone=row["tone"],
        mood=row["mood"],
        source=row["source"],
        state=row["state"],
        created_at=row["created_at"],
        score=row["score"] if "score" in row.keys() and row["score"] is not None else 0,
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
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- 重み ---------------------------------------------------------

    def weights(self) -> dict[str, float]:
        """語 -> 重み。抽選のたびに読む。"""
        rows = self.conn.execute("SELECT term, weight FROM weights").fetchall()
        return {row["term"]: row["weight"] for row in rows}

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

    # --- 保存と取り出し -----------------------------------------------

    def save(
        self,
        seed: IdeaSeed,
        text: str,
        source: str,
        parent_id: int | None = None,
    ) -> Idea:
        """生成されたアイデアを `unseen` として蓄える。"""
        cursor = self.conn.execute(
            "INSERT INTO ideas "
            "(text, seed_json, domain, template_id, tone, mood, source, state,"
            " parent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'unseen', ?, ?)",
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
            ),
        )
        self.conn.commit()
        return self.get(cursor.lastrowid)  # type: ignore[arg-type]

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
