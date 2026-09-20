"""第三方 Agent Tool 包的解析、签名校验与落盘。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.harness import signing, tools
from app.modules.harness.models import HarnessComponent
from app.modules.harness.packages import Manifest

MANIFEST_NAME = "manifest.yaml"
SIGNATURE_NAME = "manifest.sig"
MAX_PACKAGE_BYTES = 32 * 1024 * 1024

# 与 harness-runner 容器挂载的 /packages 对应；Runner 只按相对路径取包
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = _BACKEND_ROOT / "data"
TOOL_PACKAGE_DIR = PACKAGE_ROOT / "harness-tools"


@dataclass(frozen=True)
class ToolPackage:
    manifest: Manifest
    package_path: str
    package_sha256: str
    signature_key_id: str


def parse_tool_package(db: Session, data: bytes) -> tuple[Manifest, str, str]:
    """校验 zip、manifest 与签名，返回 manifest、整包摘要和签名者。

    返回的整包摘要给 Runner 用：它取包时再算一遍，确认磁盘上的文件没被换过。
    签名校验用的是包内容摘要，两者用途不同，不要混。
    """
    if not data:
        raise BizException.bad_request("空文件")
    if len(data) > MAX_PACKAGE_BYTES:
        raise BizException.bad_request("工具包超过 32MB 上限")
    package_digest = hashlib.sha256(data).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BizException.bad_request("不是合法的 zip 包") from exc
    with archive:
        names = {name.replace("\\", "/") for name in archive.namelist()}
        for name in names:
            if name.startswith("/") or ".." in Path(name).parts:
                raise BizException.bad_request("工具包包含越界路径")
        if MANIFEST_NAME not in names or SIGNATURE_NAME not in names:
            raise BizException.bad_request(f"工具包根目录必须包含 {MANIFEST_NAME} 和 {SIGNATURE_NAME}")
        try:
            raw = yaml.safe_load(archive.read(MANIFEST_NAME).decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise BizException.bad_request(f"{MANIFEST_NAME} 解析失败: {exc}") from exc
        if not isinstance(raw, dict):
            raise BizException.bad_request(f"{MANIFEST_NAME} 必须是映射")
        raw.setdefault("kind", "agent-tool")
        try:
            signature_doc = json.loads(archive.read(SIGNATURE_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BizException.bad_request(f"{SIGNATURE_NAME} 不是合法 JSON: {exc}") from exc
        content_digest = signing.content_digest(archive)
    try:
        manifest = tools.parse_tool_manifest(raw)
    except BizException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BizException.bad_request(f"{MANIFEST_NAME} 不合法: {exc}") from exc

    result = signing.verify(
        db,
        raw,
        content_digest,
        str(signature_doc.get("signature") or ""),
        str(signature_doc.get("key_id") or ""),
    )
    if not result.verified:
        # 签名不过就不落盘：一个能跑任意代码的包，不该在磁盘上留一份"待人工确认"
        raise BizException.bad_request(f"工具包签名校验未通过：{result.reason}")
    return manifest, package_digest, result.key_id


def read_saved_package(package_uri: str) -> tuple[str, bytes]:
    """读取已落盘的第三方工具 zip，供控制台下载二次开发。"""
    uri = (package_uri or "").strip()
    if not uri:
        raise BizException.bad_request("该工具没有可下载的安装包")
    path = Path(uri)
    if not path.is_absolute():
        path = PACKAGE_ROOT / uri
    try:
        resolved = path.resolve()
        root = PACKAGE_ROOT.resolve()
    except OSError as exc:
        raise BizException.not_found("工具包文件") from exc
    if root not in resolved.parents and resolved.parent != root:
        raise BizException.bad_request("拒绝读取包目录以外的文件")
    if not resolved.is_file():
        raise BizException.not_found("工具包文件")
    return resolved.name, resolved.read_bytes()


def unlink_saved_package(package_uri: str) -> None:
    """删除落盘的第三方工具 zip。路径不合法或文件不在则忽略，避免删库被磁盘拖死。"""
    uri = (package_uri or "").strip()
    if not uri:
        return
    path = Path(uri)
    if not path.is_absolute():
        path = PACKAGE_ROOT / uri
    try:
        resolved = path.resolve()
        root = PACKAGE_ROOT.resolve()
    except OSError:
        return
    if root not in resolved.parents and resolved.parent != root:
        return
    if resolved.is_file():
        resolved.unlink()


def save_tool_package(manifest: Manifest, data: bytes) -> str:
    """落盘并返回相对 data 目录的路径；Runner 以只读方式挂载同一目录。"""
    TOOL_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{manifest.name}-{manifest.version}.zip"
    destination = TOOL_PACKAGE_DIR / filename
    destination.write_bytes(data)
    return destination.relative_to(PACKAGE_ROOT).as_posix()


def install_tool_package(
    db: Session, data: bytes, *, actor_id: int | None = None, actor_name: str = ""
) -> HarnessComponent:
    from app.modules.harness import lifecycle, runner

    manifest, digest, key_id = parse_tool_package(db, data)
    # 装之前先确认隔离可用：装完却跑不了，用户只会在对话里看到一句"拒绝执行"
    try:
        runner.health(db)
    except runner.RunnerUnavailable as exc:
        raise BizException.bad_request(f"隔离 Runner 不可用，拒绝安装第三方工具：{exc}") from exc
    signed = manifest.model_copy(
        update={"metadata": {**(manifest.metadata or {}), "signature_key_id": key_id}}
    )
    relative = save_tool_package(signed, data)
    return lifecycle.install(
        db,
        signed,
        enable=False,
        source="upload",
        source_ref=f"tool:{signed.name}",
        package_uri=relative,
        package_sha256=digest,
        actor_id=actor_id,
        actor_name=actor_name,
    )
