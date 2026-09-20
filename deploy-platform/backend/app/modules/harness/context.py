"""Harness 显式作用域与 allow/deny 授权策略。"""
from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

Policy = Callable[["AccessRequest"], bool | str | Awaitable[bool | str]]


@dataclass
class ScopeContext:
    """可继承的显式作用域；每个作用域有独立服务实例缓存。"""

    name: str = "request"
    values: dict[str, Any] = field(default_factory=dict)
    parent: ScopeContext | None = None
    scope_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _instances: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.values:
            return self.values[key]
        return self.parent.get(key, default) if self.parent else default

    def child(self, name: str, **values: Any) -> ScopeContext:
        return ScopeContext(name=name, values=values, parent=self)


_current_scope: ContextVar[ScopeContext | None] = ContextVar("harness_scope", default=None)


@contextmanager
def bind_scope(scope: ScopeContext):
    token = _current_scope.set(scope)
    try:
        yield scope
    finally:
        _current_scope.reset(token)


def current_scope() -> ScopeContext:
    return _current_scope.get() or ScopeContext(name="root")


@dataclass(frozen=True)
class AccessRequest:
    action: str
    resource: str
    scope: ScopeContext
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str = ""


class PolicyRegistry:
    """deny 一票否决；存在 allow 时必须至少命中一条。"""

    def __init__(self) -> None:
        self._allow: list[tuple[int, str, Policy]] = []
        self._deny: list[tuple[int, str, Policy]] = []

    def allow(self, policy: Policy, *, priority: int = 0) -> Callable[[], None]:
        return self._register(self._allow, policy, priority)

    def deny(self, policy: Policy, *, priority: int = 0) -> Callable[[], None]:
        return self._register(self._deny, policy, priority)

    async def authorize(self, request: AccessRequest) -> AccessDecision:
        for _, _, policy in tuple(self._deny):
            result = policy(request)
            result = await result if inspect.isawaitable(result) else result
            if result:
                return AccessDecision(False, result if isinstance(result, str) else "命中 deny 策略")
        if not self._allow:
            return AccessDecision(True)
        for _, _, policy in tuple(self._allow):
            result = policy(request)
            result = await result if inspect.isawaitable(result) else result
            if result:
                return AccessDecision(True, result if isinstance(result, str) else "")
        return AccessDecision(False, "未命中 allow 策略")

    @staticmethod
    def _register(
        registry: list[tuple[int, str, Policy]], policy: Policy, priority: int
    ) -> Callable[[], None]:
        registration_id = uuid.uuid4().hex
        registry.append((priority, registration_id, policy))
        registry.sort(key=lambda item: (-item[0], item[1]))
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if not disposed:
                disposed = True
                registry[:] = [item for item in registry if item[1] != registration_id]

        return dispose
