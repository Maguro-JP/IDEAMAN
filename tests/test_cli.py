"""コマンドラインの入口。

CLI は**唯一、間違った使い方をされる場所**。関数は呼び方を間違えれば
TypeError になるが、コマンドは黙って別のことをする。ここで見るのは
「間違えたときに、間違えたと分かるか」。
"""

import pytest

from ideaman.cli import main
from ideaman.store import Store


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    monkeypatch.setenv("IDEAMAN_DB", str(path))
    return path


def test_theme_without_a_name_lists(db, capsys):
    assert main(["theme"]) == 0
    assert "テーマがまだありません" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["theme", "--add", "通知"],
        ["theme", "--drop", "通知"],
        ["theme", "--scan", "."],
        ["theme", "--note", "補足"],
        ["theme", "--off"],
    ],
)
def test_theme_options_without_a_name_are_refused(db, capsys, argv):
    """名前なしで材料を足そうとしたら、黙って一覧を出して終わってはいけない。

    以前はここで一覧を出して 0 を返していた。入れたつもりの材料は
    どこにも無く、しかも成功したように見えていた。
    """
    assert main(argv) == 1
    assert "どのテーマ" in capsys.readouterr().err
    store = Store(db)
    try:
        assert store.themes(active_only=False) == []
    finally:
        store.close()


def test_worker_tick_is_passed_through(db, monkeypatch):
    """`--tick` が本当にループへ届くこと。

    既定値のまま無視されても動いてしまうので、受け取った値を捕まえる。
    """
    seen = {}

    def fake_run(self, tick_seconds):
        seen["tick"] = tick_seconds

    monkeypatch.setattr("ideaman.worker.Worker.run", fake_run)
    assert main(["worker", "--tick", "7"]) == 0
    assert seen["tick"] == 7


def test_chain_of_an_unknown_idea_reads_as_a_sentence(db, capsys):
    """KeyError をそのまま print すると引用符が付いて読みにくい。"""
    assert main(["chain", "99999"]) == 1
    err = capsys.readouterr().err.strip()
    assert not err.startswith("'")


def test_mechanisms_reads_a_file(db, capsys, tmp_path):
    """`mechanisms` が文章を読んで、仕組みの一文だけを出すこと。

    取れ高は実際の文章に当てないと分からないので、測る入口をコマンドに
    しておく。ここでは入口が通ることだけを見る。
    """
    source = tmp_path / "図書館.txt"
    source.write_text(
        "図書館では、返却期限を厳しくするのではなく、"
        "返しやすい場所を増やすことで回収を早める。\n"
        "これは各地で行われている。\n"
    )
    assert main(["mechanisms", str(source)]) == 0
    out = capsys.readouterr().out
    assert "回収を早める" in out
    assert "これは各地で" not in out


def test_mechanisms_refuses_a_missing_file(db, capsys):
    assert main(["mechanisms", "/no/such/file.txt"]) == 1
    assert "読めません" in capsys.readouterr().err
