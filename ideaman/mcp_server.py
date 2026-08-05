"""IDEAMAN の MCP サーバー。

Claude から、裏で溜まり続けているアイデアを覗けるようにする。

    claude mcp add ideaman -- python -m ideaman.mcp_server

想定している使われ方: ワーカーが裏で考え続けて在庫を溜めている。ユーザーが
Claude に「何か良いアイデアない?」と聞くと、Claude は自分で考えるのに加えて
ここを見に行く。Claude 自身の思考には出てこない、衝突ベースの外部の視点が
混ざる。

`mcp` はこのモジュールの中でだけ import する（入っていない環境でも本体と
テストが動くように）。
"""

from __future__ import annotations

import json
from typing import Any

from . import service
from .diversity import measure
from .store import Idea, Store

SERVER_NAME = "ideaman_mcp"

#: 一度に返す件数の上限。文脈を食い潰さないため。
MAX_LIMIT = 50


def _as_dict(idea: Idea) -> dict[str, Any]:
    """アイデアを Claude に渡す形にする。

    内部だけの値（seed_json など）は出さない。
    """
    return {
        "id": idea.id,
        "text": idea.text,
        "score": idea.score,
        "chain_id": idea.chain_id,
        "mode": idea.mode,
        "created_at": idea.created_at,
    }


