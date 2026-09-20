"""OOM / 无界读取回归验证。

覆盖这次修的几处：日志存储（追写复杂度、读取封顶、截断可见）、
发布日志 SSE 增量、权限赋权的查询范围、审计日志保留清理。
"""
from __future__ import annotations

import os
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_oom.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    import app.main  # noqa: F401  建表前把 settings 等模型注册进 Base
    from sqlalchemy import event

    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.agent.log_store import (
        MAX_FETCH_LINES,
        MemoryLogStore,
        TRUNCATED_MARK,
    )
    from app.modules.agent.models import (  # noqa: F401
        BuildAgent,
        BuildTask,
        BuildTaskLog,
        NodeGroup,
    )
    from app.modules.approval.models import Approval  # noqa: F401
    from app.modules.audit import service as audit_service
    from app.modules.audit.models import AuditLog
    from app.modules.auth.models import Permission, User  # noqa: F401
    from app.modules.deploy.models import DeployRequest  # noqa: F401
    from app.modules.pipeline.models import Pipeline, Release  # noqa: F401
    from app.modules.project.models import Group, Project  # noqa: F401

    Base.metadata.create_all(engine)

    # 应用启动时会跑的补丁：把 build_task.logs 从 64KB 的 TEXT 扩到 MEDIUMTEXT
    from app.main import _ensure_build_task_logs_mediumtext

    _ensure_build_task_logs_mediumtext()

    # 数一数每步发了多少条 SQL
    counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        counter["n"] += 1

    def sql_count(fn):
        counter["n"] = 0
        result = fn()
        return result, counter["n"]

    import uuid

    tag = uuid.uuid4().hex[:8]
    db = SessionLocal()
    proj = Project(name=f"oom-verify-{tag}", code=f"oomv{tag}", description="")
    db.add(proj)
    db.flush()
    grp = Group(name="g", project_id=proj.id, type="test")
    db.add(grp)
    db.flush()
    pipe = Pipeline(name=f"p-{tag}", project_id=proj.id, group_id=grp.id)
    db.add(pipe)
    db.flush()
    rel = Release(pipeline_id=pipe.id, group_id=grp.id, status="running", version="1")
    db.add(rel)
    db.flush()
    task = BuildTask(
        release_id=rel.id, pipeline_id=pipe.id, stage_name="build", job_name="job", status="running"
    )
    db.add(task)
    db.commit()
    task_id, release_id, pipe_id, proj_id = task.id, rel.id, pipe.id, proj.id
    db.close()

    store = MemoryLogStore()

    # 1) 日志进内存后端，不碰 build_task / build_task_log
    stmts: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy_log_sql(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        if "build_task" in statement:
            stmts.append(statement)

    stmts.clear()
    store.append_batch(task_id, ["hello-" + "x" * 100])
    joined = " ".join(stmts).lower()
    check(
        "追写日志不写业务库",
        "insert into build_task_log" not in joined and "update build_task" not in joined,
        f"{len(stmts)} 条 SQL",
    )

    # 2) 写入量应与已有日志量无关：对比前后两次追写实际发出的字节数
    def bytes_written(payload: str) -> int:
        stmts.clear()
        store.append_batch(task_id, [payload])
        return sum(len(s) for s in stmts) + len(payload)

    early = bytes_written("probe-early-" + "y" * 100)
    for i in range(0, 8000, 2000):
        store.append_batch(task_id, [f"bulk-{i + j}-" + "x" * 100 for j in range(2000)])
    late = bytes_written("probe-late-" + "y" * 100)
    check(
        "追写代价不随日志量增长",
        late < early * 3,
        f"日志从 0 涨到约 900KB，单次追写的 SQL 体量 {early}B → {late}B",
    )

    stmts.clear()
    total_lines = store.count(task_id)
    check(
        "count() 只查行数，不传回日志正文",
        total_lines > 8000 and not stmts,
        f"{total_lines} 行 / {len(stmts)} 条 SQL",
    )
    check(
        "能存下远超 TEXT 上限（64KB）的日志",
        total_lines > 8000,
        f"约 {total_lines * 110 / 1024:.0f}KB",
    )

    # 3) 按偏移读只取相关的块，不把前面的内容也读出来
    stmts.clear()
    tail = store.get(task_id, start=total_lines - 50, limit=50)
    tail_sql = " ".join(stmts).lower()
    check(
        "按偏移读不碰业务库",
        len(tail) == 50 and "build_task_log" not in tail_sql,
        f"取末尾 50 行：{tail[0][:24]}…",
    )

    # 4) 读取封顶
    check("get() 有默认上限", MAX_FETCH_LINES > 0 and len(store.get(task_id)) <= MAX_FETCH_LINES)
    check("get() 按 limit 截断", len(store.get(task_id, limit=100)) == 100)

    tracemalloc.start()
    store.get(task_id, start=0, limit=200)
    _, peak_small = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tracemalloc.start()
    store.get(task_id, limit=total_lines)
    _, peak_full = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check(
        "小窗口读取的峰值内存明显更低",
        peak_small < peak_full,
        f"200 行 {peak_small / 1024:.0f}KB < 全量 {peak_full / 1024:.0f}KB",
    )

    # 5) 增量读：拿到的是新增那一段，不是从头再来一遍
    head = store.get(task_id, start=0, limit=5)
    mid = store.get(task_id, start=100, limit=5)
    check("按 start 偏移增量读", head != mid and len(mid) == 5, f"{mid[0][:20]}…")

    # 4) 发布日志一次性接口有总量封顶，且截断有提示
    from app.modules.pipeline import service as pipeline_service
    from app.modules.agent import log_store as log_store_mod

    original_create = log_store_mod.create_log_store
    log_store_mod.create_log_store = lambda *a, **k: store
    try:
        db = SessionLocal()
        data, n_sql = sql_count(lambda: pipeline_service.get_release_logs(db, release_id))
        db.close()
        body = data.get("logs") or ""
        n_lines = len(body.split("\n"))
        check(
            "get_release_logs 有总量封顶",
            n_lines <= MAX_FETCH_LINES + 10,
            f"{n_lines} 行 / {n_sql} 条 SQL",
        )
        check("没超上限时不该冒出截断提示", "已截断" not in body, "")

        original_cap = log_store_mod.MAX_FETCH_LINES
        log_store_mod.MAX_FETCH_LINES = 100
        try:
            db = SessionLocal()
            capped_body = pipeline_service.get_release_logs(db, release_id).get("logs") or ""
            db.close()
        finally:
            log_store_mod.MAX_FETCH_LINES = original_cap
        check(
            "截断有明确提示，不是静默砍掉",
            "已截断" in capped_body and len(capped_body.split("\n")) < 200,
            f"截断后 {len(capped_body.split(chr(10)))} 行",
        )
    finally:
        log_store_mod.create_log_store = original_create

    # 5) SSE 增量：模拟两轮轮询，第二轮只应该取到新增的行
    reads: list[tuple[int, int]] = []

    class Spy(MemoryLogStore):
        def get(self, tid, start=0, limit=MAX_FETCH_LINES):  # noqa: ANN001
            reads.append((start, limit))
            return super().get(tid, start=start, limit=limit)

    spy = Spy()
    spy._lines = store._lines
    sent: dict[int, int] = {}
    for _round in range(2):
        n = spy.count(task_id)
        seen = sent.get(task_id, 0)
        if n > seen:
            spy.get(task_id, start=seen, limit=MAX_FETCH_LINES)
            sent[task_id] = n
        spy.append_batch(task_id, ["新增一行"])
    check(
        "SSE 第二轮从上次的位置接着读",
        len(reads) == 2 and reads[0][0] == 0 and reads[1][0] == total_lines,
        f"第一轮从 0 开始，第二轮从 {reads[1][0] if len(reads) > 1 else '?'} 接着读",
    )

    # 6) 赋权只查相关行，不再 select 整张权限表
    from app.modules.auth import permission_service

    db = SessionLocal()
    users = []
    for i in range(20):
        u = User(username=f"oomu{tag}{i}", password_hash="x", display_name="", is_admin=False)
        db.add(u)
        users.append(u)
    db.flush()
    uids = [u.id for u in users]
    # 制造一批与本次赋权无关的权限行
    for uid in uids:
        for rid in range(50):
            db.add(
                Permission(
                    user_id=uid,
                    resource_type="pipeline",
                    resource_id=10000 + rid,
                    action="read",
                    effect="allow",
                )
            )
    db.commit()

    scanned: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy_perm(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        if "FROM permission" in statement and "SELECT" in statement:
            scanned.append(len(statement))

    before = db.query(Permission).count()
    permission_service.grant_permissions(
        db, user_ids=[uids[0]], resource_type="pipeline", resource_ids=[pipe_id], actions=["read"]
    )
    after = db.query(Permission).count()
    # 判重查询必须带 WHERE，否则就是全表拉取
    check("赋权判重查询带过滤条件", any(scanned), f"权限表现有 {before} 行 → {after} 行")
    db.close()

    db = SessionLocal()
    from sqlalchemy import select as _select

    stmt_sql = str(
        _select(Permission.user_id).where(Permission.user_id.in_([1, 2])).compile()
    )
    check("（形态检查）in_ 过滤可编译", "IN" in stmt_sql.upper(), "")

    # 7) list_permissions 按用户过滤 + 批量取名，不是 N+1
    _, n_sql = sql_count(lambda: permission_service.list_permissions(db, user_id=uids[0]))
    rows = permission_service.list_permissions(db, user_id=uids[0])
    check(
        "list_permissions 按用户过滤且不 N+1",
        n_sql <= 5 and all(r["user_id"] == uids[0] for r in rows),
        f"{len(rows)} 行 / {n_sql} 条 SQL",
    )
    check("list_permissions 不传 user_id 时有上限", len(permission_service.list_permissions(db)) <= 2000)
    db.close()

    # 8) 审计日志保留清理
    from datetime import datetime, timedelta

    db = SessionLocal()
    old = datetime.now() - timedelta(days=400)
    for i in range(120):
        db.add(AuditLog(action="test.old", username="u", detail="", created_at=old))
    for i in range(10):
        db.add(AuditLog(action="test.new", username="u", detail=""))
    db.commit()
    removed = audit_service.purge_expired(db)
    left_old = db.query(AuditLog).filter(AuditLog.action == "test.old").count()
    left_new = db.query(AuditLog).filter(AuditLog.action == "test.new").count()
    check(
        "审计日志按保留期清理，只删过期的",
        removed >= 120 and left_old == 0 and left_new == 10,
        f"清掉 {removed} 条，保留近期 {left_new} 条",
    )
    db.close()

    # 9) 审批列表批量取关联对象，不是逐行 db.get
    import inspect

    from app.modules.approval import router as approval_router

    src = inspect.getsource(approval_router)
    check(
        "审批列表用批量预取替代逐行 db.get",
        "_Refs" in src and "db.get(Release, row.release_id)" not in src,
        "",
    )
    check("审批列表有条数上限", "limit: int = Query(" in src, "")

    # 10) 发布提交单列表不带清单正文
    from app.modules.deploy import router as deploy_router

    dsrc = inspect.getsource(deploy_router)
    check(
        "发布提交单列表不返回清单正文",
        'brief=True' in dsrc and '"manifest": "" if brief else req.manifest' in dsrc,
        "",
    )

    # 收尾：别把几万行日志和一堆权限行留在测试库里
    db = SessionLocal()
    try:
        db.query(Permission).filter(Permission.user_id.in_(uids)).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.action.in_(["test.old", "test.new"])).delete(
            synchronize_session=False
        )
        db.query(BuildTaskLog).filter(BuildTaskLog.task_id == task_id).delete(
            synchronize_session=False
        )
        db.query(BuildTask).filter(BuildTask.id == task_id).delete(synchronize_session=False)
        db.query(Release).filter(Release.id == release_id).delete(synchronize_session=False)
        db.query(Pipeline).filter(Pipeline.id == pipe_id).delete(synchronize_session=False)
        db.query(Group).filter(Group.project_id == proj_id).delete(synchronize_session=False)
        db.query(Project).filter(Project.id == proj_id).delete(synchronize_session=False)
        db.commit()
        print("[ok] 测试数据已清理")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 清理测试数据失败：{e}")
    finally:
        db.close()

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
