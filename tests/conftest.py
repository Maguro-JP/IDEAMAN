import pytest

from ideaman.store import Store


@pytest.fixture
def store(tmp_path):
    """テストごとに使い捨ての保管庫。"""
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """テスト中は LLM 仕上げを完全に無効化する。

    キーが環境にあってもテストが外部を叩かないようにする。
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
