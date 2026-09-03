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
