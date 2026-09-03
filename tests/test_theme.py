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


# --- 日本語が壊れないこと ------------------------------------------------
# 「連鎖の誰が決めるかを自動で忘れさせる」のような文が実際に出ていた。
# 疑問節を動詞の目的語に入れたことが原因で、テンプレートを増やすたびに
# 再発しうるので、テストで固定する。


def _clause_terms():
    from ideaman.engine.lexicon import default_lexicon

    return {t.term for t in default_lexicon().pool("aspects") if t.kind == "clause"}


def test_clause_aspects_never_land_in_a_noun_slot():
    """疑問節は「〜を{操作}」の位置に来てはいけない。"""
    from ideaman.engine.lexicon import default_lexicon

    lexicon = default_lexicon()
    clauses = _clause_terms()
    assert clauses, "疑問節の語が一つも無い（分類が壊れている）"

    for seed in range(300):
        idea_seed = combine(materials=["通知"], seed=seed)
        if "aspect" in idea_seed.slots:
            assert idea_seed.slots["aspect"] not in clauses


def test_every_template_can_be_rendered():
    """全テンプレートが、必要な語を揃えて実際に文字列になること。"""
    from ideaman.engine import templates
    from ideaman.engine.lexicon import default_lexicon

    lexicon = default_lexicon()
    seen = set()
    for seed in range(500):
        for materials in (["通知"], []):
            idea_seed = combine(materials=materials, seed=seed)
            text = templates.render(idea_seed, lexicon)
            assert text and "{" not in text
            seen.add(idea_seed.template_id)
    assert seen == {t.id for t in lexicon.templates}, "使われないテンプレートがある"


def test_probe_does_not_change_the_signature():
    """試し方が違うだけの2件は、同じ発想として扱う。

    これが効かないと「Botで誰が得をしているか」が probe 違いで
    何度も表に出る。
    """
    a = combine(materials=["通知"], seed=7)
    if "probe" not in a.slots:
        return  # probe を使わない型が引かれた回は対象外
    b_slots = dict(a.slots)
    b_slots["probe"] = "まったく別の試し方"
    from dataclasses import replace

    b = replace(
        a,
        slots=b_slots,
        terms=tuple(
            (pool, "まったく別の試し方" if pool == "probes" else term)
            for pool, term in a.terms
        ),
    )
    assert a.signature == b.signature


def test_curator_avoids_repeating_the_same_sentence_shape(store):
    """同じ文型が連続して選ばれ続けないこと。"""
    from ideaman import curator

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "評価", "在庫"])
    for _ in range(200):
        service.generate_idea(store, use_llm=False)

    chosen = sorted(
        (i for i in store.search(None, limit=500) if i.verdict == "chosen"),
        key=lambda i: i.id,
    )
    assert len(chosen) > 10
    repeats = sum(
        1 for a, b in zip(chosen, chosen[1:]) if a.template_id == b.template_id
    )
    # 完全に禁じはしない（良い案なら型が被っても出す）が、半分は超えない
    assert repeats < len(chosen) / 2
    assert curator.W_FRESH > 0
