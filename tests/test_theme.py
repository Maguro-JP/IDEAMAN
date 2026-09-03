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


def test_stock_made_before_a_theme_existed_is_served_last(store):
    """テーマ登録直後のボタンで、汎用の在庫が先に出ないこと。

    新規デプロイで実際に起きた: 起動時の先読みがテーマ無しの在庫を作り、
    その後テーマを登録しても、最初の1件は汎用の警句だった。
    """
    # テーマがまだ無い状態で在庫を作る
    for _ in range(40):
        service.generate_idea(store, use_llm=False)
    themeless = [
        i
        for i in store.search(None, limit=200)
        if i.verdict == "chosen" and i.state == "unseen"
    ]
    assert themeless, "前提となる汎用の在庫が作れていない"

    # あとからテーマを登録し、そのテーマの在庫も作る
    theme = store.add_theme("あとから登録")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖"])
    for _ in range(40):
        service.generate_idea(store, use_llm=False)

    taken = store.take_ready()
    assert taken is not None
    assert taken.theme_id == theme.id


def test_stock_from_a_paused_theme_is_served_last(store):
    """休ませたテーマの在庫も後回しになること。"""
    old = store.add_theme("休むほう")
    store.add_materials(old.id, ["帳簿"])
    for _ in range(40):
        service.generate_idea(store, use_llm=False)
    store.set_theme_active(old.id, False)

    now = store.add_theme("いまのほう")
    store.add_materials(now.id, ["通知"])
    for _ in range(40):
        service.generate_idea(store, use_llm=False)

    taken = store.take_ready()
    assert taken is not None
    assert taken.theme_id == now.id


def test_themeless_stock_is_not_served_while_a_theme_is_active(store):
    """テーマ付きの在庫がまだ1件も無くても、汎用の在庫で埋め合わせない。

    並べ替えだけでは足りなかった箇所。テーマを登録した「直後」は、
    そのテーマの在庫がまだ存在しないので、順番を後ろにしただけでは
    結局テーマ無しが出てしまう。
    """
    for _ in range(40):
        service.generate_idea(store, use_llm=False)
    assert store.stock_count() > 0

    theme = store.add_theme("いま考えたいこと")
    store.add_materials(theme.id, ["通知"])

    # このテーマの在庫はまだ 0。汎用在庫があっても出さない。
    assert store.stock_count() == 0
    assert store.take_ready() is None

    # その場で考えれば、ちゃんとテーマのものが返る
    fresh = service.receive_idea(store, use_llm=False)
    assert fresh.theme_id == theme.id


def test_old_stock_comes_back_when_every_theme_is_paused(store):
    """テーマ無しの在庫は捨てない。全部休ませれば戻ってくる。"""
    for _ in range(40):
        service.generate_idea(store, use_llm=False)
    before = store.stock_count()

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知"])
    assert store.stock_count() == 0

    store.set_theme_active(theme.id, False)
    assert store.stock_count() == before


def test_materials_can_be_dropped(store):
    """`--scan` は25語まとめて入れるので、外す手段が要る。

    いらない語が1つ混ざるだけで、その語を含む案が出続ける。
    """
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "場合", "語彙"])
    assert store.drop_materials(theme.id, ["場合"]) == 1
    assert store.materials(theme.id) == ["通知", "語彙"]
    # 無い語を外しても壊れない
    assert store.drop_materials(theme.id, ["存在しない語"]) == 0


def test_drop_materials_api(client):
    created = client.post(
        "/api/themes", json={"name": "t", "materials": ["通知", "場合"]}
    ).json()
    res = client.post(
        f"/api/themes/{created['id']}/materials/drop", json={"materials": ["場合"]}
    )
    assert res.status_code == 200
    assert res.json()["materials"] == ["通知"]
    assert (
        client.post("/api/themes/999/materials/drop", json={"materials": ["x"]}).status_code
        == 404
    )


