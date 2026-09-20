# -*- coding: utf-8 -*-
"""Linux 节点安装：没有可用 JDK 时从平台拉包，校验通过才继续装 Agent。"""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.core.response import BizException
from app.db.base import Base
from app.modules.agent import jdk_linux
from app.modules.agent import router as agent_router
from app.modules.agent.models import BuildAgent
from app.modules.agent.router import download_jdk_linux, agent_jar_sha256
from app.modules.agent.shared_packs import pack_dir
from app.modules.settings.models import PlatformSetting

_GZIP = b"\x1f\x8bhello-jdk"


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[BuildAgent.__table__, PlatformSetting.__table__])
    return Session(engine)


def test_linux_node_script_installs_jdk_then_verifies() -> None:
    """没 Java 不能直接死掉；必须先装 JDK、校验版本，失败则停在装 Agent 之前。"""
    script = Path(__file__).resolve().parents[1] / "agent-java" / "scripts" / "install-node.sh"
    text = script.read_text(encoding="utf-8")
    assert "未找到 java，请先安装" not in text
    assert "/api/v1/agents/jdk-linux" in text
    assert "java_usable" in text
    assert "ensure_java" in text
    assert "新装的 JDK 校验失败" in text
    assert "已停止安装 Agent" in text
    assert text.index("ensure_java") < text.index("下载 jar")
    assert "command -v java" not in text.split("echo \"[6/8] 注册 systemd 服务\"", 1)[1].split("STARTEOF", 1)[0]
    assert "节点管理" in text
    assert "grant_site_dir_acl" in text
    assert "setfacl -R -m" not in text.split("grant_site_dir_acl()", 1)[0]
    assert "/api/v1/agents/jar-sha256" in text
    assert "Nice=10" in text
    assert "CPUQuota=50%" in text
    # 成功路径必须以 return 0 收尾。原先最后一句是失败的 `[ parent = / ] && die`，
    # 函数返回 1，外层 set -e 让脚本在打印备份路径后静默退出，重装看起来没效果。
    start = text.index("assert_backup_root_safe()")
    end = text.index("\nallow_under_backup()", start)
    body = text[start:end]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert lines[-1] == "}"
    assert lines[-2] == "return 0"


