"""发布计划：权限内任意流水线可挂；清单只在增量线上必填。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.deploy.models import DeployRequest
from app.modules.deploy.service import (
    MISSING_MANIFEST_MSG,
    VAR_MANIFEST,
    create_request,
    delete_request,
    run_params_for,
    update_request,
)
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

HARDCODED_YAML = """
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
                manifest: |
                  bin/*.dll
                  Areas/
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


def test_incremental_empty_ticket_cannot_become_run_params() -> None:
    """草稿空清单不能写成 DEPLOY_MANIFEST=""：占位会被替换成空，打包要跑到现场才失败。"""
    db = _db()
    pipe = _pipe(db, "Windows增量发布", INCREMENTAL_YAML)
    req = create_request(
        db,
        project_id=1,
        pipeline_id=pipe.id,
        title="草稿",
        repo="",
        source_ref="",
        changelog="",
        manifest="",
        status="draft",
        operator_id=1,
    )
    try:
        run_params_for(req, pipe)
        raise AssertionError("增量占位未填必须拒绝")
    except BizException as exc:
        assert exc.code == 400
        assert exc.message == MISSING_MANIFEST_MSG


def test_incremental_ticket_manifest_wins_over_extra() -> None:
    """额外 run_params 里的 ** 不能盖过单子点名的文件。"""
    db = _db()
    pipe = _pipe(db, "Windows增量发布", INCREMENTAL_YAML)
    req = create_request(
        db,
        project_id=1,
        pipeline_id=pipe.id,
        title="今晚发 Windows",
        repo="",
        source_ref="",
        changelog="修登录",
        manifest="bin/*.dll\nAreas/",
        status="submitted",
        operator_id=1,
    )
    params = run_params_for(req, pipe, {VAR_MANIFEST: "**", "FOO": "1"})
    assert params[VAR_MANIFEST] == "bin/*.dll\nAreas/"
    assert params["FOO"] == "1"
    assert params["DEPLOY_CHANGELOG"] == "修登录"
    assert params["DEPLOY_REQUEST_ID"] == str(req.id)


def test_frontend_plan_run_params_omit_empty_manifest() -> None:
    """非整包增量线不写 DEPLOY_MANIFEST，空串不能进变量表。"""
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
    params = run_params_for(req, pipe)
    assert VAR_MANIFEST not in params
    assert params["DEPLOY_REQUEST_ID"] == str(req.id)


def test_hardcoded_step_empty_ticket_does_not_blank_manifest() -> None:
    """步骤已写死文件列表时，空单子不能注入空的 DEPLOY_MANIFEST。"""
    db = _db()
    pipe = _pipe(db, "Windows写死清单", HARDCODED_YAML)
    req = create_request(
        db,
        project_id=1,
        pipeline_id=pipe.id,
        title="草稿",
        repo="",
        source_ref="",
        changelog="",
        manifest="",
        status="draft",
        operator_id=1,
    )
    params = run_params_for(req, pipe)
    assert VAR_MANIFEST not in params


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
