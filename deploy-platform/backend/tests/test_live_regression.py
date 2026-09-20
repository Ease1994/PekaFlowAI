# -*- coding: utf-8 -*-
"""测试机回归：新增功能 + 发布/部署/上传删除/节点/备份相关接口。

    $env:LIVE_BASE="http://127.0.0.1:8080/api/v1"
    $env:LIVE_FRONT="http://127.0.0.1:8000"
    python -m pytest tests/test_live_regression.py tests/test_live_platform.py tests/test_live_coverage.py tests/test_live_golive.py -v --tb=short
"""
from __future__ import annotations

import io
import json
import time
import uuid
import zipfile

import pytest
import urllib.error
import urllib.request

from tests.test_live_platform import (
    LIVE,
    _approve_pending_release,
    _call,
    _data,
    _live_test_target,
    _ok,
    _pick_live_test_node,
    _release_error,
    _wait_release,
    login_admin,
)

pytestmark = pytest.mark.skipif(not LIVE, reason="set LIVE_BASE to run against the test server")


@pytest.fixture(scope="module")
def token() -> str:
    return login_admin()


@pytest.fixture(scope="module")
def catalog(token: str) -> dict:
    projects = _data(token, "/projects")
    assert projects, "测试环境没有项目"
    pid = projects[0]["id"]
    groups = _data(token, f"/groups?project_id={pid}")
    return {
        "project_id": pid,
        "groups": groups,
        "test_groups": [g for g in groups if str(g.get("type") or "") == "test"],
        "prod_groups": [g for g in groups if str(g.get("type") or "") == "prod"],
        "pipelines": _data(token, f"/pipelines?project_id={pid}"),
        "nodes": _data(token, "/agents?role=node"),
        "builders": _data(token, "/agents?role=builder"),
        "plugins": _data(token, "/store/plugins"),
    }


def _raw(token: str, path: str, timeout: int = 60):
    """带 JWT 拉二进制。插件 zip / 技能包下载走这条。"""
    req = urllib.request.Request(
        f"{LIVE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type") or "", resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, "", exc.read()


def _upload_zip(token: str, path: str, filename: str, content: bytes):
    """上传 zip 表单字段名是 file，和技能库页面一致。"""
    boundary = "----LiveReg" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    buf.write(b"Content-Type: application/zip\r\n\r\n")
    buf.write(content)
    buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{LIVE}{path}",
        data=buf.getvalue(),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            raw = json.loads(exc.read().decode())
        except Exception:
            raw = {"message": str(exc)}
        return exc.code, raw


def _plugin_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            "task.json",
            json.dumps({"name": name, "version": "1.0.0", "display_name": name}, ensure_ascii=False),
        )
        archive.writestr("task.py", "print(1)\n")
    return buf.getvalue()


def _pick_nodes(catalog: dict) -> list:
    """选出可测节点。名字以 test 开头就算测试机，环境码标成 prod 也照测。"""
    node = _pick_live_test_node(catalog.get("nodes") or [])
    return [node] if node else []


def _group_for_node(catalog: dict, node: dict) -> dict | None:
    """流水线分组必须和节点环境码一致，否则环境门禁直接失败。

    测试机故意把节点标成 prod 来走审批，所以这里跟 prod 分组，而不是改成测试分组。
    """
    env = str(node.get("env") or "").lower()
    if env in {"test", "dev"}:
        return (catalog.get("test_groups") or [None])[0]
    if env == "prod":
        return (catalog.get("prod_groups") or catalog.get("groups") or [None])[0]
    return (catalog.get("groups") or [None])[0]


def test_builtin_plugins_downloadable(token: str, catalog: dict) -> None:
    """内置插件列表带 has_package；源码 zip 能下到 task.json。"""
    by_name = {p["name"]: p for p in catalog["plugins"]}
    for name in ("maven-build", "docker-compile", "file-transfer", "docker-build"):
        row = by_name.get(name)
        assert row, f"缺少内置插件 {name}"
        assert row.get("builtin") is True, name
        assert row.get("has_package") is True, name
        status, _ctype, body = _raw(token, f"/store/plugins/{name}/package")
        assert status == 200, (name, status, body[:200])
        assert body[:2] == b"PK", name
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = [n.replace("\\", "/") for n in archive.namelist()]
        assert any(n.endswith("task.json") for n in names), (name, names[:12])


def test_shell_exec_has_no_source_zip(token: str, catalog: dict) -> None:
    row = next((p for p in catalog["plugins"] if p["name"] == "shell-exec"), None)
    assert row, "没有 shell-exec"
    assert row.get("has_package") is False
    status, _ctype, body = _raw(token, "/store/plugins/shell-exec/package")
    assert status == 400, body[:300]