def test_resolve_backup_root_survives_set_e() -> None:
    """合法安装目录在 set -e 下必须跑完，不能在打印备份路径后静默退出。"""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        return
    script = Path(__file__).resolve().parents[1] / "agent-java" / "scripts" / "install-node.sh"
    text = script.read_text(encoding="utf-8")
    start = text.index("derive_backup_root()")
    end = text.index("\njava_usable()")
    payload = (
        "set -e\n"
        'die() { echo "错误：$*" >&2; exit 1; }\n'
        + text[start:end]
        + "\nINSTALL_DIR=/data/release\nBACKUP_ROOT=\n"
        "resolve_backup_root\n"
        'echo SURVIVED:$BACKUP_ROOT\n'
    )
    result = subprocess.run([bash, "-c", payload], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "SURVIVED:/data/release-backup" in result.stdout.replace("\r", "")


def test_windows_node_script_pins_backup_root_and_verifies_jar() -> None:
    """Windows 安装必须带 --backup-root，并校验 jar sha256，进程低于正常优先级。"""
    script = Path(__file__).resolve().parents[1] / "agent-java" / "scripts" / "install-node.ps1"
    text = script.read_text(encoding="utf-8")
    assert "--backup-root" in text
    assert "jar-sha256" in text
    assert "/belownormal" in text
    assert "release-backup" in text


def test_jdk_linux_download_uses_same_bootstrap_auth() -> None:
    """JDK 下载和 jar 一样走接入凭证，不能匿名。"""
    names: list[str] = []
    for route in agent_router.router.routes:
        if getattr(route, "path", None) != "/agents/jdk-linux":
            continue
        if "GET" not in (getattr(route, "methods", None) or set()):
            continue

        def walk(dep) -> None:
            call = getattr(dep, "call", None)
            if call is not None:
                names.append(getattr(call, "__name__", ""))
            for child in getattr(dep, "dependencies", None) or []:
                walk(child)

        walk(route.dependant)
        break
    else:
        raise AssertionError("没有 GET /agents/jdk-linux")
    assert "download_jdk_linux" in names
    src = Path(agent_router.__file__).read_text(encoding="utf-8")
    block = src.split("def download_jdk_linux", 1)[1].split("INSTALL_SCRIPTS", 1)[0]
    assert "_authorize_agent_bootstrap" in block


def test_pack_dir_nests_under_shared_packs() -> None:
    """公共包必须进 shared-packs 子目录，不能摊在 data 根下。"""
    path = pack_dir("jdk-linux")
    assert path.parts[-2:] == ("shared-packs", "jdk-linux")
    try:
        pack_dir("..", "artifacts")
        raise AssertionError("越界路径应拒绝")
    except ValueError:
        pass


def test_find_prefers_data_volume(tmp_path, monkeypatch) -> None:
    """数据卷里的包优先于镜像里的包，重建镜像也不会改到这份。"""
    data = tmp_path / "data"
    data.mkdir()
    kept = data / "jdk-8u271-linux-x64.tar.gz"
    kept.write_bytes(b"volume")
    bundled = tmp_path / "jdk-8u271-linux-x64.tar.gz"
    bundled.write_bytes(b"image")
    monkeypatch.setattr(jdk_linux, "DATA_DIR", data)
    monkeypatch.setattr(jdk_linux, "LEGACY_DIR", tmp_path / "legacy-missing")
    monkeypatch.setattr(jdk_linux, "_bundled", lambda: bundled)
    found = jdk_linux.find()
    assert found == kept
    assert found.read_bytes() == b"volume"


def test_find_seeds_bundled_into_data_dir(tmp_path, monkeypatch) -> None:
    """镜像里有包、数据卷没有时，第一次查找就拷进数据卷。"""
    data = tmp_path / "data"
    bundled = tmp_path / "bundle" / "jdk-8u271-linux-x64.tar.gz"
    bundled.parent.mkdir()
    bundled.write_bytes(b"from-image")
    monkeypatch.setattr(jdk_linux, "DATA_DIR", data)
    monkeypatch.setattr(jdk_linux, "LEGACY_DIR", tmp_path / "legacy-missing")
    monkeypatch.setattr(jdk_linux, "_bundled", lambda: bundled)
    found = jdk_linux.find()
    seeded = data / "jdk-8u271-linux-x64.tar.gz"
    assert found == seeded
    assert seeded.read_bytes() == b"from-image"
    assert jdk_linux.status()["persistent"] is True


def test_find_migrates_legacy_jdk_linux_dir(tmp_path, monkeypatch) -> None:
    """旧路径 data/jdk-linux 里的包要拷进 shared-packs/jdk-linux。"""
    data = tmp_path / "shared-packs" / "jdk-linux"
    legacy = tmp_path / "jdk-linux"
    legacy.mkdir()
    old = legacy / "jdk-8u271-linux-x64.tar.gz"
    old.write_bytes(b"legacy-pack")
    monkeypatch.setattr(jdk_linux, "DATA_DIR", data)
    monkeypatch.setattr(jdk_linux, "LEGACY_DIR", legacy)
    monkeypatch.setattr(jdk_linux, "_bundled", lambda: None)
    found = jdk_linux.find()
    seeded = data / "jdk-8u271-linux-x64.tar.gz"
    assert found == seeded
    assert seeded.read_bytes() == b"legacy-pack"


def test_save_upload_writes_data_dir(tmp_path, monkeypatch) -> None:
    """页面上传写入数据卷，覆盖旧包。"""
    monkeypatch.setattr(jdk_linux, "DATA_DIR", tmp_path)
    old = tmp_path / "OpenJDK8U-jdk_x64_linux_hotspot_old.tar.gz"
    old.write_bytes(b"old")
    upload = UploadFile(filename="jdk-8u271-linux-x64.tar.gz", file=BytesIO(_GZIP))
    result = asyncio.run(jdk_linux.save_upload(upload))
    assert result["ready"] is True
    assert result["persistent"] is True
    assert result["filename"] == "jdk-8u271-linux-x64.tar.gz"
    assert (tmp_path / "jdk-8u271-linux-x64.tar.gz").read_bytes() == _GZIP
    assert not old.exists()


def test_save_upload_rejects_non_gzip(tmp_path, monkeypatch) -> None:
    """改个后缀的 zip 不能当 JDK 包。"""
    monkeypatch.setattr(jdk_linux, "DATA_DIR", tmp_path)
    upload = UploadFile(filename="jdk-8u271-linux-x64.tar.gz", file=BytesIO(b"PK\x03\x04not-gzip"))
    try:
        asyncio.run(jdk_linux.save_upload(upload))
        raise AssertionError("非 gzip 应拒绝")
    except BizException as exc:
        assert exc.code == 400
        assert "gzip" in exc.message


def test_jar_sha256_rejects_anonymous() -> None:
    """没有接入凭证时不能拿 jar 指纹。"""
    db = _db()
    try:
        agent_jar_sha256(db, "", "", "")
        raise AssertionError("匿名不应拿到 jar 指纹")
    except BizException as exc:
        assert exc.code == 401


def test_jar_sha256_returns_plain_hex(monkeypatch) -> None:
    """凭证正确时返回 64 位 hex，安装脚本直接比对。"""
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    db.commit()
    monkeypatch.setattr(agent_router, "current_jar_sha256", lambda: "a" * 64)
    resp = agent_jar_sha256(db, "enroll-secret", "", "")
    assert resp.body.decode().strip() == "a" * 64


def test_jdk_download_rejects_anonymous() -> None:
    """没有接入凭证、登录态、Agent token 时不能拉 JDK。"""
    db = _db()
    try:
        download_jdk_linux(db, "", "", "")
        raise AssertionError("匿名不应下到 JDK")
    except BizException as exc:
        assert exc.code == 401


def test_jdk_download_404_when_package_missing(monkeypatch) -> None:
    """凭证对了但平台没放包，明确 404，安装脚本据此停在装 Agent 之前。"""
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    db.commit()
    monkeypatch.setattr(agent_router, "_jdk_linux_tarball", lambda: None)
    try:
        download_jdk_linux(db, "enroll-secret", "", "")
        raise AssertionError("缺包应 404")
    except BizException as exc:
        assert exc.code == 404
        assert "JDK" in exc.message
        assert "节点管理" in exc.message


def test_jdk_download_returns_tarball(tmp_path, monkeypatch) -> None:
    """凭证正确且包在位时按文件名下发。"""
    pkg = tmp_path / "jdk-8u271-linux-x64.tar.gz"
    pkg.write_bytes(b"jdk-bytes")
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    db.commit()
    monkeypatch.setattr(agent_router, "_jdk_linux_tarball", lambda: str(pkg))
    resp = download_jdk_linux(db, "enroll-secret", "", "")
    assert resp.filename == "jdk-8u271-linux-x64.tar.gz"
