"""拔插式插件：打包、上传、安装、下载（Agent 只拉 zip 跑入口命令）。"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.store.models import Plugin

logger = logging.getLogger(__name__)

# backend/app/modules/store/plugin_service.py → backend/plugins
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_SRC_ROOT = _BACKEND_ROOT / "plugins"
PACKAGE_DIR = _BACKEND_ROOT / "data" / "plugin-packages"

# 插件标识会进 zip 文件名和构建机缓存目录，不能带路径。
_PLUGIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_PLUGIN_DOWNLOAD = re.compile(
    r"^/api/v1/store/(?:plugins/[A-Za-z0-9][A-Za-z0-9._-]{0,62}|plugin-drafts/[1-9][0-9]{0,11})/package$"
)
_PLUGIN_SHA = re.compile(r"^[a-fA-F0-9]{64}$")


def _ensure_package_dir() -> Path:
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    return PACKAGE_DIR


def zip_plugin_dir(src: Path) -> bytes:
    """把插件目录打成 zip（根目录含 task.json）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                zf.write(p, p.relative_to(src).as_posix())
    return buf.getvalue()


def assert_plugin_identity(name: str, version: str = "1.0.0") -> tuple[str, str]:
    """插件标识和版本只能是字母数字和 ._- ，避免写进文件名时带上 ../。"""
    n = (name or "").strip()
    v = (version or "1.0.0").strip()
    if not _PLUGIN_ID.fullmatch(n):
        raise BizException.bad_request(f"插件标识「{name}」不合法，只能是字母数字和 ._-")
    if not _PLUGIN_ID.fullmatch(v):
        raise BizException.bad_request(f"插件版本「{version}」不合法，只能是字母数字和 ._-")
    return n, v


def is_safe_plugin_download_path(path: str) -> bool:
    """构建机只从本平台商店或草稿包地址拉 zip。"""
    return bool(_PLUGIN_DOWNLOAD.fullmatch(path or ""))


def parse_task_json_from_zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        for n in names:
            p = n.replace("\\", "/")
            if p.startswith("/") or any(part == ".." for part in Path(p).parts):
                raise BizException.bad_request("插件包包含越界路径")
        task_name = None
        for n in names:
            if n.replace("\\", "/").rstrip("/").endswith("task.json") and not n.endswith("/"):
                # 优先根目录
                if n.replace("\\", "/") == "task.json" or n.count("/") == 0:
                    task_name = n
                    break
                if task_name is None:
                    task_name = n
        if not task_name:
            raise BizException.bad_request("插件包缺少 task.json")
        raw = zf.read(task_name).decode("utf-8")
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError as e:
            raise BizException.bad_request(f"task.json 不是合法 JSON: {e}") from e
        if not meta.get("name"):
            raise BizException.bad_request("task.json 缺少 name")
        assert_plugin_identity(str(meta.get("name") or ""), str(meta.get("version") or "1.0.0"))
        return meta


def save_package(name: str, version: str, data: bytes) -> tuple[str, str]:
    name, version = assert_plugin_identity(name, version)
    _ensure_package_dir()
    safe_ver = version.replace("/", "_")
    dest = (PACKAGE_DIR / f"{name}-{safe_ver}.zip").resolve()
    if dest.parent != PACKAGE_DIR.resolve():
        raise BizException.bad_request("插件包路径越界")
    dest.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    return str(dest), sha


