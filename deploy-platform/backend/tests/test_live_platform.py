"""对已部署环境做全功能测试（不做压测）。

默认跳过，避免 CI 依赖外网环境。本地跑：
    $env:LIVE_BASE="http://127.0.0.1:8080/api/v1"
    python -m pytest tests/test_live_platform.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from io import BytesIO

import pytest

LIVE = (os.environ.get("LIVE_BASE") or "").rstrip("/")
pytestmark = pytest.mark.skipif(not LIVE, reason="set LIVE_BASE to run against the test server")


def login_admin() -> str:
    """拿管理员正式会话。测试机开了双因子时要 LIVE_TOTP，或先关掉平台双因子开关。"""
    status, payload = _call("POST", "/auth/login", body={"username": "admin", "password": "admin123"})
    assert status == 200 and payload.get("code") == 0, payload
    data = payload.get("data") or {}
    token = data.get("token") or ""
    if data.get("status") in {"totp_required", "totp_setup"} or not token:
        code = (os.environ.get("LIVE_TOTP") or "").strip()
        pending = data.get("pending_token") or ""
        if not code or not pending:
            pytest.fail(
                "测试机开启了双因子，密码登录没有正式 token。"
                "请临时关闭「平台设置 → 双因子」，或设置环境变量 LIVE_TOTP=当前 6 位验证码后再跑。"
            )
        status, payload = _call(
            "POST", "/auth/login/totp", body={"pending_token": pending, "code": code}
        )
        assert status == 200 and payload.get("code") == 0, payload
        data = payload.get("data") or {}
        token = data.get("token") or ""
    assert token, payload
    return token

WAIT_MARKERS = ("正在查询", "稍等", "马上为您", "请稍候", "帮你查")
MINIMAL_YAML = """
pipeline:
  name: live-func-test
  triggers:
    - type: manual
  stages:
    - name: s1
      jobs:
        - name: j1
          agent: windows
          steps:
            - name: echo
              plugin: shell
              with:
                script: echo live-func-test
"""
DRAFT_TASK_PY = (
    "import release_atom_sdk as sdk\n"
    "params = sdk.get_input()\n"
    "sdk.log.info('live functional test plugin')\n"
    "sdk.set_output('ok', '1')\n"
)


def _call(method: str, path: str, token: str | None = None, body=None, timeout: int = 30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{LIVE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode())
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        try:
            raw = json.loads(exc.read().decode())
        except Exception:
            raw = {"message": str(exc)}
        return exc.code, raw


def _upload(token: str, filename: str, content: bytes, timeout: int = 30):
    boundary = "----LiveFunc" + uuid.uuid4().hex
    body = BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode())
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(content)
    body.write(b"\r\n")
    body.write(f"--{boundary}\r\n".encode())
    body.write(b'Content-Disposition: form-data; name="rel_paths"\r\n\r\n')
    body.write(filename.encode())
    body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    data = body.getvalue()
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(f"{LIVE}/ai/attachments", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            raw = json.loads(exc.read().decode())
        except Exception:
            raw = {"message": str(exc)}
        return exc.code, raw


@pytest.fixture(scope="module")
def token() -> str:
    return login_admin()


@pytest.fixture(scope="module")
def catalog(token: str) -> dict:
    projects = _data(token, "/projects")
    assert projects, "测试环境没有项目"
    pid = projects[0]["id"]
    groups = _data(token, f"/groups?project_id={pid}")
    pipes = _data(token, f"/pipelines?project_id={pid}")
    repos = _data(token, f"/repositories?project_id={pid}")
    nodes = _tool(token, "list_push_nodes").get("nodes") or []
    return {
        "projects": projects,
        "project_id": pid,
        "groups": groups,
        "pipelines": pipes,
        "repos": repos,
        "nodes": nodes,
    }


def _data(token: str, path: str):
    status, payload = _call("GET", path, token)
    assert status == 200 and payload.get("code") == 0, (path, payload)
    return payload["data"]


def _ok(token: str, method: str, path: str, body=None, timeout: int = 30):
    status, payload = _call(method, path, token, body, timeout=timeout)
    assert status == 200 and payload.get("code") == 0, (method, path, payload)
    return payload.get("data")


def _tool(token: str, name: str, params: dict | None = None) -> dict:
    data = _ok(token, "POST", "/ai/tool", {"tool": name, "params": params or {}})
    assert isinstance(data, dict), (name, data)
    return data


def _wait_release(token: str, release_id: int, timeout: int = 180) -> str:
    """轮询发布单直到终态。

    实现：每 3 秒拉一次 /releases/{id}，success/failed/cancelled/rejected 即停。
    超时返回最后一次看到的状态，由调用方断言。
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        detail = _data(token, f"/releases/{release_id}")
        last = str(detail.get("status") or (detail.get("release") or {}).get("status") or "")
        if last in {"success", "failed", "cancelled", "rejected"}:
            return last
        time.sleep(3)
    return last


