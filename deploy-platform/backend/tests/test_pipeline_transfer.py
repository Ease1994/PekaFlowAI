"""项目 + 流水线导入导出：重名列出后由调用方选覆盖、_copy 或跳过。"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.db.base import Base
from app.modules.auth.models import Permission, Role, UserRole
from app.modules.pipeline.models import Pipeline
from app.modules.pipeline.sub_pipeline import collect_run_pipeline_targets
from app.modules.pipeline.transfer import apply_import, export_catalog, item_key, preview_import
from app.modules.project.models import Group, Project

ADMIN = CurrentUser(id=1, username="admin", is_admin=True)

_PARENT_YAML = """
pipeline:
  name: parent-line
  stages:
    - name: s1
      jobs:
        - id: j1
          name: job
          steps:
            - plugin: run-pipeline
              with:
                pipelineId: {child_id}
"""


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Project.__table__,
            Group.__table__,
            Pipeline.__table__,
            Permission.__table__,
            Role.__table__,
            UserRole.__table__,
        ],
    )
    return Session(engine)


def _project(db: Session, code: str = "B2C") -> tuple[Project, Group, Group]:
    proj = Project(name="B2C 电商", code=code, description="电商")
    db.add(proj)
    db.flush()
    prod = Group(project_id=proj.id, name="生产", type="prod", approval_required=True)
    test = Group(project_id=proj.id, name="测试", type="test", approval_required=False)
    db.add_all([prod, test])
    db.commit()
    db.refresh(proj)
    return proj, prod, test


def test_export_includes_empty_project() -> None:
    db = _db()
    proj, _, _ = _project(db)
    bundle = export_catalog(db, ADMIN, project_ids=[proj.id])
    assert bundle["format"] == "rp-catalog"
    assert bundle["projects"][0]["code"] == "B2C"
    assert bundle["projects"][0]["pipelines"] == []
    assert [g["name"] for g in bundle["projects"][0]["groups"]] == ["生产", "测试"]


def test_roundtrip_create_then_conflict_copy_and_overwrite() -> None:
    src = _db()
    proj, prod, _ = _project(src)
    child = Pipeline(project_id=proj.id, group_id=prod.id, name="child-line", yaml="pipeline:\n  name: child-line\n")
    src.add(child)
    src.commit()
    src.refresh(child)
    parent = Pipeline(
        project_id=proj.id,
        group_id=prod.id,
        name="parent-line",
        yaml=_PARENT_YAML.format(child_id=child.id),
    )
    src.add(parent)
    src.commit()

    bundle = export_catalog(src, ADMIN, project_ids=[proj.id])
    assert {p["name"] for p in bundle["projects"][0]["pipelines"]} == {"child-line", "parent-line"}

    dst = _db()
    preview = preview_import(dst, ADMIN, bundle)
    assert preview["conflict_count"] == 0
    assert preview["create_count"] == 2
    assert preview["new_projects"][0]["code"] == "B2C"

    first = apply_import(dst, ADMIN, bundle, {})
    assert first["imported"] == 2
    child_row = dst.scalar(select(Pipeline).where(Pipeline.name == "child-line"))
    parent_row = dst.scalar(select(Pipeline).where(Pipeline.name == "parent-line"))
    assert child_row is not None and parent_row is not None
    assert collect_run_pipeline_targets(parent_row.yaml or "") == {child_row.id}

    preview2 = preview_import(dst, ADMIN, bundle)
    assert preview2["conflict_count"] == 2
    try:
        apply_import(dst, ADMIN, bundle, {})
        raise AssertionError("must ask for decisions")
    except BizException as exc:
        assert exc.code == 400

    copied = apply_import(
        dst,
        ADMIN,
        bundle,
        {
            item_key("B2C", "child-line"): "copy",
            item_key("B2C", "parent-line"): "copy",
        },
    )
    assert copied["imported"] == 2
    copy_child = dst.scalar(select(Pipeline).where(Pipeline.name == "child-line_copy"))
    copy_parent = dst.scalar(select(Pipeline).where(Pipeline.name == "parent-line_copy"))
    assert copy_child is not None and copy_parent is not None
    assert collect_run_pipeline_targets(copy_parent.yaml or "") == {copy_child.id}

    child_row.yaml = "pipeline:\n  name: child-line\n  description: old\n"
    dst.commit()
    overwritten = apply_import(
        dst,
        ADMIN,
        bundle,
        {
            item_key("B2C", "child-line"): "overwrite",
            item_key("B2C", "parent-line"): "overwrite",
        },
    )
    assert overwritten["imported"] == 2
    dst.refresh(child_row)
    assert "old" not in (child_row.yaml or "")


def test_conflict_skip_leaves_existing_pipeline() -> None:
    """跳过重名流水线：已有配置不动，也不建 _copy。"""
    src = _db()
    proj, prod, _ = _project(src)
    src.add(Pipeline(project_id=proj.id, group_id=prod.id, name="keep-me", yaml="pipeline:\n  name: keep-me\n"))
    src.commit()
    bundle = export_catalog(src, ADMIN, project_ids=[proj.id])

    dst = _db()
    apply_import(dst, ADMIN, bundle, {})
    row = dst.scalar(select(Pipeline).where(Pipeline.name == "keep-me"))
    assert row is not None
    row.yaml = "pipeline:\n  name: keep-me\n  description: local\n"
    dst.commit()

    skipped = apply_import(dst, ADMIN, bundle, {item_key("B2C", "keep-me"): "skip"})
    assert skipped["imported"] == 0
    actions = {r["action"] for r in skipped["results"]}
    assert actions == {"skip"}
    dst.refresh(row)
    assert "local" in (row.yaml or "")
    assert dst.scalar(select(Pipeline).where(Pipeline.name == "keep-me_copy")) is None


def test_non_admin_cannot_import_or_export() -> None:
    src = _db()
    proj, prod, _ = _project(src)
    src.add(Pipeline(project_id=proj.id, group_id=prod.id, name="demo", yaml=""))
    src.commit()
    user = CurrentUser(id=2, username="dev", is_admin=False)
    try:
        export_catalog(src, user, project_ids=[proj.id])
        raise AssertionError("non-admin export should fail")
    except BizException as exc:
        assert exc.code == 403
    bundle = export_catalog(src, ADMIN, project_ids=[proj.id])
    dst = _db()
    try:
        preview_import(dst, user, bundle)
        raise AssertionError("non-admin preview should fail")
    except BizException as exc:
        assert exc.code == 403
    try:
        apply_import(dst, user, bundle, {})
        raise AssertionError("non-admin import should fail")
    except BizException as exc:
        assert exc.code == 403
