# -*- coding: utf-8 -*-
"""生产节点运行时安全：终态不被覆盖、领取不被积压挤掉、越界目录提交时就拒绝。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.response import BizException
from app.db.base import Base
from app.modules.agent.models import BuildAgent, BuildTask
from app.modules.agent.presence import HEARTBEAT_TIMEOUT_SECONDS, agent_is_online
from app.modules.agent.task_service import (
    MAX_KEEP_BACKUPS,
    _assert_node_usable,
    complete_task,
    fetch_task,
    reap_dead_agent_tasks,
    resolve_keep_backups,
)
from app.modules.pipeline.models import Release


def _engine():
    """多 Session 共用同一块内存库：回收任务会自己开会话，不能每次连到空库。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[BuildAgent.__table__, BuildTask.__table__, Release.__table__],
    )
    return engine


def _db() -> Session:
    return Session(_engine())


def _online_node(**kwargs) -> BuildAgent:
    """测试里默认当成刚心跳过的在线节点，避免误走离线拒绝。"""
    kwargs.setdefault("role", "node")
    kwargs.setdefault("os", "windows")
    kwargs.setdefault("env", "prod")
    kwargs.setdefault("status", "online")
    kwargs.setdefault("last_heartbeat", datetime.now())
    return BuildAgent(**kwargs)


def test_keep_backups_hard_cap() -> None:
    assert resolve_keep_backups(5) == 5
    assert resolve_keep_backups(MAX_KEEP_BACKUPS) == MAX_KEEP_BACKUPS
    assert resolve_keep_backups(MAX_KEEP_BACKUPS + 100) == MAX_KEEP_BACKUPS
    assert resolve_keep_backups(0) == 1


