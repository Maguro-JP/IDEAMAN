"""設定の健康診断。

このシステムは**黙って劣化する**。テーマを登録し忘れれば警句しか出ず、
DB が揮発する場所にあれば毎回記憶を失い、材料に的外れな語が1つ混ざれば
その語を含む案が出続ける。どれもエラーにはならないので、動いているように
見えたまま役に立たなくなる。

だから診断する。見つけた問題ごとに、**次に打つコマンドまで書く**。

`diversity` との違い: あちらは「思考のリズムが健康か」を見る。こちらは
「設定が正しいか」を見る。前者は動かした後、後者は動かす前。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .harvest import STOPWORDS
from .store import Store

#: これより長く何も考えていなければ、ワーカーが止まっている可能性が高い。
STALE_AFTER = timedelta(hours=6)

#: 材料がこの数を下回ると、同じ語ばかりが出る。
THIN_MATERIALS = 4

#: 評価がこの数に満たないうちは、選定のスコアはほぼ機能しない。
RATINGS_FOR_SCORING = 20

#: 揮発しやすい置き場所。ここに DB があると再起動で蓄積が消える。
EPHEMERAL_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")


@dataclass
class Finding:
    """見つかったこと。"""

    #: ok / warn / bad
    level: str
    title: str
    detail: str
    #: 次に打つコマンド。無ければ None。
    fix: str | None = None

    def __str__(self) -> str:
        mark = {"ok": "  ", "warn": "！", "bad": "×"}[self.level]
        out = f"{mark} {self.title}\n     {self.detail}"
        if self.fix:
            out += f"\n     → {self.fix}"
        return out


@dataclass
class Diagnosis:
    findings: list[Finding] = field(default_factory=list)

    @property
    def worst(self) -> str:
        for level in ("bad", "warn"):
            if any(f.level == level for f in self.findings):
                return level
        return "ok"

    def report(self) -> str:
        lines = [str(f) for f in self.findings]
        tail = {
            "ok": "\n問題ありません。",
            "warn": "\n動きますが、上の！を直すと出てくる案が良くなります。",
            "bad": "\n×の項目を直さないと、有益な案は出ません。",
        }[self.worst]
        return "\n".join(lines) + "\n" + tail


def _check_storage(store: Store) -> Finding:
    path = Path(store.path).resolve()
    if any(str(path).startswith(root) for root in EPHEMERAL_ROOTS):
        return Finding(
            "bad",
            "DB が消える場所にあります",
            f"{path} は再起動で消えます。溜めて育つのがこのシステムの本体なので、"
            "ここに置くと毎回ゼロからやり直しになります。",
            "IDEAMAN_DB=/data/ideaman.db のように永続ディスク上を指す",
        )
    return Finding("ok", "保管庫", f"{path}")


def _check_themes(store: Store) -> list[Finding]:
    themes = store.themes(active_only=False)
    active = [t for t in themes if t.active]
    if not themes:
        return [
            Finding(
                "bad",
                "テーマが登録されていません",
                "何について考えればいいか分からないので、汎用の語をぶつけた"
                "警句しか作れません。ここが有益さの入口です。",
                'ideaman theme "いま取り組んでいること" --scan .',
            )
        ]
    if not active:
        return [
            Finding(
                "warn",
                "有効なテーマがありません",
                f"{len(themes)} 件ありますが全部休んでいます。",
                'ideaman theme "テーマ名" --on',
            )
        ]

    out: list[Finding] = []
    for theme in active:
        materials = store.materials(theme.id)
        junk = [m for m in materials if m.lower() in STOPWORDS or len(m) < 2]
        if not materials:
            out.append(
                Finding(
                    "bad",
                    f"「{theme.name}」に材料がありません",
                    "テーマ名だけでは案を具体的にできません。あなた自身の語が要ります。",
                    f'ideaman theme "{theme.name}" --scan .',
                )
            )
        elif len(materials) < THIN_MATERIALS:
            out.append(
                Finding(
                    "warn",
                    f"「{theme.name}」の材料が {len(materials)} 語しかありません",
                    "同じ語ばかりが出ます。10語を超えるあたりから当たりが増えます。",
                    f'ideaman theme "{theme.name}" --scan .',
                )
            )
        else:
            out.append(
                Finding(
                    "ok",
                    f"「{theme.name}」",
                    f"材料 {len(materials)} 語: {'、'.join(materials[:8])}"
                    + ("…" if len(materials) > 8 else ""),
                )
            )
        if junk:
            out.append(
                Finding(
                    "warn",
                    f"「{theme.name}」に的外れな材料があります",
                    f"{'、'.join(junk)} は、どのプロジェクトにも出てくる語です。"
                    "残しておくと、これを含む案が出続けます。",
                    f'ideaman theme "{theme.name}" --drop {",".join(junk)}',
                )
            )
    return out


def _check_thinking(store: Store) -> list[Finding]:
    out: list[Finding] = []
    stock = store.stock_count()
    row = store.conn.execute("SELECT MAX(created_at) AS t FROM ideas").fetchone()
    last = row["t"] if row else None

    if last is None:
        out.append(
            Finding(
                "warn",
                "まだ一度も考えていません",
                "在庫が空なので、ボタンを押すとその場で考えることになります。",
                "ideaman worker  （常駐して考え続ける）",
            )
        )
        return out

    when = datetime.fromisoformat(last)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    idle = datetime.now(timezone.utc) - when

    if idle > STALE_AFTER:
        out.append(
            Finding(
                "warn",
                "しばらく何も考えていません",
                f"最後に考えたのは {int(idle.total_seconds() // 3600)} 時間前です。"
                "ワーカーが止まっている可能性があります。",
                "ideaman worker  /  IDEAMAN_EMBED_WORKER=1 で Web と同居させる",
            )
        )
    else:
        out.append(Finding("ok", "思考", f"最後に考えたのは {int(idle.total_seconds() // 60)} 分前"))

    if stock == 0:
        out.append(
            Finding(
                "warn",
                "出せる在庫がありません",
                "ボタンを押すとその場で考えます（数秒かかります）。"
                "有効なテーマの在庫だけを数えるので、テーマを変えた直後は 0 になります。",
                "ideaman worker --once",
            )
        )
    else:
        out.append(Finding("ok", "在庫", f"{stock} 件"))
    return out


def _check_learning(store: Store) -> Finding:
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM ratings WHERE score != 0"
    ).fetchone()
    rated = row["n"]
    if rated < RATINGS_FOR_SCORING:
        return Finding(
            "warn",
            f"評価が {rated} 件です",
            "評価が溜まるまで、選定のスコアはほぼ「語の遠さ」しか見ていません。"
            f"{RATINGS_FOR_SCORING} 件を超えるあたりから、あなたの好みが効き始めます。",
            "出てきた案に 👍 / 👎 を付ける（Web・Discord・Claude のどれからでも）",
        )
    return Finding("ok", "学習", f"評価 {rated} 件。重みが育っています")


def diagnose(store: Store) -> Diagnosis:
    """設定を診断する。"""
    findings = [_check_storage(store)]
    findings += _check_themes(store)
    findings += _check_thinking(store)
    findings.append(_check_learning(store))
    return Diagnosis(findings)
