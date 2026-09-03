"""長期稼動。

このシステムは何週間も回り続けるのが本体なのに、長く回したことが
一度も無かった。時間を進めて、枯れないこと・溢れないことを確かめる。

**時計は一つにする。** 書き込みだけ仮想時計にして読み取りを実時刻のまま
にすると、休眠中の語が99件あるように見える（実際は0件）。最初の測定は
それで間違えた。
"""

from datetime import UTC, datetime, timedelta

import pytest

from ideaman import store as store_module
from ideaman.diversity import measure
from ideaman.notify import QueueNotifier
from ideaman.store import Store
from ideaman.worker import Worker, parse_schedule

START = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def clock():
    """止まった時計。テストが終わったら必ず実時刻に戻す。"""
    state = {"now": START}
    store_module.set_clock(lambda: state["now"])
    try:
        yield state
    finally:
        store_module.set_clock(None)


@pytest.fixture
def running(tmp_path, clock):
    store = Store(tmp_path / "long.db")
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫", "評価", "休眠"])
    worker = Worker(
        store,
        [QueueNotifier(store, "discord")],
        stock=4,
        schedule=parse_schedule("08:00,13:00,21:00"),
        use_llm=False,
    )
    try:
        yield store, worker, clock
    finally:
        store.close()


def _run_days(store, worker, clock, days: int) -> None:
    for day in range(days):
        for hour in (0, 8, 13, 21):
            clock["now"] = START + timedelta(days=day, hours=hour)
            worker.tick(clock["now"].replace(tzinfo=None))


def test_ten_days_of_running_stays_healthy(running):
    store, worker, clock = running
    _run_days(store, worker, clock, 10)

    health = measure(store, limit=3000)

    # 語彙が枯れていないこと。休眠は一時的で、明けて戻ってくる。
    assert health.revived > 0, "休眠から一語も戻っていない"
    assert health.dormant_now < 30, f"{health.dormant_now} 語が眠ったまま"

    # 少数の語に収束していないこと
    assert health.top_share < 0.35, f"上位5語に {health.top_share:.0%} 集中している"
    assert health.coverage > 0.4, f"語彙の {health.coverage:.0%} しか使っていない"
    # 材料も分母に入るので 100% を超えないこと
    assert health.coverage <= 1.0

    # 在庫を保てていること
    assert store.stock_count() >= 2


def test_ten_days_delivers_three_times_a_day(running):
    """溜まった分をまとめて撃たないこと。"""
    store, worker, clock = running
    _run_days(store, worker, clock, 10)

    queued = store.conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
    # 10日 × 3回 + 起動直後の1回。多少の前後は許すが、桁は違わない。
    assert 25 <= queued <= 35, f"10日で {queued} 件も配信している"


def test_the_archive_keeps_everything(running):
    """捨てないこと。選ばれなかった思考も残る。"""
    store, worker, clock = running
    _run_days(store, worker, clock, 5)

    total = store.conn.execute("SELECT COUNT(*) AS n FROM ideas").fetchone()["n"]
    kept = store.conn.execute(
        "SELECT COUNT(*) AS n FROM ideas WHERE verdict = 'kept'"
    ).fetchone()["n"]
    assert total > 20
    assert kept > 0, "選ばれなかったものが残っていない"


def test_the_clock_can_be_put_back(tmp_path):
    """時計を戻し忘れると、他のテストが壊れる。"""
    state = {"now": START}
    store_module.set_clock(lambda: state["now"])
    store_module.set_clock(None)
    assert (store_module.utcnow() - datetime.now(UTC)).total_seconds() < 5
