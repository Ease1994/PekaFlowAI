"""稳定服务 key、Provider/Consumer seam 与可逆 effect 栈。"""
from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from app.modules.harness.context import ScopeContext, current_scope

T = TypeVar("T")
Disposer = Callable[[], Any]
Factory = Callable[[ScopeContext], Any]
_KEY_PART = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True, order=True)
class ServiceKey(Generic[T]):
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not _KEY_PART.fullmatch(self.namespace) or not _KEY_PART.fullmatch(self.name):
            raise ValueError("服务键只能使用小写字母、数字、点、下划线和连字符")

    @classmethod
    def parse(cls, value: str) -> ServiceKey[Any]:
        namespace, separator, name = value.partition(":")
        if not separator:
            raise ValueError("服务键格式必须为 namespace:name")
        return cls(namespace, name)

    def __str__(self) -> str:
        return f"{self.namespace}:{self.name}"


@dataclass(frozen=True)
class ServiceDefinition(Generic[T]):
    key: ServiceKey[T]
    contract: type[T] | None = None
    description: str = ""


@dataclass(frozen=True)
class ServiceProvider(Generic[T]):
    definition: ServiceDefinition[T]
    factory: Factory
    lifecycle: Literal["singleton", "scoped", "transient"] = "singleton"
    provider_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class ServiceConsumer(Generic[T]):
    key: ServiceKey[T]
    required: bool = True


class EffectStack:
    """注册副作用的 disposer，关闭时严格后进先出。"""

    def __init__(self) -> None:
        self._disposers: list[Disposer] = []

    def add(self, disposer: Disposer) -> Disposer:
        self._disposers.append(disposer)
        return disposer

    async def rollback(self) -> list[Exception]:
        errors: list[Exception] = []
        while self._disposers:
            try:
                result = self._disposers.pop()()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        return errors

    def rollback_sync(self) -> list[Exception]:
        errors: list[Exception] = []
        while self._disposers:
            try:
                result = self._disposers.pop()()
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    raise RuntimeError("同步生命周期不能回滚异步 disposer")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        return errors


class ServiceRegistry:
    def __init__(self) -> None:
        self._definitions: dict[ServiceKey[Any], ServiceDefinition[Any]] = {}
        self._providers: dict[ServiceKey[Any], ServiceProvider[Any]] = {}
        self._consumers: dict[str, ServiceConsumer[Any]] = {}
        self._singletons: dict[str, Any] = {}

    def register_definition(self, definition: ServiceDefinition[Any]) -> Disposer:
        if definition.key in self._definitions:
            raise ValueError(f"服务定义已注册: {definition.key}")
        self._definitions[definition.key] = definition
        return _once(lambda: self._definitions.pop(definition.key, None))

    def register_provider(self, provider: ServiceProvider[Any]) -> Disposer:
        key = provider.definition.key
        if key not in self._definitions:
            raise ValueError(f"服务定义尚未注册: {key}")
        if key in self._providers:
            raise ValueError(f"服务 Provider 已注册: {key}")
        self._providers[key] = provider

        def dispose() -> None:
            self._providers.pop(key, None)
            instance = self._singletons.pop(provider.provider_id, None)
            close = getattr(instance, "close", None)
            if callable(close):
                close()

        return _once(dispose)

    def register_consumer(self, consumer: ServiceConsumer[Any]) -> Disposer:
        registration_id = uuid.uuid4().hex
        self._consumers[registration_id] = consumer
        return _once(lambda: self._consumers.pop(registration_id, None))

    def consume(
        self,
        consumer: ServiceConsumer[T] | ServiceKey[T],
        scope: ScopeContext | None = None,
    ) -> T | None:
        spec = consumer if isinstance(consumer, ServiceConsumer) else ServiceConsumer(consumer)
        provider = self._providers.get(spec.key)
        if provider is None:
            if spec.required:
                raise LookupError(f"没有可用 Provider: {spec.key}")
            return None
        context = scope or current_scope()
        cache = (
            self._singletons
            if provider.lifecycle == "singleton"
            else context._instances
            if provider.lifecycle == "scoped"
            else None
        )
        if cache is not None and provider.provider_id in cache:
            return cache[provider.provider_id]
        instance = provider.factory(context)
        contract = provider.definition.contract
        if contract is not None and not isinstance(instance, contract):
            raise TypeError(f"{spec.key} Provider 返回值不满足 {contract.__name__}")
        if cache is not None:
            cache[provider.provider_id] = instance
        return instance


def _once(callback: Disposer) -> Disposer:
    disposed = False

    def dispose() -> Any:
        nonlocal disposed
        if disposed:
            return None
        disposed = True
        return callback()

    return dispose