def _pick_live_test_node(nodes: list) -> dict | None:
    """挑一台可落盘的测试节点。

    测试机常把节点环境码故意标成 prod，只为走生产审批链路。名字以 test 开头、
    或 env 为 test/dev 的都按测试环境对待，可以真实写盘。白名单目录带 test 的优先，
    避免铺到站点根。
    """
    with_paths = [n for n in nodes if n.get("allow_paths")]
    env_hit = [n for n in with_paths if str(n.get("env") or "").lower() in {"test", "dev"}]
    named = [n for n in with_paths if str(n.get("name") or "").lower().startswith("test")]
    picked = env_hit or named
    if not picked:
        return None
    preferred = [
        n
        for n in picked
        if any("test" in str(p).lower() for p in (n.get("allow_paths") or []))
    ]
    return (preferred or picked)[0]


def _live_test_target(node: dict, subdir: str = "") -> str:
    """测试节点落盘目录。

    实现：白名单里带 test 的路径优先。节点不会自动建子目录，默认就写已存在的白名单根；
    需要子目录时由调用方传入，且目录必须事先存在。
    """
    paths = node.get("allow_paths") or ["D:\\\\wwwroot"]
    root = str(next((p for p in paths if "test" in str(p).lower()), paths[0])).rstrip("\\/")
    if subdir:
        return root + "\\" + subdir
    return root


def _release_error(token: str, release_id: int) -> str:
    """发布单上的短失败原因。"""
    detail = _data(token, f"/releases/{release_id}")
    return str(detail.get("error_message") or (detail.get("release") or {}).get("error_message") or "")


def _approve_pending_release(token: str, release_id: int) -> None:
    """生产标签节点会进审批；测试机标签是假的，这里一律批准让它真正下发。"""
    pending = _data(token, "/approvals/pending")
    rows = pending if isinstance(pending, list) else (pending.get("items") or pending.get("list") or [])
    hit = next((r for r in rows if r.get("release_id") == release_id), None)
    if hit:
        _ok(
            token,
            "POST",
            f"/approvals/{hit['id']}/decide",
            {"approved": True, "comment": "live functional test，测试节点真实落盘"},
            timeout=60,
        )
        return
    _ok(
        token,
        "POST",
        f"/releases/{release_id}/approve",
        {"approved": True, "comment": "live functional test"},
        timeout=60,
    )


def _release_items(releases) -> list:
    if isinstance(releases, list):
        return releases
    if isinstance(releases, dict):
        return releases.get("items") or releases.get("list") or []
    return []


def test_health_and_login_rejects_bad_password() -> None:
    status, payload = _call("GET", "/health")
    assert status == 200 and payload.get("code") == 0, payload
    assert (payload.get("data") or {}).get("status") == "up"
    status, payload = _call("POST", "/auth/login", body={"username": "admin", "password": "wrong"})
    assert status in {400, 401, 403} or payload.get("code") not in {0, None}


def test_me_and_catalog(token: str, catalog: dict) -> None:
    me = _data(token, "/auth/me")
    assert me.get("username") == "admin" or me.get("is_admin") is True
    assert catalog["projects"]
    assert catalog["project_id"]
    plugins = _data(token, "/store/plugins")
    assert isinstance(plugins, list) and plugins, "预置插件丢失"
    kinds = _data(token, "/env-kinds")
    assert kinds


