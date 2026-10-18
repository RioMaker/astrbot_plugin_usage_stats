"""Execution-contract tests; no real model/network requests are made."""

import ast
import asyncio
import collections.abc
import contextlib
import importlib.util
import inspect
import os
import sys
import traceback
import types
import typing
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "instrumentation.py"
SPEC = importlib.util.spec_from_file_location(
    "usage_stats_instrumentation_tests", MODULE_PATH
)
instrumentation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = instrumentation
SPEC.loader.exec_module(instrumentation)
Tracker = instrumentation.Tracker


def response(tokens=None, *, chunk=False):
    usage = (
        None
        if tokens is None
        else types.SimpleNamespace(input_other=tokens, input_cached=0, output=0)
    )
    return types.SimpleNamespace(usage=usage, is_chunk=chunk)


class Event:
    def __init__(self, name, handlers=(), parsed=None, action=None):
        self.name = name
        self.action = action
        self.stopped = False
        self.extras = {
            "activated_handlers": list(handlers),
            "handlers_parsed_params": parsed or {},
        }

    def get_extra(self, key):
        return self.extras.get(key)


class CommandFilter:
    def __init__(self, name, parents=None):
        self.command_name = name
        self.parent_command_names = parents or [""]


def metadata(handler, name="plugin_handler", command="报告", parents=None):
    return types.SimpleNamespace(
        handler=handler,
        handler_full_name=name,
        event_filters=[] if command is None else [CommandFilter(command, parents)],
    )


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = []
        self.calls = []
        self.errors = []
        self.providers = []
        self.trackers = []
        self.star = types.ModuleType("star_request")

        async def call_handler(event, handler, *args, **kwargs):
            result = handler(event, *args, **kwargs)
            if hasattr(result, "__aiter__"):
                async for item in result:
                    yield item
            else:
                yield await result

        self.star.call_handler = call_handler
        star = self.star

        class Scheduler:
            async def execute(self, event):
                for md in event.get_extra("activated_handlers"):
                    if event.stopped:
                        break
                    params = event.get_extra("handlers_parsed_params").get(
                        md.handler_full_name, {}
                    )
                    async for _ in star.call_handler(event, md.handler, **params):
                        pass
                if event.action:
                    await event.action()
                return "original-result"

        self.scheduler_cls = Scheduler
        self.scheduler = Scheduler()
        modules = {
            "astrbot.core.pipeline.scheduler": types.SimpleNamespace(
                PipelineScheduler=Scheduler
            ),
            "astrbot.core.pipeline.process_stage.method.star_request": self.star,
            "astrbot.core.star.filter.command": types.SimpleNamespace(
                CommandFilter=CommandFilter
            ),
        }
        self.import_patch = patch.object(
            instrumentation.importlib, "import_module", side_effect=modules.__getitem__
        )
        self.import_patch.start()
        self.context = types.SimpleNamespace(get_all_providers=lambda: self.providers)
        self.tracker = self.new_tracker()

    def new_tracker(self, command=None, llm=None):
        async def on_command(event, key, label):
            self.commands.append((event.name, key, label))

        async def on_llm(event, provider, result, failed):
            usage = getattr(result, "usage", None)
            self.calls.append(
                (
                    event.name,
                    provider,
                    None if usage is None else usage.input_other,
                    failed,
                )
            )

        tracker = Tracker(
            self.context, command or on_command, llm or on_llm, self.errors.append
        )
        self.trackers.append(tracker)
        self.assertTrue(tracker.install())
        return tracker

    def tearDown(self):
        for tracker in reversed(self.trackers):
            tracker.uninstall()
        self.import_patch.stop()

    def provider(self, value=7):
        class Provider:
            async def text_chat(self):
                return response(value)

            async def text_chat_stream(self):
                yield response(2, chunk=True)
                yield response(value)

        provider = Provider()
        self.providers.append(provider)
        return provider

    async def test_only_executed_parsed_command_is_counted_and_aliases_use_canonical(
        self,
    ):
        async def stop(event):
            event.stopped = True

        async def skipped(event):
            raise AssertionError("blocked handler must not execute")

        first = metadata(stop, "p_stop", "查询", ["组", "group"])
        second = metadata(skipped, "p_skipped")
        event = Event("u", [first, second], {"p_stop": {}, "p_skipped": {}})
        await self.scheduler.execute(event)
        self.assertEqual(self.commands, [("u", "p_stop", "组 查询")])

    async def test_listener_or_unparsed_handler_is_not_a_command(self):
        async def listen(event):
            pass

        listener = metadata(listen, command=None)
        await self.scheduler.execute(Event("u", [listener], {"plugin_handler": {}}))
        command = metadata(listen)
        await self.scheduler.execute(Event("u", [command]))
        self.assertEqual(self.commands, [])

    async def test_direct_llm_and_main_agent_each_count_once(self):
        provider = self.provider()

        async def handler(event):
            return await provider.text_chat()

        md = metadata(handler)
        event = Event(
            "u", [md], {"plugin_handler": {}}, action=lambda: provider.text_chat()
        )
        self.assertEqual(await self.scheduler.execute(event), "original-result")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual([call[2:] for call in self.calls], [(7, False), (7, False)])

    async def test_stream_records_last_final_not_chunks_or_multiple_final_sums(self):
        class Provider:
            async def text_chat(self):
                return response(1)

            async def text_chat_stream(self):
                yield response(100, chunk=True)
                yield response(200, chunk=True)
                yield response(6)
                yield response(9)

        provider = Provider()
        self.providers.append(provider)

        async def action():
            values = [part async for part in provider.text_chat_stream()]
            self.assertEqual(len(values), 4)

        await self.scheduler.execute(Event("u", action=action))
        self.assertEqual([call[2:] for call in self.calls], [(9, False)])

    async def test_stream_aclose_marks_partial_usage_unknown_and_failure(self):
        provider = self.provider()

        async def action():
            stream = provider.text_chat_stream()
            await stream.__anext__()
            await stream.aclose()

        await self.scheduler.execute(Event("u", action=action))
        self.assertEqual([call[2:] for call in self.calls], [(None, True)])

    async def test_stream_does_not_hold_provider_depth_across_yield(self):
        provider = self.provider()

        async def action():
            stream = provider.text_chat_stream()
            await stream.__anext__()
            await provider.text_chat()
            await stream.aclose()

        await self.scheduler.execute(Event("u", action=action))
        self.assertEqual([call[2:] for call in self.calls], [(7, False), (None, True)])

    async def test_failed_chunk_only_stream_does_not_invent_complete_usage(self):
        class Provider:
            async def text_chat(self):
                return response(1)

            async def text_chat_stream(self):
                part = response(4, chunk=True)
                yield part
                part.usage.input_other = 999
                raise RuntimeError("stream disconnected")

        provider = Provider()
        self.providers.append(provider)

        async def action():
            with self.assertRaisesRegex(RuntimeError, "stream disconnected"):
                async for _ in provider.text_chat_stream():
                    pass

        await self.scheduler.execute(Event("u", action=action))
        self.assertEqual([call[2:] for call in self.calls], [(None, True)])

    async def test_inheritance_and_stream_chat_fallback_do_not_double_count(self):
        class Base:
            async def text_chat(self):
                return response(11)

            async def text_chat_stream(self):
                yield await self.text_chat()

        class Child(Base):
            async def text_chat(self):
                return await super().text_chat()

        base, child = Base(), Child()
        self.providers.extend([base, child])

        async def action():
            await child.text_chat()
            async for _ in child.text_chat_stream():
                pass

        await self.scheduler.execute(Event("u", action=action))
        self.assertEqual([call[2:] for call in self.calls], [(11, False), (11, False)])

    async def test_different_nested_providers_are_separate_calls(self):
        inner = self.provider(3)

        class Outer:
            async def text_chat(self):
                await inner.text_chat()
                return response(5)

            async def text_chat_stream(self):
                yield response(5)

        outer = Outer()
        self.providers.append(outer)
        await self.scheduler.execute(Event("u", action=lambda: outer.text_chat()))
        self.assertEqual([call[2:] for call in self.calls], [(3, False), (5, False)])

    async def test_concurrent_events_are_isolated(self):
        provider = self.provider()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow():
            entered.set()
            await release.wait()
            await provider.text_chat()

        task = asyncio.create_task(self.scheduler.execute(Event("first", action=slow)))
        await entered.wait()
        await self.scheduler.execute(
            Event("second", action=lambda: provider.text_chat())
        )
        release.set()
        await task
        self.assertEqual([call[0] for call in self.calls], ["second", "first"])
        await provider.text_chat()
        self.assertEqual(len(self.calls), 2)

    async def test_background_task_loses_attribution_after_parent_finishes(self):
        provider = self.provider()
        release = asyncio.Event()
        tasks = []

        async def background():
            await release.wait()
            await provider.text_chat()

        async def action():
            tasks.append(asyncio.create_task(background()))

        await self.scheduler.execute(Event("u", action=action))
        release.set()
        await tasks[0]
        self.assertEqual(self.calls, [])

    async def test_failures_and_missing_usage_are_reported_without_rewriting_error(
        self,
    ):
        class Provider:
            async def text_chat(self):
                raise ValueError("upstream failure")

            async def text_chat_stream(self):
                yield response()

        provider = Provider()
        self.providers.append(provider)
        with self.assertRaisesRegex(ValueError, "upstream failure"):
            await self.scheduler.execute(
                Event("failure", action=lambda: provider.text_chat())
            )

        async def stream():
            async for _ in provider.text_chat_stream():
                pass

        await self.scheduler.execute(Event("unknown", action=stream))
        self.assertEqual(
            [call[2:] for call in self.calls], [(None, True), (None, False)]
        )

    async def test_cancelled_call_is_recorded_and_cancellation_propagates(self):
        entered = asyncio.Event()

        class Provider:
            async def text_chat(self):
                entered.set()
                await asyncio.Event().wait()

            async def text_chat_stream(self):
                yield response()

        provider = Provider()
        self.providers.append(provider)
        task = asyncio.create_task(
            self.scheduler.execute(Event("u", action=lambda: provider.text_chat()))
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([call[2:] for call in self.calls], [(None, True)])

    async def test_callback_failure_does_not_break_provider_or_command(self):
        self.tracker.uninstall()

        async def broken(*args):
            raise OSError("database unavailable")

        self.new_tracker(command=broken, llm=broken)
        provider = self.provider()

        async def handler(event):
            return await provider.text_chat()

        md = metadata(handler)
        result = await self.scheduler.execute(Event("u", [md], {"plugin_handler": {}}))
        self.assertEqual(result, "original-result")
        self.assertTrue(
            any("database unavailable" in message for message in self.errors)
        )

    async def test_provider_loaded_after_install_is_discovered(self):
        provider = self.provider()
        self.assertEqual(self.tracker.status()["provider_classes"], 0)
        await self.scheduler.execute(Event("u", action=lambda: provider.text_chat()))
        self.assertEqual(self.tracker.status()["provider_classes"], 1)
        self.assertEqual(len(self.calls), 1)

    async def test_uninstall_restores_inherited_attributes(self):
        class Base:
            async def text_chat(self):
                return response(1)

            async def text_chat_stream(self):
                yield response(1)

        class Child(Base):
            pass

        original = Base.text_chat
        self.providers.append(Child())
        self.tracker.refresh()
        self.assertIn("text_chat", vars(Child))
        self.tracker.uninstall()
        self.assertNotIn("text_chat", vars(Child))
        self.assertIs(Child.text_chat, original)

    async def test_uninstall_preserves_later_wrapper_and_reload_counts_once(self):
        provider = self.provider()
        self.tracker.refresh()
        retained = type(provider).text_chat
        third_party_hits = []

        async def later(instance, *args, **kwargs):
            third_party_hits.append(True)
            return await retained(instance, *args, **kwargs)

        type(provider).text_chat = later
        self.tracker.uninstall()
        self.assertIs(type(provider).text_chat, later)
        self.new_tracker()
        await self.scheduler.execute(Event("u", action=lambda: provider.text_chat()))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(third_party_hits), 1)

    async def test_handler_generator_does_not_leak_context_after_yield(self):
        provider = self.provider()
        self.tracker.refresh()

        async def handler(event):
            yield await provider.text_chat()

        md = metadata(handler)
        event = Event("u", [md], {"plugin_handler": {}})
        iterator = self.star.call_handler(event, handler)
        await iterator.__anext__()
        await provider.text_chat()
        await iterator.aclose()
        self.assertEqual(len(self.calls), 1)

    async def test_unsupported_core_reports_inactive_status(self):
        self.tracker.uninstall()
        with patch.object(
            instrumentation.importlib,
            "import_module",
            side_effect=ImportError("unsupported"),
        ):
            self.assertFalse(self.tracker.install())
        self.assertFalse(self.tracker.status()["installed"])
        self.assertTrue(any("unsupported" in message for message in self.errors))

    async def test_observer_metadata_errors_do_not_stop_handler(self):
        ran = []

        async def handler(event):
            ran.append(True)

        md = metadata(handler, parents=[object()])
        event = Event("u", [md], {"plugin_handler": {}})
        with patch.object(
            self.tracker, "_command", side_effect=ValueError("metadata changed")
        ):
            await self.scheduler.execute(event)
        self.assertEqual(ran, [True])
        self.assertEqual(self.commands, [])
        self.assertTrue(any("metadata changed" in message for message in self.errors))

    async def test_snapshot_failure_preserves_stream_output_and_reports_unknown(self):
        class OpaqueResponse:
            is_chunk = False

            def __copy__(self):
                raise TypeError("cannot copy")

        original_response = OpaqueResponse()

        class Provider:
            async def text_chat(self):
                return original_response

            async def text_chat_stream(self):
                yield original_response

        provider = Provider()
        self.providers.append(provider)

        async def action():
            values = [part async for part in provider.text_chat_stream()]
            self.assertIs(values[0], original_response)

        await self.scheduler.execute(Event("u", action=action))
        self.assertEqual([call[2:] for call in self.calls], [(None, False)])
        self.assertTrue(any("cannot copy" in message for message in self.errors))

    async def test_cancelled_stream_keeps_an_already_received_complete_usage(self):
        class Provider:
            async def text_chat(self):
                return response(1)

            async def text_chat_stream(self):
                yield response(8)

        provider = Provider()
        self.providers.append(provider)

        async def action():
            stream = provider.text_chat_stream()
            await stream.__anext__()
            await stream.aclose()

        await self.scheduler.execute(Event("u", action=action))
        self.assertEqual([call[2:] for call in self.calls], [(8, True)])


