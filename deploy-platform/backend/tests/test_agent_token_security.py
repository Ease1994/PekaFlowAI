"""Agent token 只存哈希、续期不能改 role、鉴权失败统一 401。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.db.base import Base
from app.modules.agent.models import BuildAgent
from app.modules.agent.router import (
    _authenticate_agent,
    _authorize_agent_bootstrap,
    _rotate_agent_token,
    heartbeat,
    public_platform_url,
    register_agent,
    update_agent,
)
from app.modules.agent.tokens import hash_agent_token, is_stored_hash, migrate_plaintext_tokens, token_matches
from app.modules.audit.models import AuditLog
from app.modules.auth.models import UserMenuDeny
from app.modules.settings.models import PlatformSetting

ADMIN = CurrentUser(id=1, username="admin", is_admin=True)


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[BuildAgent.__table__, PlatformSetting.__table__, UserMenuDeny.__table__],
    )
    return Session(engine)


def test_hash_and_migrate_plaintext() -> None:
    plain = "a" * 64
    stored = hash_agent_token(plain)
    assert stored.startswith("s256:")
    assert token_matches(stored, plain)
    assert not token_matches(stored, "b" * 64)
    assert token_matches(plain, plain)

    db = _db()
    db.add(BuildAgent(name="box", token=plain, token_prev="c" * 64, os="linux"))
    db.commit()
    n = migrate_plaintext_tokens(db)
    assert n == 1
    agent = db.scalar(select(BuildAgent).where(BuildAgent.name == "box"))
    assert is_stored_hash(agent.token)
    assert is_stored_hash(agent.token_prev)
    assert token_matches(agent.token, plain)
    assert migrate_plaintext_tokens(db) == 0


def test_auth_missing_or_wrong_token_is_401() -> None:
    db = _db()
    agent = BuildAgent(name="n1", token=hash_agent_token("secret-token"), os="linux")
    db.add(agent)
    db.commit()
    db.refresh(agent)
    try:
        _authenticate_agent(db, 99999, "secret-token")
        raise AssertionError("missing agent should 401")
    except BizException as exc:
        assert exc.code == 401
        assert "鉴权失败" in exc.message
    try:
        _authenticate_agent(db, agent.id, "wrong")
        raise AssertionError("bad token should 401")
    except BizException as exc:
        assert exc.code == 401
        assert "鉴权失败" in exc.message
    assert _authenticate_agent(db, agent.id, "secret-token").id == agent.id


def test_jar_download_accepts_staff_with_agents_menu(monkeypatch) -> None:
    """能看见构建机菜单的登录用户可以直接下载 jar，不必管理员密码。"""
    db = _db()
    staff = CurrentUser(id=7, username="dev", is_admin=False)
    monkeypatch.setattr("app.core.deps.verify_token", lambda t, *a, **k: staff if t == "staff-jwt" else None)
    _authorize_agent_bootstrap(db, "", "Bearer staff-jwt")


def test_jar_download_rejects_staff_when_agent_menus_revoked(monkeypatch) -> None:
    """取消该用户的构建机和节点菜单后，普通人 JWT 不能再拉 jar。"""
    db = _db()
    db.add(UserMenuDeny(user_id=7, menu_key="agents"))
    db.add(UserMenuDeny(user_id=7, menu_key="nodes"))
    db.commit()
    staff = CurrentUser(id=7, username="dev", is_admin=False)
    monkeypatch.setattr("app.core.deps.verify_token", lambda t, *a, **k: staff if t == "staff-jwt" else None)
    try:
        _authorize_agent_bootstrap(db, "", "Bearer staff-jwt")
        raise AssertionError("看不见菜单时不应靠 JWT 拉 jar")
    except BizException as exc:
        assert exc.code == 401


def test_jar_download_accepts_hashed_agent_token() -> None:
    """自升级拉 jar 带的是明文 token，库里是哈希，不能按列等值去查。"""
    db = _db()
    plain = "secret-token"
    db.add(BuildAgent(name="n1", token=hash_agent_token(plain), os="linux"))
    db.commit()
    _authorize_agent_bootstrap(db, "", "", plain)
    try:
        _authorize_agent_bootstrap(db, "", "", "wrong")
        raise AssertionError("wrong token should 401")
    except BizException as exc:
        assert exc.code == 401


def test_jar_download_accepts_previous_token_during_rotation() -> None:
    """轮换宽限期内旧明文还在用，拉 jar 必须认 token_prev。"""
    db = _db()
    old_plain = "old-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    db.add(
        BuildAgent(
            name="n1",
            token=hash_agent_token("new-token-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            token_prev=hash_agent_token(old_plain),
            os="linux",
        )
    )
    db.commit()
    _authorize_agent_bootstrap(db, "", "", old_plain)


def test_plugin_download_accepts_hashed_agent_token() -> None:
    """构建机拉插件包和拉 jar 同一套凭证，同样不能 SQL 等值查明文。"""
    from app.modules.store.router import _authed

    db = _db()
    plain = "secret-token"
    db.add(BuildAgent(name="n1", token=hash_agent_token(plain), os="linux"))
    db.commit()
    req = _request({})
    assert _authed(req, db, plain, None)
    assert not _authed(req, db, "wrong", None)


def test_renew_cannot_promote_node_to_builder() -> None:
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    plain = "tok-" + "ab" * 16
    agent = BuildAgent(
        name="prod-node",
        role="node",
        token=hash_agent_token(plain),
        os="windows",
        status="online",
    )
    db.add(agent)
    db.commit()

    resp = register_agent(
        {"name": "prod-node", "role": "builder", "host": "10.0.0.1", "os": "windows"},
        db,
        x_enroll_token="",
        x_agent_token=plain,
    )
    db.refresh(agent)
    assert agent.role == "node"
    assert resp.data["token"] == plain


def test_rotate_resends_plaintext_until_ack() -> None:
    db = _db()
    old_plain = "old" + "aa" * 15
    agent = BuildAgent(
        name="b1",
        token=hash_agent_token(old_plain),
        token_rotated_at=datetime.now() - timedelta(days=30),
        os="linux",
    )
    db.add(agent)
    db.commit()
    new_plain = _rotate_agent_token(agent, old_plain)
    assert new_plain
    assert new_plain != old_plain
    assert is_stored_hash(agent.token)
    assert agent.token_issued == new_plain
    again = _rotate_agent_token(agent, old_plain)
    assert again == new_plain
    assert _rotate_agent_token(agent, new_plain) is None
    assert agent.token_prev == ""
    assert agent.token_issued == ""


def _request(headers: dict[str, str]) -> Request:
    encoded = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/agents/install-script",
        "raw_path": b"/api/v1/agents/install-script",
        "query_string": b"",
        "headers": encoded,
        "client": ("127.0.0.1", 9),
        "server": ("test", 80),
    }
    return Request(scope)


def test_install_script_ignores_forwarded_host() -> None:
    db = _db()
    db.add(PlatformSetting(key="public_app_base", value=""))
    db.commit()
    req = _request({"host": "192.0.2.10:8000", "x-forwarded-host": "evil.example"})
    url = public_platform_url(req, db)
    assert "evil" not in url
    assert "192.0.2.10:8000" in url

    row = db.scalar(select(PlatformSetting).where(PlatformSetting.key == "public_app_base"))
    row.value = "https://ci.example"
    db.commit()
    url2 = public_platform_url(req, db)
    assert url2 == "https://ci.example"
    assert "evil" not in url2


def test_renew_does_not_overwrite_allow_paths() -> None:
    """页面上改过的允许目录，Agent 带着启动参数续期也不能覆盖。"""
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    plain = "tok-" + "ab" * 16
    agent = BuildAgent(
        name="prod-node",
        role="node",
        token=hash_agent_token(plain),
        os="windows",
        status="online",
        allow_paths=json.dumps(["D:\\wwwroot"], ensure_ascii=False),
    )
    db.add(agent)
    db.commit()

    register_agent(
        {
            "name": "prod-node",
            "role": "node",
            "host": "10.0.0.1",
            "os": "windows",
            "allow_paths": ["E:\\other"],
        },
        db,
        x_enroll_token="",
        x_agent_token=plain,
    )
    db.refresh(agent)
    assert json.loads(agent.allow_paths) == ["D:\\wwwroot"]


def test_reregister_does_not_overwrite_allow_paths() -> None:
    """拿接入凭证重装同样不能把页面上的目录改回去。"""
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    agent = BuildAgent(
        name="prod-node",
        role="node",
        token=hash_agent_token("tok-" + "ab" * 16),
        os="windows",
        allow_paths=json.dumps(["D:\\wwwroot"], ensure_ascii=False),
    )
    db.add(agent)
    db.commit()
    register_agent(
        {"name": "prod-node", "role": "node", "allow_paths": ["E:\\hack"]},
        db,
        x_enroll_token="enroll-secret",
        x_agent_token="",
    )
    db.refresh(agent)
    assert json.loads(agent.allow_paths) == ["D:\\wwwroot"]


def test_empty_allow_paths_filled_on_register() -> None:
    """库里还是空的（老节点没报过）时，注册可以把启动参数补进去。"""
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    plain = "tok-" + "ab" * 16
    agent = BuildAgent(
        name="old-node",
        role="node",
        token=hash_agent_token(plain),
        os="windows",
        allow_paths="[]",
    )
    db.add(agent)
    db.commit()
    register_agent(
        {"name": "old-node", "role": "node", "allow_paths": ["D:\\wwwroot"]},
        db,
        x_enroll_token="",
        x_agent_token=plain,
    )
    db.refresh(agent)
    assert json.loads(agent.allow_paths) == ["D:\\wwwroot"]


def test_patch_allow_paths_heartbeat_and_rejects() -> None:
    """管理员改目录后心跳带回新名单；空名单和系统目录不能过。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[BuildAgent.__table__, PlatformSetting.__table__, AuditLog.__table__],
    )
    db = Session(engine)
    plain = "tok-" + "cd" * 16
    agent = BuildAgent(
        name="n1",
        role="node",
        token=hash_agent_token(plain),
        os="windows",
        allow_paths=json.dumps(["D:\\wwwroot"], ensure_ascii=False),
        status="online",
        last_heartbeat=datetime.now(),
        agent_version="dev",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    updated = update_agent(
        agent.id,
        {"allow_paths": ["D:\\wwwroot", "D:\\wwwroot\\site2"]},
        db,
        ADMIN,
    )
    assert updated.data["allow_paths"] == ["D:\\wwwroot", "D:\\wwwroot\\site2"]

    hb = heartbeat(agent.id, db, x_agent_token=plain, payload={})
    assert hb.data["allow_paths"] == ["D:\\wwwroot", "D:\\wwwroot\\site2"]

    try:
        update_agent(agent.id, {"allow_paths": []}, db, ADMIN)
        raise AssertionError("empty should 400")
    except BizException as exc:
        assert exc.code == 400
        assert "至少保留" in exc.message

    try:
        update_agent(agent.id, {"allow_paths": ["C:\\Windows\\Temp"]}, db, ADMIN)
        raise AssertionError("system dir should 400")
    except BizException as exc:
        assert exc.code == 400

    try:
        update_agent(agent.id, {"allow_paths": ["D:\\"]}, db, ADMIN)
        raise AssertionError("drive root should 400")
    except BizException as exc:
        assert exc.code == 400

    try:
        update_agent(agent.id, {"allow_paths": ["/data"]}, db, ADMIN)
        raise AssertionError("linux volume root should 400")
    except BizException as exc:
        assert exc.code == 400
        assert "整块盘" in exc.message

    try:
        update_agent(agent.id, {"allow_paths": ["/tmp/site"]}, db, ADMIN)
        raise AssertionError("tmp should 400")
    except BizException as exc:
        assert exc.code == 400
