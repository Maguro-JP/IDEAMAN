"""Discord Bot の中身。

実際の Discord には繋げないので、偽のチャンネルを差し込んで経路を確かめる。
トークンを設定して初めて壊れているのが分かる、が一番痛い箇所なので、
組み立てだけでなく**投稿・スレッド・評価の書き戻し**まで通す。
"""

import asyncio

import pytest

discord = pytest.importorskip("discord")

from ideaman import service  # noqa: E402
from ideaman.bots import discord_bot  # noqa: E402


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.reactions: list[str] = []
        self.thread: FakeThread | None = None

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def create_thread(self, name: str):
        self.thread = FakeThread(name)
        return self.thread


class FakeThread:
    def __init__(self, name: str) -> None:
        self.name = name
        self.messages: list[str] = []

    async def send(self, body: str) -> None:
        self.messages.append(body)


class FakeChannel:
    def __init__(self) -> None:
        self.posted: list[FakeMessage] = []
        self._next_id = 1000

    async def send(self, text: str) -> FakeMessage:
        self._next_id += 1
        message = FakeMessage(self._next_id)
        message.text = text
        self.posted.append(message)
        return message


@pytest.fixture
def ready(store):
    """テーマと在庫を持ち、配信待ちが1件ある状態。"""
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫"])
    for _ in range(60):
        service.generate_idea(store, use_llm=False)
    idea = store.take_ready()
    assert idea is not None, "在庫が作れていない"
    store.queue_delivery(idea.id, discord_bot.CHANNEL)
    return idea


def test_delivery_posts_and_records_the_message_id(store, ready, monkeypatch):
    """投稿したら、その Discord メッセージ ID で引けるようになること。

    ここが切れると 👍 を押しても評価が届かない。
    """
    channel = FakeChannel()
    bot = discord_bot.build_bot(store, 42)
    monkeypatch.setattr(bot, "get_channel", lambda _id: channel)

    asyncio.run(bot.drain_queue.coro())

    assert len(channel.posted) == 1
    message = channel.posted[0]
    assert ready.text in message.text
    assert discord_bot.UP in message.reactions
    assert discord_bot.DOWN in message.reactions

    found = store.idea_by_external_id(discord_bot.CHANNEL, str(message.id))
    assert found is not None and found.id == ready.id


def test_the_rest_of_the_chain_goes_into_a_thread(store, ready, monkeypatch):
    """表に出すのは1〜2件でも、そこに至る思考は捨てていない。"""
    channel = FakeChannel()
    bot = discord_bot.build_bot(store, 42)
    monkeypatch.setattr(bot, "get_channel", lambda _id: channel)

    asyncio.run(bot.drain_queue.coro())
    message = channel.posted[0]

    siblings = [i for i in store.chain(ready.id) if i.id != ready.id]
    if not siblings:
        pytest.skip("この連鎖には他の一手が無い")
    assert message.thread is not None
    body = message.thread.messages[0]
    assert any(i.text in body for i in siblings)
    assert ready.text not in body, "表に出した1件をスレッドにも入れている"


def test_a_failed_post_does_not_stop_the_rest(store, ready, monkeypatch):
    """1件の失敗で残りを止めないこと。失敗は failed として記録する。"""
    second = store.take_ready()
    if second is None:
        pytest.skip("2件目の在庫が無い")
    store.queue_delivery(second.id, discord_bot.CHANNEL)

    class HalfBrokenChannel(FakeChannel):
        async def send(self, text: str):
            if not self.posted:
                self.posted.append(FakeMessage(-1))
                raise RuntimeError("Discord が落ちている")
            return await super().send(text)

    channel = HalfBrokenChannel()
    bot = discord_bot.build_bot(store, 42)
    monkeypatch.setattr(bot, "get_channel", lambda _id: channel)

    asyncio.run(bot.drain_queue.coro())

    # 失敗しても例外は外に出ず、2件目は投稿されている
    assert len(channel.posted) == 2
    assert not store.pending_deliveries(discord_bot.CHANNEL)


