"""常駐ワーカーのテスト。

外部サービスは一切叩かない。配信は DB のキューに積むだけなので、
Discord が無くてもここは全部検証できる。
"""

from datetime import datetime, timedelta
from datetime import time as dtime

import pytest

from ideaman.notify import QueueNotifier, deliver_all
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
    """在庫は「選ばれてまだ出していない数」。

    連鎖が閉じるまで選別が走らないので、4件確保するのに何回も考える。
    """
    worker = Worker(store, stock=4, use_llm=False)
    assert worker.think() >= 4  # 返るのは考えた回数
    assert store.stock_count() >= 4
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


# --- Bot が止まっていても配信を積み続けないこと --------------------------
# ワーカーは「届いた時刻」ではなく「配信を決めた時刻」で判断する。
# 届いたかどうかは Bot の都合であって、ワーカーの都合ではない。


def test_the_worker_does_not_queue_on_every_tick(store, monkeypatch):
    """Bot が一度も投稿していなくても、1日3回で止まること。

    `last_delivery_at`（Bot が投稿した時刻）で判断していたとき、Bot が
    動いていないと永久に「まだ配っていない」になり、**3日で864件**積んで
    いた。復帰した瞬間に全部流れる——「ずっとアイデアが溢れてくる Bot」。
    """
    from datetime import datetime

    import ideaman.store as store_module
    from ideaman import service
    from ideaman.notify import QueueNotifier
    from ideaman.worker import Worker, parse_schedule

    clock = {"now": datetime(2026, 9, 1, 0, 0)}
    monkeypatch.setattr(store_module, "_now", lambda: clock["now"].isoformat())

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫"])
    for _ in range(80):
        service.generate_idea(store, use_llm=False)

    worker = Worker(
        store,
        [QueueNotifier(store, "discord")],
        stock=3,
        schedule=parse_schedule("08:00,13:00,21:00"),
        use_llm=False,
    )

    delivered = 0
    for minute in range(0, 2 * 24 * 60, 5):
        clock["now"] = datetime(2026, 9, 1, 0, 0) + timedelta(minutes=minute)
        if worker.maybe_deliver(clock["now"]):
            delivered += 1

    # 2日 = 6回 + 起動直後の1回。Bot は一度も投稿していない。
    assert delivered <= 8, f"{delivered} 件も積んでいる"
    assert delivered >= 6, f"{delivered} 件しか配っていない"
    assert store.last_delivery_at("discord") is None, "前提が崩れている"


def test_queued_and_delivered_are_different_questions(store):
    """「配信を決めた」と「届いた」を混同しないこと。"""
    from ideaman import service

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙"])
    idea = service.generate_idea(store, use_llm=False)

    delivery_id = store.queue_delivery(idea.id, "discord")
    assert store.last_queued_at("discord") is not None
    assert store.last_delivery_at("discord") is None, "まだ誰も見ていない"

    store.mark_delivered(delivery_id, "123")
    assert store.last_delivery_at("discord") is not None
