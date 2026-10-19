import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone
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
            def register(function):
                function.command_name = args[0]
                return function

            return register

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


def test_token_commands_registered_and_personal_today_with_start_time(
    entry, tmp_path, monkeypatch
):
    assert entry.UsageStats.token_stats.command_name == "token"
    assert entry.UsageStats.token_ranking.command_name == "token排行"
    plugin = entry.UsageStats(object(), {})
    plugin.store = entry.Store(tmp_path / "data.db")
    event = Event(group="")
    identity = plugin._identity(event)
    instant = datetime(2026, 10, 19, 1, 30, tzinfo=timezone.utc)
    previous = datetime(2026, 10, 18, 1, 30, tzinfo=timezone.utc)
    original = plugin.store.token_summary
    monkeypatch.setattr(
        plugin.store, "token_summary", lambda person: original(person, now=instant)
    )
    plugin.store.record_llm(identity, "p", "m", 9, 80, 20, 100, occurred_at=previous)
    plugin.store.record_llm(identity, "p", "m", 9, 30, 20, 50, occurred_at=instant)
    plugin.store.record_llm(
        identity, "p", "m", 9, None, None, None, occurred_at=instant
    )

    async def run():
        result = (await collect(plugin.token_stats(event)))[0]
        assert event.stopped
        assert "累计已知 Token：150" in result
        assert "今日（2026-10-19，北京时间）已知 Token：50" in result
        assert "今日 LLM 调用：2 次 · 未返回总用量 1 次" in result
        assert "统计开始：" in result and "每日统计开始：" in result
        assert "未估算" in result
        assert original(identity, now=instant)["llm_calls"] == 3
        assert (await collect(plugin.token_ranking(event))) == [
            "请在群聊中使用 /token排行。"
        ]
        await plugin.terminate()

    asyncio.run(run())


def test_token_ranking_image_receives_group_only_tokens_and_metadata(
    entry, tmp_path, monkeypatch
):
    plugin = entry.UsageStats(object(), {"leaderboard_size": 1})
    plugin.store = entry.Store(tmp_path / "data.db")
    own, other = Event(), Event(user="u2")
    plugin.store.record_llm(plugin._identity(own), "p", "m", 0, 10, 5, 15)
    plugin.store.record_llm(plugin._identity(other), "p", "m", 0, 20, 10, 30)
    plugin.store.record_llm(
        plugin._identity(Event(group="other")), "p", "m", 0, 999, 1, 1000
    )
    plugin.store.record_llm(plugin._identity(own), "p", "m", 0, None, None, None)
    captured = {}

    def render(rows, **kwargs):
        captured.update(rows=rows, **kwargs)
        return b"test-png"

    monkeypatch.setattr(entry, "render_leaderboard", render)

    async def run():
        assert (await collect(plugin.token_ranking(own))) == [[("image", b"test-png")]]
        assert own.stopped
        assert len(captured["rows"]) == 1
        assert captured["rows"][0]["count"] == 30
        assert captured["personal"]["count"] == 15
        assert captured["personal"]["rank"] == 2
        assert captured["metric"] == "tokens"
        assert captured["missing_usage"] == 1
        assert len(captured["started_at"]) == 19
        # The existing leaderboard remains command-only.
        assert plugin.store.leaderboard(plugin._identity(own))["entries"] == []
        await plugin.terminate()

    asyncio.run(run())


def test_token_fallback_unknown_usage_warning_and_read_failure(entry, tmp_path):
    plugin = entry.UsageStats(object(), {"font_path": str(tmp_path / "missing.ttf")})
    plugin.store = entry.Store(tmp_path / "data.db")
    event = Event()
    plugin.store.record_llm(plugin._identity(event), "p", "m", 0, None, None, None)

    async def run():
        result = (await collect(plugin.token_ranking(event)))[0]
        assert "本群暂无已知 Token 记录" in result
        assert "统计开始：" in result
        assert "本群未返回总用量：1 次" in result
        assert "次指令" not in result
        plugin._tracking_error("test")
        assert "可能不完整" in (await collect(plugin.token_stats(event)))[0]
        assert "可能不完整" in (await collect(plugin.token_ranking(event)))[0]
        plugin.store.close()
        assert "读取失败" in (await collect(plugin.token_stats(event)))[0]
        assert "读取失败" in (await collect(plugin.token_ranking(event)))[0]
        await plugin.terminate()
        assert "尚未就绪" in (await collect(plugin.token_stats(event)))[0]
        assert "尚未就绪" in (await collect(plugin.token_ranking(event)))[0]

    asyncio.run(run())


def test_llm_records_one_completion_instant_for_beijing_day_and_hour(
    entry, tmp_path, monkeypatch
):
    plugin = entry.UsageStats(object(), {})
    plugin.store = entry.Store(tmp_path / "data.db")
    instant = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(tz):
            return instant.astimezone(tz)

    monkeypatch.setattr(entry, "datetime", Clock)
    provider = types.SimpleNamespace(
        meta=lambda: types.SimpleNamespace(id="p", model="m")
    )
    event = Event()

    async def run():
        await plugin._llm(
            event,
            provider,
            types.SimpleNamespace(
                usage=types.SimpleNamespace(input_other=100, input_cached=30, output=20)
            ),
            False,
        )
        data = plugin.store.token_summary(plugin._identity(event), now=instant)
        assert data["today"] == "2026-10-19"
        assert data["today_total_tokens"] == 150
        assert plugin.store.personal(plugin._identity(event))["llm_hours"][0] == 1
        await plugin.terminate()

    asyncio.run(run())
