"""配信先の抽象化。

LINE や他の宛先を足すときに触る場所を1箇所に閉じ込めるためのもの。
実際の送信はプロセスが別なので、ワーカーは DB のキューに積むだけ
（`QueueNotifier`）で、受け取り側の Bot がそれを拾う。
"""

from __future__ import annotations

import logging
from typing import Protocol

from .store import Idea, Store

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """アイデアを1つ届ける先。"""

    name: str

    def deliver(self, idea: Idea) -> str | None:
        """届けて、外部での識別子（あれば）を返す。"""
        ...


class QueueNotifier:
    """DB のキューに積むだけの配信先。

    Discord Bot はワーカーと別プロセスなので直接呼べない。ここで
    `deliveries` に pending を書き、Bot 側がポーリングして投稿する。
    Redis 等を持ち込まずにプロセスを分けるための最小限の仕掛け。
    """

    def __init__(self, store: Store, channel: str) -> None:
        self.store = store
        self.name = channel

    def deliver(self, idea: Idea) -> str | None:
        self.store.queue_delivery(idea.id, self.name)
        log.info("%s への配信を積んだ: idea=%s", self.name, idea.id)
        return None


class ConsoleNotifier:
    """ターミナルに出すだけの配信先。動作確認用。"""

    name = "console"

    def deliver(self, idea: Idea) -> str | None:
        print(f"\n  {idea.text}\n")
        return None


def deliver_all(notifiers: list[Notifier], idea: Idea) -> None:
    """全ての配信先へ配る。

    1つが失敗しても他は続ける。届かないことより、届く先が道連れで
    止まることのほうが困る。
    """
    for notifier in notifiers:
        try:
            notifier.deliver(idea)
        except Exception:
            log.exception("%s への配信に失敗した", getattr(notifier, "name", "?"))
