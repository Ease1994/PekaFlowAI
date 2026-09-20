"""启动失败必须把原因带到列表和明细，不能只显示「已完成」。

覆盖：
  1. 从 error_message / 旧 release.logs 抽出发布未能启动原因
  2. 巡检清掉零任务发布时的文案
  3. 失败但没有原因时有兜底文案
  4. 成功发布不返回 error_summary
  5. 列表 decorate 带 error_summary、不带全量 logs
  6. 没有 BuildTask 时 AI 诊断仍能拿到启动失败原因
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_failure_display.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    import app.main  # noqa: F401

    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.auth.models import User
    from app.modules.pipeline.diagnose import collect_failure_context
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.pipeline.service import decorate_releases, release_error_summary
    from app.modules.project.models import Group, Project

    r = Release(status="failed", error_message="发布未能启动：没有匹配的 Windows 构建机")
    got = release_error_summary(r)
    check("抽出启动失败原因", got == "发布未能启动：没有匹配的 Windows 构建机", got)

    r.error_message = ""
    r.logs = "noise\n[系统] 发布未能启动：第一次\n[系统] 未能生成构建任务，发布中断（后台巡检清理）"
    got = release_error_summary(r)
    check("旧数据从 logs 取最后一条系统原因", "未能生成构建任务" in got, got)

    r.logs = ""
    check("失败无日志有兜底", "没有记录到失败原因" in release_error_summary(r))

    r.status = "success"
    r.logs = "[系统] 不该出现"
    check("成功不返回原因", release_error_summary(r) == "")

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_failure_display.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

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
        pipe = Pipeline(
            project_id=proj.id,
            group_id=grp.id,
            name="pl",
            yaml="",
            status="active",
            created_by=user.id,
            workspace_uuid="w",
        )
        db.add(pipe)
        db.flush()
        rel = Release(
            pipeline_id=pipe.id,
            group_id=grp.id,
            version="v1",
            status="failed",
            operator_id=user.id,
            logs="[系统] 发布未能启动：步骤「file-transfer」选择的部署节点不存在（可能已被删除）",
        )
        db.add(rel)
        db.commit()
        rid = rel.id

    with SessionLocal() as db:
        rel = db.get(Release, rid)
        rows = decorate_releases(db, [rel])
        check("列表有 error_summary", "节点不存在" in (rows[0].get("error_summary") or ""), str(rows[0].get("error_summary")))
        check("列表不带 logs", "logs" not in rows[0] or not rows[0].get("logs"))
        ctx = collect_failure_context(db, rel)
        check("诊断能吃启动失败", ctx["failed_steps"] and "节点不存在" in ctx["failed_steps"][0]["log"])
        check("诊断标记为 startup", ctx["failed_steps"][0].get("trim") == "startup")

    if FAILED:
        print("FAILED:", ", ".join(FAILED))
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
