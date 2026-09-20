"""Harness 持久化生命周期、依赖校验和 Store 兼容桥。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.audit import service as audit_service
from app.modules.harness.context import ScopeContext
from app.modules.harness.events import EventBus
from app.modules.harness.models import (
    HarnessComponent,
    HarnessDependency,
    HarnessLifecycleAudit,
    HarnessRuntime,
    HarnessVersion,
)
from app.modules.harness.packages import Manifest, resolve_dependency_dag, version_satisfies
from app.modules.harness.services import Disposer, EffectStack


class LifecycleAdapter(Protocol):
    def install(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None: ...
    def enable(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None: ...
    def disable(self, manifest: Manifest, scope: ScopeContext) -> None: ...
    def uninstall(self, manifest: Manifest, scope: ScopeContext) -> None: ...
    def health(self, manifest: Manifest, scope: ScopeContext) -> tuple[str, str]: ...


class NoopAdapter:
    def install(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None:
        return None

    def enable(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None:
        return None

    def disable(self, manifest: Manifest, scope: ScopeContext) -> None:
        return None

    def uninstall(self, manifest: Manifest, scope: ScopeContext) -> None:
        return None

    def health(self, manifest: Manifest, scope: ScopeContext) -> tuple[str, str]:
        return "healthy", "manifest、依赖与运行状态正常"


events = EventBus()
_adapters: dict[str, LifecycleAdapter] = {}
_runtime_effects: dict[int, EffectStack] = {}
_install_effects: dict[int, EffectStack] = {}
_noop = NoopAdapter()


def register_adapter(kind: str, adapter: LifecycleAdapter) -> Disposer:
    if kind in _adapters:
        raise ValueError(f"生命周期适配器已注册: {kind}")
    _adapters[kind] = adapter
    disposed = False

    def dispose() -> None:
        nonlocal disposed
        if not disposed:
            disposed = True
            _adapters.pop(kind, None)

    return dispose


def adapter_for(kind: str) -> LifecycleAdapter | None:
    return _adapters.get(kind)


def replace_adapter(kind: str, adapter: LifecycleAdapter) -> Disposer:
    """覆盖已注册的适配器；只给启动期装配用，运行期请用 register_adapter。"""
    previous = _adapters.get(kind)
    _adapters[kind] = adapter
    disposed = False

    def dispose() -> None:
        nonlocal disposed
        if disposed:
            return
        disposed = True
        if previous is None:
            _adapters.pop(kind, None)
        else:
            _adapters[kind] = previous

    return dispose


def initialize_builtin_providers() -> None:
    """幂等注册内置种类；仅操作进程内注册表，不触碰 ORM。"""
    for kind in ("pipeline-plugin", "agent-skill", "agent-tool", "model-adapter", "template"):
        _adapters.setdefault(kind, _noop)
    from app.modules.harness import adapters

    adapters.register_all()


def _apply_health(runtime: HarnessRuntime, manifest: Manifest, context: ScopeContext) -> None:
    """启用成功后立刻写健康结果。不写的话列表会一直停在默认值 unknown。"""
    try:
        status, message = _adapters.get(manifest.kind, _noop).health(manifest, context)
    except Exception as exc:  # noqa: BLE001
        status, message = "unhealthy", str(exc)
    if status not in {"healthy", "degraded", "unhealthy", "unknown"}:
        status = "unknown"
    runtime.health_status = status
    runtime.health_message = message
    runtime.last_health_at = datetime.now()


def _manifest(version: HarnessVersion) -> Manifest:
    return Manifest.model_validate_json(version.manifest_json)


def _current_version(db: Session, component: HarnessComponent) -> HarnessVersion:
    row = db.get(HarnessVersion, component.current_version_id)
    if row is None:
        raise BizException.bad_request("组件当前版本不存在")
    return row


def _scope(
    component: HarnessComponent, action: str, actor_id: int | None = None, scope_key: str = "global"
) -> ScopeContext:
    return ScopeContext(
        name="lifecycle",
        values={
            "component_id": component.id,
            "component_key": f"{component.kind}:{component.name}",
            "action": action,
            "actor_id": actor_id,
            "scope_key": scope_key,
        },
    )


def _audit(
    db: Session,
    component: HarnessComponent,
    action: str,
    old_status: str,
    *,
    version_id: int | None = None,
    runtime_id: int | None = None,
    actor_id: int | None = None,
    actor_name: str = "",
    source: str = "api",
    success: bool = True,
    message: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    key = f"{component.kind}:{component.name}"
    event_payload = {
        "component_id": component.id,
        "component_key": key,
        "action": action,
        "from_status": old_status,
        "to_status": component.status,
        "success": success,
        "message": message,
        "detail": detail or {},
    }
    events.waterfall_sync(
        "harness.lifecycle",
        event_payload,
        _scope(component, action, actor_id),
    )
    events.waterfall_sync(
        f"harness.lifecycle.{action}",
        event_payload,
        _scope(component, action, actor_id),
    )
    db.add(
        HarnessLifecycleAudit(
            component_id=component.id,
            version_id=version_id,
            runtime_id=runtime_id,
            component_key=key,
            action=action,
            from_status=old_status,
            to_status=component.status,
            success=success,
            message=message,
            actor_id=actor_id,
            actor_name=actor_name,
            source=source,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
        )
    )
    audit_service.write(
        db,
        f"harness.{action}",
        "harness_component",
        component.id,
        f"{key} {old_status}->{component.status} {message}".strip(),
        user_id=actor_id,
        username=actor_name or None,
        source=source,
    )


def _active_manifests(
    db: Session, excluding_component_id: int | None = None
) -> list[Manifest]:
    stmt = (
        select(HarnessVersion)
        .join(HarnessComponent, HarnessComponent.current_version_id == HarnessVersion.id)
        .where(HarnessComponent.status != "uninstalled")
    )
    if excluding_component_id is not None:
        stmt = stmt.where(HarnessComponent.id != excluding_component_id)
    return [_manifest(row) for row in db.scalars(stmt).all()]


def _validate_graph(
    db: Session, manifest: Manifest, excluding_component_id: int | None = None
) -> dict[str, HarnessVersion]:
    try:
        resolve_dependency_dag([*_active_manifests(db, excluding_component_id), manifest])
    except ValueError as exc:
        raise BizException.bad_request(str(exc)) from exc
    resolved: dict[str, HarnessVersion] = {}
    for dependency in manifest.dependencies:
        kind, name = dependency.key.split(":", 1)
        version = db.scalar(
            select(HarnessVersion)
            .join(HarnessComponent, HarnessComponent.current_version_id == HarnessVersion.id)
            .where(
                HarnessComponent.kind == kind,
                HarnessComponent.name == name,
                HarnessComponent.status != "uninstalled",
            )
        )
        if version is None:
            if dependency.optional:
                continue
            raise BizException.bad_request(f"{manifest.key} 缺少依赖 {dependency.key}")
        if not version_satisfies(version.version, dependency.version):
            raise BizException.bad_request(
                f"{manifest.key} 需要 {dependency.key} {dependency.version}，实际为 {version.version}"
            )
        resolved[dependency.key] = version
    return resolved


def install(
    db: Session,
    manifest: Manifest | dict[str, Any],
    *,
    enable: bool = False,
    source: str = "api",
    source_ref: str = "",
    package_uri: str = "",
    package_sha256: str = "",
    actor_id: int | None = None,
    actor_name: str = "",
) -> HarnessComponent:
    parsed = manifest if isinstance(manifest, Manifest) else Manifest.model_validate(manifest)
    component = db.scalar(
        select(HarnessComponent).where(
            HarnessComponent.kind == parsed.kind, HarnessComponent.name == parsed.name
        )
    )
    resolved = _validate_graph(db, parsed, component.id if component else None)
    old_status = component.status if component else ""
    if component is None:
        component = HarnessComponent(kind=parsed.kind, name=parsed.name)
        db.add(component)
        db.flush()
    version = db.scalar(
        select(HarnessVersion).where(
            HarnessVersion.component_id == component.id,
            HarnessVersion.version == parsed.version,
        )
    )
    manifest_json = parsed.model_dump_json()
    if version is not None and version.manifest_json != manifest_json:
        raise BizException.bad_request("同版本 manifest 不可变；请提升版本号")
    if version is None:
        version = HarnessVersion(
            component_id=component.id,
            version=parsed.version,
            manifest_json=manifest_json,
            package_uri=package_uri,
            package_sha256=package_sha256,
            created_by=actor_id,
        )
        db.add(version)
        db.flush()
        for dependency in parsed.dependencies:
            db.add(
                HarnessDependency(
                    version_id=version.id,
                    dependency_key=dependency.key,
                    version_range=dependency.version,
                    optional=dependency.optional,
                    resolved_version_id=(
                        resolved[dependency.key].id if dependency.key in resolved else None
                    ),
                )
            )
    effects = EffectStack()
    context = _scope(component, "install", actor_id)
    payload = events.waterfall_sync("harness.lifecycle.before", {"action": "install"}, context)
    try:
        _adapters.get(parsed.kind, _noop).install(parsed, context, effects)
    except Exception as exc:
        rollback_errors = effects.rollback_sync()
        db.rollback()
        raise BizException.bad_request(
            f"安装失败: {exc}; 回滚: {'; '.join(map(str, rollback_errors))}"
        ) from exc
    component.display_name = parsed.display_name or parsed.name
    component.description = parsed.description
    component.source = source
    component.source_ref = source_ref
    component.status = "installed"
    component.enabled = False
    component.current_version_id = version.id
    component.installed_at = datetime.now()
    _audit(
        db, component, "install" if not old_status or old_status == "uninstalled" else "upgrade",
        old_status, version_id=version.id, actor_id=actor_id, actor_name=actor_name,
        source=source, detail=payload,
    )
    try:
        db.commit()
    except Exception:
        effects.rollback_sync()
        db.rollback()
        raise
    db.refresh(component)
    _install_effects[component.id] = effects
    events.waterfall_sync(
        "harness.lifecycle.after", {"action": "install", "success": True}, context
    )
    if enable:
        return set_enabled(
            db, component.id, True, actor_id=actor_id, actor_name=actor_name, source=source
        )
    return component


def set_enabled(
    db: Session,
    component_id: int,
    enabled: bool,
    *,
    scope_key: str = "global",
    actor_id: int | None = None,
    actor_name: str = "",
    source: str = "api",
) -> HarnessComponent:
    component = get_component(db, component_id)
    if component.status == "uninstalled":
        raise BizException.bad_request("组件已卸载，请先重新安装")
    runtime = db.scalar(
        select(HarnessRuntime).where(
            HarnessRuntime.component_id == component.id, HarnessRuntime.scope_key == scope_key
        )
    )
    if component.enabled == enabled and (
        runtime is None or (runtime.status == "running") == enabled
    ):
        return component
    version = _current_version(db, component)
    manifest = _manifest(version)
    old_status = component.status
    action = "enable" if enabled else "disable"
    context = _scope(component, action, actor_id, scope_key)
    if enabled:
        _validate_graph(db, manifest, component.id)
        if runtime is None:
            runtime = HarnessRuntime(
                component_id=component.id, version_id=version.id, scope_key=scope_key
            )
            db.add(runtime)
            db.flush()
        effects = EffectStack()
        try:
            _adapters.get(manifest.kind, _noop).enable(manifest, context, effects)
        except Exception as exc:
            rollback_errors = effects.rollback_sync()
            runtime.status = "error"
            runtime.health_status = "unhealthy"
            runtime.health_message = str(exc)
            component.status = "error"
            _audit(
                db, component, action, old_status, version_id=version.id, runtime_id=runtime.id,
                actor_id=actor_id, actor_name=actor_name, source=source, success=False,
                message=str(exc), detail={"rollback_errors": [str(item) for item in rollback_errors]},
            )
            db.commit()
            raise BizException.bad_request(f"启用失败: {exc}") from exc
        _runtime_effects[runtime.id] = effects
        runtime.version_id = version.id
        runtime.status = "running"
        runtime.started_at = datetime.now()
        runtime.stopped_at = None
        component.enabled = True
        component.enabled_at = datetime.now()
        component.status = "enabled"
        _apply_health(runtime, manifest, context)
    else:
        if runtime is not None:
            _adapters.get(manifest.kind, _noop).disable(manifest, context)
            errors = _runtime_effects.pop(runtime.id, EffectStack()).rollback_sync()
            runtime.status = "stopped" if not errors else "error"
            runtime.stopped_at = datetime.now()
            if errors:
                runtime.health_status = "unhealthy"
                runtime.health_message = "; ".join(map(str, errors))
        else:
            errors = []
        component.enabled = False
        component.enabled_at = None
        component.status = "installed" if not errors else "error"
    _audit(
        db, component, action, old_status, version_id=version.id,
        runtime_id=runtime.id if runtime else None, actor_id=actor_id, actor_name=actor_name,
        source=source, success=component.status != "error",
    )
    db.commit()
    db.refresh(component)
    return component


def uninstall(
    db: Session,
    component_id: int,
    *,
    actor_id: int | None = None,
    actor_name: str = "",
    source: str = "api",
) -> HarnessComponent:
    component = get_component(db, component_id)
    if component.status == "uninstalled":
        return component
    key = f"{component.kind}:{component.name}"
    blockers = [
        candidate.key
        for candidate in _active_manifests(db, component.id)
        if any(dep.key == key and not dep.optional for dep in candidate.dependencies)
    ]
    if blockers:
        raise BizException.bad_request("仍被以下组件依赖: " + "、".join(sorted(blockers)))
    if component.enabled:
        set_enabled(
            db, component.id, False, actor_id=actor_id, actor_name=actor_name, source=source
        )
        component = get_component(db, component.id)
    old_status = component.status
    version = _current_version(db, component)
    manifest = _manifest(version)
    _adapters.get(manifest.kind, _noop).uninstall(
        manifest, _scope(component, "uninstall", actor_id)
    )
    errors = _install_effects.pop(component.id, EffectStack()).rollback_sync()
    component.status = "uninstalled" if not errors else "error"
    component.enabled = False
    _audit(
        db, component, "uninstall", old_status, version_id=version.id,
        actor_id=actor_id, actor_name=actor_name, source=source,
        success=not errors, message="; ".join(map(str, errors)),
    )
    db.commit()
    db.refresh(component)
    return component


def reinstall(
    db: Session,
    component_id: int,
    *,
    actor_id: int | None = None,
    actor_name: str = "",
    source: str = "api",
) -> HarnessComponent:
    """把已卸载组件按当前版本重新装上并启用。

    卸载只改状态，zip / SKILL.md 仍留在版本记录里。这里不再走上传，
    也不新建版本号，避免同版本 manifest 不可变把用户卡死。
    停用（installed）不是卸载，应走 set_enabled。
    """
    component = get_component(db, component_id)
    if component.status != "uninstalled":
        raise BizException.bad_request("组件未卸载，停用的请直接启用")
    version = _current_version(db, component)
    manifest = _manifest(version)
    old_status = component.status
    effects = EffectStack()
    context = _scope(component, "install", actor_id)
    try:
        _adapters.get(manifest.kind, _noop).install(manifest, context, effects)
    except Exception as exc:
        rollback_errors = effects.rollback_sync()
        raise BizException.bad_request(
            f"安装失败: {exc}; 回滚: {'; '.join(map(str, rollback_errors))}"
        ) from exc
    component.status = "installed"
    component.enabled = False
    component.installed_at = datetime.now()
    _install_effects[component.id] = effects
    _audit(
        db,
        component,
        "install",
        old_status,
        version_id=version.id,
        actor_id=actor_id,
        actor_name=actor_name,
        source=source,
    )
    db.commit()
    db.refresh(component)
    return set_enabled(
        db, component.id, True, actor_id=actor_id, actor_name=actor_name, source=source
    )


def purge(
    db: Session,
    component_id: int,
    *,
    actor_id: int | None = None,
    actor_name: str = "",
    source: str = "api",
) -> None:
    """从仓库彻底删掉组件：先卸载，再删版本、运行实例和落盘 zip。"""
    from app.modules.harness import packaging

    component = get_component(db, component_id)
    if component.status != "uninstalled":
        uninstall(db, component_id, actor_id=actor_id, actor_name=actor_name, source=source)
        component = get_component(db, component_id)
    versions = list(
        db.scalars(select(HarnessVersion).where(HarnessVersion.component_id == component.id))
    )
    package_uris = [row.package_uri for row in versions if row.package_uri]
    for runtime in list(
        db.scalars(select(HarnessRuntime).where(HarnessRuntime.component_id == component.id))
    ):
        _runtime_effects.pop(runtime.id, None)
        db.delete(runtime)
    _install_effects.pop(component.id, None)
    old_status = component.status
    _audit(
        db,
        component,
        "purge",
        old_status,
        actor_id=actor_id,
        actor_name=actor_name,
        source=source,
    )
    component.current_version_id = None
    db.flush()
    for version in versions:
        db.delete(version)
    db.delete(component)
    db.commit()
    for uri in package_uris:
        packaging.unlink_saved_package(uri)


def check_health(
    db: Session,
    component_id: int,
    *,
    scope_key: str = "global",
    actor_id: int | None = None,
    actor_name: str = "",
    source: str = "api",
) -> HarnessComponent:
    component = get_component(db, component_id)
    version = _current_version(db, component)
    runtime = db.scalar(
        select(HarnessRuntime).where(
            HarnessRuntime.component_id == component.id, HarnessRuntime.scope_key == scope_key
        )
    )
    if runtime is None:
        runtime = HarnessRuntime(
            component_id=component.id,
            version_id=version.id,
            scope_key=scope_key,
            status="stopped",
        )
        db.add(runtime)
        db.flush()
    old_status = component.status
    try:
        status, message = _adapters.get(component.kind, _noop).health(
            _manifest(version), _scope(component, "health", actor_id, scope_key)
        )
        if status not in {"healthy", "degraded", "unhealthy", "unknown"}:
            raise ValueError(f"非法健康状态: {status}")
    except Exception as exc:  # noqa: BLE001
        status, message = "unhealthy", str(exc)
    runtime.health_status = status
    runtime.health_message = message
    runtime.last_health_at = datetime.now()
    _audit(
        db, component, "health", old_status, version_id=version.id, runtime_id=runtime.id,
        actor_id=actor_id, actor_name=actor_name, source=source,
        success=status != "unhealthy", message=message,
    )
    db.commit()
    db.refresh(component)
    return component


def get_component(db: Session, component_id: int) -> HarnessComponent:
    component = db.get(HarnessComponent, component_id)
    if component is None:
        raise BizException.not_found("Harness 组件")
    return component


def sync_store_plugin(
    db: Session,
    plugin: Any,
    *,
    installed: bool,
    actor_id: int | None = None,
    actor_name: str = "",
) -> HarnessComponent:
    """旧 Store Plugin API 的事务后镜像，不反向改变插件协议。"""
    component = db.scalar(
        select(HarnessComponent).where(
            HarnessComponent.kind == "pipeline-plugin", HarnessComponent.name == plugin.name.lower()
        )
    )
    manifest = plugin_manifest(plugin)
    if not installed:
        if component is None:
            component = install(
                db, manifest, source="store", source_ref=f"plugin:{plugin.id}",
                package_uri=plugin.package_path or "", package_sha256=plugin.package_sha or "",
                actor_id=actor_id, actor_name=actor_name,
            )
        return uninstall(
            db, component.id, actor_id=actor_id, actor_name=actor_name, source="store"
        )
    if component is not None and component.status != "uninstalled":
        current = _current_version(db, component)
        if current.version == manifest.version and _manifest(current) == manifest:
            if component.enabled != bool(plugin.enabled):
                return set_enabled(
                    db, component.id, bool(plugin.enabled), actor_id=actor_id,
                    actor_name=actor_name, source="store",
                )
            return component
    return install(
        db, manifest, enable=bool(plugin.enabled), source="store",
        source_ref=f"plugin:{plugin.id}", package_uri=plugin.package_path or "",
        package_sha256=plugin.package_sha or "", actor_id=actor_id, actor_name=actor_name,
    )


def plugin_manifest(plugin: Any) -> Manifest:
    try:
        schema = json.loads(plugin.config_schema or "{}")
    except (TypeError, json.JSONDecodeError):
        schema = {}
    return Manifest(
        kind="pipeline-plugin",
        name=plugin.name.lower(),
        version=plugin.version or "1.0.0",
        display_name=plugin.display_name or plugin.name,
        description=plugin.description or "",
        entrypoint=plugin.entrypoint or "",
        config_schema=schema if isinstance(schema, dict) else {},
        metadata={
            "category": plugin.category or "exec",
            "language": plugin.language or "python",
            "package_sha": plugin.package_sha or "",
        },
    )