def build_server(store: Store):
    """MCP サーバーを組み立てて返す。

    MCP SDK 2.x の `MCPServer` を使う（1.x の `FastMCP` は無くなった）。

    Raises:
        ImportError: `mcp` が入っていない場合。
    """
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    mcp = MCPServer(SERVER_NAME)

    @mcp.tool(
        name="ideaman_receive_idea",
        annotations=ToolAnnotations(
            title="溜まっている考えを一つ受け取る",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def receive_idea(domain: str | None = None) -> str:
        """IDEAMAN が裏で考えて溜めておいたアイデアを一つ受け取る。

        ユーザーが行き詰まっているとき、発想のきっかけを求めているとき、
        「何か面白いアイデアない?」と聞かれたときに呼ぶ。返ってくるのは
        あなた自身の推論ではなく、普段結びつかない語同士を衝突させて
        作られた外部の視点。あなたの考えと並べて使うとよい。

        一度受け取ったアイデアは既読になり、次は別のものが出る。

        Args:
            domain: 分野を絞りたい場合のタグ（例: "city", "nature", "memory"）。
                省略すると分野を問わない。

        Returns:
            JSON 文字列。`{"id": int, "text": str, "score": int,
            "chain_id": int, "mode": "associate"|"wander", "created_at": str}`。
            `mode` が `associate` なら、それは直前の考えから連想して
            生まれたもの（`ideaman_follow_chain` で流れを辿れる）。
        """
        idea = service.receive_idea(store, domain=domain)
        return json.dumps(_as_dict(idea), ensure_ascii=False, indent=2)

    @mcp.tool(
        name="ideaman_browse_ideas",
        annotations=ToolAnnotations(
            title="溜まった考えを見渡す",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def browse_ideas(query: str | None = None, limit: int = 20) -> str:
        """これまでに生まれたアイデアを見渡す。

        特定の話題に関係しそうな発想を探すときに使う。`query` を省くと
        評価の高いものから返るので、「これまでで良かったアイデアは?」と
        聞かれたときにも使える。

        `ideaman_receive_idea` との違い: こちらは在庫を消費せず、既に
        表に出たものも含めて検索する。

        Args:
            query: 本文に含まれる語で絞る（例: "地図", "待ち時間"）。
                省略すると評価の高い順。
            limit: 返す件数。1〜50。

        Returns:
            JSON 文字列。`{"count": int, "ideas": [...]}`。各要素は
            `ideaman_receive_idea` と同じ形。
        """
        limit = max(1, min(limit, MAX_LIMIT))
        ideas = store.search(query, limit=limit)
        return json.dumps(
            {"count": len(ideas), "ideas": [_as_dict(i) for i in ideas]},
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool(
        name="ideaman_follow_chain",
        annotations=ToolAnnotations(
            title="連鎖を辿る",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def follow_chain(idea_id: int) -> str:
        """そのアイデアが属する思考の連鎖を、古い順に辿る。

        IDEAMAN は一つの発想から連想を広げていくので、面白いアイデアの
        前後には関連する発想が並んでいることがある。一つ気になるものが
        あったとき、その周辺を読むために使う。

        Args:
            idea_id: 起点にするアイデアの ID。

        Returns:
            JSON 文字列。`{"chain_id": int, "count": int, "ideas": [...]}`。
            古い順に並ぶ。連鎖が1件だけなら、それは連想ではなく単発の発想
            （放浪モード）。

        Errors:
            存在しない ID を渡すと `{"error": "..."}` が返る。
        """
        try:
            chain = store.chain(idea_id)
        except KeyError:
            return json.dumps(
                {"error": f"アイデア {idea_id} は存在しません。"
                          " ideaman_browse_ideas で有効な ID を探してください。"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "chain_id": chain[0].chain_id if chain else None,
                "count": len(chain),
                "ideas": [_as_dict(i) for i in chain],
            },
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool(
        name="ideaman_rate_idea",
        annotations=ToolAnnotations(
            title="アイデアを評価する",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def rate_idea(idea_id: int, score: int, note: str | None = None) -> str:
        """アイデアに評価をつける。IDEAMAN はこれで育つ。

        会話の中でユーザーが「これは良い」「それは違う」と反応したら呼ぶ。
        評価はそのアイデアを構成していた語の重みを動かし、次に何を考えるかに
        効いてくる。**評価しないと育たない**ので、反応があったら記録すること。

        Args:
            idea_id: 評価するアイデアの ID。
            score: `1` なら良い、`-1` ならそうでもない、`0` は中立。
            note: 一言残す場合の理由（例: "この切り口は使えそう"）。

        Returns:
            JSON 文字列。更新後のアイデア（`score` は累計）。

        Errors:
            存在しない ID、または -1/0/1 以外の score で `{"error": "..."}`。
        """
        try:
            idea = store.rate(idea_id, score, note)
        except KeyError:
            return json.dumps(
                {"error": f"アイデア {idea_id} は存在しません。"},
                ensure_ascii=False,
            )
        except ValueError:
            return json.dumps(
                {"error": f"score は 1 / 0 / -1 のいずれかです（受け取った値: {score}）。"},
                ensure_ascii=False,
            )
        return json.dumps(_as_dict(idea), ensure_ascii=False, indent=2)

    @mcp.tool(
        name="ideaman_stock_status",
        annotations=ToolAnnotations(
            title="在庫といま考えていること",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def stock_status() -> str:
        """いくつ考えが溜まっているか、いま何を考えているかを見る。

        「IDEAMAN は今どうなってる?」と聞かれたときや、受け取る前に在庫を
        確かめたいときに使う。

        Returns:
            JSON 文字列:
            `stock` 未閲覧の在庫数、`mode` いまの思考モード
            (`associate` = 連想中 / `wander` = 放浪中)、
            `chain_length` 現在の連鎖の長さ、`dormant_terms` いま休眠中の語数
            （しばらく考えないようにしている題材）、`total` 総生成数。
        """
        state = store.thought_state()
        health = measure(store, limit=200)
        return json.dumps(
            {
                "stock": store.stock_count(),
                "mode": state.mode,
                "chain_length": state.chain_length,
                "dormant_terms": health.dormant_now,
                "total": health.total,
            },
            ensure_ascii=False,
            indent=2,
        )

    return mcp


def main() -> int:
    store = Store()
    try:
        mcp = build_server(store)
    except ImportError:
        print('mcp が必要です: pip install -e ".[mcp]"')
        return 1
    try:
        mcp.run()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