def test_complete_task_does_not_overwrite_cancelled() -> None:
    db = _db()
    rel = Release(pipeline_id=1, group_id=1, status="cancelled")
    db.add(rel)
    db.commit()
    db.refresh(rel)
    task = BuildTask(
        release_id=rel.id,
        pipeline_id=1,
        status="cancelled",
        agent_tag="node",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    got = complete_task(db, task.id, success=True)
    assert got.status == "cancelled"

    got = complete_task(db, task.id, success=False)
    assert got.status == "cancelled"


def test_assert_node_rejects_target_dir_outside_allow_paths() -> None:
    db = _db()
    node = _online_node(
        name="web-1",
        allow_paths=json.dumps(["D:\\wwwroot\\site"]),
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    try:
        _assert_node_usable(
            db,
            node.id,
            [{"plugin": "file-transfer", "with": {"targetDir": "C:\\Windows\\Temp"}}],
            "prod",
        )
        raise AssertionError("越界目录应当在建任务时拒绝")
    except BizException as exc:
        assert exc.code == 400
        assert "允许的范围" in str(exc.message)


def test_assert_node_rejects_offline() -> None:
    """节点已标离线时，发送文件必须当场失败，不能建成永远没人领的 pending。"""
    db = _db()
    node = BuildAgent(
        name="web-offline",
        role="node",
        os="windows",
        env="prod",
        status="offline",
        last_heartbeat=datetime.now(),
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    try:
        _assert_node_usable(
            db,
            node.id,
            [{"plugin": "file-transfer", "with": {"targetDir": "D:\\wwwroot"}}],
            "prod",
        )
        raise AssertionError("离线节点应当在建任务时拒绝")
    except BizException as exc:
        assert exc.code == 400
        assert "离线" in str(exc.message)
        assert "web-offline" in str(exc.message)


def test_assert_node_rejects_online_without_heartbeat() -> None:
    """库里标 online 但从未报过心跳，不能当成能领任务。"""
    db = _db()
    node = BuildAgent(
        name="web-no-hb",
        role="node",
        os="windows",
        env="prod",
        status="online",
        last_heartbeat=None,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    try:
        _assert_node_usable(db, node.id, [{"plugin": "file-transfer"}], "prod")
        raise AssertionError("没有心跳的节点应当拒绝")
    except BizException as exc:
        assert exc.code == 400
        assert "离线" in str(exc.message)


def test_assert_node_rejects_stale_heartbeat() -> None:
    """库里仍标 online、但心跳已经超时，按离线拒绝。"""
    db = _db()
    node = _online_node(
        name="web-stale",
        last_heartbeat=datetime.now() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS + 5),
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    assert not agent_is_online(node)
    try:
        _assert_node_usable(
            db,
            node.id,
            [{"plugin": "file-transfer"}],
            "prod",
        )
        raise AssertionError("心跳超时应当视为离线并拒绝")
    except BizException as exc:
        assert exc.code == 400
        assert "离线" in str(exc.message)


def test_reap_pending_node_task_when_target_offline() -> None:
    """已经派出、节点随后离线的 pending 部署任务，巡检应收成失败而不是一直转圈。"""
    engine = _engine()
    Factory = sessionmaker(bind=engine)
    with Factory() as db:
        node = BuildAgent(
            name="web-gone",
            role="node",
            os="windows",
            env="prod",
            status="offline",
            last_heartbeat=datetime.now() - timedelta(seconds=120),
        )
        db.add(node)
        db.flush()
        rel = Release(pipeline_id=1, group_id=1, status="running")
        db.add(rel)
        db.flush()
        task = BuildTask(
            release_id=rel.id,
            pipeline_id=1,
            status="pending",
            agent_tag="node",
            env="prod",
            target_agent_id=node.id,
        )
        later = BuildTask(
            release_id=rel.id,
            pipeline_id=1,
            status="pending",
            agent_tag="node",
            env="prod",
            target_agent_id=node.id,
        )
        db.add_all([task, later])
        db.commit()
        rid, tid, later_id = rel.id, task.id, later.id

    reaped = reap_dead_agent_tasks(Factory)
    assert reaped >= 1
    with Factory() as db:
        assert db.get(BuildTask, tid).status == "failed"
        # 同一次发布的后续部署不能继续挂着
        assert db.get(BuildTask, later_id).status in ("failed", "cancelled")
        rel = db.get(Release, rid)
        assert rel.status == "failed"
        assert rel.finished_at is not None
        assert "离线" in (rel.error_message or "")


def test_reap_skips_pending_node_task_when_target_online() -> None:
    """目标节点在线时，pending 部署任务留给 Agent 正常领取。"""
    engine = _engine()
    Factory = sessionmaker(bind=engine)
    with Factory() as db:
        node = _online_node(name="web-live", tags="[]")
        db.add(node)
        db.flush()
        rel = Release(pipeline_id=1, group_id=1, status="running")
        db.add(rel)
        db.flush()
        task = BuildTask(
            release_id=rel.id,
            pipeline_id=1,
            status="pending",
            agent_tag="node",
            env="prod",
            target_agent_id=node.id,
        )
        db.add(task)
        db.commit()
        tid, rid = task.id, rel.id

    assert reap_dead_agent_tasks(Factory) == 0
    with Factory() as db:
        assert db.get(BuildTask, tid).status == "pending"
        assert db.get(Release, rid).status == "running"


def test_fetch_task_node_not_starved_by_builder_backlog() -> None:
    """积压的构建任务不能把指名给节点的部署任务挤出领取窗口。"""
    db = _db()
    node = _online_node(name="n1", tags="[]")
    builder = BuildAgent(
        name="b1",
        role="builder",
        os="linux",
        env="prod",
        tags=json.dumps(["linux"]),
    )
    db.add_all([node, builder])
    db.commit()
    db.refresh(node)
    db.refresh(builder)

    rel = Release(pipeline_id=1, group_id=1, status="running")
    db.add(rel)
    db.commit()
    db.refresh(rel)

    for _ in range(60):
        db.add(
            BuildTask(
                release_id=rel.id,
                pipeline_id=1,
                status="pending",
                agent_tag="linux",
                env="prod",
            )
        )
    deploy = BuildTask(
        release_id=rel.id,
        pipeline_id=1,
        status="pending",
        agent_tag="node",
        env="prod",
        target_agent_id=node.id,
    )
    db.add(deploy)
    db.commit()
    db.refresh(deploy)

    got = fetch_task(db, node.id)
    assert got is not None
    assert got.id == deploy.id
    assert got.status == "running"


def test_fetch_task_builder_not_starved_by_other_os_backlog() -> None:
    """积压的 linux 构建任务不能把 windows 构建机自己的任务挤出领取窗口。"""
    db = _db()
    windows = BuildAgent(
        name="win-1",
        role="builder",
        os="windows",
        env="prod",
        tags=json.dumps(["windows"]),
    )
    db.add(windows)
    db.commit()
    db.refresh(windows)

    rel = Release(pipeline_id=1, group_id=1, status="running")
    db.add(rel)
    db.commit()
    db.refresh(rel)

    for _ in range(60):
        db.add(
            BuildTask(
                release_id=rel.id,
                pipeline_id=1,
                status="pending",
                agent_tag="linux",
                env="prod",
            )
        )
    mine = BuildTask(
        release_id=rel.id,
        pipeline_id=1,
        status="pending",
        agent_tag="windows",
        env="prod",
    )
    db.add(mine)
    db.commit()
    db.refresh(mine)

    got = fetch_task(db, windows.id)
    assert got is not None
    assert got.id == mine.id
    assert got.status == "running"
