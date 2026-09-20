"""插件试跑不能占用生产流水线。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.pipeline.models import Pipeline
from app.modules.project.models import Group, Project
from app.modules.store.models import PluginDraft


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Project.__table__, Group.__table__, Pipeline.__table__, PluginDraft.__table__],
    )
    return Session(engine)


def test_trial_rejects_prod_pipeline(monkeypatch) -> None:
    from app.modules.store import draft_service

    db = _db()
    proj = Project(name="p", code="p")
    db.add(proj)
    db.flush()
    g = Group(project_id=proj.id, name="生产", type="prod")
    db.add(g)
    db.flush()
    pipe = Pipeline(project_id=proj.id, group_id=g.id, name="prod-line", yaml="", status="active")
    db.add(pipe)
    draft = PluginDraft(
        name="demo",
        version="0.1",
        language="python",
        entrypoint="python3 task.py",
        files_json="{}",
    )
    db.add(draft)
    db.commit()
    monkeypatch.setattr(draft_service, "relint", lambda *_a, **_k: {"ok": True})
    try:
        draft_service.start_trial(
            db, draft.id, pipeline_id=pipe.id, agent_tag="linux", params={}, operator_id=1
        )
        raise AssertionError("should reject")
    except BizException as e:
        assert "试跑" in e.message or "测试" in e.message
