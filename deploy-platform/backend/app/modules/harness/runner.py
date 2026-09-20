"""隔离 Tool Runner 客户端。

第三方 Agent Tool 的代码永远不在 API 进程里执行：它只在 harness-runner 容器中
以非 root、只读根文件系统、无外网的方式跑。Runner 没配、连不上、或自报的隔离
条件不完整时，这里直接拒绝执行，绝不退化成本地 subprocess——那样等于把一个
「可以跑任意第三方代码」的后门开在拿着数据库连接和平台密钥的进程里。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.modules.harness import broker

logger = logging.getLogger(__name__)

REQUIRED_ISOLATION = {
    "database_access": False,
    "root_filesystem": "read-only",
}


class RunnerUnavailable(RuntimeError):
    """Runner 缺失或隔离不达标；调用方必须 fail-closed。"""


@dataclass(frozen=True)
class RunnerConfig:
    base_url: str
    token: str
    timeout_sec: int


@dataclass(frozen=True)
class ToolInvocation:
    call_id: str
    package_path: str
    package_sha256: str
    entrypoint: list[str]
    method: str
    arguments: dict[str, Any]
    capabilities: list[str]
    user_id: int


def load_config(db: Session) -> RunnerConfig:
    from app.modules.settings import service as settings_service

    base_url = settings_service.get_setting(db, "harness_runner_url", "").strip().rstrip("/")
    token = settings_service.get_setting(db, "harness_runner_token", "").strip()
    if not base_url or not token:
        raise RunnerUnavailable("未配置隔离 Runner（harness_runner_url / harness_runner_token）")
    try:
        timeout = int(settings_service.get_setting(db, "harness_tool_timeout_sec", "60"))
    except ValueError:
        timeout = 60
    return RunnerConfig(base_url, token, max(1, min(timeout, 300)))


def _request(config: RunnerConfig, path: str, payload: dict | None, timeout: int) -> dict:
    request = urllib.request.Request(
        f"{config.base_url}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Runner-Token": config.token,
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RunnerUnavailable(f"Runner 返回 HTTP {exc.code}: {detail[:400]}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise RunnerUnavailable(f"Runner 不可达: {exc}") from exc


def health(db: Session) -> dict:
    """探测 Runner，并核对它自报的隔离条件。"""
    config = load_config(db)
    payload = _request(config, "/health", None, timeout=10)
    if not payload.get("ok"):
        raise RunnerUnavailable("Runner 自检未通过")
    for key, expected in REQUIRED_ISOLATION.items():
        if payload.get(key) != expected:
            raise RunnerUnavailable(f"Runner 隔离条件不满足: {key}={payload.get(key)!r}")
    return payload


def execute(db: Session, invocation: ToolInvocation) -> dict:
    """在隔离 Runner 中执行一次工具调用；任何隔离缺口都抛 RunnerUnavailable。"""
    config = load_config(db)
    health(db)
    permit = broker.grant(
        invocation.call_id, invocation.user_id, invocation.capabilities, ttl_sec=config.timeout_sec + 30
    )
    payload = _request(
        config,
        "/execute",
        {
            "call_id": invocation.call_id,
            "package_path": invocation.package_path,
            "package_sha256": invocation.package_sha256,
            "entrypoint": invocation.entrypoint,
            "method": invocation.method,
            "arguments": invocation.arguments,
            "capability_token": permit.token,
            "capabilities": list(permit.capabilities),
            "timeout_sec": config.timeout_sec,
        },
        timeout=config.timeout_sec + 15,
    )
    if not payload.get("ok"):
        error = payload.get("error") or {}
        return {"error": error.get("message") or "工具执行失败", "code": error.get("code") or "TOOL_FAILED"}
    response = payload.get("response") or {}
    if "error" in response:
        error = response["error"] or {}
        return {"error": str(error.get("message") or error), "code": str(error.get("code") or "TOOL_ERROR")}
    return {"result": response.get("result")}
