"""构建机 / 节点：先卸载，卸完才能删除登记。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.db.base import Base
from app.modules.agent.models import BuildAgent, NodeGroupMember
from app.modules.agent.presence import agent_is_online
from app.modules.agent.router import (
    _effective_status,
    delete_agent,
    heartbeat,
    register_agent,
    request_uninstall,
)
from app.modules.agent.tokens import hash_agent_token
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Permission
from app.modules.settings.models import PlatformSetting

ADMIN = CurrentUser(id=1, username="admin", is_admin=True)
PLAIN = "a" * 64


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            BuildAgent.__table__,
            NodeGroupMember.__table__,
            AuditLog.__table__,
            Permission.__table__,
            PlatformSetting.__table__,
        ],
    )
    return Session(engine)


def _agent(db: Session, **kw) -> BuildAgent:
    """在线构建机，默认刚心跳过。"""
    now = datetime.now()
    status = kw.pop("status", "online")
    row = BuildAgent(
        name=kw.pop("name", "win-C#"),
        role=kw.pop("role", "builder"),
        status=status,
        last_heartbeat=kw.pop("last_heartbeat", now),
        token=hash_agent_token(PLAIN),
        agent_version="abc123abc123",
        **kw,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_delete_refuses_before_uninstall():
    """没卸完不能删登记，避免名单没了机器上 Agent 还在。"""
    db = _db()
    a = _agent(db)
    with pytest.raises(BizException) as exc:
        delete_agent(a.id, db, ADMIN)
    assert "请先卸载" in exc.value.message


def test_uninstall_then_heartbeat_then_delete():
    """在线卸载：心跳带回 should_uninstall，Agent 上报 uninstalled 后才允许删除。"""
    db = _db()
    a = _agent(db)
    request_uninstall(a.id, db, ADMIN)
    db.refresh(a)
    assert a.uninstall_requested_at is not None
    assert _effective_status(a) == "uninstalling"
    assert not agent_is_online(a)

    hb = heartbeat(a.id, db, x_agent_token=PLAIN, payload={"running_count": 0})
    assert hb.data["should_uninstall"] is True
    assert hb.data["should_upgrade"] is False

    heartbeat(a.id, db, x_agent_token=PLAIN, payload={"uninstalled": True})
    db.refresh(a)
    assert a.uninstalled_at is not None
    assert _effective_status(a) == "uninstalled"

    delete_agent(a.id, db, ADMIN)
    assert db.get(BuildAgent, a.id) is None


def test_offline_uninstall_completes_immediately():
    """当时不在心跳的机器收不到指令，点卸载即记为卸完，可以删除。"""
    db = _db()
    a = _agent(db, last_heartbeat=datetime.now() - timedelta(minutes=10), status="offline")
    request_uninstall(a.id, db, ADMIN)
    db.refresh(a)
    assert a.uninstalled_at is not None
    assert _effective_status(a) == "uninstalled"
    delete_agent(a.id, db, ADMIN)
    assert db.get(BuildAgent, a.id) is None


def test_uninstalled_report_ignored_without_request():
    """没点卸载时，心跳带 uninstalled 不能把自己变成可删除。"""
    db = _db()
    a = _agent(db)
    heartbeat(a.id, db, x_agent_token=PLAIN, payload={"uninstalled": True})
    db.refresh(a)
    assert a.uninstalled_at is None
    with pytest.raises(BizException) as exc:
        delete_agent(a.id, db, ADMIN)
    assert "请先卸载" in exc.value.message


def test_heartbeat_after_uninstalled_does_not_refresh_presence():
    """卸完后的 ping 不再刷新心跳时间，也不改回在线。"""
    db = _db()
    a = _agent(db)
    request_uninstall(a.id, db, ADMIN)
    heartbeat(a.id, db, x_agent_token=PLAIN, payload={"uninstalled": True})
    db.refresh(a)
    stamped = a.uninstalled_at
    last = a.last_heartbeat
    assert stamped is not None
    heartbeat(a.id, db, x_agent_token=PLAIN, payload={"running_count": 0})
    db.refresh(a)
    assert a.uninstalled_at == stamped
    assert a.last_heartbeat == last
    assert _effective_status(a) == "uninstalled"


def test_reinstall_with_enroll_token_clears_uninstall():
    """卸完后带接入凭证重装：接到原来那条登记，清掉卸载状态，重新上线。"""
    db = _db()
    db.add(PlatformSetting(key="agent_enroll_token", value="enroll-secret"))
    db.commit()
    a = _agent(db)
    agent_id = a.id
    request_uninstall(a.id, db, ADMIN)
    heartbeat(a.id, db, x_agent_token=PLAIN, payload={"uninstalled": True})
    db.refresh(a)
    assert a.uninstalled_at is not None

    resp = register_agent(
        {"name": a.name, "role": "builder", "host": "172.17.3.164", "os": "windows"},
        db,
        x_enroll_token="enroll-secret",
        x_agent_token="",
    )
    db.refresh(a)
    assert a.id == agent_id
    assert a.uninstalled_at is None
    assert a.uninstall_requested_at is None
    assert _effective_status(a) == "online"
    assert resp.data["agent_id"] == agent_id

