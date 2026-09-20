"""隔离 Agent Tool JSON-RPC Runner。

Runner 不持有数据库连接或平台密钥，只接受后端签发的单次调用令牌。
第三方代码始终在独立子进程中执行；任何隔离条件不满足都 fail-closed。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(os.getenv("HARNESS_PACKAGE_ROOT", "/packages")).resolve()
MAX_ARCHIVE_BYTES = int(os.getenv("HARNESS_MAX_ARCHIVE_BYTES", str(32 * 1024 * 1024)))
MAX_OUTPUT_BYTES = int(os.getenv("HARNESS_MAX_OUTPUT_BYTES", str(2 * 1024 * 1024)))
DEFAULT_TIMEOUT = int(os.getenv("HARNESS_TOOL_TIMEOUT_SEC", "60"))


def _load_runner_token() -> str:
    env = (os.getenv("HARNESS_RUNNER_TOKEN") or "").strip()
    if env:
        return env
    for path in (ROOT / ".secrets.json", Path("/packages/.secrets.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        val = data.get("harness_runner_token")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


RUNNER_TOKEN = _load_runner_token()

app = FastAPI(title="RELEASE Harness Runner", docs_url=None, redoc_url=None)


class ExecuteRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=128)
    package_path: str = Field(min_length=1, max_length=512)
    package_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    entrypoint: list[str] = Field(min_length=1, max_length=16)
    method: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    capability_token: str = Field(min_length=1, max_length=4096)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    timeout_sec: int = Field(default=DEFAULT_TIMEOUT, ge=1, le=300)


def _authorized(value: str) -> bool:
    import hmac

    return bool(RUNNER_TOKEN) and hmac.compare_digest(value, RUNNER_TOKEN)


def _package_file(relative: str) -> Path:
    candidate = (ROOT / relative).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError as exc:
        raise HTTPException(400, "package_path 越界") from exc
    if not candidate.is_file() or candidate.suffix.lower() != ".zip":
        raise HTTPException(404, "工具包不存在")
    if candidate.stat().st_size > MAX_ARCHIVE_BYTES:
        raise HTTPException(413, "工具包超过大小限制")
    return candidate


def _safe_extract(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            dest = (target / info.filename).resolve()
            try:
                dest.relative_to(target)
            except ValueError as exc:
                raise HTTPException(400, "工具包包含越界路径") from exc
            mode = info.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise HTTPException(400, "工具包不允许符号链接")
        zf.extractall(target)


def _limit_process() -> None:
    """Linux 子进程资源上限；容器 cgroup 是第二层边界。"""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, OSError, ValueError):
        os._exit(126)


@app.get("/health")
def health() -> dict:
    return {
        "ok": bool(RUNNER_TOKEN) and ROOT.is_dir(),
        "isolation": "container",
        "database_access": False,
        "root_filesystem": "read-only",
        "network": "internal-only",
    }


@app.post("/execute")
def execute(body: ExecuteRequest, x_runner_token: str = Header(default="")) -> dict:
    if not _authorized(x_runner_token):
        raise HTTPException(401, "Runner 认证失败")
    archive = _package_file(body.package_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest.lower() != body.package_sha256.lower():
        raise HTTPException(409, "工具包摘要不匹配")
    if any(not str(part).strip() or "\x00" in str(part) for part in body.entrypoint):
        raise HTTPException(400, "非法入口命令")

    work = Path(tempfile.mkdtemp(prefix=f"call-{body.call_id[:32]}-", dir="/tmp"))
    try:
        _safe_extract(archive, work)
        request = {
            "jsonrpc": "2.0",
            "id": body.call_id,
            "method": body.method,
            "params": body.arguments,
        }
        env = {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONUNBUFFERED": "1",
            "HARNESS_CALL_ID": body.call_id,
            "HARNESS_CAPABILITY_TOKEN": body.capability_token,
            "HARNESS_CAPABILITIES": ",".join(sorted(set(body.capabilities))),
            "HARNESS_BROKER_URL": os.getenv("HARNESS_BROKER_URL", "http://backend:8080/api/v1/harness/broker"),
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
        }
        try:
            completed = subprocess.run(
                body.entrypoint,
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                cwd=work,
                env=env,
                capture_output=True,
                timeout=body.timeout_sec,
                check=False,
                preexec_fn=_limit_process,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "工具执行超时") from exc
        stdout = completed.stdout.encode("utf-8", errors="replace")
        stderr = completed.stderr.encode("utf-8", errors="replace")
        if len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES:
            raise HTTPException(413, "工具输出超过限制")
        if completed.returncode != 0:
            return {
                "ok": False,
                "error": {
                    "code": "TOOL_PROCESS_FAILED",
                    "message": completed.stderr[-4000:] or f"exit {completed.returncode}",
                },
            }
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HTTPException(502, "工具未返回合法 JSON-RPC") from exc
        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != body.call_id:
            raise HTTPException(502, "工具 JSON-RPC 信封无效")
        return {"ok": "error" not in result, "response": result}
    finally:
        shutil.rmtree(work, ignore_errors=True)