def test_operations_with_their_own_object_stay_out_of_the_verb_slot():
    """「頻度を主役と脇役を入れ替える」のような「を」の二重を出さないこと。

    aspects の名詞／疑問節と同じ種類のバグ。プールに文法的な形が混ざって
    いるのに、スロット側が区別していなかった。テンプレートを足すたびに
    再発しうるので固定する。
    """
    import re

    from ideaman.engine import templates
    from ideaman.engine.lexicon import default_lexicon

    lexicon = default_lexicon()
    phrases = [t.term for t in lexicon.pool("operations") if t.kind == "phrase"]
    assert phrases, "目的語つきの語句が一つも無い（分類が壊れている）"

    for seed in range(400):
        for materials in (["通知"], []):
            text = templates.render(combine(materials=materials, seed=seed), lexicon)
            for phrase in phrases:
                assert not re.search("を" + re.escape(phrase), text), text


def test_recent_terms_are_penalised_so_ideas_do_not_echo(store):
    """材料だけ変えた同じ案が続けて選ばれないこと。

    「いちばんヘビーに使う一人のためだけに◯◯を作り直す。まず頻度が
    変わるはず」が、材料だけ変えて何度も出ていた。側面や操作が同じなら、
    材料が違っても読み手には同じ案に見える。
    """
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "評価", "在庫", "休眠"])
    for _ in range(200):
        service.generate_idea(store, use_llm=False)

    chosen = sorted(
        (i for i in store.search(None, limit=500) if i.verdict == "chosen"),
        key=lambda i: i.id,
    )
    assert len(chosen) > 10
    overlaps = []
    for older, newer in zip(chosen, chosen[1:]):
        a = {t for _, t in store.terms_of(older.id)}
        b = {t for _, t in store.terms_of(newer.id)}
        if a and b:
            overlaps.append(len(a & b) / len(b))
    mean = sum(overlaps) / len(overlaps)
    assert mean < 0.35, f"隣り合う案が語を共有しすぎている: {mean:.0%}"


def test_every_concept_can_bring_a_way_of_doing_things():
    """概念は「やり方」を持っているものにする。

    元は「星座は、手紙のかたちをしている」という警句のために選んだ詩的な
    語だったが、いまのテンプレートは「{concept}のやり方を{material}に
    持ち込む」と聞く。苔や湯気には持ち込むべきやり方が無い。

    機械では判定できないので、**やり方を持たないと分かっている語**が
    戻ってこないことだけを守る。
    """
    from ideaman.engine.lexicon import default_lexicon

    names = {t.term for t in default_lexicon().pool("concepts")}
    for imageonly in ("苔", "湯気", "静電気", "雨音", "影", "年輪", "寝言"):
        assert imageonly not in names, f"{imageonly} には持ち込むべきやり方が無い"


def test_every_pool_is_reachable_with_and_without_a_theme():
    """テーマの有無にかかわらず、全ての語彙プールが使われること。

    側面を足す改修で、テーマ未登録の間だけ operations プールが丸ごと
    死んでいた（テンプレートから operation が消えたため）。語彙を増やしても
    引かれなければ意味がないので、両方の経路で確かめる。
    """
    from ideaman.engine.lexicon import POOL_NAMES, default_lexicon

    lexicon = default_lexicon()
    for materials in (["通知"], []):
        used = set()
        for seed in range(600):
            for pool, _ in combine(materials=materials, seed=seed).terms:
                used.add(pool)
        missing = set(POOL_NAMES) - used
        label = "テーマあり" if materials else "テーマなし"
        assert not missing, f"{label} のとき {missing} が一度も引かれない"


def test_weak_templates_name_what_they_compare():
    """側面を名指ししない型は弱い。

    「給水塔にあって放浪に無いものは何か」は、何を比べているのか言って
    いないので読み手に全部の仕事を投げていた。概念を持ち出す型は、
    比較の的（側面）を必ず名指しすること。
    """
    from ideaman.engine.lexicon import default_lexicon

    for template in default_lexicon().templates:
        if "concept_a" not in template.needs:
            continue
        assert any("aspect" in n for n in template.needs), (
            f"{template.id} は概念を持ち出すのに、何を比べるか言っていない"
        )


# --- 材料は「語」であること ----------------------------------------------
# 自動収集は最大6文字程度に収まるが、手で登録する経路（--add / Web / MCP）は
# 何でも通ってしまう。材料は文中に名詞として埋め込まれるので、文を貼られると
# 「予約の締切をどうするか決めきれていないの一番よく使う部分を…」になる。