def upsert_plugin_from_meta(
    db: Session,
    meta: dict,
    *,
    package_path: str,
    package_sha: str,
    installed: bool,
) -> Plugin:
    name = str(meta["name"]).strip()
    schema = meta.get("config_schema") or {}
    schema_str = json.dumps(schema, ensure_ascii=False) if not isinstance(schema, str) else schema

    p = db.scalar(select(Plugin).where(Plugin.name == name))
    if p is None:
        p = Plugin(name=name)
        db.add(p)
    p.display_name = str(meta.get("display_name") or name)
    p.category = str(meta.get("category") or "exec")
    p.version = str(meta.get("version") or "1.0.0")
    p.description = str(meta.get("description") or "")
    p.config_schema = schema_str
    p.language = str(meta.get("language") or "python")
    p.entrypoint = str(meta.get("entrypoint") or "python task.py")
    p.package_path = package_path
    p.package_sha = package_sha
    p.installed = installed
    p.enabled = installed
    p.status = "installed" if installed else "uploaded"
    db.commit()
    db.refresh(p)
    if installed:
        _sync_harness(db, p, installed=True)
    return p


def require_third_party_signature(db: Session, data: bytes, name: str) -> None:
    """第三方流水线插件必须带与 Harness Tool 相同的 manifest.sig。

    内置插件（仓库 plugins/ 目录、编排目录、节点内置）启动同步，不验签。
    签名覆盖 task.json 规范化 JSON + 包内容摘要，公钥复用 harness_tool_signing_keys。
    """
    if is_builtin_plugin(name):
        return
    from app.modules.harness import signing

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BizException.bad_request("不是合法的 zip 包") from exc
    with archive:
        names = {n.replace("\\", "/") for n in archive.namelist()}
        if signing.SIGNATURE_NAME not in names:
            raise BizException.bad_request(
                "第三方插件必须包含 manifest.sig。请用模板里的 sign.py 签名后再上传"
            )
        task_name = None
        for n in names:
            if n.endswith("task.json") and not n.endswith("/"):
                if n == "task.json":
                    task_name = n
                    break
                if task_name is None:
                    task_name = n
        if not task_name:
            raise BizException.bad_request("插件包缺少 task.json")
        try:
            meta = json.loads(archive.read(task_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BizException.bad_request(f"task.json 不是合法 JSON: {exc}") from exc
        if not isinstance(meta, dict):
            raise BizException.bad_request("task.json 必须是对象")
        try:
            sig_doc = json.loads(archive.read(signing.SIGNATURE_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BizException.bad_request("manifest.sig 不是合法 JSON") from exc
        digest = signing.content_digest(archive)
    result = signing.verify(
        db,
        meta,
        digest,
        str(sig_doc.get("signature") or ""),
        str(sig_doc.get("key_id") or ""),
    )
    if not result.verified:
        raise BizException.bad_request(f"插件签名校验失败：{result.reason}")


def upload_zip(db: Session, data: bytes, filename: str = "") -> Plugin:
    if not data:
        raise BizException.bad_request("空文件")
    if filename and not filename.lower().endswith(".zip"):
        raise BizException.bad_request("请上传 zip 插件包")
    meta = parse_task_json_from_zip(data)
    name = str(meta.get("name") or "").strip()
    refuse_builtin_overwrite(name)
    require_third_party_signature(db, data, name)
    path, sha = save_package(name, str(meta.get("version") or "1.0.0"), data)
    # 上传后默认未安装，需点「安装」才出现在编排器（与蓝鲸商店一致）
    return upsert_plugin_from_meta(db, meta, package_path=path, package_sha=sha, installed=False)


def install_plugin(db: Session, plugin_id: int) -> Plugin:
    p = db.get(Plugin, plugin_id)
    if p is None:
        raise BizException.not_found("插件")
    if not p.package_path or not Path(p.package_path).is_file():
        raise BizException.bad_request("插件包不存在，请先上传 zip")
    data = Path(p.package_path).read_bytes()
    require_third_party_signature(db, data, p.name)
    p.installed = True
    p.enabled = True
    p.status = "installed"
    db.commit()
    db.refresh(p)
    _sync_harness(db, p, installed=True)
    return p


def uninstall_plugin(db: Session, plugin_id: int) -> Plugin:
    p = db.get(Plugin, plugin_id)
    if p is None:
        raise BizException.not_found("插件")
    if is_builtin_plugin(p.name):
        raise BizException.bad_request("内置插件不可卸载")
    p.installed = False
    p.enabled = False
    p.status = "uploaded" if p.package_path else "catalog"
    db.commit()
    db.refresh(p)
    _sync_harness(db, p, installed=False)
    return p


def builtin_plugin_names() -> set[str]:
    """平台自带流水线插件：编排目录 + plugins/ 实现 + 节点内置，不可卸载也不可删除。"""
    from app.modules.pipeline.plugins import PLUGIN_META

    names = set(NODE_BUILTIN_PLUGINS)
    names.update(PLUGIN_META)
    root = PLUGIN_SRC_ROOT
    if not root.is_dir():
        return names
    for child in root.iterdir():
        if not child.is_dir() or child.name in ("sdk", "examples"):
            continue
        names.add(child.name)
        task = child / "task.json"
        if not task.is_file():
            continue
        try:
            meta = json.loads(task.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(meta.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def is_builtin_plugin(name: str) -> bool:
    return (name or "").strip() in builtin_plugin_names()


def refuse_builtin_overwrite(name: str) -> None:
    """第三方上传/发布不得占用内置标识，否则会改掉平台自带实现。

    内置插件可以下载二次开发，但必须换一个 name 再作为第三方包进来。
    """
    key = (name or "").strip()
    if is_builtin_plugin(key):
        raise BizException.bad_request(
            f"「{key}」是平台内置插件，不能覆盖。"
            "请改 task.json 里的 name（例如 my-maven-build），签名后再上传"
        )


def serialize_plugin(plugin: Plugin) -> dict:
    """商店列表用的插件视图。builtin / has_package 不是表字段，现算。"""
    data = {c.key: getattr(plugin, c.key) for c in plugin.__table__.columns}
    data["builtin"] = is_builtin_plugin(plugin.name)
    data["has_package"] = _package_file(plugin) is not None or plugin_source_dir(plugin.name) is not None
    return data


def plugin_source_dir(name: str) -> Path | None:
    """仓库 plugins/ 里对应该标识的源码目录。shell-exec 这类 Agent 内置步骤没有目录。

    先按目录名匹配，再扫各目录 task.json 的 name，避免目录名和标识不一致。
    """
    key = (name or "").strip()
    if not key or not _PLUGIN_ID.fullmatch(key) or not PLUGIN_SRC_ROOT.is_dir():
        return None
    # 目录名就是插件标识时直接返回
    direct = PLUGIN_SRC_ROOT / key
    if (direct / "task.json").is_file():
        return direct
    # 目录名和 task.json.name 不一致时按标识反查
    for child in PLUGIN_SRC_ROOT.iterdir():
        if not child.is_dir() or child.name in ("sdk", "examples"):
            continue
        task = child / "task.json"
        if not task.is_file():
            continue
        try:
            meta = json.loads(task.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(meta.get("name") or "").strip() == key:
            return child
    return None


def read_plugin_package(db: Session, name: str) -> tuple[str, bytes]:
    """给控制台下载二次开发用的 zip。优先已登记的包，没有再从源码目录现打。"""
    key = (name or "").strip()
    p = db.scalar(select(Plugin).where(Plugin.name == key))
    if p is None:
        raise BizException.not_found("插件")
    # 启动同步已经打过的 zip，和 Agent 拉到的是同一份
    pkg = _package_file(p)
    if pkg is not None:
        return pkg.name, pkg.read_bytes()
    # 磁盘包丢了时，仍允许从仓库源码现打，方便二次开发
    src = plugin_source_dir(key)
    if src is not None:
        version = (p.version or "1.0.0").replace("/", "_")
        return f"{key}-{version}.zip", zip_plugin_dir(src)
    raise BizException.bad_request(
        "该插件实现在 Agent 内，没有可下载的源码包。请下载「插件开发模板」自行开发"
    )


def _unlink_owned_package(package_path: str) -> None:
    if not package_path:
        return
    path = Path(package_path)
    try:
        resolved = path.resolve()
        package_root = PACKAGE_DIR.resolve()
        if resolved.parent != package_root and package_root not in resolved.parents:
            logger.warning("拒绝删除非插件包目录下的文件: %s", package_path)
            return
        resolved.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("删除插件包失败 %s: %s", package_path, e)


def delete_plugin(db: Session, plugin_id: int) -> None:
    """从仓库删掉第三方插件：先卸载，再删 zip 和库记录。内置插件不允许删。"""
    p = db.get(Plugin, plugin_id)
    if p is None:
        raise BizException.not_found("插件")
    if is_builtin_plugin(p.name):
        raise BizException.bad_request("内置插件不可删除")
    package_path = p.package_path or ""
    uninstall_plugin(db, plugin_id)
    _unlink_owned_package(package_path)
    db.delete(p)
    db.commit()


def _sync_harness(db: Session, plugin: Plugin, *, installed: bool) -> None:
    """Store 保持旧 API/表不变，同时将生命周期代理到 Harness。"""
    # 延迟导入避免 Harness -> Store manifest 兼容层形成循环依赖。
    from app.modules.harness.lifecycle import sync_store_plugin

    sync_store_plugin(db, plugin, installed=installed)


def get_package_file(db: Session, name: str) -> Path:
    p = db.scalar(select(Plugin).where(Plugin.name == name, Plugin.installed.is_(True)))
    if p is None or not p.package_path:
        raise BizException.not_found("已安装插件包")
    path = Path(p.package_path)
    if not path.is_file():
        raise BizException.not_found("插件包文件")
    return path


# 这些插件的实现内置在部署节点 Agent 里，仓库里只有 task.json（表单定义）。
# rollback-files 连 task.json 都没有：它只由平台在生成回滚计划时下发，
# 不该出现在流水线编辑器的插件面板里让人手动编排
NODE_BUILTIN_PLUGINS = {"file-transfer", "iis-control", "service-control", "rollback-files"}

# Agent jar 里直接跑、不下载 zip 的构建步骤。
JOB_INLINE_PLUGINS = frozenset({"shell-exec", "bat-exec"})

# 不是构建步骤：触发器走流水线级配置；rollback-files 由平台下发。
NOT_JOB_PLUGINS = frozenset({
    "rollback-files",
    "cron-trigger",
    "manual-trigger",
    "webhook-trigger",
})


def _package_file(plugin: Plugin) -> Path | None:
    """插件 zip 在磁盘上的路径。未登记或文件已丢返回 None。"""
    raw = (plugin.package_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def is_runnable_job_plugin(plugin: Plugin) -> bool:
    """编排器能否把这个插件加进构建步骤、并且 Agent 真能跑起来。

    Agent 只认两类：jar 里写死的 shell/bat，以及 zip 还在磁盘上的已打包插件。
    种子和旧回填曾经把没有实现的名字标成已安装，选中后构建机报 not installed。
    """
    name = (plugin.name or "").strip()
    if name in NOT_JOB_PLUGINS:
        return False
    if name in JOB_INLINE_PLUGINS:
        return True
    return _package_file(plugin) is not None


def visible_in_store(plugin: Plugin) -> bool:
    """商店列表要不要展示这一条。

    空壳占位（无 zip、也不是 Agent 内置）不占商店位置，避免点「安装」却没有包。
    已上传未安装的第三方 zip 仍展示。触发器和平台下发的回滚步骤不进商店。
    """
    name = (plugin.name or "").strip()
    if name in NOT_JOB_PLUGINS:
        return False
    if name in JOB_INLINE_PLUGINS:
        return True
    return bool((plugin.package_path or "").strip())


def retire_unrunnable_catalog_plugins(db: Session) -> int:
    """把没有实现却被标成已安装的占位插件收成目录项。

    编排器按 installed=true 拉列表。历史 seed / backfill 会把空壳标已安装。
    有 zip 的（含未安装的第三方包）不动。
    """
    n = 0
    for plugin in db.scalars(select(Plugin)).all():
        if plugin.name in JOB_INLINE_PLUGINS:
            continue
        if _package_file(plugin) is not None:
            continue
        if not plugin.installed and not plugin.enabled and (plugin.status or "") != "installed":
            continue
        plugin.installed = False
        plugin.enabled = False
        plugin.status = "catalog"
        n += 1
    if n:
        db.commit()
    return n


def attach_package_meta(db: Session, steps: list) -> list:
    """给步骤挂上 Agent 执行所需的包信息（无包则 package=null）。"""
    out = []
    for step in steps:
        if not isinstance(step, dict):
            out.append(step)
            continue
        name = step.get("plugin")
        item = dict(step)
        if name in NODE_BUILTIN_PLUGINS:
            # 部署节点上的插件由节点 Agent 内置实现：生产机不下载、不解压、不执行任何脚本
            item["package"] = None
            out.append(item)
            continue
        # 草稿试跑会预先挂上本平台的草稿包地址。其它来源的 package 一律丢掉，
        # 改从仓库已安装插件查，避免流水线 JSON 把下载地址拐到外网。
        preset = step.get("package")
        if isinstance(preset, dict) and is_safe_plugin_download_path(str(preset.get("download_path") or "")):
            pkg = sanitize_package_meta(preset)
            if pkg is not None:
                item["package"] = pkg
                out.append(item)
                continue
        pkg = None
        if name:
            p = db.scalar(select(Plugin).where(Plugin.name == name, Plugin.installed.is_(True)))
            if p is not None and p.package_path:
                try:
                    pname, pver = assert_plugin_identity(p.name, str(p.version or "1.0.0"))
                except BizException:
                    item["package"] = None
                    out.append(item)
                    continue
                sha = str(p.package_sha or "").strip()
                if _PLUGIN_SHA.fullmatch(sha):
                    pkg = {
                        "name": pname,
                        "version": pver,
                        "language": p.language or "python",
                        "entrypoint": p.entrypoint or "python task.py",
                        "sha256": sha.lower(),
                        "download_path": f"/api/v1/store/plugins/{pname}/package",
                    }
        item["package"] = pkg
        out.append(item)
    return out


def sanitize_package_meta(preset: dict) -> dict | None:
    """只保留标识、哈希和本站下载路径，入口命令仍以仓库/草稿声明为准。"""
    try:
        name, version = assert_plugin_identity(
            str(preset.get("name") or ""),
            str(preset.get("version") or "1.0.0"),
        )
    except BizException:
        return None
    sha = str(preset.get("sha256") or "").strip()
    if not _PLUGIN_SHA.fullmatch(sha):
        return None
    path = str(preset.get("download_path") or "")
    if not is_safe_plugin_download_path(path):
        return None
    return {
        "name": name,
        "version": version,
        "language": str(preset.get("language") or "python"),
        "entrypoint": str(preset.get("entrypoint") or "python task.py"),
        "sha256": sha.lower(),
        "download_path": path,
    }


def sync_builtin_plugins(db: Session) -> None:
    """把仓库内 plugins/*/ 打成 zip 并登记为已安装（git-checkout 等内置示例）。"""
    root = PLUGIN_SRC_ROOT
    if not root.is_dir():
        logger.warning("插件源目录不存在，跳过内置同步: %s", root)
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in ("sdk", "examples"):
            continue
        task = child / "task.json"
        if not task.is_file():
            continue
        try:
            meta = json.loads(task.read_text(encoding="utf-8"))
            data = zip_plugin_dir(child)
            path, sha = save_package(meta["name"], str(meta.get("version") or "1.0.0"), data)
            upsert_plugin_from_meta(db, meta, package_path=path, package_sha=sha, installed=True)
            logger.info("已同步内置插件 %s %s", meta.get("name"), meta.get("version"))
        except Exception as e:  # noqa: BLE001
            logger.warning("同步内置插件 %s 失败: %s", child, e)
