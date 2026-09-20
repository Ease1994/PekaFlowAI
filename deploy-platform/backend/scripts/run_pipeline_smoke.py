# -*- coding: utf-8 -*-
"""run-pipeline：循环检测 / 命名空间 / 任务拆段 本地单测。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "mysql+pymysql://root:root123@127.0.0.1:1/unused"


def test_namespace() -> None:
    from app.modules.pipeline.sub_pipeline import namespaced_outputs, normalize_namespace

    assert normalize_namespace("") == "sub_pipeline_"
    assert normalize_namespace("sub_pipeline") == "sub_pipeline_"
    assert normalize_namespace("foo_") == "foo_"
    out = namespaced_outputs(
        {"deploy_env": "test", "image_tag": "v1"},
        output_vars="deploy_env, missing",
        namespace="sub_pipeline",
        extra={"release_id": 9, "status": "success"},
    )
    assert out["sub_pipeline_deploy_env"] == "test"
    assert "sub_pipeline_image_tag" not in out
    assert out["sub_pipeline_release_id"] == "9"
    print("[ok] namespace")


def test_cycle_and_split() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    import app.db.models  # noqa: F401
    from app.modules.pipeline.models import Pipeline
    from app.modules.pipeline.sub_pipeline import assert_no_cycle, collect_run_pipeline_targets
    from app.modules.agent.task_service import _split_job_segments
    from app.core.response import BizException

    yaml_a = """
pipeline:
  name: A
  stages:
    - name: s
      jobs:
        - id: "1"
          agent: linux
          steps:
            - plugin: run-pipeline
              with:
                pipelineId: 2
"""
    yaml_b = """
pipeline:
  name: B
  stages:
    - name: s
      jobs:
        - id: "1"
          agent: linux
          steps:
            - plugin: run-pipeline
              with:
                pipelineId: 1
"""
    assert collect_run_pipeline_targets(yaml_a) == {2}
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Pipeline(id=1, project_id=1, group_id=1, name="A", yaml=yaml_a, workspace_uuid="a"))
        db.add(Pipeline(id=2, project_id=1, group_id=1, name="B", yaml=yaml_b, workspace_uuid="b"))
        db.commit()
        try:
            assert_no_cycle(db, 1, 2)
            raise AssertionError("cycle should fail")
        except BizException as e:
            assert "循环" in e.message
    print("[ok] cycle")

    segs = _split_job_segments(
        [
            {"plugin": "git-checkout", "with": {}},
            {"plugin": "run-pipeline", "with": {"pipelineId": 2}},
            {"plugin": "shell-exec", "with": {"content": "echo 1"}},
        ]
    )
    assert [k for k, _ in segs] == ["agent", "platform", "agent"]
    print("[ok] split")


if __name__ == "__main__":
    test_namespace()
    test_cycle_and_split()
    print("[ok] run-pipeline unit")
