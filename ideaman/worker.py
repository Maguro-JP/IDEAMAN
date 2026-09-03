"""常駐ワーカー。

裏で考え続けて在庫を保ち、時間が来たら配る。これが「自発的に思考している」
の実体で、機構としてはただのループ。面白いのは何を考えるか（`rhythm`）の方。

    python -m ideaman worker
    python -m ideaman worker --once
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from datetime import time as dtime

from . import service
from .notify import Notifier, deliver_all
from .store import Store

log = logging.getLogger(__name__)

#: 在庫を保つ目標数。
DEFAULT_STOCK = 5

#: 何秒ごとに「時間が来たか」を確認するか。
TICK_SECONDS = 60

#: 既定の配信時刻。1日に数回。ウザくならない程度。
DEFAULT_SCHEDULE = "08:00,13:00,21:00"


def parse_schedule(raw: str) -> list[dtime]:
    """`"08:00,13:00"` を時刻の一覧にする。

    Raises:
        ValueError: 時刻として読めない要素があった場合。
    """
    times = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        hour, _, minute = chunk.partition(":")
        times.append(dtime(hour=int(hour), minute=int(minute or 0)))
    if not times:
        raise ValueError(f"配信時刻が読めません: {raw!r}")
    return sorted(times)


def due_now(
    schedule: list[dtime],
    last_sent: datetime | None,
    now: datetime,
) -> bool:
    """いま配る時間か。

    直近の配信時刻を過ぎていて、かつその時刻より後にまだ配っていなければ配る。
    落ちていた間の分をまとめて撃たないよう、**遡って配るのは最後の1回分だけ**。
    """
    today = now.date()
    passed = [datetime.combine(today, t) for t in schedule]
    passed = [p for p in passed if p <= now]
    if not passed:
        # 今日はまだ一度も時刻が来ていない。昨日の最後の時刻を見る。
        yesterday = datetime.combine(today - timedelta(days=1), schedule[-1])
        passed = [yesterday]

    latest = passed[-1]
    if last_sent is None:
        return True
    return last_sent.replace(tzinfo=None) < latest


class Worker:
    """考え続けて配るループ。"""

    def __init__(
        self,
        store: Store,
        notifiers: list[Notifier] | None = None,
        *,
        stock: int = DEFAULT_STOCK,
        schedule: list[dtime] | None = None,
        use_llm: bool = True,
        channel: str = "discord",
    ) -> None:
        self.store = store
        self.notifiers = notifiers or []
        self.stock = stock
        self.schedule = schedule or parse_schedule(
            os.environ.get("IDEAMAN_SCHEDULE", DEFAULT_SCHEDULE)
        )
        self.use_llm = use_llm
        self.channel = channel

    def think(self) -> int:
        """在庫を保つ。新しく考えた数を返す。"""
        made = service.prefill(self.store, self.stock, use_llm=self.use_llm)
        if made:
            log.info("%d 件考えた（在庫 %d）", made, self.store.stock_count())
        return made

    def maybe_deliver(self, now: datetime | None = None) -> bool:
        """時間が来ていれば1件配る。配ったら True。"""
        now = now or datetime.now()
        if not self.notifiers:
            return False
        # **届いた時刻ではなく、配信を決めた時刻**で判断する。届いたかどうかは
        # Bot の都合。Bot が止まっているあいだ毎 tick 配信を積み続けると、
        # 復帰した瞬間に何百件も流れる（実測で3日で864件積んでいた）。
        last = self.store.last_queued_at(self.channel)
        if not due_now(self.schedule, last, now):
            return False

        idea = service.receive_idea(self.store, use_llm=self.use_llm)
        deliver_all(self.notifiers, idea)
        return True

    def tick(self, now: datetime | None = None) -> None:
        """1周ぶんの仕事。"""
        self.think()
        self.maybe_deliver(now)

    def run(self, tick_seconds: int = TICK_SECONDS) -> None:
        """止められるまで回り続ける。"""
        log.info(
            "ワーカー開始（在庫 %d, 配信 %s）",
            self.stock,
            ",".join(t.strftime("%H:%M") for t in self.schedule),
        )
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick に失敗した。次の周回で続ける")
            time.sleep(tick_seconds)
