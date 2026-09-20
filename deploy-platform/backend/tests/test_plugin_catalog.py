# -*- coding: utf-8 -*-
"""商店编目：没有 zip 的占位插件不能出现在编排器「已安装」列表。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.store import plugin_service
from app.modules.store.models import Plugin


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Plugin.__table__])
    with Session(engine) as session:
        yield session


def _plugin(
    db: Session,
    *,
    name: str,
    package_path: str = "",
    installed: bool = True,
    enabled: bool = True,
    status: str = "installed",
    version: str = "1.0.0",
) -> Plugin:
    p = Plugin(
        name=name,
        display_name=name,
        package_path=package_path,
        installed=installed,
        enabled=enabled,
        status=status,
        version=version,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_shell_exec_is_runnable_without_zip(db: Session) -> None:
    """Agent jar 内置的 shell 没有包，编排器仍应能选。"""
    p = _plugin(db, name="shell-exec", package_path="")
    assert plugin_service.is_runnable_job_plugin(p)
    assert plugin_service.visible_in_store(p)


def test_placeholder_without_zip_hidden(db: Session) -> None:
    """ssh-deploy / notify-im 这类空壳不能进编排器，也不能在商店里假装能安装。"""
    p = _plugin(db, name="notify-im", package_path="")
    assert not plugin_service.is_runnable_job_plugin(p)
    assert not plugin_service.visible_in_store(p)


def test_trigger_not_a_job_plugin(db: Session) -> None:
    """定时触发是流水线级配置，不能当构建步骤选。"""
    p = _plugin(db, name="cron-trigger", package_path="")
    assert not plugin_service.is_runnable_job_plugin(p)
    assert not plugin_service.visible_in_store(p)


def test_zip_plugin_runnable(db: Session, tmp_path: Path) -> None:
    """磁盘上还有 zip 的已安装插件可以选。"""
    zip_path = tmp_path / "git-checkout-1.0.6.zip"
    zip_path.write_bytes(b"zip")
    p = _plugin(db, name="git-checkout", package_path=str(zip_path))
    assert plugin_service.is_runnable_job_plugin(p)
    assert plugin_service.visible_in_store(p)


def test_missing_zip_file_not_runnable(db: Session, tmp_path: Path) -> None:
    """库里记了路径但文件丢了，编排器不应再列出。"""
    p = _plugin(db, name="maven-build", package_path=str(tmp_path / "gone.zip"))
    assert not plugin_service.is_runnable_job_plugin(p)


def test_retire_uninstalls_placeholders(db: Session, tmp_path: Path) -> None:
    """启动收回：空壳从已安装变成目录项；有 zip 的不动。"""
    zip_path = tmp_path / "real.zip"
    zip_path.write_bytes(b"zip")
    shell = _plugin(db, name="shell-exec", package_path="")
    real = _plugin(db, name="k8s-deploy", package_path=str(zip_path))
    fake = _plugin(db, name="win-deploy", package_path="")
    notify = _plugin(db, name="notify-im", package_path="")

    n = plugin_service.retire_unrunnable_catalog_plugins(db)

    db.refresh(shell)
    db.refresh(real)
    db.refresh(fake)
    db.refresh(notify)
    assert n == 2
    assert shell.installed is True
    assert real.installed is True
    assert fake.installed is False
    assert fake.status == "catalog"
    assert notify.installed is False


def _zip_task(name: str) -> bytes:
    """最小合法插件 zip：只有 task.json，用于测上传拦截。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("task.json", json.dumps({"name": name, "version": "1.0.0"}))
        archive.writestr("task.py", "print(1)\n")
    return buf.getvalue()


def test_upload_rejects_builtin_plugin_name(db: Session) -> None:
    """内置标识不能被第三方 zip 覆盖，必须改 name 再上传。"""
    with pytest.raises(BizException, match="不能覆盖"):
        plugin_service.upload_zip(db, _zip_task("maven-build"), "maven-build.zip")


def test_download_builtin_plugin_from_source(db: Session) -> None:
    """库里还没登记 zip 时，控制台仍能从 plugins/ 源码目录打出包。"""
    p = _plugin(db, name="maven-build", package_path="", version="1.0.8")
    filename, data = plugin_service.read_plugin_package(db, "maven-build")
    assert filename == "maven-build-1.0.8.zip"
    assert plugin_service.serialize_plugin(p)["has_package"] is True
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [name.replace("\\", "/") for name in archive.namelist()]
    assert any(name.endswith("task.json") for name in names)


def test_shell_exec_has_no_downloadable_package(db: Session) -> None:
    """Agent 内置步骤没有源码包，提示去下开发模板。"""
    p = _plugin(db, name="shell-exec", package_path="")
    assert plugin_service.serialize_plugin(p)["has_package"] is False
    with pytest.raises(BizException, match="没有可下载"):
        plugin_service.read_plugin_package(db, "shell-exec")


def test_parse_zip_rejects_path_escape_and_bad_name() -> None:
    """插件标识会进文件名和构建机缓存目录，带 ../ 的必须在解析期拦住。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("../evil.py", "print(1)\n")
        archive.writestr("task.json", json.dumps({"name": "ok-plugin", "version": "1.0.0"}))
    with pytest.raises(BizException, match="越界"):
        plugin_service.parse_task_json_from_zip(buf.getvalue())

    with pytest.raises(BizException, match="不合法"):
        plugin_service.parse_task_json_from_zip(_zip_task("../.ssh"))


def test_attach_package_meta_drops_foreign_download_path(db: Session) -> None:
    """流水线 JSON 里自带的外网下载地址不能进任务体。"""
    steps = plugin_service.attach_package_meta(db, [{
        "plugin": "custom-echo",
        "with": {},
        "package": {
            "name": "custom-echo",
            "version": "1.0.0",
            "sha256": "a" * 64,
            "download_path": "@evil.example/malware.zip",
            "entrypoint": "python task.py",
        },
    }])
    assert steps[0]["package"] is None


def test_sanitize_package_meta_keeps_store_path() -> None:
    pkg = plugin_service.sanitize_package_meta({
        "name": "git-checkout",
        "version": "1.0.8",
        "sha256": "b" * 64,
        "download_path": "/api/v1/store/plugins/git-checkout/package",
        "entrypoint": "python task.py",
    })
    assert pkg is not None
    assert pkg["download_path"].endswith("/git-checkout/package")

