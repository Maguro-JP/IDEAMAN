"""在庫（＝すでに考えてあったもの）の振る舞い。

「裏で勝手に考えている」感を支えているのはここ。ボタンは生成を依頼するので
はなく、溜まっている考えを取り出す。
"""

from ideaman import service
from ideaman.engine import combinator


def test_receive_takes_from_stock_without_generating(store, monkeypatch):
    stocked = service.generate_idea(store, use_llm=False, seed=42)

    def fail(*args, **kwargs):
        raise AssertionError("在庫があるのに新規生成が走った")

    monkeypatch.setattr(combinator, "combine", fail)
    received = service.receive_idea(store, use_llm=False)

    assert received.id == stocked.id
    assert received.state == "seen"
    assert store.stock_count() == 0


def test_receive_generates_when_stock_is_empty(store):
    assert store.stock_count() == 0
    idea = service.receive_idea(store, use_llm=False)
    assert idea.state == "seen"
    assert store.stock_count() == 0


def test_same_idea_is_never_handed_out_twice(store):
    service.prefill(store, 5, use_llm=False)
    seen = [service.receive_idea(store, use_llm=False).id for _ in range(5)]
    assert len(set(seen)) == 5
    assert store.stock_count() == 0


def test_prefill_tops_up_to_target_only(store):
    assert service.prefill(store, 3, use_llm=False) == 3
    assert store.stock_count() == 3
    # すでに足りていれば何も作らない
    assert service.prefill(store, 3, use_llm=False) == 0
    assert store.stock_count() == 3


def test_prefill_accounts_for_consumed_stock(store):
    service.prefill(store, 3, use_llm=False)
    service.receive_idea(store, use_llm=False)
    assert service.prefill(store, 3, use_llm=False) == 1
    assert store.stock_count() == 3


def test_domain_request_prefers_matching_stock(store):
    service.generate_idea(store, use_llm=False, domain="nature")
    received = service.receive_idea(store, domain="nature", use_llm=False)
    assert received.domain == "nature"
