# -*- coding: utf-8 -*-
"""AI 往节点下发文件的审批闸门。

生产节点必须先进审批。测试平台上也会故意标几台「生产」来把这条链路测完整，
闸门不能因为「整套环境是测试的」就放行——它只看节点自己的 env。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.env import skip_node_push_approval
from app.db.base import Base
from app.modules.ai.nodepush import SYSTEM_GROUP_NAME, ensure_pipeline
from app.modules.pipeline.models import Pipeline
from app.modules.pipeline.service import approval_required_for
from app.modules.project.models import Group, Project


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Project.__table__, Group.__table__, Pipeline.__table__],
    )
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_prod_node_must_be_gated_test_node_may_skip():
    assert skip_node_push_approval("prod") is False
    assert skip_node_push_approval("") is False
    assert skip_node_push_approval(None) is False
    assert skip_node_push_approval("uat") is False
    assert skip_node_push_approval("test") is True
    assert skip_node_push_approval("dev") is True


def test_builtin_group_heals_if_someone_turned_approval_off(db: Session):
    """分组模型默认 approval_required=False。只在首次创建时写成 True 不够：
    老数据或有人在项目页关掉审批之后，生产节点下发会直接落盘。
    """
    pipe = ensure_pipeline(db)
    grp = db.get(Group, pipe.group_id)
    assert grp is not None
    grp.approval_required = False
    grp.type = "test"
    pipe.approval_mode = "exempt"
    db.commit()

    healed = ensure_pipeline(db)
    grp = db.get(Group, healed.group_id)
    assert grp is not None
    assert grp.approval_required is True
    assert grp.type == "prod"
    assert (healed.approval_mode or "inherit") == "inherit"
    assert approval_required_for(grp, healed) is True


def test_visible_pipelines_hides_platform_builtin(db: Session):
    """节点文件下发是平台自用线，不能混进「有哪些流水线 / 正在发布」。"""
    from types import SimpleNamespace

    from app.modules.ai.context import visible_pipelines
    from app.modules.ai.nodepush import SYSTEM_PIPELINE_NAME

    ensure_pipeline(db)
    biz = Project(name="业务", code="BIZ")
    db.add(biz)
    db.flush()
    grp = Group(project_id=biz.id, name="测试", type="test")
    db.add(grp)
    db.flush()
    db.add(Pipeline(project_id=biz.id, group_id=grp.id, name="order-service", yaml="pipeline:\n  stages: []"))
    db.commit()

    names = [p.name for p, _, _ in visible_pipelines(db, SimpleNamespace(id=1, is_admin=True))]
    assert "order-service" in names
    assert SYSTEM_PIPELINE_NAME not in names


def test_check_target_dir_rejects_node_without_allow_paths():
    """没有允许目录的节点不能把校验推到节点侧，平台先拒绝。"""
    from types import SimpleNamespace

    from app.core.response import BizException
    from app.modules.ai.nodepush import check_target_dir

    node = SimpleNamespace(name="web-01", allow_paths="[]")
    with pytest.raises(BizException) as ei:
        check_target_dir(node, r"D:\wwwroot\site")
    assert "允许目录" in str(ei.value.message)