@unittest.skipUnless(
    os.environ.get("ASTRBOT_USAGE_SOURCE_DIR"),
    "set ASTRBOT_USAGE_SOURCE_DIR to the pinned official source snapshot",
)
class OfficialSourceContractTests(unittest.IsolatedAsyncioTestCase):
    """Execute real upstream definitions with only outside dependencies stubbed.

    Source files are intentionally not downloaded by the test suite. The audit
    snapshot used here is AstrBot master dffe922773725e9c178392c423676ad65044748c
    (4.29.1); supply its directory explicitly for integration-contract checks.
    """

    def source(self, filename, names, namespace):
        path = Path(os.environ["ASTRBOT_USAGE_SOURCE_DIR"]) / filename
        self.assertTrue(path.is_file(), str(path))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if getattr(node, "name", None) in names]
        self.assertEqual(len(nodes), len(names))
        future = ast.parse("from __future__ import annotations").body
        module = ast.fix_missing_locations(
            ast.Module(body=future + nodes, type_ignores=[])
        )
        exec(compile(module, str(path), "exec"), namespace)

    async def test_real_scheduler_and_star_request_stop_before_blocked_command(self):
        logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )

        class Result:
            def message(self, text):
                return self

        async def hook(*args):
            return False

        star = types.ModuleType("official_star_request")
        star.__dict__.update(
            {
                "inspect": inspect,
                "logger": logger,
                "MessageEventResult": Result,
                "CommandResult": Result,
                "traceback": traceback,
                "Stage": object,
                "star_map": {"test": types.SimpleNamespace(name="test")},
                "call_event_hook": hook,
                "EventType": types.SimpleNamespace(OnPluginErrorEvent=object()),
            }
        )
        self.source(
            "astrbot__core__pipeline__context_utils.py", {"call_handler"}, star.__dict__
        )
        self.source(
            "astrbot__core__pipeline__process_stage__method__star_request.py",
            {"StarRequestSubStage"},
            star.__dict__,
        )
        scheduler_ns = {
            "logger": logger,
            "AsyncGenerator": collections.abc.AsyncGenerator,
            "cast": typing.cast,
            "active_event_registry": types.SimpleNamespace(
                register=lambda e: None, unregister=lambda e: None
            ),
            "log_context": lambda *a: contextlib.nullcontext(),
            "WebChatMessageEvent": type("WebChat", (), {}),
            "WecomAIBotMessageEvent": type("Wecom", (), {}),
        }
        self.source(
            "astrbot__core__pipeline__scheduler.py", {"PipelineScheduler"}, scheduler_ns
        )
        scheduler_cls = scheduler_ns["PipelineScheduler"]
        scheduler = object.__new__(scheduler_cls)
        scheduler.stages = [star.StarRequestSubStage()]

        class RealEvent(Event):
            is_at_or_wake_command = True

            def is_stopped(self):
                return self.stopped

            def stop_event(self):
                self.stopped = True

            def clear_result(self):
                pass

            def get_platform_id(self):
                return "test"

            def get_platform_name(self):
                return "test"

            def cleanup_temporary_local_files(self):
                pass

        calls, commands = [], []

        class Provider:
            async def text_chat(self):
                return response(12)

            async def text_chat_stream(self):
                yield response(12)

        provider = Provider()

        async def on_command(event, key, label):
            commands.append((event.name, key, label))

        async def on_llm(event, instance, result, failed):
            calls.append((event.name, result.usage.input_other, failed))

        modules = {
            "astrbot.core.pipeline.scheduler": types.SimpleNamespace(
                PipelineScheduler=scheduler_cls
            ),
            "astrbot.core.pipeline.process_stage.method.star_request": star,
            "astrbot.core.star.filter.command": types.SimpleNamespace(
                CommandFilter=CommandFilter
            ),
        }
        context = types.SimpleNamespace(get_all_providers=lambda: [provider])
        tracker = Tracker(context, on_command, on_llm)
        with patch.object(
            instrumentation.importlib, "import_module", side_effect=modules.__getitem__
        ):
            self.assertTrue(tracker.install())
        try:

            async def gate(event):
                if event.name == "blocked":
                    event.stop_event()

            async def command(event):
                return await provider.text_chat()

            gate_md = metadata(gate, "gate", None)
            command_md = metadata(command, "actual_command", "查询", ["工具", "tools"])
            gate_md.handler_module_path = command_md.handler_module_path = "test"
            gate_md.handler_name = "gate"
            command_md.handler_name = "command"
            for user in ("blocked", "allowed"):
                await scheduler.execute(
                    RealEvent(user, [gate_md, command_md], {"actual_command": {}})
                )
            self.assertEqual(commands, [("allowed", "actual_command", "工具 查询")])
            self.assertEqual(calls, [("allowed", 12, False)])
        finally:
            tracker.uninstall()

    async def test_real_command_parser_greedy_annotation_requires_no_default(self):
        # Prevent the easy-to-miss Core behavior where a default argument makes
        # a GreedyStr parameter look like a normal string default to the parser.
        import re

        namespace = {
            "inspect": inspect,
            "re": re,
            "types": types,
            "typing": typing,
            "HandlerFilter": object,
        }
        self.source(
            "astrbot__core__star__filter__command.py",
            {"GreedyStr", "unwrap_optional", "CommandFilter"},
            namespace,
        )
        greedy = namespace["GreedyStr"]

        async def full(self, event, query):
            pass

        full.__annotations__["query"] = greedy
        filter_obj = namespace["CommandFilter"](
            "使用统计", handler_md=types.SimpleNamespace(handler=full)
        )
        self.assertEqual(
            filter_obj.validate_and_convert_params(
                ["指令", "2"], filter_obj.handler_params
            ),
            {"query": "指令 2"},
        )
        self.assertEqual(
            filter_obj.validate_and_convert_params([], filter_obj.handler_params),
            {"query": ""},
        )

        async def with_default(self, event, query=""):
            pass

        with_default.__annotations__["query"] = greedy
        filter_obj = namespace["CommandFilter"](
            "使用统计", handler_md=types.SimpleNamespace(handler=with_default)
        )
        self.assertEqual(
            filter_obj.validate_and_convert_params(
                ["指令", "2"], filter_obj.handler_params
            ),
            {"query": "指令"},
        )


if __name__ == "__main__":
    unittest.main()
