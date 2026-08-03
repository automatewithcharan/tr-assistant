"""Registered Discord callback tests using only in-memory fakes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pkscreener.classes.AutoPilotInvestor import CycleResult
from pkscreener.classes.DiscordTradeBot import run_discord_trade_bot


class _Response:
    def __init__(self, events):
        self.events = events
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content, **kwargs):
        self._done = True
        self.events.append(("response", str(content)))

    async def defer(self, **kwargs):
        self._done = True
        self.events.append(("defer", ""))


class _Followup:
    def __init__(self, events):
        self.events = events

    async def send(self, content, **kwargs):
        self.events.append(("followup", str(content)))


class _Interaction:
    def __init__(self, user_id=101, channel_id=202, guild_id=303):
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.events = []
        self.response = _Response(self.events)
        self.followup = _Followup(self.events)


class _Channel:
    def __init__(self):
        self.id = 202
        self.events = []

    async def send(self, content, **kwargs):
        self.events.append(str(content))


class _Message:
    def __init__(self, content, user_id=101, channel_id=202):
        self.content = content
        self.author = SimpleNamespace(id=user_id)
        self.channel = _Channel()
        self.channel.id = channel_id


class _Broker:
    def __init__(self):
        self.mutations = 0


class _Manager:
    instances = []
    broker = _Broker()
    attempts = 0
    max_order_value = 5000.0

    def __init__(self, output_dir=None):
        self.__class__.instances.append(self)

    def _load_ticks(self):
        return {"RELIANCE": {"last_price": 100.0}}

    def get_position(self, symbol):
        return None

    def place_buy_order(self, **kwargs):
        self.__class__.attempts += 1
        return SimpleNamespace(success=False, message="central policy denied", qty=0, order_id="")

    def place_sell_order(self, **kwargs):
        self.__class__.attempts += 1
        return SimpleNamespace(success=False, message="central policy denied", qty=0, order_id="")

    def place_gtt_oco(self, *args, **kwargs):
        self.__class__.attempts += 1
        return {"success": False, "message": "central policy denied"}

    def reconcile_with_broker(self):
        return {"broker_positions": [], "tracked_positions": [], "mismatches": []}


class _Investor:
    configs = []

    def __init__(self, config):
        self.__class__.configs.append(config)

    def run_once(self):
        return CycleResult()


class _Config:
    def checkConfigFile(self):
        return True

    def getConfig(self, *_args, **_kwargs):
        return self


def _surface(monkeypatch, tmp_path, user="101", channel="202"):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-only-token")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", user)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", channel)
    monkeypatch.setenv("DISCORD_GUILD_ID", "303")

    from PKDevTools.classes import Archiver
    import pkscreener.classes.DiscordTradeBot as module
    import pkscreener.classes.ConfigManager as config_module

    monkeypatch.setattr(Archiver, "get_user_outputs_dir", lambda: str(tmp_path))
    monkeypatch.setattr(config_module, "tools", lambda: _Config())
    _Manager.instances.clear()
    _Manager.attempts = 0
    _Manager.broker.mutations = 0
    _Investor.configs.clear()
    holder = {}

    async def capture(bot):
        holder["bot"] = bot

    async def build():
        await run_discord_trade_bot(
            account_name="test",
            universe="allstocks",
            manager_factory=_Manager,
            autopilot_factory=_Investor,
            test_surface=capture,
        )
    asyncio.run(build())
    return holder["bot"]


def _command(bot, name):
    command = bot.tree.get_command(name)
    assert command is not None, f"registered command missing: {name}"
    return command.callback


def test_registered_autopilot_is_scan_only(monkeypatch, tmp_path):
    monkeypatch.setenv("KITE_AUTO_TRADE", "1")
    bot = _surface(monkeypatch, tmp_path)
    interaction = _Interaction()
    asyncio.run(_command(bot, "autopilot")(interaction))
    assert any("Blind-Buy Scan Results" in text for _, text in interaction.events)
    assert _Investor.configs and _Investor.configs[-1].broker_execution_enabled is False
    assert _Manager.broker.mutations == 0


@pytest.mark.parametrize("name", ["kitebuy", "kitesell"])
def test_registered_kite_mutations_require_complete_policy(monkeypatch, tmp_path, name):
    bot = _surface(monkeypatch, tmp_path)
    interaction = _Interaction()
    asyncio.run(_command(bot, name)(interaction, "RELIANCE"))
    assert interaction.events
    assert _Manager.broker.mutations == 0


def test_registered_callbacks_deny_wrong_identity_and_malformed_ids(monkeypatch, tmp_path):
    bot = _surface(monkeypatch, tmp_path)
    for interaction in (_Interaction(user_id=999), _Interaction(channel_id=999), _Interaction(user_id="bad")):
        asyncio.run(_command(bot, "kitebuy")(interaction, "RELIANCE"))
        assert any(
            token in text.lower()
            for _, text in interaction.events
            for token in ("author", "disabled", "sensitive", "trade commands")
        )
    assert _Manager.broker.mutations == 0


def test_registered_read_only_whoami_remains_usable(monkeypatch, tmp_path):
    bot = _surface(monkeypatch, tmp_path)
    interaction = _Interaction()
    asyncio.run(_command(bot, "whoami")(interaction))
    assert any("User ID: 101" in text for _, text in interaction.events)
    assert _Manager.broker.mutations == 0


def test_registered_bought_listener_is_in_memory_and_fail_closed(monkeypatch, tmp_path):
    bot = _surface(monkeypatch, tmp_path)
    message = _Message("bought RELIANCE 10 100")
    asyncio.run(bot.on_message(message))
    assert message.channel.events
    assert _Manager.broker.mutations == 0

    malformed = _Message("bought RELIANCE nope 100")
    asyncio.run(bot.on_message(malformed))
    assert any("Could not parse" in text for text in malformed.channel.events)
    assert _Manager.broker.mutations == 0


def test_registered_bought_listener_rejects_wrong_user_and_channel(monkeypatch, tmp_path):
    bot = _surface(monkeypatch, tmp_path)
    for message in (_Message("bought RELIANCE 10 100", user_id=999), _Message("bought RELIANCE 10 100", channel_id=999)):
        asyncio.run(bot.on_message(message))
        assert message.channel.events == []
    assert _Manager.broker.mutations == 0