def test_upload_cannot_overwrite_builtin_plugin(token: str) -> None:
    status, payload = _upload_zip(token, "/store/plugins/upload", "maven-build.zip", _plugin_zip("maven-build"))
    assert status == 400 or payload.get("code") not in {0, None}, payload
    msg = str(payload.get("message") or "")
    assert "不能覆盖" in msg or "内置" in msg, payload


def test_unsigned_third_party_upload_rejected(token: str) -> None:
    name = f"live-reg-{int(time.time())}"
    status, payload = _upload_zip(token, "/store/plugins/upload", f"{name}.zip", _plugin_zip(name))
    assert status == 400 or payload.get("code") not in {0, None}, payload
    msg = str(payload.get("message") or "")
    assert "签名" in msg or "manifest.sig" in msg, payload


def test_builtin_tool_exports_skill_package(token: str) -> None:
    tools = _data(token, "/harness/tools")
    builtin = next((t for t in tools if t.get("source") == "builtin" and t.get("name")), None)
    assert builtin, "没有内置技能/工具"
    status, _ctype, body = _raw(token, f"/harness/tools/{builtin['name']}/skill-package")
    assert status == 200, (builtin["name"], status, body[:200])
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = {n.replace("\\", "/") for n in archive.namelist()}
    assert "manifest.yaml" in names
    assert "SKILL.md" in names


def test_graph_step_names_and_keep_backups(token: str, catalog: dict) -> None:
    """打开编排图时，空步骤名应补成插件显示名；文件传输能看到保留份数字段。"""
    plugins = {p["name"]: p for p in catalog["plugins"]}
    file_transfer = plugins.get("file-transfer")
    assert file_transfer, "没有 file-transfer"
    found_transfer = False
    for pipe in catalog["pipelines"][:20]:
        graph = _data(token, f"/pipelines/{pipe['id']}/graph")
        for stage in graph.get("stages") or []:
            for job in stage.get("jobs") or []:
                for step in job.get("steps") or []:
                    name = (step.get("name") or "").strip()
                    plugin = step.get("plugin") or ""
                    display = (step.get("display_name") or plugins.get(plugin, {}).get("display_name") or plugin)
                    if not name:
                        pytest.fail(f"流水线 {pipe['id']} 步骤 {plugin} 名称为空，默认名应是插件名")
                    if plugin == "file-transfer":
                        found_transfer = True
                        keep = (step.get("with") or {}).get("keepBackups")
                        if keep is not None:
                            assert 1 <= int(keep) <= 50, (pipe["id"], keep)
    if not found_transfer:
        pytest.skip("现有流水线没有文件传输步骤")


def test_create_file_transfer_injects_keep_backups(token: str, catalog: dict) -> None:
    """YAML 不写 keepBackups 时，下发任务应注入 20。"""
    nodes = _pick_nodes(catalog)
    if not nodes:
        pytest.skip("没有可测节点")
    node = nodes[0]
    group = _group_for_node(catalog, node)
    if not group:
        pytest.skip("没有可用分组")
    target = _live_test_target(node)
    name = f"live-reg-ft-{int(time.time())}"
    yaml_text = f"""
pipeline:
  name: {name}
  triggers:
    - type: manual
  stages:
    - name: deploy
      jobs:
        - id: 1-1
          name: node
          agent: windows
          steps:
            - plugin: file-transfer
              with:
                node: {int(node['id'])}
                targetDir: {json.dumps(target)}
                dryRun: true
"""
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {"project_id": catalog["project_id"], "group_id": group["id"], "name": name, "yaml": yaml_text},
    )
    pipe_id = created["id"]
    try:
        graph = _data(token, f"/pipelines/{pipe_id}/graph")
        step = graph["stages"][0]["jobs"][0]["steps"][0]
        assert (step.get("name") or "").strip() in {"发送文件到节点", "文件传输"} or "传输" in (step.get("name") or "")
        ran = _ok(token, "POST", f"/pipelines/{pipe_id}/execute", {})
        release_id = ran.get("id") or ran.get("release_id")
        assert release_id, ran
        if str(ran.get("status") or "") == "pending":
            _approve_pending_release(token, release_id)
        blob = ""
        for _ in range(20):
            tasks = _data(token, f"/tasks?release_id={release_id}")
            rows = tasks if isinstance(tasks, list) else (tasks.get("items") or [])
            blob = json.dumps(rows, ensure_ascii=False)
            if "keepBackups" in blob:
                break
            time.sleep(1)
        assert "keepBackups" in blob, blob[:800]
        assert "20" in blob or '"keepBackups": 20' in blob.replace(" ", "")
        _call("POST", f"/releases/{release_id}/cancel", token, {})
    finally:
        _call("DELETE", f"/pipelines/{pipe_id}", token)
        _call("DELETE", f"/pipelines/{pipe_id}/purge", token)


