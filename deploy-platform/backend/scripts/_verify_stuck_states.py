"""「永远卡住」类状态的回收验证。

平台里所有非终态都必须有人负责推进，否则发布就一直转圈，
还把整条流水线占着（queued/running 都算忙）。这里覆盖几条之前没人管的：

  1. Agent 掉线后，它手上 running 的任务没人上报结果
  2. 拆任务失败留下的「零任务却占着流水线」的发布
  3. 平台任务的父发布已结束，任务却一直 pending（会把子流水线步骤饿死）
  4. 平台任务已 running 但没记下等谁（进程在两次提交之间重启）
  5. 目标节点已离线的 pending 部署任务（发送文件一直转圈）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 硬覆盖：这个脚本会造数据并跑清理，绝不能落到真库上
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_stuck.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def main() -> int:  # noqa: C901
    import app.main  # noqa: F401  建表前注册所有模型

    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.agent import task_service
    from app.modules.agent.models import BuildAgent, BuildTask
    from app.modules.auth.models import User
    from app.modules.pipeline import platform_worker
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.pipeline.service import rescue_stranded_releases
    from app.modules.project.models import Group, Project

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_stuck.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

    now = datetime.now()
    with SessionLocal() as db:
        user = User(username="admin", password_hash="x", display_name="管理员", is_admin=True)
        db.add(user)
        db.flush()
        proj = Project(name="p", code="p")
        db.add(proj)
        db.flush()
        grp = Group(project_id=proj.id, name="测试", type="test")
        db.add(grp)
        db.flush()
        pipe = Pipeline(project_id=proj.id, group_id=grp.id, name="pl", yaml="",
                        status="active", created_by=user.id, workspace_uuid="w")
        db.add(pipe)
        # 一台早就没心跳的机器，一台刚刚还在心跳的机器
        dead = BuildAgent(name="dead-builder", os="linux", role="builder", env="test",
                          status="online", token="t1",
                          last_heartbeat=now - timedelta(seconds=3600))
        alive = BuildAgent(name="alive-builder", os="linux", role="builder", env="test",
                           status="online", token="t2", last_heartbeat=now)
        db.add_all([dead, alive])
        db.commit()
        ids = {"user": user.id, "grp": grp.id, "pipe": pipe.id,
               "dead": dead.id, "alive": alive.id}

    # ---- 1. 失联构建机手上的任务要被回收 ----
    with SessionLocal() as db:
        rel = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="v1",
                      status="running", operator_id=ids["user"], started_at=now)
        db.add(rel)
        db.flush()
        t_running = BuildTask(release_id=rel.id, pipeline_id=ids["pipe"], job_name="build",
                              agent_tag="linux", env="test", status="running",
                              agent_id=ids["dead"], started_at=now - timedelta(seconds=3600))
        t_pending = BuildTask(release_id=rel.id, pipeline_id=ids["pipe"], job_name="deploy",
                              agent_tag="linux", env="test", status="pending")
        db.add_all([t_running, t_pending])
        db.commit()
        rid, tid_run, tid_pend = rel.id, t_running.id, t_pending.id

    reaped = task_service.reap_dead_agent_tasks(SessionLocal)
    check("回收了失联构建机的任务", reaped == 1, f"reaped={reaped}")
    with SessionLocal() as db:
        check("任务标成 timeout", db.get(BuildTask, tid_run).status == "timeout",
              db.get(BuildTask, tid_run).status)
        check("同发布的后续任务级联取消",
              db.get(BuildTask, tid_pend).status == "cancelled",
              db.get(BuildTask, tid_pend).status)
        r = db.get(Release, rid)
        check("发布收尾成 failed 而不是一直 running", r.status == "failed", r.status)
        check("发布补上了结束时间", r.finished_at is not None)
    with SessionLocal() as db:
        logs = task_service.get_task_logs(db, SessionLocal, tid_run)
    check("日志里写清了为什么中断",
          any("没有心跳" in ln for ln in logs), " | ".join(logs)[:80])

    # ---- 2. 心跳正常的机器，长任务不能被误杀 ----
    with SessionLocal() as db:
        rel2 = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="v2",
                       status="running", operator_id=ids["user"], started_at=now)
        db.add(rel2)
        db.flush()
        # 跑了两小时的长构建，但机器一直在心跳
        t_long = BuildTask(release_id=rel2.id, pipeline_id=ids["pipe"], job_name="long-build",
                           agent_tag="linux", env="test", status="running",
                           agent_id=ids["alive"], started_at=now - timedelta(hours=2))
        db.add(t_long)
        db.commit()
        rid2, tid_long = rel2.id, t_long.id

    reaped2 = task_service.reap_dead_agent_tasks(SessionLocal)
    with SessionLocal() as db:
        check("心跳正常时，跑了两小时的长任务不受影响",
              reaped2 == 0 and db.get(BuildTask, tid_long).status == "running",
              f"reaped={reaped2} status={db.get(BuildTask, tid_long).status}")
        check("它的发布也没被动", db.get(Release, rid2).status == "running")

    # ---- 3. 零任务的发布：保护期内不动，超期才收 ----
    with SessionLocal() as db:
        fresh = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="v3",
                        status="queued", operator_id=ids["user"])
        db.add(fresh)
        db.commit()
        fresh_id = fresh.id
    with SessionLocal() as db:
        n = rescue_stranded_releases(db, min_age_seconds=600)
        check("刚创建、正要拆任务的发布不会被误杀", n == 0, f"收了 {n} 条")
        check("它还是 queued", db.get(Release, fresh_id).status == "queued")
    with SessionLocal() as db:
        n = rescue_stranded_releases(db, min_age_seconds=0)
        check("超过保护期后被收掉", n == 1, f"收了 {n} 条")
        check("收成 failed", db.get(Release, fresh_id).status == "failed")

    # ---- 4. 平台任务：父发布结束后不能一直 pending ----
    with SessionLocal() as db:
        done = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="v4",
                       status="failed", operator_id=ids["user"])
        db.add(done)
        db.flush()
        orphan = BuildTask(release_id=done.id, pipeline_id=ids["pipe"], job_name="sub",
                           agent_tag="platform", env="test", status="pending",
                           steps_json='[{"plugin":"run-pipeline","with":{"pipelineId":1}}]')
        db.add(orphan)
        db.commit()
        orphan_id = orphan.id

    platform_worker.tick_once()
    with SessionLocal() as db:
        check("父发布已结束的平台任务被收掉，不会饿死后来的子流水线步骤",
              db.get(BuildTask, orphan_id).status == "cancelled",
              db.get(BuildTask, orphan_id).status)

    # ---- 5. 平台任务 running 但没记下等谁 ----
    # 5a. 子流水线其实已经起来了 → 认回来接着等
    with SessionLocal() as db:
        parent = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="v5",
                         status="running", operator_id=ids["user"], started_at=now)
        db.add(parent)
        db.flush()
        child = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="c",
                        status="running", operator_id=ids["user"],
                        parent_release_id=parent.id)
        db.add(child)
        db.flush()
        detached = BuildTask(
            release_id=parent.id, pipeline_id=ids["pipe"], job_name="sub",
            agent_tag="platform", env="test", status="running",
            started_at=now - timedelta(seconds=900),
            steps_json='[{"plugin":"run-pipeline","with":{"pipelineId":%d}}]' % ids["pipe"],
        )
        db.add(detached)
        db.commit()
        detached_id, child_id = detached.id, child.id

    platform_worker.tick_once()
    with SessionLocal() as db:
        t = db.get(BuildTask, detached_id)
        check("子流水线已启动的，重新接上继续等（不会重复起一条）",
              t.status == "running" and t.wait_release_id == child_id,
              f"status={t.status} wait={t.wait_release_id}")

    # 5b. 子流水线压根没起来 → 判失败，别一直挂着
    with SessionLocal() as db:
        parent2 = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="v6",
                          status="running", operator_id=ids["user"], started_at=now)
        db.add(parent2)
        db.flush()
        lost = BuildTask(
            release_id=parent2.id, pipeline_id=ids["pipe"], job_name="sub",
            agent_tag="platform", env="test", status="running",
            started_at=now - timedelta(seconds=900),
            steps_json='[{"plugin":"run-pipeline","with":{"pipelineId":99999}}]',
        )
        db.add(lost)
        db.commit()
        lost_id, pid2 = lost.id, parent2.id

    platform_worker.tick_once()
    with SessionLocal() as db:
        check("子流水线没起来的，判失败而不是永远挂着",
              db.get(BuildTask, lost_id).status == "failed",
              db.get(BuildTask, lost_id).status)
        check("父发布跟着收尾", db.get(Release, pid2).status == "failed",
              db.get(Release, pid2).status)

    # ---- 6. 目标节点已离线的 pending 部署任务不能一直转圈 ----
    with SessionLocal() as db:
        offline_node = BuildAgent(
            name="dead-node", os="windows", role="node", env="test",
            status="offline", token="t3",
            last_heartbeat=now - timedelta(seconds=120),
        )
        db.add(offline_node)
        db.flush()
        rel_n = Release(pipeline_id=ids["pipe"], group_id=ids["grp"], version="v7",
                        status="running", operator_id=ids["user"], started_at=now)
        db.add(rel_n)
        db.flush()
        t_pend_node = BuildTask(
            release_id=rel_n.id, pipeline_id=ids["pipe"], job_name="file-transfer",
            agent_tag="node", env="test", status="pending",
            target_agent_id=offline_node.id,
        )
        db.add(t_pend_node)
        db.commit()
        rid_n, tid_n = rel_n.id, t_pend_node.id

    reaped_n = task_service.reap_dead_agent_tasks(SessionLocal)
    check("回收了离线节点上的 pending 部署任务", reaped_n == 1, f"reaped={reaped_n}")
    with SessionLocal() as db:
        check("部署任务标成 failed", db.get(BuildTask, tid_n).status == "failed",
              db.get(BuildTask, tid_n).status)
        rn = db.get(Release, rid_n)
        check("发布收尾成 failed 而不是一直 running", rn.status == "failed", rn.status)
        check("失败原因点名节点离线", "离线" in (rn.error_message or ""),
              rn.error_message or "")

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
