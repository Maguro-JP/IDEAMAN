"""Discord Bot。

ワーカーとは別プロセスで動く。ワーカーが `deliveries` に積んだものを拾って
投稿し、押されたリアクションを評価として書き戻す。

    export DISCORD_BOT_TOKEN=...
    export DISCORD_CHANNEL_ID=...
    python -m ideaman.bots.discord_bot

`discord.py` はこのモジュールの中でだけ import する。入っていない環境でも
本体とテストが動くようにするため。
"""

from __future__ import annotations

import logging
import os

from .. import service
from ..store import Store

log = logging.getLogger(__name__)

CHANNEL = "discord"

#: ワーカーが積んだ配信を拾いに行く間隔（秒）。
POLL_SECONDS = 30

UP = "\N{THUMBS UP SIGN}"
DOWN = "\N{THUMBS DOWN SIGN}"

#: リアクションと点数の対応。
REACTIONS = {UP: 1, DOWN: -1}


def _format(idea) -> str:
    """投稿の文面。装飾は最小限にする（天啓に絵文字は要らない）。"""
    return f"> {idea.text}"


def build_bot(store: Store, channel_id: int):
    """Bot を組み立てて返す。

    Raises:
        ImportError: `discord.py` が入っていない場合。
    """
    import discord
    from discord.ext import commands, tasks

    intents = discord.Intents.default()
    intents.reactions = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @tasks.loop(seconds=POLL_SECONDS)
    async def drain_queue() -> None:
        """ワーカーが積んだ配信を投稿する。"""
        channel = bot.get_channel(channel_id)
        if channel is None:
            log.warning("チャンネル %s が見つからない", channel_id)
            return

        for delivery_id, idea in store.pending_deliveries(CHANNEL):
            try:
                message = await channel.send(_format(idea))
                # 押しやすいように先に付けておく
                await message.add_reaction(UP)
                await message.add_reaction(DOWN)
                store.mark_delivered(delivery_id, str(message.id))
                log.info("投稿した: idea=%s message=%s", idea.id, message.id)
            except Exception:  # noqa: BLE001 - 1件の失敗で残りを止めない
                log.exception("投稿に失敗した: idea=%s", idea.id)
                store.mark_delivered(delivery_id, None, failed=True)

    @drain_queue.before_loop
    async def _wait_ready() -> None:
        await bot.wait_until_ready()

    @bot.event
    async def on_ready() -> None:
        log.info("接続した: %s", bot.user)
        if not drain_queue.is_running():
            drain_queue.start()
        try:
            await bot.tree.sync()
        except Exception:  # noqa: BLE001 - コマンド同期の失敗で落とさない
            log.exception("スラッシュコマンドの同期に失敗した")

    @bot.event
    async def on_raw_reaction_add(payload) -> None:
        """リアクションをそのまま評価にする。"""
        if bot.user is not None and payload.user_id == bot.user.id:
            return  # 自分が付けた初期リアクション
        score = REACTIONS.get(str(payload.emoji))
        if score is None:
            return
        idea = store.idea_by_external_id(CHANNEL, str(payload.message_id))
        if idea is None:
            return
        store.rate(idea.id, score)
        log.info("評価がついた: idea=%s score=%+d", idea.id, score)

    @bot.tree.command(name="idea", description="溜まっている考えを受け取る")
    async def idea_command(interaction, domain: str | None = None) -> None:
        await interaction.response.defer()
        idea = service.receive_idea(store, domain=domain)
        message = await interaction.followup.send(_format(idea), wait=True)
        store.mark_delivered(
            store.queue_delivery(idea.id, CHANNEL), str(message.id)
        )
        await message.add_reaction(UP)
        await message.add_reaction(DOWN)

    @bot.tree.command(name="stock", description="いくつ考えが溜まっているか")
    async def stock_command(interaction) -> None:
        state = store.thought_state()
        await interaction.response.send_message(
            f"{store.stock_count()} 個の考えが溜まっている"
            f"（いまは{'連想中' if state.is_associating else '放浪中'}）"
        )

    return bot


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        print("DISCORD_BOT_TOKEN と DISCORD_CHANNEL_ID を設定してください")
        return 1

    store = Store()
    try:
        bot = build_bot(store, int(channel_id))
    except ImportError:
        print('discord.py が必要です: pip install -e ".[bot]"')
        return 1

    try:
        bot.run(token)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
