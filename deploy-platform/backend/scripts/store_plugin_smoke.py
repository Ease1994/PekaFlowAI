# -*- coding: utf-8 -*-
"""研发商店：打包 / 上传 / 安装 / 下载 烟雾测试。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "_store_smoke.db"
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB).replace("\\", "/")


def _service_layer() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    import app.db.models  # noqa: F401
    from app.modules.store.plugin_service import (
        attach_package_meta,
        get_package_file,
        install_plugin,
        uninstall_plugin,
        upload_zip,
        zip_plugin_dir,
    )

    if DB.exists():
        try:
            DB.unlink()
        except OSError:
            pass

    engine = create_engine(f"sqlite:///{DB.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    example = ROOT / "plugins" / "examples" / "hello-nodejs"
    data = zip_plugin_dir(example)

    with Session() as db:
        p = upload_zip(db, data, "hello-nodejs-1.0.0.zip")
        assert p.installed is False
        print(f"[ok] upload id={p.id} name={p.name}")
        p2 = install_plugin(db, p.id)
        assert p2.installed is True
        print("[ok] install")
        assert get_package_file(db, "hello-nodejs").is_file()
        print("[ok] package file")
        steps = attach_package_meta(db, [{"plugin": "hello-nodejs", "with": {}}])
        assert steps[0]["package"]["entrypoint"] == "node task.js"
        print("[ok] attach_package_meta")
        uninstall_plugin(db, p.id)
        print("[ok] uninstall")
        install_plugin(db, p.id)
        print("[ok] re-install")

    engine.dispose()
    print("[ok] service layer")


def _http_layer() -> None:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("[skip] HTTP layer: 当前解释器未安装 fastapi")
        return

    from app.core.deps import CurrentUser, get_current_user
    from app.core.security import create_access_token
    from app.main import app
    from app.modules.store.plugin_service import zip_plugin_dir

    def fake_user():
        return CurrentUser(id=1, username="admin", is_admin=True)

    app.dependency_overrides[get_current_user] = fake_user
    zip_bytes = zip_plugin_dir(ROOT / "plugins" / "examples" / "hello-nodejs")
    token = create_access_token(1, "admin", True)
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/store/plugins/upload",
            files={"file": ("hello-nodejs.zip", zip_bytes, "application/zip")},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["code"] == 0, body
        pid = body["data"]["id"]
        print(f"[ok] HTTP upload plugin_id={pid}")

        r2 = client.post(f"/api/v1/store/plugins/{pid}/install", headers=headers)
        assert r2.status_code == 200, r2.text
        assert r2.json()["data"]["installed"] is True
        print("[ok] HTTP install")

        r3 = client.get("/api/v1/store/plugins", params={"installed": True}, headers=headers)
        names = [x["name"] for x in r3.json()["data"]]
        assert "hello-nodejs" in names
        print("[ok] HTTP list installed")

        r4 = client.get("/api/v1/store/plugins/hello-nodejs/package", headers=headers)
        assert r4.status_code == 200, r4.text
        assert r4.content[:2] == b"PK"
        print("[ok] HTTP download zip")

    app.dependency_overrides.clear()
    print("[ok] HTTP layer")


def main() -> None:
    _service_layer()
    _http_layer()
    try:
        DB.unlink(missing_ok=True)
    except OSError:
        pass
    print("[ok] store upload/install/download ALL PASSED")


if __name__ == "__main__":
    main()
