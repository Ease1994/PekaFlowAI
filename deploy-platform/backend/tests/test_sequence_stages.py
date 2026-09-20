# -*- coding: utf-8 -*-
"""执行序列：编排定义与已落库任务合并，未开跑的 Job 不能从树上消失。"""
from app.modules.pipeline.router import _merge_sequence_stages


def _job(job_id: str, name: str, status: str, steps: int = 1) -> dict:
    """构造一条序列 Job，字段与接口返回对齐。"""
    return {
        "task_id": 1 if status != "pending" else None,
        "id": job_id,
        "name": name,
        "agent": "linux",
        "status": status,
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "duration_label": None,
        "steps": [
            {
                "order": i,
                "task_id": 1 if status != "pending" else None,
                "log_index": i,
                "name": f"step-{i}",
                "plugin": "maven-build",
                "with": {},
                "status": status,
                "started_at": None,
                "duration": None,
                "duration_label": None,
            }
            for i in range(steps)
        ],
    }


def test_merge_keeps_unstarted_jobs_when_only_first_task_exists() -> None:
    """Maven 已开跑时，Docker 构建/部署仍应留在树上，不能只剩第一条任务。"""
    planned = [
        {
            "id": "stage-1",
            "name": "构建环境-Linux",
            "status": "pending",
            "jobs": [
                _job("maven-build", "Maven 构建", "pending"),
                _job("docker-build", "Docker 镜像构建", "pending"),
                _job("docker-deploy", "Docker 容器部署", "pending"),
            ],
        }
    ]
    live = [
        {
            "name": "构建环境-Linux",
            "status": "running",
            "jobs": [_job("maven-build", "Maven 构建", "running")],
        }
    ]
    got = _merge_sequence_stages(planned, live)
    ids = [j["id"] for j in got[0]["jobs"]]
    assert ids == ["maven-build", "docker-build", "docker-deploy"]
    assert got[0]["jobs"][0]["status"] == "running"
    assert got[0]["jobs"][1]["status"] == "pending"
    assert got[0]["jobs"][2]["status"] == "pending"
    assert got[0]["status"] == "running"


def test_merge_uses_live_when_all_jobs_have_tasks() -> None:
    """全部 Job 都有任务时，用实况状态，不再保留计划里的 pending。"""
    planned = [
        {
            "id": "stage-1",
            "name": "构建",
            "status": "pending",
            "jobs": [
                _job("a", "A", "pending"),
                _job("b", "B", "pending"),
            ],
        }
    ]
    live = [
        {
            "name": "构建",
            "status": "success",
            "jobs": [_job("a", "A", "success"), _job("b", "B", "success")],
        }
    ]
    got = _merge_sequence_stages(planned, live)
    assert [j["status"] for j in got[0]["jobs"]] == ["success", "success"]
    assert got[0]["status"] == "success"


def test_merge_keeps_later_stage_when_only_first_stage_has_tasks() -> None:
    """只建了第一阶段任务时，后面的阶段仍按计划摆着。"""
    planned = [
        {"id": "stage-1", "name": "构建", "status": "pending", "jobs": [_job("a", "A", "pending")]},
        {"id": "stage-2", "name": "发布", "status": "pending", "jobs": [_job("b", "B", "pending")]},
    ]
    live = [{"name": "构建", "status": "running", "jobs": [_job("a", "A", "running")]}]
    got = _merge_sequence_stages(planned, live)
    assert [s["name"] for s in got] == ["构建", "发布"]
    assert got[0]["jobs"][0]["status"] == "running"
    assert got[1]["jobs"][0]["status"] == "pending"


def test_merge_appends_live_jobs_not_in_plan() -> None:
    """任务里多出来的 Job（拆段、回滚补步）接到该阶段末尾，不能丢掉。"""
    planned = [
        {"id": "stage-1", "name": "构建", "status": "pending", "jobs": [_job("a", "A", "pending")]}
    ]
    live = [
        {
            "name": "构建",
            "status": "running",
            "jobs": [_job("a", "A", "success"), _job("extra", "补步", "running")],
        }
    ]
    got = _merge_sequence_stages(planned, live)
    assert [j["id"] for j in got[0]["jobs"]] == ["a", "extra"]


def test_merge_empty_sides() -> None:
    """只有一边有数据时，原样返回，不能拼出空树。"""
    planned = [{"id": "stage-1", "name": "构建", "status": "pending", "jobs": [_job("a", "A", "pending")]}]
    live = [{"name": "构建", "status": "running", "jobs": [_job("a", "A", "running")]}]
    assert _merge_sequence_stages([], live) == live
    assert _merge_sequence_stages(planned, []) == planned
