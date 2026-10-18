"""Reversible runtime observation of AstrBot's actual command/provider calls.

The public response hook runs only after the main agent finishes and misses direct
plugin requests. These small wrappers observe the execution boundaries instead.
They never change event propagation, return values, exceptions, or provider data.
"""

from __future__ import annotations

import copy
import importlib
import inspect
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from types import SimpleNamespace
from typing import Any, Callable


@dataclass
class _Scope:
    event: Any
    active: bool = True


@dataclass
class _Patch:
    target: Any
    name: str
    original: Any
    wrapper: Any
    owned: bool


class Tracker:
    """Observe supported AstrBot execution paths without modifying Core files.

    Callbacks are async ``on_command(event, key, label)`` and
    ``on_llm(event, provider, response_or_none, failed)``. Callback failures are
    reported through the optional synchronous ``on_error(message)`` callback.
    ``install`` returns False when the required Core interfaces are unavailable.
    Background jobs with no live originating message cannot be attributed.
    """

    def __init__(
        self,
        context: Any,
        on_command: Callable,
        on_llm: Callable,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.context = context
        self.on_command = on_command
        self.on_llm = on_llm
        self.on_error = on_error
        self.active = False
        self._scope: ContextVar[_Scope | None] = ContextVar(
            f"usage_stats_scope_{id(self)}", default=None
        )
        self._depth: ContextVar[frozenset[int]] = ContextVar(
            f"usage_stats_provider_depth_{id(self)}", default=frozenset()
        )
        self._patches: list[_Patch] = []
        self._provider_classes: set[type] = set()
        self._errors: list[str] = []
        self._command_type: type | None = None
        self._commands_installed = False

    def _warn(self, message: str) -> None:
        # A bounded, de-duplicated status list also avoids per-message log spam.
        if message in self._errors:
            return
        if len(self._errors) >= 20:
            return
        self._errors.append(message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                pass

    def _patch(self, target: Any, name: str, wrapper: Any) -> None:
        original = getattr(target, name)
        owned = name in vars(target)
        setattr(target, name, wrapper)
        self._patches.append(_Patch(target, name, original, wrapper, owned))

    def install(self) -> bool:
        if self.active:
            return True
        try:
            scheduler = importlib.import_module("astrbot.core.pipeline.scheduler")
            star_request = importlib.import_module(
                "astrbot.core.pipeline.process_stage.method.star_request"
            )
            command = importlib.import_module("astrbot.core.star.filter.command")
            scheduler_cls = scheduler.PipelineScheduler
            if not inspect.iscoroutinefunction(scheduler_cls.execute):
                raise TypeError("PipelineScheduler.execute 不是异步协程")
            if not inspect.isasyncgenfunction(star_request.call_handler):
                raise TypeError("StarRequest.call_handler 不是异步生成器")
            if not callable(getattr(self.context, "get_all_providers", None)):
                raise TypeError("Context.get_all_providers 不可用")
            self._command_type = command.CommandFilter
            self.active = True
            self._patch(
                scheduler_cls, "execute", self._wrap_execute(scheduler_cls.execute)
            )
            self._patch(
                star_request,
                "call_handler",
                self._wrap_handler(star_request.call_handler),
            )
            self._commands_installed = True
            self.refresh()
            return True
        except Exception as exc:
            self.uninstall()
            self._warn(f"使用统计接入失败，未启用统计：{type(exc).__name__}: {exc}")
            return False

    def uninstall(self) -> None:
        self.active = False
        for patch in reversed(self._patches):
            # A plugin installed later may have retained our wrapper. In that
            # case leave its chain intact; this owner's wrapper is now inert.
            if getattr(patch.target, patch.name, None) is not patch.wrapper:
                continue
            if patch.owned:
                setattr(patch.target, patch.name, patch.original)
            else:
                delattr(patch.target, patch.name)
        self._patches.clear()
        self._provider_classes.clear()
        self._commands_installed = False

    def status(self) -> dict[str, Any]:
        return {
            "installed": self.active,
            "command_tracking": self.active and self._commands_installed,
            "provider_classes": len(self._provider_classes),
            "errors": list(self._errors),
        }

    def refresh(self) -> int:
        """Discover configured provider classes, including providers loaded later."""
        if not self.active:
            return 0
        try:
            providers = self.context.get_all_providers()
            if inspect.isawaitable(providers):
                if inspect.iscoroutine(providers):
                    providers.close()
                raise TypeError("get_all_providers 变为异步接口")
            for provider in providers:
                cls = type(provider)
                if cls in self._provider_classes:
                    continue
                patched = False
                for name, stream in (("text_chat", False), ("text_chat_stream", True)):
                    original = getattr(cls, name, None)
                    if not callable(original):
                        self._warn(f"模型 {cls.__name__}.{name} 不可用，该路径无法统计")
                        continue
                    if name in vars(provider):
                        self._warn(
                            f"模型 {cls.__name__}.{name} 被实例覆盖，该实例路径无法统计"
                        )
                    wrapper = (
                        self._wrap_stream(original)
                        if stream
                        else self._wrap_chat(original)
                    )
                    self._patch(cls, name, wrapper)
                    patched = True
                if patched:
                    self._provider_classes.add(cls)
        except Exception as exc:
            self._warn(f"模型统计接入不完整：{type(exc).__name__}: {exc}")
        return len(self._provider_classes)

    def _live_scope(self) -> _Scope | None:
        scope = self._scope.get()
        return scope if self.active and scope and scope.active else None

    async def _emit(self, callback: Callable, scope: _Scope, *args: Any) -> None:
        if not self.active or not scope.active:
            return
        try:
            await callback(scope.event, *args)
        except Exception as exc:
            self._warn(f"统计写入失败：{type(exc).__name__}: {exc}")

    def _wrap_execute(self, original: Callable) -> Callable:
        @wraps(original)
        async def execute(scheduler: Any, event: Any, *args: Any, **kwargs: Any):
            if not self.active:
                return await original(scheduler, event, *args, **kwargs)
            self.refresh()
            scope = _Scope(event)
            token = self._scope.set(scope)
            try:
                return await original(scheduler, event, *args, **kwargs)
            finally:
                # Copied ContextVars in child tasks share this mutable flag.
                scope.active = False
                self._scope.reset(token)

        return execute

    def _command(self, event: Any, handler: Any) -> tuple[str, str] | None:
        if self._command_type is None:
            return None
        activated = event.get_extra("activated_handlers") or []
        parsed = event.get_extra("handlers_parsed_params") or {}
        for metadata in activated:
            registered = getattr(metadata, "handler", None)
            if registered is not handler and registered != handler:
                continue
            full_name = getattr(metadata, "handler_full_name", "")
            # The waking stage writes this only after command argument parsing.
            if not full_name or full_name not in parsed:
                continue
            for command_filter in getattr(metadata, "event_filters", ()):
                if not isinstance(command_filter, self._command_type):
                    continue
                parents = getattr(command_filter, "parent_command_names", None) or [""]
                name = str(getattr(command_filter, "command_name", "")).strip()
                if not name:
                    continue
                label = " ".join(
                    part for part in (str(parents[0]).strip(), name) if part
                )
                # Handler identity separates identically named commands from
                # different plugins. Registered aliases share this same key.
                return str(full_name), label
        return None

    def _wrap_handler(self, original: Callable) -> Callable:
        @wraps(original)
        async def call_handler(event: Any, handler: Any, *args: Any, **kwargs: Any):
            scope = self._live_scope()
            owns_scope = scope is None or scope.event is not event
            if owns_scope:
                scope = _Scope(event, active=self.active)
            assert scope is not None
            iterator = original(event, handler, *args, **kwargs)
            try:
                command = None
                try:
                    command = self._command(event, handler) if self.active else None
                except Exception as exc:
                    self._warn(
                        f"指令元数据无法识别，该次不计指令：{type(exc).__name__}: {exc}"
                    )
                if command:
                    await self._emit(self.on_command, scope, *command)
                while True:
                    token = self._scope.set(scope)
                    try:
                        response = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        self._scope.reset(token)
                    # Never leave a token installed across a generator yield.
                    yield response
            finally:
                token = self._scope.set(scope)
                try:
                    await iterator.aclose()
                finally:
                    self._scope.reset(token)
                    if owns_scope:
                        scope.active = False

        return call_handler

    def _wrap_chat(self, original: Callable) -> Callable:
        @wraps(original)
        async def text_chat(provider: Any, *args: Any, **kwargs: Any):
            scope = self._live_scope()
            depth = self._depth.get()
            if scope is None or id(provider) in depth:
                return await original(provider, *args, **kwargs)
            token = self._depth.set(depth | {id(provider)})
            response = None
            failed = True
            try:
                response = await original(provider, *args, **kwargs)
                failed = False
                return response
            finally:
                self._depth.reset(token)
                await self._emit(self.on_llm, scope, provider, response, failed)

        return text_chat

    def _snapshot(self, response: Any) -> Any:
        """Freeze only the small usage object; never copy prompt/response bodies."""
        try:
            result = copy.copy(response)
            if getattr(response, "usage", None) is not None:
                result.usage = copy.copy(response.usage)
            return result
        except Exception as exc:
            self._warn(
                f"模型用量对象无法读取，该次用量记为未知：{type(exc).__name__}: {exc}"
            )
            return SimpleNamespace(usage=None, raw_completion=None, is_chunk=False)

    def _wrap_stream(self, original: Callable) -> Callable:
        @wraps(original)
        async def text_chat_stream(provider: Any, *args: Any, **kwargs: Any):
            scope = self._live_scope()
            depth = self._depth.get()
            observe = scope is not None and id(provider) not in depth
            iterator = original(provider, *args, **kwargs)
            final_response = None
            received_chunk = False
            failed = True
            try:
                while True:
                    token = self._depth.set(depth | {id(provider)})
                    try:
                        response = await iterator.__anext__()
                    except StopAsyncIteration:
                        failed = False
                        break
                    finally:
                        self._depth.reset(token)
                    if observe:
                        try:
                            if not getattr(response, "is_chunk", False):
                                final_response = self._snapshot(response)
                            else:
                                received_chunk = True
                        except Exception as exc:
                            self._warn(
                                "模型响应类型无法读取，该次用量记为未知："
                                f"{type(exc).__name__}: {exc}"
                            )
                    yield response
            finally:
                token = self._depth.set(depth | {id(provider)})
                try:
                    await iterator.aclose()
                finally:
                    self._depth.reset(token)
                    if observe:
                        response = final_response
                        if response is None and received_chunk:
                            # A chunk can contain only a delta. Without a final
                            # response, no complete per-call usage is known.
                            response = SimpleNamespace(
                                usage=None, raw_completion=None, is_chunk=True
                            )
                        await self._emit(self.on_llm, scope, provider, response, failed)

        return text_chat_stream
