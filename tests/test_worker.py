"""常駐ワーカーのテスト。

外部サービスは一切叩かない。配信は DB のキューに積むだけなので、
Discord が無くてもここは全部検証できる。
"""

from datetime import datetime, time as dtime, timedelta

import pytest

from ideaman.notify import Notifier, QueueNotifier, deliver_all
from ideaman.worker import Worker, due_now, parse_schedule


# --- スケジュール ----------------------------------------------------


def test_parse_schedule():
    assert parse_schedule("08:00,21:30") == [dtime(8, 0), dtime(21, 30)]


def test_parse_schedule_sorts_and_ignores_blanks():
    assert parse_schedule("21:00, 08:00 ,") == [dtime(8, 0), dtime(21, 0)]


def test_parse_schedule_rejects_empty():
    with pytest.raises(ValueError):
        parse_schedule("  ")


def test_due_when_never_delivered():
    assert due_now([dtime(8, 0)], None, datetime(2026, 8, 5, 9, 0))


def test_due_after_a_scheduled_time_passes():
    last = datetime(2026, 8, 5, 7, 0)
    assert due_now([dtime(8, 0)], last, datetime(2026, 8, 5, 8, 1))


def test_not_due_before_the_next_time():
    last = datetime(2026, 8, 5, 8, 1)
    assert not due_now([dtime(8, 0), dtime(21, 0)], last, datetime(2026, 8, 5, 12, 0))


def test_not_due_twice_for_the_same_slot():
    last = datetime(2026, 8, 5, 8, 30)
    assert not due_now([dtime(8, 0)], last, datetime(2026, 8, 5, 9, 0))


def test_downtime_does_not_cause_a_burst():
    """3日止まっていても、まとめて撃たずに1回だけ。"""
    last = datetime(2026, 8, 2, 8, 30)
    schedule = [dtime(8, 0), dtime(13, 0), dtime(21, 0)]
    now = datetime(2026, 8, 5, 14, 0)

    assert due_now(schedule, last, now)
    # 1回配ったことにすると、次の時刻までは配らない
    assert not due_now(schedule, now, now)


# --- ワーカー --------------------------------------------------------


def test_think_keeps_the_stock_topped_up(store):
    worker = Worker(store, stock=4, use_llm=False)
    assert worker.think() == 4
    assert store.stock_count() == 4
    # すでに足りていれば考えない
    assert worker.think() == 0


def test_tick_queues_a_delivery(store):
    worker = Worker(store, [QueueNotifier(store, "discord")], stock=3, use_llm=False)
    worker.tick(now=datetime(2026, 8, 5, 9, 0))

    pending = store.pending_deliveries("discord")
    assert len(pending) == 1


def test_no_delivery_without_notifiers(store):
    worker = Worker(store, [], stock=2, use_llm=False)
    assert not worker.maybe_deliver(now=datetime(2026, 8, 5, 9, 0))


def test_delivery_is_not_repeated_within_a_slot(store):
    worker = Worker(store, [QueueNotifier(store, "discord")], stock=3, use_llm=False)
    now = datetime(2026, 8, 5, 9, 0)

    assert worker.maybe_deliver(now=now)
    delivery_id, _ = store.pending_deliveries("discord")[0]
    store.mark_delivered(delivery_id, "msg-1")

    # 同じ時間帯では二度配らない
    assert not worker.maybe_deliver(now=now)


def test_one_failing_notifier_does_not_block_the_others(store):
    """届かないことより、道連れで止まることのほうが困る。"""
    delivered = []

    class Broken:
        name = "broken"

        def deliver(self, idea):
            raise RuntimeError("死んでいる")

    class Working:
        name = "working"

        def deliver(self, idea):
            delivered.append(idea.id)
            return "ok"

    from ideaman import service

    idea = service.generate_idea(store, use_llm=False)
    deliver_all([Broken(), Working()], idea)

    assert delivered == [idea.id]


def test_queue_notifier_round_trip(store):
    from ideaman import service

    idea = service.generate_idea(store, use_llm=False)
    QueueNotifier(store, "discord").deliver(idea)

    pending = store.pending_deliveries("discord")
    assert [i.id for _, i in pending] == [idea.id]

    store.mark_delivered(pending[0][0], "message-42")
    assert store.pending_deliveries("discord") == []
    # 外部IDから引けること（リアクション評価の逆引き）
    assert store.idea_by_external_id("discord", "message-42").id == idea.id


def test_failed_delivery_is_recorded_and_not_retried_forever(store):
    from ideaman import service

    idea = service.generate_idea(store, use_llm=False)
    delivery_id = store.queue_delivery(idea.id, "discord")
    store.mark_delivered(delivery_id, None, failed=True)

    assert store.pending_deliveries("discord") == []
