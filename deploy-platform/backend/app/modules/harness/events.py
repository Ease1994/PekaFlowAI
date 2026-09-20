"""同步/异步 waterfall 事件总线。"""
from __future__ import annotations

import inspect
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypeVar

from app.modules.harness.context import ScopeContext, current_scope

T = TypeVar("T")
EventHandler = Callable[[Any, ScopeContext], Any]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[int, str, EventHandler]]] = defaultdict(list)

    def on(self, event: str, handler: EventHandler, *, priority: int = 0) -> Callable[[], None]:
        registration_id = uuid.uuid4().hex
        self._handlers[event].append((priority, registration_id, handler))
        self._handlers[event].sort(key=lambda item: (-item[0], item[1]))
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if not disposed:
                disposed = True
                self._handlers[event][:] = [
                    item for item in self._handlers[event] if item[1] != registration_id
                ]

        return dispose

    def waterfall_sync(
        self, event: str, value: T, scope: ScopeContext | None = None
    ) -> T:
        context = scope or current_scope()
        current: Any = value
        for _, _, handler in tuple(self._handlers.get(event, ())):
            result = handler(current, context)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise RuntimeError(f"事件 {event} 包含异步 handler，请使用 waterfall")
            if result is not None:
                current = result
        return current

    async def waterfall(
        self, event: str, value: T, scope: ScopeContext | None = None
    ) -> T:
        context = scope or current_scope()
        current: Any = value
        for _, _, handler in tuple(self._handlers.get(event, ())):
            result = handler(current, context)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                current = result
        return current
