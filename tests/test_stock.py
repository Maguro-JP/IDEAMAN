"""在庫（＝すでに考えてあったもの）の振る舞い。

「裏で勝手に考えている」感を支えているのはここ。ボタンは生成を依頼するので
はなく、溜まっている考えを取り出す。
"""

from ideaman import service
from ideaman.engine import combinator


def test_receive_takes_from_stock_without_generating(store, monkeypatch):
    service.prefill(store, 1, use_llm=False)
    assert store.stock_count() >= 1

    def fail(*args, **kwargs):
        raise AssertionError("在庫があるのに新規生成が走った")

    monkeypatch.setattr(combinator, "combine", fail)
    received = service.receive_idea(store, use_llm=False)

    assert received.state == "seen"
    assert received.verdict == "chosen"


def test_receive_generates_when_stock_is_empty(store):
    assert store.stock_count() == 0
    idea = service.receive_idea(store, use_llm=False)
    assert idea.state == "seen"


def test_receive_never_returns_a_mid_chain_fragment(store):
    """連鎖の途中を単独で出さない。

    文脈なしに一片だけ流れてくると意味不明になるので、判定が付いたもの
    しか外に出さない。
    """
    for _ in range(30):
        idea = service.receive_idea(store, use_llm=False)
        assert idea.verdict is not None, "判定前のもの（独り言）が出てきた"


def test_same_idea_is_never_handed_out_twice(store):
    service.prefill(store, 5, use_llm=False)
    seen = [service.receive_idea(store, use_llm=False).id for _ in range(5)]
    assert len(set(seen)) == 5


def test_prefill_tops_up_to_target_only(store):
    """在庫は「選ばれた数」。連鎖が閉じるまで増えないので何回も考える。"""
    thought = service.prefill(store, 3, use_llm=False)
    assert thought >= 3  # 返るのは考えた回数
    assert store.stock_count() >= 3
    # すでに足りていれば考えない
    assert service.prefill(store, 3, use_llm=False) == 0


def test_prefill_accounts_for_consumed_stock(store):
    service.prefill(store, 3, use_llm=False)
    before = store.stock_count()
    service.receive_idea(store, use_llm=False)
    assert store.stock_count() == before - 1

    service.prefill(store, 3, use_llm=False)
    assert store.stock_count() >= 3


def test_domain_request_prefers_matching_stock(store):
    service.generate_idea(store, use_llm=False, domain="nature")
    received = service.receive_idea(store, domain="nature", use_llm=False)
    assert received.domain == "nature"


def test_the_promise_holds_even_when_a_chain_will_not_close(tmp_path):
    """**判定前のものは、どんな回り方をしても出さない。**

    「独り言を垂れ流さない」はこのシステムの核心の約束。だが以前は、連鎖が
    閉じないまま上限に達したときの安全弁が、判定前のものをそのまま出して
    いた。40回に1回ほど漏れていて、CI で初めて落ちた（手元では通っていた）。

    乱数で表に出たり出なかったりする種類なので、**繰り返して確かめる**。
    1回のテストが「たまたま通った」で終わらないように。
    """
    from ideaman.store import Store

    leaked = []
    for trial in range(25):
        store = Store(tmp_path / f"t{trial}.db")
        try:
            for _ in range(20):
                idea = service.receive_idea(store, use_llm=False)
                if idea.verdict is None:
                    leaked.append(idea.text)
                    break
        finally:
            store.close()
    assert not leaked, f"判定前のものが漏れた: {leaked}"


def test_the_last_resort_still_returns_something(store, monkeypatch):
    """上限に達しても、待たせた人には何かを返すこと。

    約束を守るために黙ることはしない。判定を確定させてから出す。
    """
    monkeypatch.setattr(service, "MAX_THINKING_STEPS", 1)
    idea = service.receive_idea(store, use_llm=False)
    assert idea.text.strip()
    assert idea.verdict in ("chosen", "kept")
