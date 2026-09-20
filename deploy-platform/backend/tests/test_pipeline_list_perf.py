# -*- coding: utf-8 -*-
"""列表路径不再搬 YAML、可见性与 check_permission 对齐、派生列可驱动筛选和环检测。"""
from __future__ import annotations

import json

from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import Session, sessionmaker

import app.db.models  # noqa: F401
from app.core.deps import CurrentUser, check_permission, visible_pipeline_ids
from app.db.base import Base
from app.modules.auth.models import Permission, Role, User, UserRole
from app.modules.deploy.service import consumes_deploy_manifest
from app.modules.pipeline.models import Pipeline, YAML_FEATURES_VER
from app.modules.pipeline.service import _apply_yaml_feature_columns, backfill_trigger_columns
from app.modules.pipeline.sub_pipeline import (
    build_run_pipeline_graph,
    collect_run_pipeline_targets,
    encode_sub_pipeline_ids,
)
from app.modules.project.models import Group, Project


MANUAL_YAML = """
pipeline:
  name: manual-line
  triggers:
    - type: manual
  stages: []
"""

MANIFEST_YAML = """
pipeline:
  name: iis-incr
  triggers:
    - type: manual
  stages:
    - name: 发布
      jobs:
        - name: pack
          steps:
            - name: 增量包
              plugin: pack-incremental
              with:
                manifest: ${{DEPLOY_MANIFEST}}
"""

SUB_YAML = """
pipeline:
  name: caller
  triggers:
    - type: manual
  stages:
    - name: 调用
      jobs:
        - name: run
          steps:
            - name: 子流水线
              plugin: run-pipeline
              with:
                pipelineId: 9
"""

CRON_YAML = """
pipeline:
  name: nightly
  triggers:
    - type: cron
      cron: "0 2 * * *"
  stages: []
"""


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db: Session, name: str, *, admin: bool = False) -> User:
    u = User(username=name, display_name=name, password_hash="x", is_admin=admin)
    db.add(u)
    db.flush()
    return u


def _seed_project(db: Session) -> tuple[Project, Group, Group]:
    proj = Project(name="订单", code="order", description="")
    db.add(proj)
    db.flush()
    test = Group(project_id=proj.id, name="测试", type="test")
    prod = Group(project_id=proj.id, name="生产", type="prod")
    db.add_all([test, prod])
    db.flush()
    return proj, test, prod


def _pipe(db: Session, proj: Project, group: Group, name: str, yaml: str = "") -> Pipeline:
    p = Pipeline(project_id=proj.id, group_id=group.id, name=name, yaml=yaml or MANUAL_YAML)
    db.add(p)
    db.flush()
    return p


def _actor(u: User) -> CurrentUser:
    return CurrentUser(id=u.id, username=u.username, is_admin=u.is_admin)


def test_visible_pipeline_ids_matches_check_permission() -> None:
    db = _db()
    proj, test, prod = _seed_project(db)
    a = _pipe(db, proj, test, "a")
    b = _pipe(db, proj, test, "b")
    c = _pipe(db, proj, prod, "c")
    user = _user(db, "dev")
    db.add(Permission(user_id=user.id, resource_type="pipeline", resource_id=a.id, action="read"))
    db.add(Permission(user_id=user.id, resource_type="group", resource_id=prod.id, action="*"))
    db.add(Permission(user_id=user.id, resource_type="pipeline", resource_id=c.id, action="read", effect="deny"))
    db.commit()

    actor = _actor(user)
    visible = visible_pipeline_ids(db, actor)
    assert visible is not None
    for p in (a, b, c):
        assert (p.id in visible) == check_permission(db, actor, "pipeline", p.id, "read")
    assert a.id in visible
    assert b.id not in visible
    assert c.id not in visible


def test_visible_pipeline_ids_role_covers_project() -> None:
    db = _db()
    proj, test, _prod = _seed_project(db)
    p = _pipe(db, proj, test, "owned")
    user = _user(db, "ops")
    role = Role(
        project_id=proj.id,
        name="开发",
        permissions=json.dumps({"pipeline": ["read", "execute"]}),
    )
    db.add(role)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()

    actor = _actor(user)
    visible = visible_pipeline_ids(db, actor)
    assert visible == {p.id}
    assert check_permission(db, actor, "pipeline", p.id, "read") is True


