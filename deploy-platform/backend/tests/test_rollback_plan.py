# -*- coding: utf-8 -*-
"""回滚计划：整次发布启停、多站点对齐、不完整记录拒绝、已撤销不误报。"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.agent.models import BuildAgent, BuildTask
from app.modules.deployment.models import DeploymentRecord
from app.modules.deployment.service import (
    _file_backup_steps,
    build_undo_jobs,
    check_undoable,
    latest_on_target,
    mark_release_records_undone,
    preview_item,
)
from app.modules.pipeline.models import Release
from app.modules.pipeline.service import (
    RELEASE_PENDING,
    RELEASE_QUEUED,
    inflight_rollback_of,
)


def _db(*tables) -> Session:
    """内存库。回滚提示会去查节点名和构建号，这两张表始终建上。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    needed = list(tables)
    names = {t.__table__.name for t in needed}
    for extra in (BuildAgent, Release):
        if extra.__table__.name not in names:
            needed.append(extra)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in needed])
    return Session(engine)


def _task(db: Session, release_id: int, steps: list[dict], pipeline_id: int = 1) -> BuildTask:
    task = BuildTask(
        release_id=release_id,
        pipeline_id=pipeline_id,
        steps_json=json.dumps(steps, ensure_ascii=False),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _row(
    db: Session,
    *,
    release_id: int,
    task: BuildTask,
    step_index: int,
    payload: dict,
    target: str = "",
    kind: str = "file-backup",
    pipeline_id: int = 1,
    agent_id: int = 9,
    summary: str = "",
) -> DeploymentRecord:
    row = DeploymentRecord(
        release_id=release_id,
        pipeline_id=pipeline_id,
        task_id=task.id,
        step_index=step_index,
        agent_id=agent_id,
        kind=kind,
        payload_json=json.dumps(payload, ensure_ascii=False),
        target=target or str(payload.get("targetDir") or ""),
        summary=summary,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _names(db: Session, row: DeploymentRecord) -> list[str]:
    return [s["name"] for s in _file_backup_steps(db, row, json.loads(row.payload_json))]


def test_same_task_replays_pool_and_site() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(
            db,
            32,
            [
                {"name": "停止应用池", "plugin": "iis-control", "with": {"action": "stop", "target": "apppool", "name": "AppPool"}},
                {"name": "停止站点", "plugin": "iis-control", "with": {"action": "stop", "target": "site", "name": "Site_Uat"}},
                {"name": "file-transfer", "plugin": "file-transfer", "with": {"targetDir": r"D:\wwwroot\testagent"}},
                {"name": "启动应用池", "plugin": "iis-control", "with": {"action": "start", "target": "apppool", "name": "AppPool"}},
                {"name": "启动站点", "plugin": "iis-control", "with": {"action": "start", "target": "site", "name": "Site_Uat"}},
            ],
        )
        row = _row(
            db,
            release_id=32,
            task=task,
            step_index=2,
            payload={
                "targetDir": r"D:\wwwroot\testagent",
                "backupDir": r"D:\backup\x",
                "stop_with": {"action": "stop", "name": "Site_Uat"},
                "start_with": {"action": "start", "name": "AppPool"},
            },
        )
        assert _names(db, row) == [
            "停止应用池",
            "停止站点",
            r"还原备份到 D:\wwwroot\testagent",
            "启动应用池",
            "启动站点",
        ]
    finally:
        db.close()


def test_cross_job_stop_and_start() -> None:
    """停 IIS、传文件、起 IIS 拆成三个任务时，回滚必须整段重放。"""
    db = _db(BuildTask, DeploymentRecord)
    try:
        _task(
            db,
            40,
            [{"name": "停止应用池", "plugin": "iis-control", "with": {"action": "stop", "name": "PoolA"}},
             {"name": "停止站点", "plugin": "iis-control", "with": {"action": "stop", "name": "SiteA"}}],
        )
        transfer = _task(
            db,
            40,
            [{"name": "file-transfer", "plugin": "file-transfer", "with": {"targetDir": r"D:\site\a"}}],
        )
        _task(
            db,
            40,
            [{"name": "启动应用池", "plugin": "iis-control", "with": {"action": "start", "name": "PoolA"}},
             {"name": "启动站点", "plugin": "iis-control", "with": {"action": "start", "name": "SiteA"}}],
        )
        row = _row(
            db,
            release_id=40,
            task=transfer,
            step_index=0,
            payload={"targetDir": r"D:\site\a", "backupDir": r"D:\backup\a"},
        )
        assert _names(db, row) == [
            "停止应用池",
            "停止站点",
            r"还原备份到 D:\site\a",
            "启动应用池",
            "启动站点",
        ]
    finally:
        db.close()


def test_skips_build_and_status_between_stop_and_transfer() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        _task(db, 41, [{"name": "停止应用池", "plugin": "iis-control", "with": {"action": "stop", "name": "PoolA"}}])
        _task(db, 41, [{"name": "msbuild", "plugin": "msbuild-build", "with": {}}])
        transfer = _task(
            db,
            41,
            [
                {"name": "查状态", "plugin": "iis-control", "with": {"action": "status", "name": "PoolA"}},
                {"name": "file-transfer", "plugin": "file-transfer", "with": {"targetDir": r"D:\site\a"}},
                {"name": "回收应用池", "plugin": "iis-control", "with": {"action": "recycle", "name": "PoolA"}},
            ],
        )
        row = _row(
            db,
            release_id=41,
            task=transfer,
            step_index=1,
            payload={"targetDir": r"D:\site\a", "backupDir": r"D:\backup\a"},
        )
        assert _names(db, row) == [
            "停止应用池",
            r"还原备份到 D:\site\a",
            "回收应用池",
        ]
    finally:
        db.close()


def test_two_sites_do_not_steal_each_others_iis() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(
            db,
            42,
            [
                {"name": "停 A 池", "plugin": "iis-control", "with": {"action": "stop", "name": "PoolA"}},
                {"name": "传 A", "plugin": "file-transfer", "with": {"targetDir": r"D:\site\a"}},
                {"name": "起 A 池", "plugin": "iis-control", "with": {"action": "start", "name": "PoolA"}},
                {"name": "停 B 池", "plugin": "iis-control", "with": {"action": "stop", "name": "PoolB"}},
                {"name": "传 B", "plugin": "file-transfer", "with": {"targetDir": r"D:\site\b"}},
                {"name": "起 B 池", "plugin": "iis-control", "with": {"action": "start", "name": "PoolB"}},
            ],
        )
        row_a = _row(
            db,
            release_id=42,
            task=task,
            step_index=99,
            payload={"targetDir": r"D:\site\a", "backupDir": r"D:\backup\a"},
            target=r"D:\site\a",
        )
        row_b = _row(
            db,
            release_id=42,
            task=task,
            step_index=99,
            payload={"targetDir": r"D:\site\b", "backupDir": r"D:\backup\b"},
            target=r"D:\site\b",
        )
        assert _names(db, row_a) == ["停 A 池", r"还原备份到 D:\site\a", "起 A 池"]
        assert _names(db, row_b) == ["停 B 池", r"还原备份到 D:\site\b", "起 B 池"]
    finally:
        db.close()


def test_missing_backup_dir_refuses_rollback() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(db, 43, [{"plugin": "file-transfer", "with": {"targetDir": r"D:\site\a"}}])
        _row(
            db,
            release_id=43,
            task=task,
            step_index=0,
            payload={"targetDir": r"D:\site\a", "backupDir": ""},
            target=r"D:\site\a",
        )
        rows, _warnings, reason = check_undoable(db, 43)
        assert rows == []
        assert "备份目录" in reason
        assert build_undo_jobs(db, records_of(db, 43)) == []
    finally:
        db.close()


def records_of(db: Session, release_id: int) -> list[DeploymentRecord]:
    from app.modules.deployment.service import records_of_release
    return records_of_release(db, release_id)


def test_incomplete_one_site_blocks_the_whole_release() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(
            db,
            44,
            [
                {"plugin": "file-transfer", "with": {"targetDir": r"D:\site\a"}},
                {"plugin": "file-transfer", "with": {"targetDir": r"D:\site\b"}},
            ],
        )
        _row(
            db,
            release_id=44,
            task=task,
            step_index=0,
            payload={"targetDir": r"D:\site\a", "backupDir": r"D:\backup\a"},
            target=r"D:\site\a",
        )
        _row(
            db,
            release_id=44,
            task=task,
            step_index=1,
            payload={"targetDir": r"D:\site\b", "backupDir": ""},
            target=r"D:\site\b",
        )
        rows, _warnings, reason = check_undoable(db, 44)
        assert rows == []
        assert "拆成两半" in reason
    finally:
        db.close()


def test_docker_and_k8s_without_undo_target_refused() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(db, 45, [{"plugin": "docker-deploy", "with": {"image": "app:2"}}])
        _row(
            db,
            release_id=45,
            task=task,
            step_index=0,
            payload={"undo_with": {}},
            target="app",
            kind="docker-image",
        )
        rows, _warnings, reason = check_undoable(db, 45)
        assert rows == []
        assert "镜像" in reason

        k8s = _row(
            db,
            release_id=46,
            task=task,
            step_index=0,
            payload={"undo_with": {"namespace": "prod"}, "previous_revision": ""},
            target="prod/deploy/web",
            kind="k8s-revision",
        )
        rows, _warnings, reason = check_undoable(db, 46)
        assert rows == []
        assert "revision" in reason
        assert build_undo_jobs(db, [k8s]) == []
    finally:
        db.close()


def test_later_undone_deploy_is_not_a_newer_cover() -> None:
    db = _db(DeploymentRecord)
    try:
        older = DeploymentRecord(
            release_id=10,
            pipeline_id=1,
            task_id=1,
            step_index=0,
            kind="file-backup",
            payload_json=json.dumps({"targetDir": r"D:\site", "backupDir": r"D:\b1"}),
            target=r"D:\site",
        )
        newer = DeploymentRecord(
            release_id=11,
            pipeline_id=1,
            task_id=2,
            step_index=0,
            kind="file-backup",
            payload_json=json.dumps({"targetDir": r"D:\site", "backupDir": r"D:\b2"}),
            target=r"D:\site",
            undone_at=datetime.now(),
            undone_by_release_id=99,
        )
        db.add_all([older, newer])
        db.commit()
        db.refresh(older)
        db.refresh(newer)
        assert latest_on_target(db, 1, r"D:\site", agent_id=older.agent_id).id == older.id
        rows, warnings, reason = check_undoable(db, 10)
        assert reason == ""
        assert [r.id for r in rows] == [older.id]
        assert warnings == []
    finally:
        db.close()


def test_same_dir_on_three_nodes_is_not_a_cover() -> None:
    """负载均衡三台机器写同一站点目录，不能互相当成「之后又部署过」。"""
    db = _db(DeploymentRecord)
    try:
        for agent_id, name in ((1, "web-a"), (2, "web-b"), (3, "web-c")):
            db.add(BuildAgent(id=agent_id, name=name, role="node", host=f"10.0.0.{agent_id}"))
        db.add_all([
            Release(id=70, pipeline_id=1, group_id=1, build_number=8, status="success"),
            Release(id=71, pipeline_id=1, group_id=1, build_number=9, status="success"),
        ])
        db.commit()
        rows = []
        for i, agent_id in enumerate((1, 2, 3), start=1):
            row = DeploymentRecord(
                release_id=70,
                pipeline_id=1,
                task_id=i,
                step_index=0,
                agent_id=agent_id,
                kind="file-backup",
                payload_json=json.dumps(
                    {"targetDir": r"D:\wwwroot\app", "backupDir": rf"D:\backup\{agent_id}"}
                ),
                target=r"D:\wwwroot\app",
            )
            db.add(row)
            rows.append(row)
        later_on_node2 = DeploymentRecord(
            release_id=71,
            pipeline_id=1,
            task_id=9,
            step_index=0,
            agent_id=2,
            kind="file-backup",
            payload_json=json.dumps(
                {"targetDir": r"D:\wwwroot\app", "backupDir": r"D:\backup\2b"}
            ),
            target=r"D:\wwwroot\app",
        )
        db.add(later_on_node2)
        db.commit()
        for row in rows:
            db.refresh(row)
        db.refresh(later_on_node2)

        usable, warnings, reason = check_undoable(db, 70)
        assert reason == ""
        assert {r.agent_id for r in usable} == {1, 2, 3}
        assert any("节点「web-b 10.0.0.2」" in w and "构建 #9" in w for w in warnings)
        assert not any("web-a" in w for w in warnings)
        assert not any("web-c" in w for w in warnings)
        assert not any("节点 #" in w or "发布 #" in w for w in warnings)
        assert latest_on_target(db, 1, r"D:\wwwroot\app", agent_id=1).id == rows[0].id
        assert latest_on_target(db, 1, r"D:\wwwroot\app", agent_id=2).id == later_on_node2.id
    finally:
        db.close()


def test_cover_warning_omits_internal_ids_when_lookup_missing() -> None:
    """节点或发布行不在了，警告也只说目录被覆盖，不甩节点表/发布表主键。"""
    db = _db(DeploymentRecord)
    try:
        older = DeploymentRecord(
            release_id=10,
            pipeline_id=1,
            task_id=1,
            step_index=0,
            agent_id=30,
            kind="file-backup",
            payload_json=json.dumps({"targetDir": "/data/site", "backupDir": "/data/b1"}),
            target="/data/site",
        )
        newer = DeploymentRecord(
            release_id=221,
            pipeline_id=1,
            task_id=2,
            step_index=0,
            agent_id=30,
            kind="file-backup",
            payload_json=json.dumps({"targetDir": "/data/site", "backupDir": "/data/b2"}),
            target="/data/site",
        )
        db.add_all([older, newer])
        db.commit()
        _, warnings, reason = check_undoable(db, 10)
        assert reason == ""
        assert len(warnings) == 1
        assert "部署节点" in warnings[0]
        assert "/data/site" in warnings[0]
        assert "节点 #30" not in warnings[0]
        assert "发布 #221" not in warnings[0]
        assert "#221" not in warnings[0]
    finally:
        db.close()


def test_platform_marks_all_records_when_rollback_succeeds() -> None:
    db = _db(DeploymentRecord)
    try:
        a = DeploymentRecord(
            release_id=50,
            pipeline_id=1,
            task_id=1,
            kind="file-backup",
            payload_json="{}",
            target="a",
        )
        b = DeploymentRecord(
            release_id=50,
            pipeline_id=1,
            task_id=2,
            kind="file-backup",
            payload_json="{}",
            target="b",
        )
        db.add_all([a, b])
        db.commit()
        n = mark_release_records_undone(db, 50, 88)
        db.commit()
        db.refresh(a)
        db.refresh(b)
        assert n == 2
        assert a.undone_by_release_id == 88
        assert b.undone_at is not None
        assert mark_release_records_undone(db, 50, 89) == 0
    finally:
        db.close()


def test_inflight_rollback_includes_pending() -> None:
    db = _db(Release)
    try:
        original = Release(pipeline_id=1, group_id=1, status="success", build_number=1)
        db.add(original)
        db.commit()
        db.refresh(original)
        pending = Release(
            pipeline_id=1,
            group_id=1,
            status=RELEASE_PENDING,
            build_number=2,
            rollback_of_release_id=original.id,
        )
        db.add(pending)
        db.commit()
        db.refresh(pending)
        found = inflight_rollback_of(db, original.id)
        assert found is not None and found.id == pending.id
        pending.status = "failed"
        db.commit()
        assert inflight_rollback_of(db, original.id) is None
        queued = Release(
            pipeline_id=1,
            group_id=1,
            status=RELEASE_QUEUED,
            build_number=3,
            rollback_of_release_id=original.id,
        )
        db.add(queued)
        db.commit()
        assert inflight_rollback_of(db, original.id) is not None
    finally:
        db.close()


def test_preview_lists_real_steps() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(
            db,
            60,
            [
                {"name": "停止应用池", "plugin": "iis-control", "with": {"action": "stop", "name": "PoolA"}},
                {"name": "file-transfer", "plugin": "file-transfer", "with": {"targetDir": r"D:\site\a"}},
                {"name": "启动应用池", "plugin": "iis-control", "with": {"action": "start", "name": "PoolA"}},
            ],
        )
        row = _row(
            db,
            release_id=60,
            task=task,
            step_index=1,
            payload={"targetDir": r"D:\site\a", "backupDir": r"D:\backup\a"},
            summary="覆盖 3 个文件",
        )
        item = preview_item(db, row)
        assert [s["name"] for s in item["steps"]] == [
            "停止应用池",
            r"还原备份到 D:\site\a",
            "启动应用池",
        ]
    finally:
        db.close()


def test_rolling_undo_one_job_last_node_first() -> None:
    """三台节点合成一个 Job、后发先撤，拆段后才能一台一台串起来。"""
    from app.modules.agent.task_service import _split_job_segments

    db = _db(BuildTask, DeploymentRecord)
    try:
        rows = []
        for i, agent_id in enumerate((11, 12, 13), start=1):
            task = _task(
                db,
                80,
                [
                    {
                        "name": f"停 {agent_id}",
                        "plugin": "iis-control",
                        "with": {"action": "stop", "name": f"Pool{agent_id}", "node": agent_id},
                    },
                    {
                        "name": "file-transfer",
                        "plugin": "file-transfer",
                        "with": {"targetDir": r"D:\wwwroot\app", "node": agent_id},
                    },
                    {
                        "name": f"起 {agent_id}",
                        "plugin": "iis-control",
                        "with": {"action": "start", "name": f"Pool{agent_id}", "node": agent_id},
                    },
                ],
            )
            rows.append(
                _row(
                    db,
                    release_id=80,
                    task=task,
                    step_index=1,
                    agent_id=agent_id,
                    payload={
                        "targetDir": r"D:\wwwroot\app",
                        "backupDir": rf"D:\backup\{agent_id}",
                    },
                    target=r"D:\wwwroot\app",
                )
            )
        jobs = build_undo_jobs(db, rows)
        assert len(jobs) == 1
        assert jobs[0]["id"] == "undo-rolling"
        assert len(jobs[0]["rolling_order"]) == 3
        segs = _split_job_segments(jobs[0]["steps"])
        assert [kind for kind, _steps, _node in segs] == ["node", "node", "node"]
        assert [node for _kind, _steps, node in segs] == [13, 12, 11]
    finally:
        db.close()


def test_split_image_ref_keeps_registry_port() -> None:
    from app.modules.deployment.service import split_image_ref

    assert split_image_ref("registry.example.com:5000/app:10") == ("registry.example.com:5000/app", "10")
    assert split_image_ref("registry.example.com/app") == ("registry.example.com/app", "")
    assert split_image_ref("registry-1.docker.io/library/nginx:1.27") == (
        "registry-1.docker.io/library/nginx",
        "1.27",
    )


def test_docker_preview_exposes_current_and_previous_tags() -> None:
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(db, 90, [{"plugin": "docker-deploy"}])
        row = _row(
            db,
            release_id=90,
            task=task,
            step_index=0,
            kind="docker-image",
            target="ssh://root@192.0.2.10/cics-core",
            payload={
                "undo_with": {"image": "reg/common/cics-core-sit:9", "containerName": "cics-core"},
                "previous_image": "reg/common/cics-core-sit:9",
                "current_image": "reg/common/cics-core-sit:10",
            },
            summary="reg/common/cics-core-sit:9 → reg/common/cics-core-sit:10",
        )
        item = preview_item(db, row)
        assert item["current_image"] == "reg/common/cics-core-sit:10"
        assert item["previous_image"] == "reg/common/cics-core-sit:9"
        assert item["image_repo"] == "reg/common/cics-core-sit"
        assert item["current_tag"] == "10"
        assert item["previous_tag"] == "9"
    finally:
        db.close()


def test_replace_image_tag_rejects_path() -> None:
    from app.core.response import BizException
    from app.modules.deployment.service import replace_image_tag

    assert replace_image_tag("reg/app:9", "8") == "reg/app:8"
    try:
        replace_image_tag("reg/app:9", "../evil")
        raise AssertionError("expected reject")
    except BizException as exc:
        assert "不合法" in str(exc)


def test_docker_undo_uses_chosen_tag_not_only_previous() -> None:
    """确认框改了 tag 后，回滚计划必须去拉那个版本，不能仍用登记时的上一版。"""
    db = _db(BuildTask, DeploymentRecord)
    try:
        task = _task(db, 91, [{"plugin": "docker-deploy"}])
        row = _row(
            db,
            release_id=91,
            task=task,
            step_index=0,
            kind="docker-image",
            target="host/cics-core",
            payload={
                "undo_with": {"image": "reg/app:9", "containerName": "cics-core"},
                "previous_image": "reg/app:9",
                "current_image": "reg/app:10",
            },
        )
        jobs = build_undo_jobs(db, [row], image_tags={row.id: "7"})
        assert jobs[0]["steps"][0]["with"]["image"] == "reg/app:7"
        assert "reg/app:7" in jobs[0]["steps"][0]["name"]
    finally:
        db.close()
