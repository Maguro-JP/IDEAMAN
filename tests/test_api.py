import pytest
from fastapi.testclient import TestClient

from ideaman.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """在庫を1件だけ持たせた、空の DB でアプリを起動する。"""
    db = tmp_path / "api.db"
    monkeypatch.setenv("IDEAMAN_DB", str(db))
    monkeypatch.setenv("IDEAMAN_STOCK", "1")
    with TestClient(app) as c:
        # 環境変数が効かず既定の DB を掴んでいたら、ここで気づけるようにする
        assert c.app.state.store.path == db
        yield c


def test_index_serves_the_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "IDEAMAN" in res.text


def test_full_round_trip(client):
    """受け取る → 評価する → 一覧に出る。"""
    res = client.post("/api/idea")
    assert res.status_code == 200
    idea = res.json()
    assert idea["text"]
    assert idea["state"] == "seen"
    # LLM キーが無いのでテンプレート出力になる
    assert idea["source"] == "engine"

    rated = client.post(f"/api/idea/{idea['id']}/rate", json={"score": 1, "note": "良い"})
    assert rated.status_code == 200
    assert rated.json()["score"] == 1

    listed = client.get("/api/ideas").json()["ideas"]
    assert [i["id"] for i in listed] == [idea["id"]]
    assert listed[0]["score"] == 1


def test_works_without_api_key(client):
    """ANTHROPIC_API_KEY が無くても全機能が動く。"""
    for _ in range(3):
        assert client.post("/api/idea").status_code == 200


def test_stock_is_prefilled_at_startup(client):
    """在庫は目標を下回らない。

    連鎖が閉じると1〜2件がまとめて選ばれるので、ちょうど1件とは限らない。
    """
    before = client.get("/api/stock").json()["count"]
    assert before >= 1
    client.post("/api/idea")
    assert client.get("/api/stock").json()["count"] == before - 1


def test_rate_unknown_idea_is_404(client):
    assert client.post("/api/idea/9999/rate", json={"score": 1}).status_code == 404


def test_rate_rejects_out_of_range_score(client):
    idea = client.post("/api/idea").json()
    res = client.post(f"/api/idea/{idea['id']}/rate", json={"score": 7})
    assert res.status_code == 422


def test_rate_without_note(client):
    idea = client.post("/api/idea").json()
    assert client.post(f"/api/idea/{idea['id']}/rate", json={"score": -1}).status_code == 200


def test_ideas_pagination(client):
    for _ in range(4):
        client.post("/api/idea")
    page = client.get("/api/ideas?limit=2&offset=1").json()["ideas"]
    assert len(page) == 2


def test_domains_endpoint(client):
    domains = client.get("/api/domains").json()["domains"]
    assert "nature" in domains
    assert domains == sorted(domains)
