"""テーマと材料。

ここが「有益なアイデア」の前提。テーマが無ければ汎用の警句にしかならず、
テーマがあれば案の中にユーザー自身の語が現れる。
"""

import pytest
from fastapi.testclient import TestClient

from ideaman import service
from ideaman.app import app
from ideaman.engine.combinator import combine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("IDEAMAN_DB", str(tmp_path / "theme.db"))
    monkeypatch.setenv("IDEAMAN_STOCK", "0")
    with TestClient(app) as c:
        yield c


def test_add_theme_is_idempotent(store):
    a = store.add_theme("通知を直す", "補足")
    b = store.add_theme("通知を直す")
    assert a.id == b.id
    assert store.theme(a.id).note == "補足"  # 二度目の None で消えない


def test_materials_ignore_blanks_and_duplicates(store):
    theme = store.add_theme("t")
    assert store.add_materials(theme.id, ["通知", " 語彙 ", "", "  "]) == 2
    assert store.add_materials(theme.id, ["通知"]) == 0
    assert store.materials(theme.id) == ["通知", "語彙"]


def test_inactive_theme_is_not_thought_about(store):
    theme = store.add_theme("休むほう")
    store.set_theme_active(theme.id, False)
    assert store.themes(active_only=True) == []
    assert len(store.themes(active_only=False)) == 1


def test_materials_appear_in_the_text(store):
    """材料を渡すと、案の中にその語が出る。ここが具体性の源。"""
    store.add_theme("t")
    for seed in range(30):
        idea_seed = combine(materials=["通知"], seed=seed)
        assert idea_seed.slots.get("material") == "通知"
        assert ("materials", "通知") in idea_seed.terms


def test_without_materials_no_material_template_is_chosen(store):
    """テーマが無いときは material を要求しないテンプレだけを使う。

    埋められないスロットを選ぶと描画で落ちるので、ここは落ちてはいけない。
    """
    for seed in range(30):
        idea_seed = combine(seed=seed)
        assert "material" not in idea_seed.slots


def test_chain_stays_on_one_theme(store):
    """一つの思考の流れは一つの話題についてのもの。"""
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙"])
    for _ in range(20):
        service.generate_idea(store, use_llm=False)
    chains = {}
    for idea in store.search(None, limit=100):
        if idea.chain_id:
            chains.setdefault(idea.chain_id, set()).add(idea.theme_id)
    assert all(len(themes) == 1 for themes in chains.values())


def test_ideas_record_their_theme(store):
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知"])
    idea = service.generate_idea(store, use_llm=False)
    assert idea.theme_id == theme.id


def test_theme_api_round_trip(client):
    created = client.post(
        "/api/themes",
        json={"name": "IDEAMANを伸ばす", "note": "本体", "materials": ["通知", "語彙"]},
    )
    assert created.status_code == 200
    theme = created.json()
    assert theme["materials"] == ["通知", "語彙"]

    # 同名に足す
    client.post("/api/themes", json={"name": "IDEAMANを伸ばす", "materials": ["連鎖"]})
    listed = client.get("/api/themes").json()["themes"]
    assert len(listed) == 1
    assert listed[0]["materials"] == ["通知", "語彙", "連鎖"]

    off = client.post(f"/api/themes/{theme['id']}/active?active=false")
    assert off.json()["active"] is False


def test_toggling_unknown_theme_is_404(client):
    assert client.post("/api/themes/999/active?active=true").status_code == 404


def test_registered_theme_grounds_the_web_idea(client):
    client.post(
        "/api/themes", json={"name": "t", "materials": ["通知"]}
    )
    texts = [client.post("/api/idea").json()["text"] for _ in range(5)]
    assert any("通知" in text for text in texts)
