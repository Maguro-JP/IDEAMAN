"""Discord / MCP の依存が無い環境でも本体が動くことの保証。

Phase 1 の「キーが無くても全機能が動く」方針を、依存にも広げたもの。
モジュールを import するだけなら失敗せず、実際に組み立てるときだけ
ImportError になること。
"""

import pytest


def test_modules_import_without_their_dependencies():
    """import が通ること。ここで落ちるとテスト収集ごと巻き添えになる。"""
    import ideaman.bots.discord_bot
    import ideaman.mcp_server  # noqa: F401


def test_building_the_mcp_server_reports_a_missing_dependency(store):
    from ideaman import mcp_server

    try:
        import mcp  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            mcp_server.build_server(store)
    else:
        # 入っているなら組み立てられること
        assert mcp_server.build_server(store) is not None


def test_building_the_discord_bot_reports_a_missing_dependency(store):
    from ideaman.bots import discord_bot

    try:
        import discord  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            discord_bot.build_bot(store, 123)
    else:
        assert discord_bot.build_bot(store, 123) is not None


def test_cli_works_without_optional_dependencies(store, monkeypatch, tmp_path, capsys):
    """ワーカーと CLI は追加依存なしで完結する。"""
    monkeypatch.setenv("IDEAMAN_DB", str(tmp_path / "cli.db"))
    from ideaman.cli import main

    assert main(["--no-llm", "worker", "--once", "--stock", "2"]) == 0
    assert main(["diversity"]) == 0
    assert "連鎖の長さ" in capsys.readouterr().out


def test_a_missing_chain_reads_as_a_sentence(store, monkeypatch, tmp_path, capsys):
    """`chain 99999` が引用符付きで出ないこと。

    KeyError をそのまま print すると `'…は存在しません'` になる。
    人に見せる文なので、中身だけ出す。
    """
    monkeypatch.setenv("IDEAMAN_DB", str(tmp_path / "c.db"))
    from ideaman.cli import main

    assert main(["chain", "99999"]) == 1
    err = capsys.readouterr().err.strip()
    assert err == "アイデア 99999 は存在しません"
