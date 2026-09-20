from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.harness import (
    AccessRequest,
    EffectStack,
    HarnessKernel,
    Manifest,
    ScopeContext,
    ServiceConsumer,
    ServiceDefinition,
    ServiceKey,
    ServiceProvider,
    resolve_dependency_dag,
)
from app.modules.harness import lifecycle
from app.modules.harness.models import (
    HarnessComponent,
    HarnessDependency,
    HarnessLifecycleAudit,
    HarnessRuntime,
    HarnessVersion,
)
from app.modules.harness.packages import version_satisfies
from app.modules.store.models import Plugin
from app.modules.store.plugin_service import _sync_harness


def test_service_seam_scope_waterfall_and_policy() -> None:
    runtime = HarnessKernel()
    key = ServiceKey[dict]("test", "counter")
    definition = ServiceDefinition(key, dict)
    dispose_definition = runtime.register_definition(definition)
    dispose_provider = runtime.register_provider(
        ServiceProvider(definition, lambda scope: {"scope": scope.scope_id}, lifecycle="scoped")
    )
    consumer = ServiceConsumer(key)
    scope = ScopeContext()
    assert runtime.consume(consumer, scope) is runtime.consume(consumer, scope)
    assert runtime.consume(consumer, scope.child("child")) is not runtime.consume(consumer, scope)

    runtime.on("build", lambda value, _: value + ["low"], priority=0)
    runtime.on("build", lambda value, _: value + ["high"], priority=10)
    assert runtime.waterfall_sync("build", [], scope) == ["high", "low"]
    assert asyncio.run(runtime.waterfall("build", [], scope)) == ["high", "low"]

    runtime.allow(lambda request: request.attributes.get("team") == "platform")
    runtime.deny(lambda request: request.resource == "secret")
    allowed = asyncio.run(
        runtime.authorize(AccessRequest("read", "public", scope, {"team": "platform"}))
    )
    denied = asyncio.run(
        runtime.authorize(AccessRequest("read", "secret", scope, {"team": "platform"}))
    )
    assert allowed.allowed is True
    assert denied.allowed is False
    dispose_provider()
    dispose_definition()


def test_effects_rollback_in_reverse_order() -> None:
    calls: list[int] = []
    effects = EffectStack()
    effects.add(lambda: calls.append(1))
    effects.add(lambda: calls.append(2))
    assert effects.rollback_sync() == []
    assert calls == [2, 1]


def test_manifest_semver_and_dependency_dag() -> None:
    base = Manifest(kind="agent-tool", name="shell", version="1.4.0")
    consumer = Manifest(
        kind="agent-skill",
        name="release",
        version="2.0.0",
        dependencies=[{"key": base.key, "version": "^1.2.0"}],
    )
    assert version_satisfies("1.4.0", "^1.2.0")
    assert [item.key for item in resolve_dependency_dag([consumer, base])] == [
        base.key,
        consumer.key,
    ]
    with pytest.raises(ValueError, match="缺少依赖"):
        resolve_dependency_dag([consumer])
    left = Manifest(
        kind="agent-tool",
        name="left",
        version="1.0.0",
        dependencies=[{"key": "agent-tool:right"}],
    )
    right = Manifest(
        kind="agent-tool",
        name="right",
        version="1.0.0",
        dependencies=[{"key": "agent-tool:left"}],
    )
    with pytest.raises(ValueError, match="循环依赖"):
        resolve_dependency_dag([left, right])


def test_lifecycle_persists_status_and_events() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        HarnessComponent.__table__,
        HarnessVersion.__table__,
        HarnessDependency.__table__,
        HarnessRuntime.__table__,
        HarnessLifecycleAudit.__table__,
        AuditLog.__table__,
    ):
        table.create(engine)
    with Session(engine) as db:
        row = lifecycle.install(
            db,
            Manifest(kind="template", name="basic", version="1.0.0"),
            enable=True,
            actor_id=1,
            actor_name="admin",
        )
        assert row.status == "enabled"
        runtime = db.scalar(select(HarnessRuntime).where(HarnessRuntime.component_id == row.id))
        assert runtime.health_status == "healthy"
        lifecycle.check_health(db, row.id)
        runtime = db.scalar(select(HarnessRuntime).where(HarnessRuntime.component_id == row.id))
        assert runtime.health_status == "healthy"
        assert lifecycle.uninstall(db, row.id).status == "uninstalled"
        actions = list(
            db.scalars(
                select(HarnessLifecycleAudit).where(
                    HarnessLifecycleAudit.component_id == row.id
                )
            )
        )
        actions = [event.action for event in actions]
        assert {"install", "enable", "health", "disable", "uninstall"} <= set(actions)
        restored = lifecycle.reinstall(db, row.id, actor_id=1, actor_name="admin")
        assert restored.status == "enabled"
        assert restored.enabled is True
        with pytest.raises(Exception, match="未卸载"):
            lifecycle.reinstall(db, row.id)
        lifecycle.uninstall(db, row.id)
        with pytest.raises(Exception, match="请先重新安装"):
            lifecycle.set_enabled(db, row.id, True)
        cid = row.id
        lifecycle.purge(db, row.id, actor_id=1, actor_name="admin")
        assert db.get(HarnessComponent, cid) is None


def test_enable_failure_rolls_back_effects_in_reverse_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        HarnessComponent.__table__,
        HarnessVersion.__table__,
        HarnessDependency.__table__,
        HarnessRuntime.__table__,
        HarnessLifecycleAudit.__table__,
        AuditLog.__table__,
    ):
        table.create(engine)
    calls: list[str] = []

    class BrokenAdapter(lifecycle.NoopAdapter):
        def enable(self, manifest, scope, effects):
            effects.add(lambda: calls.append("first"))
            effects.add(lambda: calls.append("second"))
            raise RuntimeError("boom")

    dispose = lifecycle.replace_adapter("template", BrokenAdapter())
    try:
        with Session(engine) as db:
            row = lifecycle.install(db, Manifest(kind="template", name="broken", version="1.0.0"))
            with pytest.raises(Exception, match="启用失败"):
                lifecycle.set_enabled(db, row.id, True)
            assert calls == ["second", "first"]
    finally:
        dispose()


def test_store_plugin_protocol_is_mirrored_to_harness() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        Plugin.__table__,
        HarnessComponent.__table__,
        HarnessVersion.__table__,
        HarnessDependency.__table__,
        HarnessRuntime.__table__,
        HarnessLifecycleAudit.__table__,
        AuditLog.__table__,
    ):
        table.create(engine)
    with Session(engine) as db:
        plugin = Plugin(
            name="legacy-task",
            display_name="Legacy",
            version="1.2.3",
            config_schema="{}",
            installed=True,
            enabled=True,
            status="installed",
        )
        db.add(plugin)
        db.commit()
        db.refresh(plugin)
        _sync_harness(db, plugin, installed=True)
        component = db.scalar(
            select(HarnessComponent).where(HarnessComponent.name == "legacy-task")
        )
        assert component.kind == "pipeline-plugin"
        assert component.enabled is True
        assert plugin.name == "legacy-task"
