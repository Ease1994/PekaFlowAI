"""测试环境 AI Agent 主路径：确认卡必须是数字 id + 签名，发布和权限不能互抢。

    $env:LIVE_BASE="http://127.0.0.1:8080/api/v1"
    python -m pytest tests/test_live_ai_agent.py -v --tb=short
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

import pytest

from tests.test_live_platform import LIVE, login_admin

pytestmark = pytest.mark.skipif(not LIVE, reason="set LIVE_BASE to run against the test server")


def _call(method: str, path: str, token: str | None = None, body=None, timeout: int = 180):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{LIVE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            raw = json.loads(exc.read().decode())
        except Exception:
            raw = {"message": str(exc)}
        return exc.code, raw


def _ok(token: str, method: str, path: str, body=None, timeout: int = 180):
    status, payload = _call(method, path, token, body, timeout=timeout)
    assert status == 200 and payload.get("code") == 0, (method, path, payload)
    return payload.get("data")


@pytest.fixture(scope="module")
def token() -> str:
    return login_admin()


def _new_session(token: str, title: str) -> int:
    return _ok(token, "POST", "/ai/sessions", {"title": f"{title}-{int(time.time())}"})["id"]


def test_login_and_list_projects(token: str) -> None:
    me = _ok(token, "GET", "/auth/me")
    assert me.get("is_admin") is True or me.get("username") == "admin"
    projects = _ok(token, "GET", "/projects")
    assert projects, "测试环境没有项目"


def test_execute_project_env_card_has_numeric_id_and_token(token: str) -> None:
    """「执行AI陪练项目生产环境」不能把原话当成 pipeline_id。"""
    sid = _new_session(token, "live-ai-coach")
    try:
        data = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "执行AI陪练项目生产环境", "session_id": sid},
        )
        reply = data.get("reply") or ""
        actions = data.get("actions") or []
        assert "流水线=#AI陪练项目生产环境" not in reply, reply
        types = {a.get("type") for a in actions}
        if "confirm_release" in types:
            payload = actions[0].get("payload") or {}
            pid = payload.get("pipeline_id")
            assert isinstance(pid, int) and pid > 0, payload
            assert actions[0].get("token"), "确认卡没有签名，点下去会确认信息无效"
            assert re.search(rf"流水线=#{pid}\b", reply), reply
        else:
            # 没权限或该环境多条线：必须说明原因，不能编造 id
            assert "108" not in reply
            assert "找不到" in reply or "看不到" in reply or "请指定" in reply or "权限" in reply, reply
    finally:
        _call("DELETE", f"/ai/sessions/{sid}", token)


def test_named_pipeline_release_then_act(token: str) -> None:
    """点名流水线出卡，带 token 的确认应能提交；YAML 名和显示名不一致时以显示名为准。"""
    sid = _new_session(token, "live-ai-testc")
    try:
        data = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "发布 test-C 流水线", "session_id": sid},
        )
        reply = data.get("reply") or ""
        assert "流水线=#AI陪练" not in reply
        actions = data.get("actions") or []
        if "confirm_release" not in {a.get("type") for a in actions}:
            # 副本流水线 YAML 仍写 test-C 时，未修匹配会列出 #15 和 #47。用显示名对应的 id 再出卡。
            ids = [int(x) for x in re.findall(r"#(\d+)\s+test-C\b", reply)]
            assert ids, data
            data = _ok(
                token,
                "POST",
                "/ai/chat",
                {"message": f"发布 #{ids[0]}", "session_id": sid},
            )
            actions = data.get("actions") or []
        types = {a.get("type") for a in actions}
        assert "confirm_release" in types, data
        action = actions[0]
        pid = (action.get("payload") or {}).get("pipeline_id")
        assert isinstance(pid, int) and pid > 0
        assert action.get("token")
        assert re.search(rf"流水线=#{pid}\b", data.get("reply") or ""), data.get("reply")
        status, payload = _call(
            "POST",
            "/ai/act",
            token,
            {
                "type": action["type"],
                "payload": action["payload"],
                "token": action["token"],
                "session_id": sid,
            },
        )
        assert status == 200 and payload.get("code") == 0, payload
        status2, payload2 = _call(
            "POST",
            "/ai/act",
            token,
            {
                "type": action["type"],
                "payload": action["payload"],
                "token": action["token"],
                "session_id": sid,
            },
        )
        assert payload2.get("code") != 0 or status2 >= 400
        msg = str(payload2.get("message") or "")
        assert "执行过" in msg or "无效" in msg or "不一致" in msg, payload2
    finally:
        _call("DELETE", f"/ai/sessions/{sid}", token)


def test_act_without_token_is_rejected(token: str) -> None:
    status, payload = _call(
        "POST",
        "/ai/act",
        token,
        {"type": "confirm_release", "payload": {"pipeline_id": 1}, "token": ""},
    )
    assert status >= 400 or payload.get("code") != 0
    assert "无效" in str(payload.get("message") or "")


def test_status_question_is_not_a_release_card(token: str) -> None:
    sid = _new_session(token, "live-ai-status")
    try:
        data = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "test-C 发布得怎样了", "session_id": sid},
        )
        types = {a.get("type") for a in (data.get("actions") or [])}
        assert "confirm_release" not in types, data
        reply = data.get("reply") or ""
        assert reply
        assert "流水线=#AI陪练项目生产环境" not in reply
        assert "请用返回的 id 再调一次" not in reply, reply
        traces = [str(t.get("name") or "") for t in (data.get("traces") or [])]
        assert "propose_release" not in traces, data
    finally:
        _call("DELETE", f"/ai/sessions/{sid}", token)


def test_access_apply_does_not_list_unrelated_then_release_phrase(token: str) -> None:
    """先申请权限，再说执行某项目生产，不能继续去申请 DMS。"""
    sid = _new_session(token, "live-ai-access-then-release")
    try:
        first = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "整个项目下所有流水线的执行权限", "session_id": sid},
        )
        first_reply = first.get("reply") or ""
        second = _ok(
            token,
            "POST",
            "/ai/chat",
            {"message": "执行AI陪练项目生产环境", "session_id": sid},
        )
        reply = second.get("reply") or ""
        actions = second.get("actions") or []
        assert "流水线=#AI陪练项目生产环境" not in reply
        # 第二句不该再交一张「整个项目」申请把 DMS 带出来
        if "已提交" in reply and "DMS" in reply and "confirm_release" not in {a.get("type") for a in actions}:
            pytest.fail(f"发布话术被续接到权限申请：{reply}\n上一轮：{first_reply}")
    finally:
        _call("DELETE", f"/ai/sessions/{sid}", token)
