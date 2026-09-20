"""平台编排步骤：在后端执行 run-pipeline，不占用构建机。

对齐 Harness Pipeline stage：编排器启动子流水线并在库内等待结束；
表单与权限语义仍对齐蓝鲸 SubPipelineExec。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.modules.agent.models import BuildTask
from app.modules.agent.task_service import _deps_ready, complete_task, report_log
from app.modules.pipeline.models import Release
from app.modules.pipeline.sub_pipeline import (
    PLUGIN_NAME,
    TERMINAL_STATUSES,
    collect_release_outputs,
    namespaced_outputs,
    start_sub_pipeline,
    user_from_operator,
)

_TICK_SEC = 2


def _log(db: Session, task_id: int, line: str) -> None:
    try:
        report_log(db, SessionLocal, task_id, line)
    except Exception:  # noqa: BLE001
        print(f"[platform-worker] log failed task={task_id}: {line}")


def _step_with(task: BuildTask) -> dict:
    try:
        steps = json.loads(task.steps_json or "[]")
    except json.JSONDecodeError:
        steps = []
    if not steps or not isinstance(steps, list):
        return {}
    first = steps[0] if isinstance(steps[0], dict) else {}
    with_ = first.get("with") or {}
    return with_ if isinstance(with_, dict) else {}


def _poll_interval(cfg: dict) -> int:
    try:
        n = int(cfg.get("pollInterval") or cfg.get("poll_interval") or 10)
    except (TypeError, ValueError):
        n = 10
    return max(1, n)


def tick_once() -> None:
    with SessionLocal() as db:
        pending = list(
            db.scalars(
                select(BuildTask)
                .where(BuildTask.status == "pending", BuildTask.agent_tag == "platform")
                .order_by(BuildTask.id)
                .limit(20)
            ).all()
        )
        for task in pending:
            parent = db.get(Release, task.release_id)
            if parent is None or parent.status in ("cancelled", "failed", "success", "rolled_back"):
                # 父发布已经结束了，这个任务不会再有人来推进。光 continue 的话它永远
                # 留在 pending，而上面那条查询是 ORDER BY id LIMIT 20——这种孤儿 id 最小、
                # 永远排在最前面，攒够 20 个就把后面所有子流水线步骤都饿死了
                task.status = "cancelled"
                task.finished_at = datetime.now()
                db.commit()
                continue
            if not _deps_ready(db, task):
                continue
            _start_platform_task(db, task, parent)

        _recover_detached_tasks(db)

        waiting = list(
            db.scalars(
                select(BuildTask).where(
                    BuildTask.status == "running",
                    BuildTask.agent_tag == "platform",
                    BuildTask.wait_release_id.is_not(None),
                )
            ).all()
        )
        for task in waiting:
            _poll_platform_task(db, task)


# 「已置 running、还没记下等谁」这个窗口只有几毫秒，留足保护期再动手
_DETACHED_GRACE_SEC = 300


def _recover_detached_tasks(db: Session) -> None:
    """捞回「running 但没记下 wait_release_id」的平台任务。

    _start_platform_task 先提交 status=running，再启动子流水线，最后才提交
    wait_release_id。中间要是进程重启了，这个任务就掉进了没人扫的缝里：
    它不是 pending（孤儿清理够不着），也没有 agent（失联回收够不着），
    更不在下面那条 waiting 查询里（要求 wait_release_id 非空）——永远卡住。
    """
    stale_before = datetime.now() - timedelta(seconds=_DETACHED_GRACE_SEC)
    detached = list(
        db.scalars(
            select(BuildTask).where(
                BuildTask.status == "running",
                BuildTask.agent_tag == "platform",
                BuildTask.wait_release_id.is_(None),
                BuildTask.started_at.is_not(None),
                BuildTask.started_at < stale_before,
            ).limit(20)
        ).all()
    )
    for task in detached:
        cfg = _step_with(task)
        child_pipeline_id = cfg.get("pipelineId") or cfg.get("pipeline_id")
        child = None
        if child_pipeline_id:
            # 子流水线可能已经起来了，只是没来得及记下来。认回它，别再起一条
            child = db.scalars(
                select(Release)
                .where(
                    Release.parent_release_id == task.release_id,
                    Release.pipeline_id == int(child_pipeline_id),
                )
                .order_by(Release.id.desc())
            ).first()
        if child is not None:
            task.wait_release_id = child.id
            db.commit()
            _log(db, task.id, f"[platform] 重新接上子流水线 release=#{child.id}")
            continue
        _log(db, task.id, "[platform] 子流水线未能启动（服务中断），本步骤判失败")
        complete_task(db, task.id, False)
        from app.modules.agent.router import _maybe_sync_release

        t = db.get(BuildTask, task.id)
        if t:
            _maybe_sync_release(db, t)


def _start_platform_task(db: Session, task: BuildTask, parent: Release) -> None:
    cfg = _step_with(task)
    plugin = ""
    try:
        steps = json.loads(task.steps_json or "[]")
        plugin = (steps[0] or {}).get("plugin") if steps else ""
    except Exception:  # noqa: BLE001
        plugin = ""
    if plugin and plugin != PLUGIN_NAME:
        _log(db, task.id, f"[platform] unsupported plugin {plugin}")
        complete_task(db, task.id, False)
        from app.modules.agent.router import _maybe_sync_release

        t = db.get(BuildTask, task.id)
        if t:
            _maybe_sync_release(db, t)
        return

    pipeline_id = cfg.get("pipelineId") or cfg.get("pipeline_id")
    if not pipeline_id:
        _log(db, task.id, "[platform] run-pipeline 未选择流水线")
        complete_task(db, task.id, False)
        from app.modules.agent.router import _maybe_sync_release

        t = db.get(BuildTask, task.id)
        if t:
            _maybe_sync_release(db, t)
        return

    task.status = "running"
    task.started_at = datetime.now()
    db.commit()
    _log(
        db,
        task.id,
        f"[platform] 启动子流水线 #{pipeline_id} "
        f"mode={cfg.get('runMode') or 'sync'} poll={_poll_interval(cfg)}s",
    )
    try:
        user = user_from_operator(db, parent.operator_id)
        child = start_sub_pipeline(
            db,
            user=user,
            pipeline_id=int(pipeline_id),
            project_id=cfg.get("projectId") or cfg.get("project_id"),
            params=cfg.get("params") if isinstance(cfg.get("params"), dict) else {},
            parent_pipeline_id=parent.pipeline_id,
            parent_release_id=parent.id,
            # 父发布是后端自己按 task.release_id 取的，不经过任何外部输入
            trusted_nested=True,
        )
    except Exception as e:  # noqa: BLE001
        _log(db, task.id, f"[platform] 启动失败: {e}")
        complete_task(db, task.id, False)
        from app.modules.agent.router import _maybe_sync_release

        t = db.get(BuildTask, task.id)
        if t:
            _maybe_sync_release(db, t)
        return

    run_mode = str(cfg.get("runMode") or cfg.get("run_mode") or "sync").lower()
    _log(db, task.id, f"[platform] 子流水线 release=#{child.id} status={child.status}")
    if run_mode in ("async", "asynchronous", "asyn"):
        _finish_with_outputs(db, task, child, cfg, success=True)
        return
    task.wait_release_id = child.id
    db.commit()


def _poll_platform_task(db: Session, task: BuildTask) -> None:
    cfg = _step_with(task)
    child = db.get(Release, task.wait_release_id)
    if child is None:
        _log(db, task.id, "[platform] 子流水线发布不存在")
        complete_task(db, task.id, False)
        from app.modules.agent.router import _maybe_sync_release

        t = db.get(BuildTask, task.id)
        if t:
            _maybe_sync_release(db, t)
        return
    if child.status not in TERMINAL_STATUSES:
        interval = _poll_interval(cfg)
        started = task.started_at or datetime.now()
        elapsed = int((datetime.now() - started).total_seconds())
        if elapsed > 0 and elapsed % interval < _TICK_SEC:
            _log(db, task.id, f"[platform] 等待子流水线 #{child.id} 状态 {child.status}")
        return
    ok = child.status == "success"
    _log(db, task.id, f"[platform] 子流水线 #{child.id} 结束 status={child.status}")
    _finish_with_outputs(db, task, child, cfg, success=ok)


def _finish_with_outputs(
    db: Session, task: BuildTask, child: Release, cfg: dict, *, success: bool
) -> None:
    from app.modules.agent.router import _maybe_sync_release
    from app.modules.pipeline.models import Release as ReleaseModel

    outputs = collect_release_outputs(db, child)
    picked = namespaced_outputs(
        outputs,
        output_vars=str(cfg.get("outputVars") or cfg.get("output_vars") or ""),
        namespace=str(cfg.get("outputNamespace") or cfg.get("output_namespace") or ""),
        extra={"release_id": child.id, "status": child.status},
    )
    parent = db.get(ReleaseModel, task.release_id)
    if parent is not None:
        try:
            cur = json.loads(parent.outputs_json or "{}")
        except json.JSONDecodeError:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        cur.update(picked)
        parent.outputs_json = json.dumps(cur, ensure_ascii=False)
        db.commit()
    for k, v in picked.items():
        _log(db, task.id, f"##[set-output]{k}={v}")
    complete_task(db, task.id, success)
    t = db.get(BuildTask, task.id)
    if t:
        _maybe_sync_release(db, t)


def platform_worker_loop() -> None:
    print("[platform-worker] started")
    while True:
        try:
            tick_once()
        except Exception as e:  # noqa: BLE001
            print(f"[platform-worker] tick 异常: {e}")
        time.sleep(_TICK_SEC)


def start_platform_worker() -> None:
    threading.Thread(target=platform_worker_loop, name="platform-worker", daemon=True).start()
