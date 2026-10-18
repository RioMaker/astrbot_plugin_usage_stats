"""AstrBot entry points. Observation never changes the host's response or routing."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .instrumentation import Tracker
from .renderer import render_leaderboard
from .reporting import (
    clean,
    command_details,
    leaderboard_text,
    model_details,
    personal_summary,
)
from .storage import Identity, Store
from .usage import field, normalize_usage, provider_labels

BEIJING = timezone(timedelta(hours=8))


class UsageStats(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = None
        self.tracker = None
        self.observation_error = ""
        self._render_lock = asyncio.Lock()

    async def initialize(self):
        directory = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_usage_stats"
        )
        directory.mkdir(parents=True, exist_ok=True)
        self.store = await asyncio.to_thread(
            Store,
            directory / "usage.db",
            max_mib=self._int("max_database_mib", 64, 1, 1024),
        )
        self.tracker = Tracker(
            self.context, self._command, self._llm, self._tracking_error
        )
        if not self.tracker.install():
            self._tracking_error(
                "当前 AstrBot 的统计接入不完整，请查看日志并核对兼容版本。"
            )

    def _int(self, key, default, lower, upper):
        try:
            return min(upper, max(lower, int(self.config.get(key, default))))
        except (TypeError, ValueError):
            return default

    def _tracking_error(self, message):
        self.observation_error = str(message)
        # Do not print upstream exception bodies, requests or provider credentials.
        summary = clean(str(message).split(": ", 1)[0], 180)
        logger.warning(f"[使用统计] {summary}；累计数据可能不完整。")

    @staticmethod
    def _identity(event):
        user = str(event.get_sender_id() or "")
        bot = str(event.get_self_id() or "")
        if not user or user == bot:
            return None
        return Identity(
            platform=str(event.get_platform_id() or event.get_platform_name()),
            bot=bot or "<unknown>",
            user=user,
            group=str(event.get_group_id() or ""),
            name=clean(event.get_sender_name() or user, 64),
        )

    async def _command(self, event, key, label):
        identity = self._identity(event)
        if identity and self.store:
            await asyncio.to_thread(
                self.store.record_command,
                identity,
                key,
                label,
                datetime.now(BEIJING).hour,
            )

    async def _llm(self, event, provider, response, failed):
        identity = self._identity(event)
        if not identity or not self.store:
            return
        input_, output, total = normalize_usage(response)
        provider_id, model = provider_labels(provider, response)
        await asyncio.to_thread(
            self.store.record_llm,
            identity,
            provider_id,
            model,
            datetime.now(BEIJING).hour,
            input_,
            output,
            total,
            failed=failed or field(response, "role") == "err",
        )

    async def _warning(self):
        if not self.store:
            return "统计存储尚未就绪。"
        state = await asyncio.to_thread(self.store.status)
        if (
            self.observation_error
            or state.get("write_failures")
            or state.get("last_error")
        ):
            return (
                "⚠ 统计接入或写入曾出现异常，累计数可能不完整，"
                "请管理员检查日志和存储容量。"
            )
        return ""

    @filter.command("使用统计")
    async def usage_stats(self, event: AstrMessageEvent, content: GreedyStr):
        """个人累计统计；使用统计 指令/模型 页码 查看明细。"""
        event.stop_event()
        identity = self._identity(event)
        if not identity or not self.store:
            yield event.plain_result("统计尚未就绪，或此消息没有可识别的发送者。")
            return
        parts = str(content).split()
        kind = parts[0] if parts else ""
        if kind not in {"", "指令", "模型"} or len(parts) > 2:
            yield event.plain_result("用法：/使用统计，或 /使用统计 指令|模型 [页码]")
            return
        try:
            page = int(parts[1]) if len(parts) == 2 else 1
            if page < 1:
                raise ValueError
        except ValueError:
            yield event.plain_result("页码请填写大于 0 的整数。")
            return
        try:
            data = await asyncio.to_thread(
                self.store.personal,
                identity,
                page=page,
                model_page=page,
                model_page_size=8,
            )
            if kind == "指令":
                text = command_details(data)
            elif kind == "模型":
                text = model_details(data, page)
            else:
                text = personal_summary(identity.name, data)
            warning = await self._warning()
            yield event.plain_result(text + ("\n" + warning if warning else ""))
        except Exception as exc:
            logger.warning(f"[使用统计] 查询失败：{type(exc).__name__}")
            yield event.plain_result(
                "统计读取失败，请管理员检查数据库状态；已有数据不会被自动清空。"
            )

    @filter.command("使用排行")
    async def usage_ranking(self, event: AstrMessageEvent):
        """显示当前群累计指令调用排行榜。"""
        event.stop_event()
        identity = self._identity(event)
        if not identity or not identity.group:
            yield event.plain_result("请在群聊中使用 /使用排行。")
            return
        if not self.store:
            yield event.plain_result("统计尚未就绪。")
            return
        try:
            data = await asyncio.to_thread(
                self.store.leaderboard,
                identity,
                limit=self._int("leaderboard_size", 10, 1, 20),
            )
            warning = await self._warning()
            if warning:
                yield event.plain_result(leaderboard_text(data) + "\n" + warning)
                return
            # Serialize rendering to bound concurrent Pillow memory. The PNG is
            # passed as bytes; the plugin never leaves image files on disk.
            async with self._render_lock:
                png = await asyncio.to_thread(
                    render_leaderboard,
                    data["entries"],
                    group_label=f"群 {identity.group}",
                    personal=data["self"],
                    font_path=self.config.get("font_path") or None,
                    subtitle="本群累计指令调用 · 包含本次查询",
                )
            yield event.chain_result([Image.fromBytes(png)])
        except Exception as exc:
            logger.warning(f"[使用统计] 排行图片不可用：{type(exc).__name__}")
            if "data" in locals():
                yield event.plain_result(leaderboard_text(data))
            else:
                yield event.plain_result("统计读取失败，请管理员检查数据库状态。")

    async def terminate(self):
        if self.tracker:
            self.tracker.uninstall()
        if self.store:
            await asyncio.to_thread(self.store.close)
            self.store = None
