"""SQLite への蓄積と、評価による重みの更新。

ORM は使わない。テーブルは3つしかなく、SQL のほうが何が起きているか読みやすい。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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


#: 材料として受け付ける長さの上限。**文字種で分ける。**
#: 日本語の語は短く（自動収集の実測で最大6文字）、英語の語は長い
#: （authentication は14文字）。一律にすると、どちらかが不当に弾かれる。
MAX_MATERIAL_LEN_JA = 12
MAX_MATERIAL_LEN_ASCII = 24

#: かな・漢字を含むか。含めば「日本語の語」として短い上限を使う。
_JAPANESE = re.compile(r"[ぁ-んァ-ヴ一-龥々〆ヵヶ]")

#: 材料に入っていたら「語ではなく文」と判断する文字。
_NOT_A_WORD = re.compile(r"[。、！？!?,\s]")

#: 少なくとも1つは文字が要る（「！！！」を弾く）。
_HAS_LETTER = re.compile(r"[0-9A-Za-zぁ-んァ-ヴ一-龥々〆ヵヶー]")


def clean_material(term: str) -> str | None:
    """材料として使える形に整える。使えないなら None。

    材料は**文中に名詞として埋め込まれる**。文を貼られると
    「予約の締切をどうするか決めきれていないの一番よく使う部分を…」に
    なるので、語であることをここで確かめる。

    弾くのは: 空、1文字、長すぎるもの、句読点や空白を含むもの（＝文）、
    文字を含まないもの（「！！！」）。
    """
    term = term.strip()
    if len(term) < 2:
        return None
    limit = (
        MAX_MATERIAL_LEN_JA
        if _JAPANESE.search(term)
        else MAX_MATERIAL_LEN_ASCII
    )
    if len(term) > limit:
        return None
    if _NOT_A_WORD.search(term) or not _HAS_LETTER.search(term):
        return None
    return term


@dataclass(frozen=True)
class Theme:
    """いま取り組んでいること。"""

    id: int
    name: str
    note: str | None
    active: bool

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "note": self.note, "active": self.active}


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
    verdict: str | None = None
    pick_score: float = 0.0
    theme_id: int | None = None
    #: 文章化に使った文型。同じ骨格を続けて出さないための判定に使う。
    template_id: str = ""

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
            "verdict": self.verdict,
            "theme_id": self.theme_id,
            "template_id": self.template_id,
        }


#: いまの時刻を返す関数。**ここを差し替えると時間を進められる。**
#:
#: このシステムは何週間も回り続けるのが本体なのに、時刻の読み取りが
#: あちこちに散っていて長期挙動を検証できなかった（休眠が明けるか、
#: 語彙が枯れないか）。書き込みと読み取りの時刻がずれると測定にならない
#: ので、入口を1つにしてある。
def _real_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _real_clock


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """時計を差し替える。`None` で実時刻に戻す。テストと長期検証のため。"""
    global _clock
    _clock = clock or _real_clock


def utcnow() -> datetime:
    """いまの時刻（tz 付き）。引数名 `now` と紛れないよう別名にしてある。"""
    moment = _clock()
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _now() -> str:
    return utcnow().isoformat()


def _row_to_theme(row: sqlite3.Row) -> Theme:
    return Theme(
        id=row["id"], name=row["name"], note=row["note"], active=bool(row["active"])
    )


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
        verdict=row["verdict"] if "verdict" in keys else None,
        theme_id=row["theme_id"] if "theme_id" in keys else None,
        template_id=(
            row["template_id"]
            if "template_id" in keys and row["template_id"] is not None
            else ""
        ),
        pick_score=(
            row["pick_score"]
            if "pick_score" in keys and row["pick_score"] is not None
            else 0.0
        ),
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

        # **接続はスレッドごとに持つ。**
        #
        # 一つの接続を複数スレッドで共有すると、破損はしない（threadsafety=3）
        # が、**同じトランザクションを共有してしまう**。片方の commit が、
        # もう片方の書きかけを確定させる。`rate()` は「評価を記録して重みを
        # 動かす」までで一つのはずなのに、途中で確定されうる。
        #
        # `IDEAMAN_EMBED_WORKER=1`（Web とワーカーを1プロセスに同居）で
        # 実際に起きる。プロセスを分けたときは元から別接続なので問題ない。
        # WAL と busy_timeout があるので、接続を分けても待ち合わせは効く。
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

        conn = self.conn
        # 表 → 列の追加 → 索引 の順。Phase 1 の DB には新しい列がまだ無いので、
        # 索引を先に張ろうとすると落ちる。
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        conn.executescript(INDEXES_PATH.read_text(encoding="utf-8"))
        conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        """このスレッド用の接続。無ければ作る。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # Web・ワーカー・Bot が同じ DB を同時に触るため。
            # WAL なら読み手が書き手を待たない。
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

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
                # CHECK 制約は付けない。ALTER TABLE で足す列に制約を付けると
                # 既存行の検証で落ちることがあるうえ、後から外せない。
                ("verdict", "TEXT"),
                ("pick_score", "REAL NOT NULL DEFAULT 0.0"),
                ("theme_id", "INTEGER"),
            ],
            "weights": [
                ("dormant_until", "TEXT"),
                ("stall_count", "INTEGER NOT NULL DEFAULT 0"),
                # 何本目の連鎖で眠ったか。時間ではなく思考量で明けるため。
                ("slept_at_chain", "INTEGER"),
            ],
            "thought_state": [
                ("mood", "TEXT NOT NULL DEFAULT ''"),
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
        """全スレッド分の接続を閉じる。

        閉じたあとに使われても、`conn` が新しい接続を作り直すだけなので
        壊れない（テストで開き直す場面がある）。
        """
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            # すでに閉じている / 使用中でも、閉じられないだけなので進む。
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local = threading.local()

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
            "SELECT term, weight, dormant_until, stall_count, slept_at_chain "
            "FROM weights"
        ).fetchall()
        chains = self.chains_so_far()
        out: dict[str, float] = {}
        for row in rows:
            dormant = (
                datetime.fromisoformat(row["dormant_until"])
                if row["dormant_until"]
                else None
            )
            slept = row["slept_at_chain"]
            out[row["term"]] = rhythm.draw_weight(
                row["weight"],
                dormant,
                stall_count=row["stall_count"] or 0,
                chains_since=None if slept is None else chains - slept,
                now=now,
            )
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

    # --- テーマ -------------------------------------------------------
    #
    # テーマが無いと、システムは「何について考えればいいか」を知らない。
    # 汎用の語だけを衝突させても宙に浮いた警句にしかならないので、
    # 有益なアイデアの前提がここ。

    def add_theme(self, name: str, note: str | None = None) -> Theme:
        """テーマを登録する。同名があればそれを返す。"""
        self.conn.execute(
            "INSERT INTO themes (name, note, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET note = COALESCE(excluded.note, note)",
            (name, note, _now()),
        )
        self.conn.commit()
        return self.theme_by_name(name)  # type: ignore[return-value]

    def add_materials(
        self,
        theme_id: int,
        terms: Iterable[str],
        weights: Mapping[str, float] | None = None,
    ) -> int:
        """テーマの材料を足す。すでにあるものは無視する。

        Args:
            theme_id: 足す先のテーマ。
            terms: 材料の語。
            weights: 語 -> 初期の重み。`--scan` は「その語がプロジェクトの
                どれだけ中心にあるか」を知っているのに、全部 1.0 で始めると
                その情報を捨てることになる。中心的な語ほど引かれやすい状態
                から始める。**既にある重み（評価で育ったもの）は上書き
                しない**——人が付けた評価のほうが常に強い。

        Returns:
            新しく足した数。
        """
        added = 0
        for raw in terms:
            term = clean_material(raw)
            if term is None:
                continue
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO theme_terms (theme_id, term) VALUES (?, ?)",
                (theme_id, term),
            )
            added += cur.rowcount or 0
            if weights and term in weights:
                self._seed_weight("materials", term, weights[term])
        self.conn.commit()
        return added

    def _seed_weight(self, pool: str, term: str, weight: float) -> None:
        """まだ重みが無い語にだけ、初期値を置く。

        既にある重みは触らない。評価で育った値や、休眠の記録を
        自動収集で踏み潰さないため。
        """
        weight = min(max(weight, MIN_WEIGHT), MAX_WEIGHT)
        self.conn.execute(
            "INSERT OR IGNORE INTO weights (pool, term, weight) VALUES (?, ?, ?)",
            (pool, term, weight),
        )

    def drop_materials(self, theme_id: int, terms: Iterable[str]) -> int:
        """材料を外す。外した数を返す。

        `--scan` は 25 語まとめて入れるので、外す手段が無いと使えない
        （いらない語が1つ混ざるだけで、その語を含む案が出続ける）。
        """
        dropped = 0
        for term in terms:
            term = term.strip()
            if not term:
                continue
            cur = self.conn.execute(
                "DELETE FROM theme_terms WHERE theme_id = ? AND term = ?",
                (theme_id, term),
            )
            if cur.rowcount:
                dropped += cur.rowcount
                self._forget_stock_with(theme_id, term)
        self.conn.commit()
        return dropped

    def _forget_stock_with(self, theme_id: int, term: str) -> int:
        """その語を含む在庫を、出す対象から外す。

        材料を外しても在庫は残るので、外したはずの語がそのあとも出続ける。
        `doctor` は「残しておくと、これを含む案が出続けます」と言って外させる
        のだから、外したらそこで途切れないと嘘になる。

        **消しはしない。** 連鎖の子が親を指しているので消すと壊れるし、
        蓄積は捨てない方針でもある。`chosen` を `kept` に落とすだけ——
        出す対象から外れて、記録には残る。見せたものには触らない。
        """
        cur = self.conn.execute(
            "UPDATE ideas SET verdict = 'kept' "
            "WHERE state = 'unseen' AND verdict = 'chosen' "
            "AND theme_id = ? AND text LIKE ?",
            (theme_id, f"%{term}%"),
        )
        return cur.rowcount or 0

    def materials(self, theme_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT term FROM theme_terms WHERE theme_id = ? ORDER BY id", (theme_id,)
        ).fetchall()
        return [row["term"] for row in rows]

    def themes(self, active_only: bool = True) -> list[Theme]:
        sql = "SELECT * FROM themes"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id"
        return [_row_to_theme(r) for r in self.conn.execute(sql).fetchall()]

    def theme_by_name(self, name: str) -> Theme | None:
        row = self.conn.execute(
            "SELECT * FROM themes WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_theme(row) if row else None

    def theme(self, theme_id: int) -> Theme | None:
        row = self.conn.execute(
            "SELECT * FROM themes WHERE id = ?", (theme_id,)
        ).fetchone()
        return _row_to_theme(row) if row else None

    def set_theme_active(self, theme_id: int, active: bool) -> None:
        """一時的に考えるのをやめる／再開する。テーマは消さない。"""
        self.conn.execute(
            "UPDATE themes SET active = ? WHERE id = ?", (1 if active else 0, theme_id)
        )
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
        now = now or utcnow()
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
        now = now or utcnow()
        chains = self.chains_so_far()
        for pool, term in terms:
            row = self.conn.execute(
                "SELECT weight, stall_count FROM weights WHERE pool = ? AND term = ?",
                (pool, term),
            ).fetchone()
            weight = row["weight"] if row else 1.0
            stalls = (row["stall_count"] if row else 0) + 1
            until = rhythm.dormancy_for(stalls, now=now)
            self.conn.execute(
                "INSERT INTO weights "
                "(pool, term, weight, dormant_until, stall_count, slept_at_chain) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (pool, term) DO UPDATE SET "
                "dormant_until = excluded.dormant_until, "
                "stall_count = excluded.stall_count, "
                "slept_at_chain = excluded.slept_at_chain",
                (pool, term, weight, until.isoformat(), stalls, chains),
            )
        self.conn.commit()

    def chains_so_far(self) -> int:
        """これまでに始まった連鎖の本数。

        休眠を**時間ではなく思考量で**明けるための物差し。連鎖 id は
        単調に増えるので、そのまま数えればよい。
        """
        row = self.conn.execute("SELECT MAX(chain_id) FROM ideas").fetchone()
        return row[0] or 0

    def dormant_terms(self, now: datetime | None = None) -> list[str]:
        """いま眠っている語（aspiration で通るものは除く）。"""
        now = now or utcnow()
        chains = self.chains_so_far()
        rows = self.conn.execute(
            "SELECT term, weight, dormant_until, stall_count, slept_at_chain "
            "FROM weights WHERE dormant_until IS NOT NULL AND dormant_until > ?",
            (now.isoformat(),),
        ).fetchall()
        asleep = []
        for row in rows:
            if row["weight"] >= rhythm.ASPIRATION_WEIGHT:
                continue
            slept = row["slept_at_chain"]
            since = None if slept is None else chains - slept
            if not rhythm.is_awake(
                datetime.fromisoformat(row["dormant_until"]),
                row["stall_count"] or 0,
                since,
                now=now,
            ):
                asleep.append(row["term"])
        return asleep

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
            # `in row` は**列名ではなく値**を見る（sqlite3.Row の仕様）。
            # `.keys()` を外すと、古い DB の判定が静かに壊れる。
            mood=row["mood"] if "mood" in row.keys() else "",  # noqa: SIM118
        )

    def save_thought_state(self, state: rhythm.ThoughtState) -> None:
        self.conn.execute(
            "INSERT INTO thought_state "
            "(id, mode, chain_id, chain_length, stale_hits, mood, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "mode = excluded.mode, chain_id = excluded.chain_id, "
            "chain_length = excluded.chain_length, stale_hits = excluded.stale_hits, "
            "mood = excluded.mood, updated_at = excluded.updated_at",
            (
                state.mode,
                state.chain_id,
                state.chain_length,
                state.stale_hits,
                state.mood,
                _now(),
            ),
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
        theme_id: int | None = None,
    ) -> Idea:
        """生成されたアイデアを `unseen` として蓄える。

        `chain_id` を省略すると、自分自身の ID が連鎖の識別子になる
        （＝新しい連鎖の起点）。
        """
        cursor = self.conn.execute(
            "INSERT INTO ideas "
            "(text, seed_json, domain, template_id, tone, mood, source, state,"
            " parent_id, created_at, signature, chain_id, mode, theme_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'unseen', ?, ?, ?, ?, ?, ?)",
            (
                text,
                json.dumps(
                    {
                        "slots": seed.slots,
                        "terms": [list(t) for t in seed.terms],
                        "seed": seed.seed,
                        # 選別のスコアに使うので残す（列は増やさない）
                        "novelty": seed.novelty,
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
                theme_id,
            ),
        )
        idea_id = cursor.lastrowid
        if chain_id is None:
            self.conn.execute(
                "UPDATE ideas SET chain_id = ? WHERE id = ?", (idea_id, idea_id)
            )
        self.conn.commit()
        return self.get(idea_id)  # type: ignore[arg-type]

    def novelty_of(self, idea_id: int) -> float:
        """そのアイデアの語がどれだけ離れていたか。古い行には無いので 0.0。"""
        row = self.conn.execute(
            "SELECT seed_json FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if row is None:
            return 0.0
        return float(json.loads(row["seed_json"]).get("novelty", 0.0))

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

    def take_ready(self, domain: str | None = None) -> Idea | None:
        """在庫から1件取り出して `seen` にする。無ければ None。

        在庫とは **連鎖が閉じたときに選ばれたもの**（`verdict='chosen'`）だけ。
        連鎖の途中（まだ判定していないもの）や、選ばれなかったものは出さない。
        独り言を垂れ流さないための関門がここ。

        FIFO ではなくスコアの高い順に出す。溜まった `chosen` は質のプール
        として機能させる。

        **有効なテーマが一つでもあれば、そのテーマの在庫しか出さない。**
        起動時の先読みで作ったテーマ無しの在庫が、後から登録したテーマより
        先に出ると、テーマを登録した直後のボタンで汎用の警句が返る（実際に
        新規デプロイでこれが起きた）。並べ替えでは足りない——テーマ付きの
        在庫がまだ1件も無い瞬間は、結局テーマ無しが出てしまうため。

        古い在庫は**消さない**。全テーマを休ませれば、また出てくる。
        該当が無ければ None を返し、呼び出し側がその場で考える。
        """
        clause = "state = 'unseen' AND verdict = 'chosen'"
        params: tuple = ()
        if domain is not None:
            clause += " AND domain = ?"
            params = (domain,)
        if self.themes(active_only=True):
            clause += " AND theme_id IN (SELECT id FROM themes WHERE active = 1)"
        row = self.conn.execute(
            f"SELECT * FROM ideas WHERE {clause} ORDER BY pick_score DESC, id LIMIT 1",
            params,
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
        """出せる在庫の件数。

        「考えた数」ではなく「選ばれてまだ出していない数」。連鎖の途中の
        独り言は含まない。

        `take_ready` と同じ条件で数える。ここがずれると「5個溜まっている」
        と表示しながらボタンを押すと在庫が使われない、という嘘になる。
        """
        clause = "state = 'unseen' AND verdict = 'chosen'"
        params: tuple = ()
        if domain is not None:
            clause += " AND domain = ?"
            params = (domain,)
        if self.themes(active_only=True):
            clause += " AND theme_id IN (SELECT id FROM themes WHERE active = 1)"
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM ideas WHERE {clause}", params
        ).fetchone()
        return row["n"]

    def unjudged(self) -> list[Idea]:
        """まだ判定していないもの（＝連鎖が閉じていない独り言）。"""
        rows = self.conn.execute(
            "SELECT * FROM ideas WHERE verdict IS NULL ORDER BY id"
        ).fetchall()
        return [_row_to_idea(row) for row in rows]

    def recent_chosen_templates(self, limit: int = 8) -> list[str]:
        """直近で表に出したアイデアの文型を、新しい順に返す。

        同じ文型が続くと、内容が違っても読み手には同じものに見える
        （「いちばんヘビーに使う一人のために◯◯を作り直す」が3回続く等）。
        選ぶときにこれを避けるために使う。
        """
        rows = self.conn.execute(
            "SELECT template_id FROM ideas WHERE verdict = 'chosen' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["template_id"] for r in rows]

    def recent_chosen_terms(self, limit: int = 8) -> list[set[str]]:
        """直近で表に出したアイデアが使った語を、新しい順に返す。

        文型だけを見ていては足りなかった。「いちばんヘビーに使う一人の
        ためだけに◯◯を作り直す。まず**頻度**が変わるはず」が、材料だけ
        変えて何度も出ていた。**側面や操作が同じなら、材料が違っても
        読み手には同じ案に見える。**
        """
        rows = self.conn.execute(
            "SELECT seed_json FROM ideas WHERE verdict = 'chosen' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[set[str]] = []
        for row in rows:
            try:
                raw = json.loads(row["seed_json"])
            except (ValueError, TypeError):
                continue
            out.append({term for _, term in raw.get("terms", ())})
        return out

    def recent_scores(self, limit: int = 50) -> list[float]:
        """直近に判定したもののスコア。単発の思いつきを測る物差しに使う。"""
        rows = self.conn.execute(
            "SELECT pick_score FROM ideas WHERE verdict IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["pick_score"] for row in rows]

    def set_verdict(self, idea_id: int, verdict: str, score: float = 0.0) -> None:
        """選別の結果を書く。

        Args:
            verdict: `chosen`（出す）か `kept`（残すが出さない）。
        """
        if verdict not in ("chosen", "kept"):
            raise ValueError(f"verdict は chosen か kept です: {verdict!r}")
        self.conn.execute(
            "UPDATE ideas SET verdict = ?, pick_score = ? WHERE id = ?",
            (verdict, score, idea_id),
        )
        self.conn.commit()

    def update_text(self, idea_id: int, text: str, source: str) -> None:
        """本文を差し替える。選ばれたものだけを磨くときに使う。"""
        self.conn.execute(
            "UPDATE ideas SET text = ?, source = ? WHERE id = ?",
            (text, source, idea_id),
        )
        self.conn.commit()

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
        """最後に**届いた**時刻（Bot が実際に投稿した時刻）。

        `last_queued_at` との違いに注意。ワーカーのスケジュール判定に
        これを使ってはいけない——Bot が止まっていると永久に NULL のままで、
        毎 tick 配信を積み続けることになる。
        """
        row = self.conn.execute(
            "SELECT sent_at FROM deliveries "
            "WHERE channel = ? AND sent_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (channel,),
        ).fetchone()
        return datetime.fromisoformat(row["sent_at"]) if row and row["sent_at"] else None

    def last_queued_at(self, channel: str) -> datetime | None:
        """最後に**配信を決めた**時刻。まだ届いていなくても数える。

        ワーカーが「この時間枠はもう配信済みか」を判断するのはこちら。
        届いたかどうかは Bot の都合であって、ワーカーの都合ではない。
        """
        row = self.conn.execute(
            "SELECT created_at FROM deliveries WHERE channel = ? "
            "ORDER BY id DESC LIMIT 1",
            (channel,),
        ).fetchone()
        return datetime.fromisoformat(row["created_at"]) if row else None

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