def test_project_detail_and_pipeline_surfaces(token: str, catalog: dict) -> None:
    pid = catalog["project_id"]
    detail = _data(token, f"/projects/{pid}")
    assert detail.get("id") == pid or detail.get("name")
    pipes = catalog["pipelines"]
    assert pipes, "测试环境没有流水线，编排/执行页会空"
    pipe_id = pipes[0]["id"]
    info = _data(token, f"/pipelines/{pipe_id}")
    assert info.get("id") == pipe_id
    graph = _data(token, f"/pipelines/{pipe_id}/graph")
    assert graph is not None
    params = _data(token, f"/pipelines/{pipe_id}/start-params")
    assert params is not None
    nxt = _data(token, f"/pipelines/{pipe_id}/next-run")
    assert nxt is not None
    stats = _data(token, "/releases/statistics")
    assert stats is not None
    releases = _data(token, "/releases")
    assert releases is not None
    recycle = _data(token, "/pipelines/recycle-bin")
    assert recycle is not None


def test_agents_nodes_metrics_notifications(token: str) -> None:
    agents = _data(token, "/agents")
    assert isinstance(agents, list)
    nodes = _data(token, "/agents?role=node")
    assert isinstance(nodes, list)
    groups = _data(token, "/node-groups")
    assert isinstance(groups, list)
    dora = _data(token, "/metrics/dora")
    assert isinstance(dora, dict)
    trend = _data(token, "/metrics/trend")
    assert trend is not None
    notices = _data(token, "/notifications")
    assert notices is not None
    unread = _data(token, "/notifications/unread-count")
    assert unread is not None
    events = _data(token, "/notifications/events")
    assert events is not None
    settings = _data(token, "/settings")
    assert isinstance(settings, dict)
    creds = _data(token, "/credentials")
    assert isinstance(creds, list)
    harness = _data(token, "/harness/runtime")
    assert isinstance(harness, dict)
    enroll = _data(token, "/agents/enroll-token")
    assert enroll is not None
    logs = _data(token, "/audit-logs")
    assert logs is not None
    releases = _data(token, "/releases")
    items = _release_items(releases)
    if items:
        rid = items[0]["id"]
        tasks = _data(token, f"/tasks?release_id={rid}")
        assert tasks is not None
        detail = _data(token, f"/releases/{rid}")
        assert detail.get("id") == rid or (detail.get("release") or {}).get("id") == rid
        _data(token, f"/releases/{rid}/sequence")
        _data(token, f"/releases/{rid}/rollback-preview")


def test_admin_access_store_llm(token: str, catalog: dict) -> None:
    users = _data(token, "/users")
    assert isinstance(users, list) and users
    perms = _data(token, "/permissions")
    assert perms is not None
    roles = _data(token, f"/roles?project_id={catalog['project_id']}")
    assert roles is not None
    actions = _data(token, "/roles/resource-actions")
    assert actions is not None
    accounts = _data(token, "/account/users")
    assert isinstance(accounts, list) and accounts
    meta = _data(token, "/store/plugin-meta")
    assert meta is not None
    caps = _data(token, "/llm/adapters/capabilities")
    assert caps is not None
    pending = _data(token, "/approvals/pending")
    assert pending is not None
    access = _data(token, "/access/applications")
    assert access is not None
    catalog = _data(token, "/access/catalog")
    assert catalog is not None
    templates = _data(token, "/store/templates")
    assert templates is not None
    drafts = _data(token, "/store/plugin-drafts")
    assert drafts is not None
    models = _data(token, "/llm/models")
    assert models is not None
    providers = _data(token, "/llm/providers")
    assert providers is not None
    adapters = _data(token, "/llm/adapters")
    assert adapters is not None
    routes = _data(token, "/llm/routes")
    assert routes is not None
    deploy_reqs = _data(token, "/deploy-requests")
    assert deploy_reqs is not None
    tokens = _data(token, "/api-tokens")
    assert tokens is not None
    wecom = _call("GET", "/auth/wecom/status", token)
    assert wecom[0] in {200, 404} or wecom[1].get("code") is not None


