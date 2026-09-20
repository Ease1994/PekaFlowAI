"""现网全功能覆盖：页面、各模块只读面、可回滚写路径、助手主路径。

依赖 LIVE_BASE。不做压测，不改平台设置，不轮换 enroll token。
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request

import pytest

from tests.test_live_platform import LIVE, _call, _data, _ok, _tool, login_admin

pytestmark = pytest.mark.skipif(not LIVE, reason="set LIVE_BASE to run against the test server")

FRONT = (os.environ.get("LIVE_FRONT") or "").rstrip("/") or (
    LIVE.replace("/api/v1", "").replace(":8080", ":8000") if LIVE else ""
)

PAGES = [
    "/",
    "/login",
    "/projects",
    "/releases",
    "/agents",
    "/nodes",
    "/deploy-requests",
    "/artifacts",
    "/skills",
    "/credentials",
    "/ai",
    "/approvals",
    "/notifications",
    "/models",
    "/settings",
    "/users",
    "/permissions",
]


@pytest.fixture(scope="module")
def token() -> str:
    return login_admin()


@pytest.fixture(scope="module")
def catalog(token: str) -> dict:
    projects = _data(token, "/projects")
    pid = projects[0]["id"]
    return {
        "projects": projects,
        "project_id": pid,
        "groups": _data(token, f"/groups?project_id={pid}"),
        "pipelines": _data(token, f"/pipelines?project_id={pid}"),
    }


def _raw(path: str, token: str | None = None, timeout: int = 30):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{LIVE}{path}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type") or "", resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, "", exc.read()


def test_frontend_spa_pages_serve_app() -> None:
    assert FRONT
    for path in PAGES:
        with urllib.request.urlopen(FRONT + path, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
            assert resp.status == 200, path
            assert "root" in html, path


def test_unauthenticated_api_is_rejected() -> None:
    status, payload = _call("GET", "/projects")
    assert status in {401, 403} or payload.get("code") not in {0, None}


def test_read_surfaces_across_modules(token: str, catalog: dict) -> None:
    pid = catalog["project_id"]
    paths = [
        "/auth/me",
        "/projects",
        f"/projects/{pid}",
        "/groups",
        f"/groups?project_id={pid}",
        "/env-kinds",
        "/pipelines",
        f"/pipelines?project_id={pid}",
        "/pipelines/recycle-bin",
        "/releases",
        "/releases/statistics",
        "/repositories",
        f"/repositories?project_id={pid}",
        "/agents",
        "/agents?role=node",
        "/agents?role=builder",
        "/node-groups",
        "/agents/enroll-token",
        "/metrics/dora",
        "/metrics/trend",
        "/notifications",
        "/notifications/unread-count",
        "/notifications/events",
        "/settings",
        "/credentials",
        "/audit-logs",
        "/users",
        "/permissions",
        "/account/users",
        f"/roles?project_id={pid}",
        "/roles/resource-actions",
        "/access/catalog",
        "/access/applications",
        "/approvals/pending",
        "/deploy-requests",
        "/artifacts",
        "/artifacts/summary",
        "/store/plugins",
        "/store/plugin-meta",
        "/store/plugin-drafts",
        "/store/templates",
        "/llm/adapters",
        "/llm/adapters/capabilities",
        "/llm/providers",
        "/llm/models",
        "/llm/routes",
        "/llm/observations",
        "/ai/skills",
        "/ai/tools",
        "/ai/models",
        "/ai/sessions",
        "/ai/conversation",
        "/ai/attachments",
        "/api-tokens",
        "/harness/runtime",
        "/harness/components",
        "/harness/versions",
        "/harness/dependencies",
        "/harness/runtimes",
        "/harness/lifecycle",
        "/harness/events",
        "/harness/skills",
        "/harness/tools",
        "/harness/tools/metrics",
        "/harness/capabilities",
    ]
    failed = []
    for path in paths:
        status, payload = _call("GET", path, token)
        if status != 200 or payload.get("code") not in {0, None}:
            failed.append((path, status, payload.get("message") or payload.get("code")))
    wecom = _call("GET", "/auth/wecom/status", token)
    if wecom[0] not in {200, 404} and wecom[1].get("code") is None:
        failed.append(("/auth/wecom/status", wecom[0], wecom[1].get("message")))
    assert not failed, failed


def test_binary_downloads(token: str) -> None:
    for path, magic in (
        ("/agents/download", b"PK"),
        ("/harness/templates/agent-skill", b"PK"),
        ("/harness/templates/agent-tool", b"PK"),
        ("/store/plugins/template", b"PK"),
    ):
        status, _ctype, body = _raw(path, token)
        assert status == 200, (path, status)
        assert body[:2] == magic, path
    status, _ctype, body = _raw("/agents/install-script", token)
    assert status == 200, status
    assert len(body) > 40


def test_harness_skill_tool_loads_playbook(token: str) -> None:
    tools = _data(token, "/harness/tools")
    names = {item.get("name") for item in tools} if isinstance(tools, list) else set()
    assert "skill" in names
    loaded = _tool(token, "skill", {"name": "rp-release"})
    assert loaded.get("error") is None, loaded
    text = loaded.get("skill_content") or loaded.get("content") or ""
    assert "propose_release" in text
    missing = _tool(token, "skill", {"name": "definitely-missing-skill"})
    assert missing.get("code") == "SKILL_NOT_FOUND"


def test_session_fork_replay_delete(token: str) -> None:
    created = _ok(token, "POST", "/ai/sessions", {"title": f"live-cov-session-{int(time.time())}"})
    sid = created["id"]
    fork_id = None
    try:
        _ok(token, "POST", "/ai/chat", {"message": "你好", "session_id": sid}, timeout=180)
        got = _data(token, f"/ai/sessions/{sid}")
        assert len(got.get("messages") or []) >= 2
        inspect = _data(token, f"/ai/sessions/{sid}/inspect")
        assert inspect
        replay = _ok(token, "POST", f"/ai/sessions/{sid}/replay", {})
        assert replay.get("read_only") is True or "projection" in replay
        forked = _ok(token, "POST", f"/ai/sessions/{sid}/fork", {"title": "live-cov-fork"})
        fork_id = forked["id"]
        assert fork_id != sid
        _ok(token, "POST", f"/ai/sessions/{sid}/resume", {})
        models = _data(token, "/ai/models")
        if models:
            _ok(token, "PATCH", f"/ai/sessions/{sid}", {"model_id": models[0]["id"]})
    finally:
        if fork_id:
            _call("DELETE", f"/ai/sessions/{fork_id}", token)
        status, payload = _call("DELETE", f"/ai/sessions/{sid}", token)
        assert status == 200 and payload.get("code") == 0, ("DELETE session", status, payload)
        listed = _data(token, "/ai/sessions")
        assert sid not in {item["id"] for item in listed}


def test_chat_named_release_and_projects(token: str) -> None:
    created = _ok(token, "POST", "/ai/sessions", {"title": f"live-cov-chat-{int(time.time())}"})
    sid = created["id"]
    try:
        data = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "发布 test-C 流水线", "session_id": sid},
            timeout=180,
        )
        types = {a.get("type") for a in (data.get("actions") or [])}
        assert "confirm_release" in types, data
        listed = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "有哪些项目", "session_id": sid},
            timeout=180,
        )
        reply = listed.get("reply") or ""
        assert reply and "<skill_content" not in reply
        assert "DMS" in reply or "#" in reply or "项目" in reply, reply
    finally:
        _call("DELETE", f"/ai/sessions/{sid}", token)


def test_credential_and_api_token_roundtrip(token: str, catalog: dict) -> None:
    name = f"live-cov-cred-{int(time.time())}"
    created = _ok(
        token,
        "POST",
        "/credentials",
        {
            "name": name,
            "type": "token",
            "secret": "live-coverage-secret",
            "project_id": catalog["project_id"],
            "description": "coverage test",
        },
    )
    cred_id = created["id"]
    try:
        rows = _data(token, f"/credentials?project_id={catalog['project_id']}")
        assert any(item.get("id") == cred_id for item in rows)
        assert "live-coverage-secret" not in str(created)
    finally:
        _ok(token, "DELETE", f"/credentials/{cred_id}")

    tok = _ok(token, "POST", "/api-tokens", {"name": f"live-cov-{int(time.time())}", "days": 1})
    assert tok.get("token") and tok.get("id")
    _ok(token, "DELETE", f"/api-tokens/{tok['id']}")


def test_node_group_roundtrip(token: str) -> None:
    name = f"live-cov-ng-{int(time.time())}"
    created = _ok(token, "POST", "/node-groups", {"name": name, "description": "coverage"})
    gid = created["id"]
    try:
        groups = _data(token, "/node-groups")
        assert any(item.get("id") == gid for item in groups)
    finally:
        _ok(token, "DELETE", f"/node-groups/{gid}")


def test_pipeline_star_execute_cancel(token: str, catalog: dict) -> None:
    groups = [g for g in catalog["groups"] if str(g.get("type") or "") == "test"]
    assert groups, "没有测试分组"
    name = f"live-cov-run-{int(time.time())}"
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {
            "project_id": catalog["project_id"],
            "group_id": groups[0]["id"],
            "name": name,
            "yaml": (
                "pipeline:\n  name: live-cov-run\n  triggers:\n    - type: manual\n"
                "  stages:\n    - name: s1\n      jobs:\n        - name: j1\n"
                "          agent: windows\n          steps:\n            - name: echo\n"
                "              plugin: shell\n              with:\n                script: echo live-cov\n"
            ),
        },
    )
    pipe_id = created["id"]
    try:
        _ok(token, "PUT", f"/pipelines/{pipe_id}/pref", {"starred": True})
        _data(token, f"/pipelines/{pipe_id}/graph")
        ran = _ok(token, "POST", f"/pipelines/{pipe_id}/execute", {})
        release_id = ran.get("id") or ran.get("release_id")
        assert release_id, ran
        detail = _data(token, f"/releases/{release_id}")
        _data(token, f"/releases/{release_id}/sequence")
        _call("GET", f"/releases/{release_id}/logs", token)
        status = str(detail.get("status") or (detail.get("release") or {}).get("status") or "")
        if status in {"pending", "queued", "running"}:
            _ok(token, "POST", f"/releases/{release_id}/cancel", {})
    finally:
        _call("DELETE", f"/pipelines/{pipe_id}", token)
        _call("DELETE", f"/pipelines/{pipe_id}/purge", token)


def test_deploy_request_roundtrip(token: str, catalog: dict) -> None:
    """发布计划可挂任意可见流水线；增量线才强制清单。"""
    pipes = _data(token, f"/pipelines?project_id={catalog['project_id']}&deploy_manifest=true")
    pipe = (pipes or [None])[0]
    if not pipe:
        pytest.skip("没有按清单增量发布的流水线")
    created = _ok(
        token,
        "POST",
        "/deploy-requests",
        {
            "project_id": catalog["project_id"],
            "pipeline_id": pipe["id"],
            "title": f"live-cov-{int(time.time())}",
            "changelog": "coverage test",
            "manifest": "app.dll",
        },
    )
    rid = created["id"]
    try:
        _data(token, f"/deploy-requests/{rid}")
        _ok(token, "POST", f"/deploy-requests/{rid}/reject", {"reason": "live coverage cleanup"})
    except Exception:
        _call("POST", f"/deploy-requests/{rid}/reject", token, {"reason": "live coverage cleanup"})
        raise


def test_notification_and_logout(token: str) -> None:
    notices = _data(token, "/notifications")
    items = notices if isinstance(notices, list) else (notices.get("items") or notices.get("list") or [])
    if items:
        nid = items[0].get("id")
        if nid:
            _call("GET", f"/notifications/{nid}", token)
            _call("POST", f"/notifications/{nid}/read", token, {})
    _ok(token, "POST", "/auth/logout", {})


def test_release_detail_surfaces(token: str) -> None:
    releases = _data(token, "/releases")
    items = releases if isinstance(releases, list) else (releases.get("items") or releases.get("list") or [])
    if not items:
        pytest.skip("没有发布记录")
    rid = items[0]["id"]
    _data(token, f"/releases/{rid}")
    _data(token, f"/releases/{rid}/sequence")
    _data(token, f"/tasks?release_id={rid}")
    _call("GET", f"/releases/{rid}/commits", token)
    preview = _call("GET", f"/releases/{rid}/rollback-preview", token)
    assert preview[0] == 200


def test_llm_model_test_endpoint(token: str) -> None:
    models = _data(token, "/llm/models")
    usable = [m for m in models if m.get("enabled") and m.get("id")]
    if not usable:
        pytest.skip("没有启用的模型")
    status, payload = _call("POST", f"/llm/models/{usable[0]['id']}/test", token, {}, timeout=60)
    assert status in {200, 400} or payload.get("code") is not None
