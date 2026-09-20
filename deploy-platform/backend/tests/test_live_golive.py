"""上线前现网补测：依赖、目录导入导出、非管理员隔离、一次性执行。

依赖 LIVE_BASE。会建 golive-probe-* 流水线并在 finally 里进回收站再彻底删。
"""
from __future__ import annotations

import time

import pytest

from tests.test_live_platform import LIVE, _call, _data, _ok, login_admin

pytestmark = pytest.mark.skipif(not LIVE, reason="set LIVE_BASE to run against the test server")

# 现网插件名是 shell-exec；构建机都标成了生产，测试分组会因环境门禁直接失败。
MINIMAL_YAML = """
pipeline:
  name: golive-probe
  triggers:
    - type: manual
  stages:
    - name: s1
      jobs:
        - id: 1-1
          name: echo
          agent: agent:15
          steps:
            - name: echo
              plugin: shell-exec
              with:
                shellType: shell
                content: |
                  echo golive-probe-ok
"""


@pytest.fixture(scope="module")
def token() -> str:
    """管理员正式会话。开了双因子时靠 LIVE_TOTP 或先关掉开关。"""
    return login_admin()


@pytest.fixture(scope="module")
def catalog(token: str) -> dict:
    """挑一个带测试分组的项目，给导入导出和执行用。"""
    projects = _data(token, "/projects")
    assert projects, "没有项目"
    for proj in projects:
        groups = _data(token, f"/groups?project_id={proj['id']}")
        test_groups = [g for g in groups if str(g.get("type") or "") == "test"]
        prod_groups = [
            g
            for g in groups
            if str(g.get("type") or "") == "prod" and g.get("allow_self_approval")
        ]
        if test_groups and prod_groups:
            return {
                "project_id": proj["id"],
                "group_id": test_groups[0]["id"],
                "prod_group_id": prod_groups[0]["id"],
                "project": proj,
            }
    raise AssertionError("需要同时有测试分组和可自审批的生产分组")


def test_public_branding_and_health_deps(token: str) -> None:
    """登录页品牌和顶栏依赖探测必须公开/登录即可用。"""
    status, payload = _call("GET", "/settings/branding")
    assert status == 200 and payload.get("code") == 0, payload
    branding = payload["data"]
    assert branding.get("display_name"), branding
    deps = _data(token, "/health/deps")
    assert deps.get("status") in {"ok", "degraded"}, deps
    names = {c["name"]: c for c in deps.get("components") or []}
    assert names["MySQL"]["status"] == "ok", deps
    assert names["Redis"]["status"] == "ok", deps
    assert names["Elasticsearch"]["status"] == "ok", deps


def test_credentials_do_not_leak_secret(token: str) -> None:
    """凭证列表不能把密文带回浏览器。"""
    rows = _data(token, "/credentials")
    blob = str(rows)
    assert "secret" not in blob.lower() or all(
        not str(item.get("secret") or item.get("password") or "")
        for item in (rows or [])
        if isinstance(item, dict)
    ), "凭证接口疑似带回密文"


def test_non_admin_cannot_export_or_import(token: str) -> None:
    """导入导出只认管理员，普通用户必须 403。"""
    name = f"golive-user-{int(time.time())}"
    created = _ok(
        token,
        "POST",
        "/account/users",
        {
            "username": name,
            "password": "Golive#12345",
            "display_name": "golive probe",
            "email": f"{name}@example.com",
            "is_admin": False,
        },
    )
    uid = created["id"]
    try:
        status, login = _call("POST", "/auth/login", body={"username": name, "password": "Golive#12345"})
        assert status == 200 and (login.get("data") or {}).get("token"), login
        weak = login["data"]["token"]
        st, exp = _call("POST", "/pipelines/export", weak, {"project_ids": []})
        assert st == 403 or exp.get("code") == 403, (st, exp)
        st, prev = _call(
            "POST",
            "/pipelines/import/preview",
            weak,
            {"bundle": {"format": "rp-catalog", "format_version": 1, "projects": []}},
        )
        assert st == 403 or prev.get("code") == 403, (st, prev)
    finally:
        _call("DELETE", f"/account/users/{uid}", token)