def test_pipeline_crud_roundtrip(token: str, catalog: dict) -> None:
    groups = catalog["groups"]
    assert groups, "没有环境分组，建不了流水线"
    name = f"live-func-{int(time.time())}"
    created = _ok(
        token,
        "POST",
        "/pipelines",
        {
            "project_id": catalog["project_id"],
            "group_id": groups[0]["id"],
            "name": name,
            "yaml": MINIMAL_YAML,
        },
    )
    pipe_id = created["id"]
    try:
        got = _data(token, f"/pipelines/{pipe_id}")
        assert got["name"] == name
        copied = _ok(token, "POST", f"/pipelines/{pipe_id}/duplicate", {})
        copy_id = copied["id"]
        _ok(token, "DELETE", f"/pipelines/{copy_id}")
        _ok(token, "DELETE", f"/pipelines/{copy_id}/purge")
        _ok(token, "DELETE", f"/pipelines/{pipe_id}")
        _ok(token, "POST", f"/pipelines/{pipe_id}/restore", {})
    finally:
        status, _ = _call("DELETE", f"/pipelines/{pipe_id}", token)
        if status == 200:
            _call("DELETE", f"/pipelines/{pipe_id}/purge", token)
        else:
            _call("DELETE", f"/pipelines/{pipe_id}/purge", token)


def test_personal_folder_and_view_pref(token: str, catalog: dict) -> None:
    pid = catalog["project_id"]
    name = f"live-folder-{int(time.time())}"
    created = _ok(token, "POST", "/personal-folders", {"project_id": pid, "name": name})
    folder_names = created if isinstance(created, list) else [name]
    try:
        folders = _data(token, f"/personal-folders?project_id={pid}")
        if folders and isinstance(folders[0], str):
            listed = set(folders)
        elif isinstance(folders, list):
            listed = {item.get("name") for item in folders if isinstance(item, dict)}
        else:
            listed = set()
        assert name in folder_names or name in listed
        _ok(token, "PUT", "/view-pref/pipeline", {"project_id": pid, "sort": "name"})
        pref = _data(token, "/view-pref/pipeline")
        assert pref is not None
    finally:
        _call("DELETE", f"/personal-folders?project_id={pid}&name={name}", token)


def test_ai_skills_models_and_tools(token: str) -> None:
    skills = _data(token, "/ai/skills")
    names = {item.get("name") for item in skills} if isinstance(skills, list) else set()
    for required in (
        "list_push_nodes",
        "list_pipelines",
        "list_repositories",
        "list_projects",
        "list_agents",
        "get_dora_metrics",
        "propose_node_push",
        "propose_plugin_draft",
        "list_plugin_drafts",
    ):
        assert required in names, f"技能 {required} 未注册"
    models = _data(token, "/ai/models")
    assert isinstance(models, list) and models, "对话框没有可用模型，助手会静默回退规则引擎"
    tools = _data(token, "/ai/tools")
    assert tools is not None


def test_ai_tools_return_real_catalogs(token: str, catalog: dict) -> None:
    nodes = _tool(token, "list_push_nodes")
    assert "nodes" in nodes
    assert nodes.get("error") is None
    pipelines = _tool(token, "list_pipelines")
    assert "pipelines" in pipelines
    repos = _tool(token, "list_repositories")
    assert "repositories" in repos
    projects = _tool(token, "list_projects")
    assert "projects" in projects
    dora = _tool(token, "get_dora_metrics")
    assert "deploy_frequency" in dora or "error" not in dora
    failed = _tool(token, "list_failed_releases")
    assert "releases" in failed
    agents = _tool(token, "list_agents")
    assert "agents" in agents or "error" in agents
    listed = _tool(token, "list_plugin_drafts")
    assert "drafts" in listed or "error" in listed
    assert catalog["projects"]
    nodes = catalog["nodes"]
    if nodes:
        node = nodes[0]
        host = str(node.get("host") or "")
        if host:
            by_host = _tool(token, "list_push_nodes", {"keyword": host})
            ids = {n.get("id") for n in (by_host.get("nodes") or [])}
            assert node["id"] in ids, (host, by_host)
    pipe_id = int((catalog["pipelines"] or [{}])[0].get("id") or 0)
    if pipe_id:
        card = _tool(token, "propose_release", {"pipeline_id": pipe_id, "version": "live-func"})
        assert card.get("error") is None, card
        assert card.get("_action") == "confirm_release", card


def test_list_push_nodes_accepts_numeric_id(token: str, catalog: dict) -> None:
    """模型常把 node_id 当 keyword。纯数字必须命中对应节点，不能查空后说机器没登记。"""
    nodes = catalog["nodes"]
    assert nodes, "没有可下发节点"
    node = nodes[0]
    by_id = _tool(token, "list_push_nodes", {"keyword": str(node["id"])})
    ids = {n.get("id") for n in (by_id.get("nodes") or [])}
    assert node["id"] in ids, (node["id"], by_id)


