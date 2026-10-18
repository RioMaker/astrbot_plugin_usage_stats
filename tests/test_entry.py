import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def entry(monkeypatch):
    def module(name, **values):
        value = types.ModuleType(name)
        value.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    class Star:
        def __init__(self, context):
            self.context = context

    class Filter:
        def command(self, *args, **kwargs):
            return lambda function: function

    class Image:
        @staticmethod
        def fromBytes(data):
            return ("image", data)

    root = Path(__file__).resolve().parents[1]
    package = module("usage_entry_test")
    package.__path__ = [str(root)]
    module("astrbot")
    logger = types.SimpleNamespace(warning=lambda *args: None)
    module("astrbot.api", AstrBotConfig=dict, logger=logger)
    module("astrbot.api.event", AstrMessageEvent=object, filter=Filter())
    module("astrbot.api.message_components", Image=Image)
    module("astrbot.api.star", Context=object, Star=Star)
    module("astrbot.core")
    module("astrbot.core.star")
    module("astrbot.core.star.filter")
    module("astrbot.core.star.filter.command", GreedyStr=str)
    module("astrbot.core.utils")
    module("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: str(root))
    for name in list(sys.modules):
        if name.startswith("usage_entry_test."):
            monkeypatch.delitem(sys.modules, name)
    return importlib.import_module("usage_entry_test.main")


class Event:
    def __init__(self, user="u1", group="g1"):
        self.user = user
        self.group = group
        self.stopped = False

    def get_sender_id(self):
        return self.user

    def get_self_id(self):
        return "bot"

    def get_platform_id(self):
        return "adapter-1"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self.group

    def get_sender_name(self):
        return "测试用户"

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


async def collect(generator):
    return [item async for item in generator]


def test_personal_uses_reported_tokens_and_private_ranking_rejected(entry, tmp_path):
    plugin = entry.UsageStats(object(), {})
    plugin.store = entry.Store(tmp_path / "data.db")
    event = Event()
    provider = types.SimpleNamespace(
        meta=lambda: types.SimpleNamespace(id="p", model="m")
    )

    async def run():
        await plugin._command(event, "plugin::command", "帮助")
        await plugin._llm(
            event,
            provider,
            types.SimpleNamespace(
                usage=types.SimpleNamespace(input_other=100, input_cached=30, output=20)
            ),
            False,
        )
        response = await collect(plugin.usage_stats(event, ""))
        assert "指令调用：1 次" in response[0]
        assert "累计已知 Token：150" in response[0]
        assert event.stopped
        assert (await collect(plugin.usage_ranking(Event(group="")))) == [
            "请在群聊中使用 /使用排行。"
        ]
        await plugin.terminate()

    asyncio.run(run())


def test_missing_font_falls_back_and_malformed_pages_do_not_raise(entry, tmp_path):
    plugin = entry.UsageStats(object(), {"font_path": str(tmp_path / "missing.ttf")})
    plugin.store = entry.Store(tmp_path / "data.db")
    event = Event()

    async def run():
        await plugin._command(event, "k", "rp")
        result = await collect(plugin.usage_ranking(event))
        assert isinstance(result[0], str)
        assert "测试用户 · 1 次" in result[0]
        assert "页码" in (await collect(plugin.usage_stats(event, "指令 -1")))[0]
        assert "用法" in (await collect(plugin.usage_stats(event, "其他")))[0]
        await plugin.terminate()

    asyncio.run(run())


def test_error_state_is_visible_and_own_bot_not_recorded(entry, tmp_path):
    plugin = entry.UsageStats(object(), {})
    plugin.store = entry.Store(tmp_path / "data.db")

    async def run():
        await plugin._command(Event(user="bot"), "k", "rp")
        plugin._tracking_error("internal error")
        result = await collect(plugin.usage_stats(Event(), ""))
        assert "可能不完整" in result[0]
        assert "指令调用：0 次" in result[0]
        await plugin.terminate()

    asyncio.run(run())
