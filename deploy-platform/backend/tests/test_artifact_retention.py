"""生产制品按流水线保留天数：窗口内全留，窗口外仍留最新两份；测试仍只留当天。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.artifact.models import Artifact
from app.modules.artifact.service import clamp_prod_retention_days, purge_expired_artifacts
from app.modules.pipeline.models import Pipeline
from app.modules.project.models import Group, Project
from app.modules.settings.models import PlatformSetting


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Project.__table__,
            Group.__table__,
            Pipeline.__table__,
            Artifact.__table__,
            PlatformSetting.__table__,
        ],
    )
    return Session(engine)


def _seed(db: Session) -> tuple[int, int]:
    proj = Project(name="订单", code="order")
    db.add(proj)
    db.flush()
    test_g = Group(project_id=proj.id, name="测试", type="test")
    prod_g = Group(project_id=proj.id, name="生产", type="prod")
    db.add_all([test_g, prod_g])
    db.flush()
    test_p = Pipeline(project_id=proj.id, group_id=test_g.id, name="order-test", yaml="")
    prod_p = Pipeline(project_id=proj.id, group_id=prod_g.id, name="order-prod", yaml="")
    db.add_all([test_p, prod_p])
    db.commit()
    return test_p.id, prod_p.id


def _art(db: Session, pipeline_id: int, name: str, *, days_ago: int, release_id: int = 1) -> Artifact:
    row = Artifact(
        pipeline_id=pipeline_id,
        release_id=release_id,
        name=name,
        storage_key="",
        size_bytes=1,
        created_at=datetime.now() - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_clamp_prod_retention_days() -> None:
    assert clamp_prod_retention_days("10") == 10
    assert clamp_prod_retention_days("0") == 1
    assert clamp_prod_retention_days("999") == 365
    assert clamp_prod_retention_days("abc") == 10


def test_prod_keeps_recent_days_and_drops_older(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.artifact.storage.remove", lambda key: False)
    db = _db()
    _test_pid, prod_pid = _seed(db)
    keep = _art(db, prod_pid, "fresh.zip", days_ago=2)
    # 窗口外但该线只有这一份更早的：必须留下，否则回滚没包
    also_keep = _art(db, prod_pid, "prev.zip", days_ago=20)
    drop = _art(db, prod_pid, "stale.zip", days_ago=30)
    stat = purge_expired_artifacts(db, prod_retention_days=10, test_retention_days=0)
    assert stat["prod_removed"] == 1
    assert db.get(Artifact, keep.id) is not None
    assert db.get(Artifact, also_keep.id) is not None
    assert db.get(Artifact, drop.id) is None


def test_idle_prod_pipeline_keeps_latest_two(monkeypatch) -> None:
    """十天没发的生产流水线：当前包和上一包都要在，第三份才清。"""
    monkeypatch.setattr("app.modules.artifact.storage.remove", lambda key: False)
    db = _db()
    _test_pid, prod_pid = _seed(db)
    current = _art(db, prod_pid, "current.zip", days_ago=20)
    prev = _art(db, prod_pid, "prev.zip", days_ago=40)
    older = _art(db, prod_pid, "older.zip", days_ago=60)
    stat = purge_expired_artifacts(db, prod_retention_days=10, test_retention_days=0)
    assert stat["prod_removed"] == 1
    assert db.get(Artifact, current.id) is not None
    assert db.get(Artifact, prev.id) is not None
    assert db.get(Artifact, older.id) is None


def test_retention_does_not_cross_pipelines(monkeypatch) -> None:
    """A 线很久没发，不能因为 B 线天天在发就把 A 的包清掉。"""
    monkeypatch.setattr("app.modules.artifact.storage.remove", lambda key: False)
    db = _db()
    _test_pid, prod_a = _seed(db)
    prod_g = db.get(Pipeline, prod_a).group_id
    proj_id = db.get(Pipeline, prod_a).project_id
    prod_b = Pipeline(project_id=proj_id, group_id=prod_g, name="other-prod", yaml="")
    db.add(prod_b)
    db.commit()
    db.refresh(prod_b)

    idle = _art(db, prod_a, "idle.zip", days_ago=40)
    fresh = _art(db, prod_b.id, "fresh.zip", days_ago=1)
    prev = _art(db, prod_b.id, "prev.zip", days_ago=20)
    stale = _art(db, prod_b.id, "stale.zip", days_ago=30)

    stat = purge_expired_artifacts(db, prod_retention_days=10, test_retention_days=0)
    assert stat["prod_removed"] == 1
    assert db.get(Artifact, idle.id) is not None
    assert db.get(Artifact, fresh.id) is not None
    assert db.get(Artifact, prev.id) is not None
    assert db.get(Artifact, stale.id) is None


def test_test_env_still_drops_yesterday(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.artifact.storage.remove", lambda key: False)
    db = _db()
    test_pid, prod_pid = _seed(db)
    today = _art(db, test_pid, "today.zip", days_ago=0)
    yest = _art(db, test_pid, "yest.zip", days_ago=1)
    prod_yest = _art(db, prod_pid, "prod-yest.zip", days_ago=1)
    stat = purge_expired_artifacts(db, prod_retention_days=10, test_retention_days=0)
    assert stat["test_removed"] == 1
    assert db.get(Artifact, today.id) is not None
    assert db.get(Artifact, yest.id) is None
    assert db.get(Artifact, prod_yest.id) is not None


def test_settings_value_is_used_when_not_passed(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.artifact.storage.remove", lambda key: False)
    db = _db()
    _test_pid, prod_pid = _seed(db)
    db.add(PlatformSetting(key="artifact_prod_retention_days", value="3"))
    db.commit()
    keep = _art(db, prod_pid, "in-window.zip", days_ago=1)
    mid = _art(db, prod_pid, "prev.zip", days_ago=10)
    drop = _art(db, prod_pid, "out.zip", days_ago=20)
    purge_expired_artifacts(db, test_retention_days=0)
    assert db.get(Artifact, keep.id) is not None
    assert db.get(Artifact, mid.id) is not None
    assert db.get(Artifact, drop.id) is None