def test_ai_conversation_endpoint(token: str) -> None:
    conv = _data(token, "/ai/conversation")
    assert conv is not None


def test_session_create_get_clear_delete(token: str) -> None:
    created = _ok(token, "POST", "/ai/sessions", {"title": "live-smoke-session"})
    session_id = created["id"]
    listed = _data(token, "/ai/sessions")
    ids = {item["id"] for item in listed}
    assert session_id in ids

    conv = _data(token, f"/ai/sessions/{session_id}")
    assert "messages" in conv, "切会话时前端靠 messages，不能只给 inspect 结构"

    _ok(token, "POST", f"/ai/sessions/{session_id}/clear", {})

    inspect = _data(token, f"/ai/sessions/{session_id}/inspect")
    assert "projection" in inspect or "events" in inspect or "session" in inspect

    search = _data(token, f"/ai/sessions/{session_id}/search?q=live")
    assert search is not None

    _ok(token, "DELETE", f"/ai/sessions/{session_id}")
    listed = _data(token, "/ai/sessions")
    ids = {item["id"] for item in listed}
    assert session_id not in ids
    status, gone = _call("GET", f"/ai/sessions/{session_id}", token)
    assert status in {404, 400} or gone.get("code") not in {0, None}


def test_session_messages_available_immediately_after_chat(token: str) -> None:
    """切左侧会话不能先空白：GET 会话必须立刻带回刚才的消息。"""
    created = _ok(token, "POST", "/ai/sessions", {"title": "live-session-switch"})
    session_id = created["id"]
    try:
        payload = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "你好，回一句即可", "session_id": session_id},
            timeout=180,
        )
        reply = (payload.get("reply") or "").strip()
        assert reply, payload
        conv = _data(token, f"/ai/sessions/{session_id}")
        messages = conv.get("messages") or []
        texts = " ".join(str(m.get("content") or m.get("text") or "") for m in messages)
        assert "你好" in texts or reply in texts, conv
        assert len(messages) >= 2, f"切会话时应立刻看到用户+助手消息，实际 {len(messages)} 条: {conv}"
    finally:
        _call("DELETE", f"/ai/sessions/{session_id}", token)


def test_attachment_upload_list_delete(token: str) -> None:
    status, payload = _upload(token, "live-func.txt", b"live functional test\n")
    assert status == 200 and payload.get("code") == 0, payload
    rows = payload["data"]
    assert rows and rows[0].get("id"), payload
    att_id = rows[0]["id"]
    listed = _data(token, "/ai/attachments")
    ids = {item["id"] for item in listed}
    assert att_id in ids
    _ok(token, "DELETE", f"/ai/attachments/{att_id}")
    listed = _data(token, "/ai/attachments")
    ids = {item["id"] for item in listed}
    assert att_id not in ids


def test_propose_node_push_returns_confirm_card(token: str, catalog: dict) -> None:
    """生产节点下发必须先出确认卡，不能被 confirm=True 拦成「未执行」。"""
    nodes = catalog["nodes"]
    assert nodes, "admin 看不到任何可下发节点，助手传文件这条路是断的"
    node = next((n for n in nodes if n.get("allow_paths")), None)
    assert node, f"节点没有 allow_paths，无法预检目录: {nodes}"
    target = (node.get("allow_paths") or ["D:\\\\wwwroot"])[0]
    status, payload = _upload(token, "live-push.txt", b"live node push probe\n")
    assert status == 200 and payload.get("code") == 0, payload
    att_id = payload["data"][0]["id"]
    try:
        result = _tool(
            token,
            "propose_node_push",
            {
                "node_ids": [int(node["id"])],
                "target_dir": target,
                "attachment_ids": [int(att_id)],
            },
        )
        assert result.get("error") is None, result
        assert "approval_required" not in result or result.get("_action"), (
            "propose_node_push 被当成需审批工具拦下了，确认卡片出不来: " + json.dumps(result, ensure_ascii=False)
        )
        assert result.get("_action") == "confirm_node_push", result
        assert result.get("payload", {}).get("node_ids") == [int(node["id"])]
        assert result.get("payload", {}).get("target_dir") == target
        assert "确认" in (result.get("label") or result.get("reply") or "")
    finally:
        _call("DELETE", f"/ai/attachments/{att_id}", token)


