"""发布计划：权限内任意流水线可挂；清单只在增量线上必填。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.deploy.models import DeployRequest
from app.modules.deploy.service import create_request, delete_request, update_request
from app.modules.pipeline.models import Pipeline


INCREMENTAL_YAML = """
pipeline:
  name: win
  stages:
    - name: 发布
      jobs:
        - name: pack
          steps:
            - name: 提取增量包
              plugin: pack-incremental
              with:
                sourceDir: _publish
                manifest: ${{DEPLOY_MANIFEST}}
"""

FRONTEND_YAML = """
pipeline:
  name: fe
  stages:
    - name: 构建
      jobs:
        - name: build
          steps:
            - name: 前端
              plugin: npm-build
              with:
                script: build
"""


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[DeployRequest.__table__, Pipeline.__table__])
    return Session(engine)


def _pipe(db: Session, name: str, yaml: str) -> Pipeline:
    row = Pipeline(
        project_id=1,
        group_id=1,
        name=name,
        yaml=yaml,
        uses_deploy_manifest="pack-incremental" in yaml,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_frontend_pipeline_plan_without_manifest() -> None:
    """前端静态等非整包增量线，空清单也能建计划。"""
    db = _db()
    pipe = _pipe(db, "前端发布", FRONTEND_YAML)
    req = create_request(
        db,
        project_id=1,
        pipeline_id=pipe.id,
        title="今晚发前端",
        repo="",
        source_ref="",
        changelog="",
        manifest="",
        status="submitted",
        operator_id=1,
    )
    assert req.id
    assert req.manifest == ""


def test_incremental_pipeline_requires_manifest() -> None:
    db = _db()
    pipe = _pipe(db, "Windows增量发布", INCREMENTAL_YAML)
    try:
        create_request(
            db,
            project_id=1,
            pipeline_id=pipe.id,
            title="今晚发 Windows",
            repo="",
            source_ref="",
            changelog="",
            manifest="",
            status="submitted",
            operator_id=1,
        )
        raise AssertionError("增量线空清单必须拒绝")
    except BizException as exc:
        assert exc.code == 400
        assert "清单" in exc.message


def test_incremental_pipeline_accepts_manifest() -> None:
    db = _db()
    pipe = _pipe(db, "Windows增量发布", INCREMENTAL_YAML)
    req = create_request(
        db,
        project_id=1,
        pipeline_id=pipe.id,
        title="今晚发 Windows",
        repo="",
        source_ref="",
        changelog="",
        manifest="bin/*.dll\nAreas/",
        status="submitted",
        operator_id=1,
    )
    assert "bin/*.dll" in req.manifest


def test_update_and_delete_plan() -> None:
    db = _db()
    pipe = _pipe(db, "前端发布", FRONTEND_YAML)
    req = create_request(
        db,
        project_id=1,
        pipeline_id=pipe.id,
        title="原标题",
        repo="",
        source_ref="",
        changelog="",
        manifest="",
        status="submitted",
        operator_id=1,
    )
    updated = update_request(db, req.id, {"title": "改过的计划"})
    assert updated.title == "改过的计划"
    delete_request(db, req.id)
    assert db.get(DeployRequest, req.id) is None


def test_cannot_delete_releasing_plan() -> None:
    db = _db()
    pipe = _pipe(db, "前端发布", FRONTEND_YAML)
    req = create_request(
        db,
        project_id=1,
        pipeline_id=pipe.id,
        title="发布中",
        repo="",
        source_ref="",
        changelog="",
        manifest="",
        status="submitted",
        operator_id=1,
    )
    req.status = "releasing"
    db.commit()
    try:
        delete_request(db, req.id)
        raise AssertionError("发布中不能删")
    except BizException as exc:
        assert exc.code == 400
        assert "正在发布" in exc.message