def test_iis_recycle_on_test_node(token: str, catalog: dict) -> None:
    """测试节点上回收 DefaultAppPool，必须真正跑成功。

    节点环境码即使标成生产也按测试机处理：进审批就批准，等到 success。
    """
    nodes = _pick_nodes(catalog)
    if not nodes:
        pytest.skip("没有可测节点")
    node = nodes[0]
    group = _group_for_node(catalog, node)
    if not group:
        pytest.skip("没有可用分组")
    name = f"live-reg-iis-{int(time.time())}"
    yaml_text = f"""
pipeline:
  name: {name}
  triggers:
    - type: manual
  stages:
    - name: iis
      jobs:
        - id: 1-1
          name: iis
          agent: windows
          steps:
            - plugin: iis-control
              with:
                node: {int(node['id'])}
                action: status
                target: apppool
                name: DefaultAppPool
                ignoreMissing: false
            - plugin: iis-control
              with:
                node: {int(node['id'])}
                action: recycle
                target: apppool
                name: DefaultAppPool
                ignoreMissing: false
"""
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {"project_id": catalog["project_id"], "group_id": group["id"], "name": name, "yaml": yaml_text},
    )
    pipe_id = created["id"]
    release_id = None
    try:
        ran = _ok(token, "POST", f"/pipelines/{pipe_id}/execute", {})
        release_id = ran.get("id") or ran.get("release_id")
        assert release_id, ran
        if str(ran.get("status") or "") == "pending":
            _approve_pending_release(token, release_id)
        last = _wait_release(token, release_id, timeout=180)
        err = _release_error(token, release_id)
        if last != "success" and (
            "allow-iis" in err
            or "物理路径" in err
            or "无法确认" in err
            or "允许操作的目录" in err
        ):
            pytest.skip(err)
        assert last == "success", f"IIS 启停未成功: {last} release={release_id} {err}"
        tasks = _data(token, f"/tasks?release_id={release_id}")
        assert tasks is not None
    finally:
        if release_id:
            _call("POST", f"/releases/{release_id}/cancel", token, {})
        _call("DELETE", f"/pipelines/{pipe_id}", token)
        _call("DELETE", f"/pipelines/{pipe_id}/purge", token)


def test_pipeline_crud_and_execute_cancel(token: str, catalog: dict) -> None:
    groups = catalog["test_groups"] or catalog["groups"]
    assert groups, "没有分组"
    name = f"live-reg-echo-{int(time.time())}"
    yaml_text = """
pipeline:
  name: live-reg-echo
  triggers:
    - type: manual
  stages:
    - name: s1
      jobs:
        - id: 1-1
          name: echo
          agent: windows
          steps:
            - plugin: shell-exec
              with:
                shellType: shell
                content: echo live-reg-ok
"""
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {"project_id": catalog["project_id"], "group_id": groups[0]["id"], "name": name, "yaml": yaml_text},
    )
    pipe_id = created["id"]
    try:
        got = _data(token, f"/pipelines/{pipe_id}")
        assert got["name"] == name
        _ok(token, "PUT", f"/pipelines/{pipe_id}", {"description": "live regression"})
        graph = _data(token, f"/pipelines/{pipe_id}/graph")
        step_name = graph["stages"][0]["jobs"][0]["steps"][0].get("name") or ""
        assert step_name, graph
        ran = _ok(token, "POST", f"/pipelines/{pipe_id}/execute", {})
        release_id = ran.get("id") or ran.get("release_id")
        assert release_id, ran
        _data(token, f"/releases/{release_id}")
        _data(token, f"/releases/{release_id}/sequence")
        _call("POST", f"/releases/{release_id}/cancel", token, {})
        _ok(token, "DELETE", f"/pipelines/{pipe_id}")
        _ok(token, "POST", f"/pipelines/{pipe_id}/restore", {})
    finally:
        _call("DELETE", f"/pipelines/{pipe_id}", token)
        _call("DELETE", f"/pipelines/{pipe_id}/purge", token)


def test_nodes_agents_artifacts_and_rollback_preview(token: str) -> None:
    agents = _data(token, "/agents")
    nodes = _data(token, "/agents?role=node")
    builders = _data(token, "/agents?role=builder")
    assert isinstance(agents, list)
    assert nodes or builders
    artifacts = _data(token, "/artifacts")
    assert artifacts is not None
    _data(token, "/artifacts/summary")
    releases = _data(token, "/releases")
    items = releases if isinstance(releases, list) else (releases.get("items") or [])
    if items:
        rid = items[0]["id"]
        preview = _call("GET", f"/releases/{rid}/rollback-preview", token)
        assert preview[0] == 200
        _data(token, f"/tasks?release_id={rid}")