def test_plugin_draft_lands_in_store(token: str) -> None:
    """助手起草的插件必须出现在技能库 → 插件草稿。"""
    name = f"live-func-{int(time.time())}"
    result = _tool(
        token,
        "propose_plugin_draft",
        {
            "name": name,
            "display_name": "功能测试插件",
            "category": "exec",
            "version": "1.0.0",
            "description": "live functional test，不会安装",
            "language": "python",
            "entrypoint": "python3 task.py",
            "files": {"task.py": DRAFT_TASK_PY},
            "intent": "live functional test",
        },
    )
    assert result.get("ok") is True, result
    draft_id = result.get("draft_id")
    assert draft_id, result
    drafts = _data(token, "/store/plugin-drafts")
    items = drafts if isinstance(drafts, list) else (drafts.get("items") or drafts.get("list") or [])
    ids = {item.get("id") for item in items}
    assert draft_id in ids, (draft_id, items[:8])
    detail = _data(token, f"/store/plugin-drafts/{draft_id}")
    assert detail.get("name") == name or (detail.get("draft") or {}).get("name") == name
    listed = _tool(token, "list_plugin_drafts", {"status": "pending"})
    listed_ids = {d.get("id") for d in (listed.get("drafts") or [])}
    assert draft_id in listed_ids
    _ok(token, "POST", f"/store/plugin-drafts/{draft_id}/reject", {"comment": "live functional test cleanup"})


def test_chat_query_all_nodes(token: str, catalog: dict) -> None:
    created = _ok(token, "POST", "/ai/sessions", {"title": "live-chat-nodes"})
    session_id = created["id"]
    try:
        data = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "查询全部节点", "session_id": session_id},
            timeout=180,
        )
        _assert_not_stuck_waiting(data.get("reply") or "", data.get("traces") or [])
        nodes = catalog["nodes"]
        if nodes:
            names = [str(n.get("name") or "") for n in nodes if n.get("name")]
            assert any(name and name in (data.get("reply") or "") for name in names), data.get("reply")
    finally:
        _call("DELETE", f"/ai/sessions/{session_id}", token)


def test_chat_query_repos_pipelines_projects(token: str) -> None:
    created = _ok(token, "POST", "/ai/sessions", {"title": "live-chat-catalog"})
    session_id = created["id"]
    try:
        for question, tool_name, key in (
            ("有哪些代码库", "list_repositories", "repositories"),
            ("有哪些流水线", "list_pipelines", "pipelines"),
            ("有哪些项目", "list_projects", "projects"),
        ):
            data = _ok(
                token,
                "POST",
                "/ai/chat",
                {"message": question, "session_id": session_id},
                timeout=180,
            )
            _assert_not_stuck_waiting(data.get("reply") or "", data.get("traces") or [])
            catalog_items = _tool(token, tool_name).get(key) or []
            if catalog_items:
                names = [
                    str(item.get("name") or item.get("display_name") or "")
                    for item in catalog_items
                ]
                names = [n for n in names if n]
                reply = data.get("reply") or ""
                hit = [n for n in names if n in reply]
                need = min(3, len(names)) if len(names) >= 3 else len(names)
                assert len(hit) >= need, (question, names[:10], reply)
    finally:
        _call("DELETE", f"/ai/sessions/{session_id}", token)


def _ask_node_push(token: str, session_id: int, node: dict, target: str, att_id: int) -> dict:
    name = node.get("name") or ""
    host = node.get("host") or name
    message = (
        f"把刚上传的文件发到节点 {name}（IP {host}）的目录 {target}，"
        f"附件 id 是 {att_id}。请直接调用 propose_node_push，"
        f"node_ids 填 [{int(node['id'])}]，target_dir 填 {target}，attachment_ids 填 [{att_id}]。"
        "不要再查询节点列表。"
    )
    last = None
    for i in range(2):
        last = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": message if i == 0 else "不要再查节点，立刻 propose_node_push。", "session_id": session_id},
            timeout=180,
        )
        actions = last.get("actions") or []
        if any(a.get("type") == "confirm_node_push" for a in actions):
            return last
    return last or {}


