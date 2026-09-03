"""MCP サーバーの中身。

Claude から IDEAMAN を覗く経路。`claude mcp add` して初めて壊れているのが
分かる、では Discord と同じ轍を踏むので、ツールを実際に呼んで通す。

`mcp` が入っていない環境ではまるごと飛ばす（本体の方針を崩さない）。
"""

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from ideaman import service  # noqa: E402
from ideaman.mcp_server import build_server  # noqa: E402


def call(_server, _tool, **arguments):
    """ツールを1つ呼んで、返ってきた JSON を辞書にする。

    引数名の先頭に `_` を付けてあるのは、ツール側の `name` 引数と
    ぶつからないようにするため。
    """
    result = asyncio.run(_server.call_tool(_tool, arguments))
    blocks = getattr(result, "content", result)
    text = "".join(getattr(b, "text", "") for b in blocks)
    assert text, f"{_tool} が何も返していない"
    return json.loads(text)


@pytest.fixture
def server(store):
    return build_server(store)


@pytest.fixture
def stocked(store):
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫"])
    for _ in range(60):
        service.generate_idea(store, use_llm=False)
    return theme


def test_all_seven_tools_are_registered(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "ideaman_receive_idea",
        "ideaman_browse_ideas",
        "ideaman_follow_chain",
        "ideaman_rate_idea",
        "ideaman_register_theme",
        "ideaman_list_themes",
        "ideaman_stock_status",
    }


def test_receive_idea_hands_over_something_and_marks_it_seen(server, store, stocked):
    idea = call(server, "ideaman_receive_idea")
    assert idea["text"].strip()
    assert store.get(idea["id"]).state == "seen"

    # 一度渡したものは、次は出てこない
    again = call(server, "ideaman_receive_idea")
    assert again["id"] != idea["id"]


def test_rate_idea_moves_the_weights(server, store, stocked):
    """会話の中の「これ良いね」がシステムを育てること。"""
    idea = call(server, "ideaman_receive_idea")
    before = dict(store.weights())
    rated = call(server, "ideaman_rate_idea", idea_id=idea["id"], score=1)
    assert rated["score"] == 1
    after = store.weights()
    assert [t for t in after if after[t] != before.get(t, 1.0)], "重みが動いていない"


def test_rate_idea_reports_bad_input_instead_of_crashing(server, store, stocked):
    """Claude が変な値を渡しても、会話が止まらないこと。"""
    assert "error" in call(server, "ideaman_rate_idea", idea_id=999999, score=1)
    idea = call(server, "ideaman_receive_idea")
    assert "error" in call(server, "ideaman_rate_idea", idea_id=idea["id"], score=5)


def test_browse_ideas_searches_and_caps_the_limit(server, store, stocked):
    all_of_them = call(server, "ideaman_browse_ideas")
    assert all_of_them["count"] > 0

    hit = call(server, "ideaman_browse_ideas", query="通知")
    assert all("通知" in i["text"] for i in hit["ideas"])

    # 文脈を食い潰さないよう上限が効くこと
    capped = call(server, "ideaman_browse_ideas", limit=999)
    assert capped["count"] <= 50


def test_follow_chain_reads_the_surrounding_thought(server, store, stocked):
    idea = call(server, "ideaman_receive_idea")
    chain = call(server, "ideaman_follow_chain", idea_id=idea["id"])
    assert chain["count"] >= 1
    assert any(i["id"] == idea["id"] for i in chain["ideas"])


def test_follow_chain_explains_a_bad_id(server, store, stocked):
    answer = call(server, "ideaman_follow_chain", idea_id=999999)
    assert "error" in answer
    # 次に何をすればいいかまで書いてあること
    assert "browse" in answer["error"]


def test_register_theme_is_how_claude_makes_it_useful(server, store):
    made = call(
        server,
        "ideaman_register_theme",
        name="予約を直す",
        materials=["予約", "キャンセル", "台帳"],
    )
    assert made["materials"] == ["予約", "キャンセル", "台帳"]

    listed = call(server, "ideaman_list_themes")
    assert [t["name"] for t in listed["themes"]] == ["予約を直す"]

    # 登録した材料が、実際に出てくる案に現れること
    texts = [call(server, "ideaman_receive_idea")["text"] for _ in range(8)]
    assert any(m in t for t in texts for m in ("予約", "キャンセル", "台帳"))


def test_register_theme_can_read_a_directory(server, store, tmp_path):
    (tmp_path / "notes.md").write_text(
        "配送の締切を見直す。梱包の手順が重い。配送と梱包は同じ台帳。\n" * 3,
        encoding="utf-8",
    )
    made = call(
        server, "ideaman_register_theme", name="読ませる", scan_path=str(tmp_path)
    )
    assert made["harvested"], "読ませたのに何も採れていない"
    assert "配送" in made["materials"]


def test_register_theme_reports_a_bad_path(server, store):
    answer = call(
        server, "ideaman_register_theme", name="x", scan_path="/存在しない場所"
    )
    assert "error" in answer


def test_stock_status_reports_the_current_state(server, store, stocked):
    status = call(server, "ideaman_stock_status")
    assert status["themes"] == 1
    assert status["mode"] in ("associate", "wander")
    assert status["total"] > 0


def test_stock_status_shows_zero_themes_so_claude_can_prompt(server, store):
    """テーマが無いことが Claude から見えること。

    ここが 0 なら案は汎用の警句にしかならない。見えなければ、Claude は
    「登録しませんか」と言えない。
    """
    assert call(server, "ideaman_stock_status")["themes"] == 0
