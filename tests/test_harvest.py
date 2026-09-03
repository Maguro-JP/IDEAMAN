"""許可された場所を読んで材料を集める。

手で材料を登録する限りテーマは空のまま放置され、出てくるのは汎用の警句に
戻る。ここが「有益さ」の実際の障壁なので、抽出の質をテストで守る。
"""

import pytest

from ideaman.harvest import Candidate, harvest


@pytest.fixture
def project(tmp_path):
    """小さなプロジェクトを作る。散文とコードの両方を含む。"""
    (tmp_path / "README.md").write_text(
        "# 予約システム\n\n"
        "予約の待ち時間を短くする。キャンセルの扱いが一番難しい。\n"
        "予約とキャンセルは同じ台帳で管理する。\n",
        encoding="utf-8",
    )
    (tmp_path / "booking.py").write_text(
        "def create_booking(store, slot):\n"
        "    # 予約を作る。台帳に書くだけ。\n"
        "    return store.insert(slot)\n"
        "\n"
        "def cancel_booking(store, booking_id):\n"
        "    # キャンセル。台帳から消す。\n"
        "    return store.delete(booking_id)\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text(
        "台帳の設計を見直す。予約の粒度とキャンセルの締切。\n", encoding="utf-8"
    )
    return tmp_path


def test_finds_the_authors_own_vocabulary(project):
    terms = [c.term for c in harvest(project)]
    assert "予約" in terms
    assert "キャンセル" in terms
    assert "台帳" in terms


def test_prose_beats_identifiers(project):
    """散文の語を識別子より上に置くこと。

    識別子は文法と慣習で決まる名前だが、コメントや README の語はその人が
    自由に選んだ語で、何を考えているかをそのまま表す。
    """
    terms = [c.term for c in harvest(project, min_files=1)]
    assert "booking" in terms, "識別子が候補にすら入っていないと比較にならない"
    assert terms.index("予約") < terms.index("booking")
    # 上位は散文の語で占められるはず
    assert set(terms[:3]) <= {"予約", "キャンセル", "台帳", "待ち時間"}


def test_generic_words_are_dropped(project):
    terms = [c.term for c in harvest(project)]
    for boring in ("store", "return", "def", "self", "テスト", "データ"):
        assert boring not in terms


def test_plurals_are_not_listed_next_to_their_singular(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.py").write_text(
            "widget = 1\nwidgets = [widget]\nwidget_id = 2\n", encoding="utf-8"
        )
    terms = [c.term for c in harvest(tmp_path, min_files=1)]
    assert "widget" in terms
    assert "widgets" not in terms


def test_skips_generated_directories(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("# 会計の締め\n", encoding="utf-8")
    junk = tmp_path / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "b.js").write_text("// 他人のコードの語彙\nconst 忍者 = 1;\n", encoding="utf-8")
    terms = [c.term for c in harvest(tmp_path, min_files=1)]
    assert "会計" in terms
    assert "忍者" not in terms


def test_empty_directory_returns_nothing(tmp_path):
    assert harvest(tmp_path) == []


def test_a_file_is_not_a_directory(tmp_path):
    path = tmp_path / "x.py"
    path.write_text("a = 1", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        harvest(path)


def test_small_project_relaxes_the_spread_requirement(tmp_path):
    """1ファイルしか無いプロジェクトでも空にならないこと。"""
    (tmp_path / "only.md").write_text("在庫の回転を上げる。棚卸しが重い。\n", encoding="utf-8")
    found = harvest(tmp_path)
    assert found
    assert all(isinstance(c, Candidate) for c in found)


def test_harvested_materials_reach_the_ideas(store, project):
    """集めた語が、実際にアイデアの本文に出てくること。"""
    from ideaman import service

    theme = store.add_theme("予約を直す")
    store.add_materials(theme.id, [c.term for c in harvest(project)])
    texts = [service.generate_idea(store, use_llm=False).text for _ in range(20)]
    assert any("予約" in t or "キャンセル" in t or "台帳" in t for t in texts)