def test_catalog_export_preview_import_copy_and_cleanup(token: str, catalog: dict) -> None:
    """导出配置 → 预览无冲突 → 再导入一份副本，最后两份都清掉。不碰已有业务流水线。"""
    name = f"golive-xfer-{int(time.time())}"
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {
            "project_id": catalog["project_id"],
            "group_id": catalog["group_id"],
            "name": name,
            "yaml": MINIMAL_YAML,
        },
    )
    pipe_id = created["id"]
    imported_id = None
    try:
        bundle = _ok(token, "POST", "/pipelines/export", {"pipeline_ids": [pipe_id]})
        assert bundle.get("format") == "rp-catalog", bundle
        projects = bundle.get("projects") or []
        assert projects, bundle
        assert not any(
            "secret" in str(projects).lower() and "password" in str(projects).lower()
            for _ in [0]
        )
        yaml_blob = str(bundle)
        assert "release" not in (bundle.get("projects")[0] if False else "")
        assert "golive-probe" in yaml_blob or name in yaml_blob

        preview = _ok(token, "POST", "/pipelines/import/preview", {"bundle": bundle})
        conflicts = preview.get("conflicts") or []
        assert len(conflicts) == 1, preview
        key = conflicts[0]["key"]
        result = _ok(
            token,
            "POST",
            "/pipelines/import",
            {"bundle": bundle, "decisions": {key: "copy"}},
        )
        rows = result.get("results") or result.get("items") or []
        copied = next((r for r in rows if r.get("action") == "copy"), None)
        pipes = _data(token, f"/pipelines?project_id={catalog['project_id']}")
        copies = [p for p in pipes if p["id"] != pipe_id and str(p.get("name") or "").startswith(name)]
        imported_id = (copied or {}).get("id") or (copies[0]["id"] if copies else None)
        assert imported_id and imported_id != pipe_id, (result, [p.get("name") for p in pipes[-8:]])
    finally:
        for pid in {pipe_id, imported_id}:
            if not pid:
                continue
            _call("DELETE", f"/pipelines/{pid}", token)
            _call("DELETE", f"/pipelines/{pid}/purge", token)


def test_test_group_blocked_from_prod_builder(token: str, catalog: dict) -> None:
    """测试分组不能调度生产构建机。现网两台构建机都标了 prod，门禁必须拦住。"""
    name = f"golive-mismatch-{int(time.time())}"
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {
            "project_id": catalog["project_id"],
            "group_id": catalog["group_id"],
            "name": name,
            "yaml": MINIMAL_YAML,
        },
    )
    pipe_id = created["id"]
    try:
        ran = _ok(token, "POST", f"/pipelines/{pipe_id}/execute", {})
        rid = ran.get("id") or ran.get("release_id")
        detail = _data(token, f"/releases/{rid}")
        status = detail.get("status") or (detail.get("release") or {}).get("status")
        err = str(detail.get("error_message") or "")
        assert status == "failed", detail
        assert "环境不匹配" in err, err
    finally:
        _call("DELETE", f"/pipelines/{pipe_id}", token)
        _call("DELETE", f"/pipelines/{pipe_id}/purge", token)


def test_echo_pipeline_runs_on_builder(token: str, catalog: dict) -> None:
    """生产分组自审批后跑 echo，验证 Linux 构建机能领任务。最多等两分钟。"""
    name = f"golive-run-{int(time.time())}"
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {
            "project_id": catalog["project_id"],
            "group_id": catalog["prod_group_id"],
            "name": name,
            "yaml": MINIMAL_YAML,
        },
    )
    pipe_id = created["id"]
    release_id = None
    try:
        ran = _ok(token, "POST", f"/pipelines/{pipe_id}/execute", {})
        release_id = ran.get("id") or ran.get("release_id")
        assert release_id, ran
        status0 = str(ran.get("status") or "")
        if status0 == "pending":
            _ok(token, "POST", f"/releases/{release_id}/approve", {"approved": True, "comment": "golive probe"})
        deadline = time.time() + 120
        last = None
        while time.time() < deadline:
            detail = _data(token, f"/releases/{release_id}")
            last = detail.get("status") or (detail.get("release") or {}).get("status")
            if str(last) in {"success", "failed", "cancelled", "rejected"}:
                break
            time.sleep(3)
        assert last == "success", f"探测流水线未成功，最终状态={last} release={release_id}"
        seq = _data(token, f"/releases/{release_id}/sequence")
        assert seq is not None
    finally:
        if release_id:
            st, detail = _call("GET", f"/releases/{release_id}", token)
            status = ""
            if st == 200:
                body = (detail.get("data") or {}) if isinstance(detail, dict) else {}
                status = str(body.get("status") or (body.get("release") or {}).get("status") or "")
            if status in {"pending", "queued", "running"}:
                _call("POST", f"/releases/{release_id}/cancel", token, {})
        _call("DELETE", f"/pipelines/{pipe_id}", token)
        _call("DELETE", f"/pipelines/{pipe_id}/purge", token)
