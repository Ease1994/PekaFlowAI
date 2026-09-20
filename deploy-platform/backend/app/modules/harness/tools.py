"""统一 Agent Tool 目录：内置工具 + 第三方隔离工具。

内置工具是平台自己的 Python 代码，跑在 API 进程里，可以热启停但删不掉。
第三方工具是签名安装的包，只能在隔离 Runner 中执行；Runner 不可用时拒绝调用。
两类工具对模型是同一份目录，对平台是完全不同的信任级别。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.harness import runner
from app.modules.harness.models import (
    HarnessComponent,
    HarnessToolInvocation,
    HarnessVersion,
)
from app.modules.harness.packages import Manifest

BUILTIN_ISOLATION = "in-process"
PACKAGE_ISOLATION = "container"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    source: str
    isolation: str
    display_name: str = ""
    category: str = "general"
    risk: str = "read"
    confirm: bool = False
    llm_visible: bool = True
    component_id: int | None = None
    version: str = ""
    capabilities: tuple[str, ...] = ()
    signature_key_id: str = ""
    package_path: str = ""
    package_sha256: str = ""
    entrypoint: tuple[str, ...] = ()
    method: str = ""
    examples: tuple[str, ...] = field(default=())

    def public(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "category": self.category,
            "risk": self.risk,
            "confirm": self.confirm,
            "parameters": self.parameters,
            "examples": list(self.examples),
            "source": self.source,
            "isolation": self.isolation,
            "component_id": self.component_id,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "signature_key_id": self.signature_key_id,
        }


def _builtin_specs() -> list[ToolSpec]:
    from app.modules.ai.registry import all_skills
    from app.modules.ai.skills import load_all

    load_all()
    return [
        ToolSpec(
            name=item.name,
            display_name=item.name,
            description=item.description,
            parameters=item.parameters or {"type": "object", "properties": {}},
            source="builtin",
            isolation=BUILTIN_ISOLATION,
            category=item.category,
            risk=item.risk,
            confirm=item.confirm,
            llm_visible=item.llm_visible,
            examples=tuple(item.examples or ()),
        )
        for item in all_skills()
    ]


def _package_specs(db: Session) -> list[ToolSpec]:
    rows = db.execute(
        select(HarnessComponent, HarnessVersion)
        .join(HarnessVersion, HarnessVersion.id == HarnessComponent.current_version_id)
        .where(
            HarnessComponent.kind == "agent-tool",
            HarnessComponent.enabled.is_(True),
            HarnessComponent.status == "enabled",
        )
        .order_by(HarnessComponent.name)
    ).all()
    specs: list[ToolSpec] = []
    for component, version in rows:
        manifest = Manifest.model_validate_json(version.manifest_json)
        metadata = manifest.metadata or {}
        entrypoint = metadata.get("entrypoint_argv") or []
        specs.append(
            ToolSpec(
                name=manifest.name,
                display_name=manifest.display_name or manifest.name,
                description=manifest.description,
                parameters=manifest.config_schema or {"type": "object", "properties": {}},
                source="package",
                isolation=PACKAGE_ISOLATION,
                category=str(metadata.get("category") or "general"),
                risk=str(metadata.get("risk") or "read"),
                confirm=bool(metadata.get("confirm", True)),
                component_id=component.id,
                version=version.version,
                capabilities=tuple(manifest.capabilities),
                signature_key_id=str(metadata.get("signature_key_id") or ""),
                package_path=version.package_uri,
                package_sha256=version.package_sha256,
                entrypoint=tuple(str(part) for part in entrypoint),
                method=str(metadata.get("method") or manifest.name),
            )
        )
    return specs


def catalog(db: Session) -> list[ToolSpec]:
    specs = {item.name: item for item in _builtin_specs()}
    for item in _package_specs(db):
        # 第三方工具不能顶掉同名内置工具，否则改个名字就能劫持发布动作
        specs.setdefault(item.name, item)
    return sorted(specs.values(), key=lambda item: (item.source != "builtin", item.name))


def resolve(db: Session, name: str) -> ToolSpec | None:
    return next((item for item in catalog(db) if item.name == name), None)


_TOOL_DESC_MAX = 140
_PARAM_DESC_MAX = 80


def _clip_text(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def _clip_schema(obj: Any, *, limit: int = _PARAM_DESC_MAX) -> Any:
    if isinstance(obj, dict):
        return {
            key: _clip_text(value, limit) if key == "description" and isinstance(value, str)
            else _clip_schema(value, limit=limit)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_clip_schema(item, limit=limit) for item in obj]
    return obj


def openai_tools(
    db: Session,
    *,
    categories: frozenset[str] | set[str] | None = None,
    names: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    specs = catalog(db)
    if names is not None:
        specs = [item for item in specs if item.name in names]
    elif categories:
        specs = [
            item
            for item in specs
            if item.source != "builtin" or item.category in categories or item.name == "skill"
        ]
    specs = sorted(
        specs,
        key=lambda item: (item.name != "skill", item.source != "builtin", item.name),
    )
    return [
        {
            "type": "function",
            "function": {
                "name": item.name,
                "description": _clip_text(item.description, _TOOL_DESC_MAX),
                "parameters": _clip_schema(item.parameters or {"type": "object", "properties": {}}),
            },
        }
        for item in specs
        if item.llm_visible
    ]


def dispatch(
    db: Session,
    name: str,
    params: dict,
    current,
    *,
    confirmed: bool = False,
    call_id: str = "",
    session_id: int | None = None,
) -> dict:
    """执行一次工具调用，并记录观测。"""
    spec = resolve(db, name)
    started = time.perf_counter()
    identifier = call_id or uuid.uuid4().hex
    if spec is None:
        result = {"error": f"未知工具 {name}", "code": "TOOL_NOT_FOUND"}
        _record(db, name, None, "unknown", "unknown", identifier, session_id, current, result, started)
        return result
    try:
        if spec.source == "builtin":
            from app.modules.ai.registry import dispatch as builtin_dispatch

            result = builtin_dispatch(db, name, params or {}, current, confirmed=confirmed)
        else:
            result = _dispatch_package(db, spec, params or {}, current, identifier)
    except Exception as exc:  # noqa: BLE001 - 统一转成工具结果，保证轨迹完整
        result = {"error": str(exc), "code": "TOOL_EXCEPTION"}
    _record(db, name, spec.component_id, spec.source, spec.isolation, identifier, session_id, current, result, started)
    return result


def _dispatch_package(
    db: Session, spec: ToolSpec, params: dict, current, call_id: str
) -> dict:
    if not spec.entrypoint or not spec.package_path or not spec.package_sha256:
        return {"error": f"工具 {spec.name} 的包信息不完整", "code": "TOOL_PACKAGE_INVALID"}
    try:
        return runner.execute(
            db,
            runner.ToolInvocation(
                call_id=call_id,
                package_path=spec.package_path,
                package_sha256=spec.package_sha256,
                entrypoint=list(spec.entrypoint),
                method=spec.method or spec.name,
                arguments=params,
                capabilities=list(spec.capabilities),
                user_id=int(getattr(current, "id", 0) or 0),
            ),
        )
    except runner.RunnerUnavailable as exc:
        # fail-closed：隔离没到位就不执行，不退化成本进程直接跑第三方代码
        return {"error": f"隔离 Runner 不可用，已拒绝执行: {exc}", "code": "RUNNER_UNAVAILABLE"}


def _record(
    db: Session,
    name: str,
    component_id: int | None,
    source: str,
    isolation: str,
    call_id: str,
    session_id: int | None,
    current,
    result: Any,
    started: float,
) -> None:
    error = result.get("error") if isinstance(result, dict) else None
    row = HarnessToolInvocation(
        tool_name=name,
        component_id=component_id,
        source=source,
        isolation=isolation,
        call_id=call_id,
        session_id=session_id,
        user_id=int(getattr(current, "id", 0) or 0) or None,
        success=not error,
        error_code=str(result.get("code") or "")[:64] if isinstance(result, dict) else "",
        error_message=str(error or "")[:2000],
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    db.add(row)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - 观测写失败不能影响工具结果
        db.rollback()


def metrics(db: Session, tool_name: str | None = None) -> list[dict]:
    """按工具聚合调用次数、成功率、P95 和最近错误。"""
    stmt = select(HarnessToolInvocation).order_by(HarnessToolInvocation.id.desc()).limit(5000)
    if tool_name:
        stmt = stmt.where(HarnessToolInvocation.tool_name == tool_name)
    grouped: dict[str, list[HarnessToolInvocation]] = {}
    for row in db.scalars(stmt).all():
        grouped.setdefault(row.tool_name, []).append(row)
    out = []
    for name, rows in sorted(grouped.items()):
        durations = sorted(row.duration_ms for row in rows)
        failures = [row for row in rows if not row.success]
        index = max(0, int(round(0.95 * (len(durations) - 1))))
        out.append(
            {
                "tool_name": name,
                "calls": len(rows),
                "success_rate": round(1 - len(failures) / len(rows), 4),
                "p95_ms": durations[index] if durations else 0,
                "last_error": failures[0].error_message if failures else "",
                "last_error_code": failures[0].error_code if failures else "",
                "last_called_at": rows[0].created_at.isoformat() if rows[0].created_at else "",
            }
        )
    return out


def parse_tool_manifest(raw: dict[str, Any]) -> Manifest:
    manifest = Manifest.model_validate(raw)
    if manifest.kind != "agent-tool":
        raise ValueError("不是 agent-tool manifest")
    entrypoint = (manifest.metadata or {}).get("entrypoint_argv")
    if not isinstance(entrypoint, list) or not entrypoint:
        raise ValueError("agent-tool 必须在 metadata.entrypoint_argv 声明入口命令数组")
    schema = manifest.config_schema or {}
    if schema.get("type") != "object":
        raise ValueError("agent-tool 的输入 schema 必须是 object")
    if not isinstance((manifest.metadata or {}).get("output_schema"), dict):
        raise ValueError("agent-tool 必须在 metadata.output_schema 声明输出 schema")
    from app.modules.harness.broker import validate_capabilities

    validate_capabilities(list(manifest.capabilities))
    return manifest


def dumps(spec: ToolSpec) -> str:
    return json.dumps(spec.public(), ensure_ascii=False)