def test_a_missing_channel_is_survived(store, ready, monkeypatch):
    bot = discord_bot.build_bot(store, 42)
    monkeypatch.setattr(bot, "get_channel", lambda _id: None)
    asyncio.run(bot.drain_queue.coro())
    # 配信待ちは残ったまま（消してはいけない）
    assert store.pending_deliveries(discord_bot.CHANNEL)


# --- リアクションが評価になること ---------------------------------------
# ここが切れると 👍 を押しても何も育たない。しかも Discord 側は何も
# エラーを出さないので、押している本人には分からない。


class FakePayload:
    def __init__(self, message_id, emoji, user_id=999):
        self.message_id = message_id
        self.emoji = emoji
        self.user_id = user_id


async def _post_one(store, bot, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr(bot, "get_channel", lambda _id: channel)
    await bot.drain_queue.coro()
    return channel.posted[0]


def test_a_thumbs_up_moves_the_weights(store, ready, monkeypatch):
    bot = discord_bot.build_bot(store, 42)
    message = asyncio.run(_post_one(store, bot, monkeypatch))

    before = dict(store.weights())
    asyncio.run(
        bot.on_raw_reaction_add(FakePayload(message.id, discord_bot.UP))
    )

    after = store.weights()
    assert store.get(ready.id).score == 1
    moved = [t for t in after if after[t] != before.get(t, 1.0)]
    assert moved, "評価はついたのに重みが動いていない"


def test_a_thumbs_down_moves_them_the_other_way(store, ready, monkeypatch):
    bot = discord_bot.build_bot(store, 42)
    message = asyncio.run(_post_one(store, bot, monkeypatch))
    asyncio.run(bot.on_raw_reaction_add(FakePayload(message.id, discord_bot.DOWN)))
    assert store.get(ready.id).score == -1


def test_other_emoji_are_ignored(store, ready, monkeypatch):
    bot = discord_bot.build_bot(store, 42)
    message = asyncio.run(_post_one(store, bot, monkeypatch))
    asyncio.run(bot.on_raw_reaction_add(FakePayload(message.id, "\N{SLICE OF PIZZA}")))
    assert store.get(ready.id).score == 0


def test_a_reaction_on_an_unknown_message_is_ignored(store, ready, monkeypatch):
    bot = discord_bot.build_bot(store, 42)
    asyncio.run(_post_one(store, bot, monkeypatch))
    # 存在しないメッセージ ID。落ちずに黙って無視すること
    asyncio.run(bot.on_raw_reaction_add(FakePayload(123456789, discord_bot.UP)))
    assert store.get(ready.id).score == 0


# --- スラッシュコマンド --------------------------------------------------
# /idea は定期投稿とは別の配信経路を通る（その場で受け取って、配信記録を
# 自分で作る）。ここが切れると、リアクションを押しても評価が届かない。


def _command(bot, name):
    for command in bot.tree.get_commands():
        if command.name == name:
            return command
    raise AssertionError(f"{name} コマンドが登録されていない")


class FakeInteraction:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False
        outer = self

        class Response:
            async def defer(self) -> None:
                outer.deferred = True

            async def send_message(self, text: str) -> None:
                outer.sent.append(text)

        class Followup:
            async def send(self, text: str, wait: bool = False):
                outer.sent.append(text)
                return FakeMessage(7777)

        self.response = Response()
        self.followup = Followup()


def test_idea_command_returns_something_and_records_it(store, ready):
    bot = discord_bot.build_bot(store, 42)
    interaction = FakeInteraction()

    asyncio.run(_command(bot, "idea").callback(interaction))

    assert interaction.deferred, "考えている間の応答を返していない"
    assert interaction.sent and interaction.sent[0].strip()

    # 押されたリアクションが評価として届く状態になっていること
    found = store.idea_by_external_id(discord_bot.CHANNEL, "7777")
    assert found is not None
    assert found.text in interaction.sent[0]


def test_stock_command_reports_the_state(store, ready):
    bot = discord_bot.build_bot(store, 42)
    interaction = FakeInteraction()
    asyncio.run(_command(bot, "stock").callback(interaction))
    text = interaction.sent[0]
    assert "溜まっている" in text
    assert "連想中" in text or "放浪中" in text