def test_chat_node_push_shows_confirm_card(token: str, catalog: dict) -> None:
    """用户拖文件后说发到某台机器，必须出确认卡片而不是口头答应或报未执行。"""
    nodes = catalog["nodes"]
    assert nodes, "没有可下发节点"
    node = next((n for n in nodes if n.get("allow_paths")), nodes[0])
    target = (node.get("allow_paths") or ["D:\\\\wwwroot"])[0]
    status, payload = _upload(token, "live-chat-push.txt", b"chat push probe\n")
    assert status == 200 and payload.get("code") == 0, payload
    att_id = payload["data"][0]["id"]
    created = _ok(token, "POST", "/ai/sessions", {"title": "live-chat-push"})
    session_id = created["id"]
    try:
        data = _ask_node_push(token, session_id, node, target, att_id)
        actions = data.get("actions") or []
        traces = data.get("traces") or []
        reply = data.get("reply") or ""
        blocked = any(
            (t.get("result") or {}).get("error")
            and "未执行" in str((t.get("result") or {}).get("error"))
            for t in traces
        )
        assert not blocked, (reply, traces)
        types = {a.get("type") for a in actions}
        assert "confirm_node_push" in types or any(
            (t.get("name") == "propose_node_push" and (t.get("result") or {}).get("_action") == "confirm_node_push")
            for t in traces
        ), (reply, actions, traces)
    finally:
        _call("DELETE", f"/ai/attachments/{att_id}", token)
        _call("DELETE", f"/ai/sessions/{session_id}", token)


def test_act_confirm_node_push_submits_release(token: str, catalog: dict) -> None:
    """点确认卡片必须真正建出发布单，并等到节点落盘成功。

    节点即使标成生产也按测试机处理：进审批就批准，再等到 success。
    文件写到已存在的白名单目录，文件名带时间戳，避免覆盖业务文件。
    """
    nodes = catalog["nodes"]
    assert nodes, "没有可下发节点"
    node = _pick_live_test_node(nodes)
    if node is None:
        pytest.skip("没有测试环境节点，跳过真实落盘下发")
    target = _live_test_target(node)
    marker = f"act-push-{int(time.time())}"
    status, payload = _upload(token, f"{marker}.txt", f"{marker}\n".encode())
    assert status == 200 and payload.get("code") == 0, payload
    att_id = payload["data"][0]["id"]
    created = _ok(token, "POST", "/ai/sessions", {"title": "live-act-push"})
    session_id = created["id"]
    release_id = None
    try:
        data = _ask_node_push(token, session_id, node, target, att_id)
        action = next((a for a in (data.get("actions") or []) if a.get("type") == "confirm_node_push"), None)
        assert action and action.get("token"), data
        out = _ok(
            token,
            "POST",
            "/ai/act",
            {
                "type": action["type"],
                "payload": action.get("payload") or {},
                "token": action["token"],
                "session_id": session_id,
            },
        )
        release_id = out.get("release_id")
        assert release_id, out
        assert out.get("status") in {"pending", "queued", "running", "success"}, out
        if out.get("status") == "pending":
            _approve_pending_release(token, release_id)
        last = _wait_release(token, release_id, timeout=180)
        err = _release_error(token, release_id)
        assert last == "success", f"测试节点落盘未成功: {last} release={release_id} {err}"
        preview = _data(token, f"/releases/{release_id}/rollback-preview")
        assert preview is not None
        tasks = _data(token, f"/tasks?release_id={release_id}")
        rows = tasks if isinstance(tasks, list) else (tasks.get("items") or [])
        assert rows, f"发布 #{release_id} 没有任务"
    finally:
        _call("DELETE", f"/ai/attachments/{att_id}", token)
        _call("DELETE", f"/ai/sessions/{session_id}", token)


def _assert_not_stuck_waiting(reply: str, traces: list) -> None:
    text = (reply or "").strip()
    assert text, "助手最终回复是空的"
    waiting = any(marker in text for marker in WAIT_MARKERS)
    if waiting:
        assert len(text) > 80, f"助手停在口头禅：{text}"
        listed = False
        for trace in traces or []:
            result = trace.get("result") or {}
            for key in ("nodes", "pipelines", "repositories", "projects", "agents", "releases"):
                if result.get(key):
                    listed = True
        assert listed or "#" in text or "共" in text, f"助手停在口头禅且没有列出数据：{text}"