def test_yaml_feature_columns_from_manifest_and_sub() -> None:
    db = _db()
    proj, test, _ = _seed_project(db)
    man = _pipe(db, proj, test, "iis", MANIFEST_YAML)
    sub = _pipe(db, proj, test, "caller", SUB_YAML)
    plain = _pipe(db, proj, test, "plain", MANUAL_YAML)
    _apply_yaml_feature_columns(man)
    _apply_yaml_feature_columns(sub)
    _apply_yaml_feature_columns(plain)
    db.commit()

    assert man.uses_deploy_manifest is True
    assert consumes_deploy_manifest(man) is True
    assert sub.sub_pipeline_ids == encode_sub_pipeline_ids(collect_run_pipeline_targets(SUB_YAML))
    assert parse_expected_sub(sub.sub_pipeline_ids) == {9}
    assert plain.uses_deploy_manifest is False
    assert plain.sub_pipeline_ids == ""
    assert man.yaml_features_ver == YAML_FEATURES_VER

    graph = build_run_pipeline_graph(db)
    assert graph[sub.id] == {9}
    assert graph[plain.id] == set()


def parse_expected_sub(raw: str) -> set[int]:
    from app.modules.pipeline.sub_pipeline import parse_sub_pipeline_ids

    return parse_sub_pipeline_ids(raw)


def test_backfill_skips_already_current_rows() -> None:
    db = _db()
    proj, test, _ = _seed_project(db)
    stale = _pipe(db, proj, test, "stale", CRON_YAML)
    stale.yaml_features_ver = 0
    done = _pipe(db, proj, test, "done", CRON_YAML)
    done.yaml_features_ver = YAML_FEATURES_VER
    done.trigger_type = "manual"
    done.cron_expr = ""
    db.commit()

    n = backfill_trigger_columns(db)
    db.refresh(stale)
    db.refresh(done)
    assert n == 1
    assert stale.trigger_type == "cron"
    assert stale.cron_expr == "0 2 * * *"
    assert stale.yaml_features_ver == YAML_FEATURES_VER
    assert done.trigger_type == "manual"
    assert done.cron_expr == ""

    assert backfill_trigger_columns(db) == 0


def test_list_pipelines_filters_by_name() -> None:
    """授权联动：记得流水线名里几个字，不必先知道分组、也不必拉全站。"""
    from app.modules.pipeline.service import list_pipelines

    db = _db()
    proj, test, prod = _seed_project(db)
    _pipe(db, proj, test, "oms-link-server")
    _pipe(db, proj, prod, "oms-link-app")
    _pipe(db, proj, test, "other-line")
    db.commit()

    rows = list_pipelines(db, proj.id, None, q="oms-link")
    assert {p.name for p in rows} == {"oms-link-server", "oms-link-app"}
    limited = list_pipelines(db, proj.id, None, q="oms", limit=1)
    assert len(limited) == 1
    grouped = list_pipelines(db, proj.id, test.id, q="oms")
    assert [p.name for p in grouped] == ["oms-link-server"]


def test_list_pipelines_defers_yaml() -> None:
    from app.modules.pipeline.service import list_pipelines

    db = _db()
    proj, test, _ = _seed_project(db)
    p = _pipe(db, proj, test, "heavy", "pipeline:\n  name: heavy\n  stages: []\n" + ("# x\n" * 50))
    _apply_yaml_feature_columns(p)
    db.commit()

    rows = list_pipelines(db, proj.id, None)
    assert len(rows) == 1
    assert "yaml" in sa_inspect(rows[0]).unloaded


def test_list_releases_defers_snapshot() -> None:
    from app.modules.pipeline.models import Release
    from app.modules.pipeline.service import list_releases

    db = _db()
    proj, test, _ = _seed_project(db)
    p = _pipe(db, proj, test, "hist")
    db.add(
        Release(
            pipeline_id=p.id,
            group_id=test.id,
            status="failed",
            snapshot="{" + ("x" * 200) + "}",
            error_message="boom",
        )
    )
    db.commit()
    rows, total = list_releases(db, pipeline_id=p.id, page=1, page_size=20)
    assert total == 1
    assert len(rows) == 1
    assert "snapshot" in sa_inspect(rows[0]).unloaded
    assert rows[0].error_message == "boom"


def test_admin_visible_is_none() -> None:
    db = _db()
    admin = _user(db, "root", admin=True)
    assert visible_pipeline_ids(db, _actor(admin)) is None