def test_a_pasted_sentence_is_not_a_material():
    from ideaman.store import clean_material

    assert clean_material("予約の締切をどうするか決めきれていない") is None
    assert clean_material("これは 空白入り") is None
    assert clean_material("終わった。") is None


def test_words_are_kept(store):
    from ideaman.store import clean_material

    for word in ("通知", "待ち時間", "キャンセル", "棚卸し", "失敗したときの挙動"):
        assert clean_material(word) == word
    assert clean_material("  語彙  ") == "語彙"


def test_the_length_limit_differs_by_script():
    """日本語の語は短く、英語の語は長い。一律だとどちらかが不当に弾かれる。"""
    from ideaman.store import clean_material

    assert clean_material("authentication") == "authentication"
    assert clean_material("user_authentication_middleware_factory") is None
    assert clean_material("失敗したときの挙動") is not None
    assert clean_material("あ" * 20) is None


def test_symbols_and_single_letters_are_rejected():
    from ideaman.store import clean_material

    for junk in ("！！！", "a", "…", "", "  "):
        assert clean_material(junk) is None


def test_bad_materials_never_reach_the_database(store):
    theme = store.add_theme("t")
    added = store.add_materials(
        theme.id, ["通知", "予約の締切をどうするか決めきれていない", "！！！", "語彙"]
    )
    assert added == 2
    assert store.materials(theme.id) == ["通知", "語彙"]


def test_doctor_flags_a_material_that_slipped_in_earlier(store):
    """すでに DB に入っている壊れた材料も見つけること。"""
    from ideaman.doctor import diagnose

    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫"])
    # 検証を通さず直接入れる（古い DB を再現）
    store.conn.execute(
        "INSERT INTO theme_terms (theme_id, term) VALUES (?, ?)",
        (theme.id, "予約の締切をどうするか決めきれていない"),
    )
    store.conn.commit()

    junk = next(f for f in diagnose(store).findings if "的外れ" in f.title)
    assert "予約の締切をどうするか決めきれていない" in junk.detail
    assert junk.fix and "--drop" in junk.fix


# --- Web から任意のパスを読ませない --------------------------------------
# Web は公開 HTTPS で動かす前提。`scan` に任意のパスを受け付けると、誰でも
# ファイルシステムを探れる（実際 /etc から語が採れた）。


def test_the_web_refuses_to_scan_by_default(client):
    answer = client.post("/api/themes", json={"name": "t", "scan": "/etc"})
    assert answer.status_code == 403
    assert "IDEAMAN_SCAN_ROOT" in answer.json()["detail"]


def test_the_web_stays_inside_the_allowed_root(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ideaman.app import app

    allowed = tmp_path / "work"
    (allowed / "src").mkdir(parents=True)
    (allowed / "src" / "a.md").write_text("配送の締切を直す。\n" * 3, encoding="utf-8")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "b.md").write_text("忍者の名簿。\n" * 3, encoding="utf-8")

    monkeypatch.setenv("IDEAMAN_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("IDEAMAN_STOCK", "0")
    monkeypatch.setenv("IDEAMAN_SCAN_ROOT", str(allowed))

    with TestClient(app) as client:
        assert (
            client.post(
                "/api/themes", json={"name": "out", "scan": str(outside)}
            ).status_code
            == 403
        )
        # `..` で外へ出ようとしても同じ
        assert (
            client.post(
                "/api/themes", json={"name": "up", "scan": str(allowed / "..")}
            ).status_code
            == 403
        )
        inside = client.post(
            "/api/themes", json={"name": "in", "scan": str(allowed / "src")}
        )
        assert inside.status_code == 200
        assert "配送" in inside.json()["materials"]


def test_symlinks_are_not_followed(tmp_path):
    """許可された場所の中にリンクを置いても、外は読まれないこと。"""
    from ideaman.harvest import harvest

    allowed = tmp_path / "work"
    allowed.mkdir()
    (allowed / "a.md").write_text("配送の締切を直す。\n" * 3, encoding="utf-8")
    outside = tmp_path / "secret.md"
    outside.write_text("忍者の名簿。\n" * 3, encoding="utf-8")
    (allowed / "link.md").symlink_to(outside)

    terms = [c.term for c in harvest(allowed, min_files=1, include_git=False)]
    assert "配送" in terms
    assert "忍者" not in terms
