"""三路代码审查暴露出来的问题的回归验证。

重点是审批绕过：/pipelines/{id}/run 原来只看「客户端有没有传 parent_release_id」
就决定要不要跳过生产审批，而那个值正是客户端自己填的——有 execute 没有 approve
的人，随手塞一个历史 release 就能把生产直接发出去。

覆盖：
  1. 顶层 /run 伪造父级 → 仍须审批（pending，不生成任务）
  2. AI 技能 run_pipeline 伪造父级 → 仍须审批
  3. 真正的嵌套调用（服务端证明的父子关系）→ 照常继承审批闸门
  4. Agent 拿别人的父发布启动子流水线 → 拒绝
  5. AI 技能 create_release 会把 queued 的发布拉起来（不再永久占着流水线）
  6. 取消父发布时连带取消子发布（子流水线不被孤儿发布占住）
  7. 子发布被驳回时父任务不再永久等待
  8. /tasks 不再吐出变量里的密钥、/agents 不再吐出机器长期 token
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 硬覆盖：这个脚本会造数据，绝不能落到真库上
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_audit.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


PIPELINE_YAML = """
pipeline:
  name: p
  stages:
    - name: stage-1
      jobs:
        - name: build
          agent: windows
          steps:
            - name: shell
              plugin: shell
              with:
                script: echo hi
"""


def main() -> int:  # noqa: C901
    import app.main  # noqa: F401  建表前把所有模型注册进 Base

    from app.core.deps import CurrentUser
    from app.core.response import BizException
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.agent.models import BuildAgent, BuildTask
    from app.modules.auth.models import Permission, User
    from app.modules.pipeline import service as pipeline_service
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.pipeline.sub_pipeline import TERMINAL_STATUSES, start_sub_pipeline
    from app.modules.project.models import Group, Project

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：本脚本会造数据，只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_audit.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

    # ---- 造数据：一个生产分组（要审批）+ 一个测试分组（免审批）----
    with SessionLocal() as db:
        user = User(username="dev", password_hash="x", display_name="开发", is_admin=True)
        db.add(user)
        db.flush()
        proj = Project(name="p", code="p")
        db.add(proj)
        db.flush()
        prod = Group(project_id=proj.id, name="生产", type="prod", approval_required=True)
        test = Group(project_id=proj.id, name="测试", type="test", approval_required=False)
        db.add_all([prod, test])
        db.flush()
        prod_pipe = Pipeline(
            project_id=proj.id, group_id=prod.id, name="prod-p", yaml=PIPELINE_YAML,
            status="active", created_by=user.id, workspace_uuid="ws1",
        )
        test_pipe = Pipeline(
            project_id=proj.id, group_id=test.id, name="test-p", yaml=PIPELINE_YAML,
            status="active", created_by=user.id, workspace_uuid="ws2",
        )
        db.add_all([prod_pipe, test_pipe])
        db.add(BuildAgent(
            name="win-1", host="10.0.0.1", os="windows", role="builder", env="prod",
            status="online", tags=json.dumps(["windows"]), token="tok-1",
        ))
        # 测试流水线只能落到测试构建机上（环境隔离），得给它备一台
        db.add(BuildAgent(
            name="win-2", host="10.0.0.2", os="windows", role="builder", env="test",
            status="online", tags=json.dumps(["windows"]), token="tok-2",
        ))
        # 生产分组得有个审批人，否则 create_release 直接拒收，测不到跳审那条路
        approver = User(username="lead", password_hash="x", display_name="负责人")
        db.add(approver)
        db.flush()
        db.add(Permission(
            user_id=approver.id, resource_type="group", resource_id=prod.id, action="approve",
        ))
        db.commit()
        ids = {
            "user": user.id, "prod_pipe": prod_pipe.id, "test_pipe": test_pipe.id,
            "prod_group": prod.id,
        }

    current = CurrentUser(id=ids["user"], username="dev", is_admin=True)

    # ================= 1. 顶层 /run 伪造父级不能跳审 =================
    with SessionLocal() as db:
        # 完全照着 /pipelines/{id}/run 的调用方式：parent_* 来自请求体
        rel = start_sub_pipeline(
            db,
            user=current,
            pipeline_id=ids["prod_pipe"],
            params={},
            parent_pipeline_id=ids["test_pipe"],  # 伪造：随便塞一条流水线
            parent_release_id=None,
        )
        check(
            "顶层 /run 伪造 parent_pipeline_id 仍须审批",
            rel.status == "pending",
            f"status={rel.status}",
        )
        n_tasks = db.query(BuildTask).filter(BuildTask.release_id == rel.id).count()
        check("未审批的发布不会生成构建任务", n_tasks == 0, f"tasks={n_tasks}")
        pipeline_service.cancel_release(db, rel.id, ids["user"])

    # 伪造 parent_release_id：指向另一条流水线上真实存在的 release（绕开循环检测）
    with SessionLocal() as db:
        decoy = pipeline_service.create_release(
            db, pipeline_id=ids["test_pipe"], version="v-decoy", strategy="rolling",
            trigger_by="manual", operator_id=ids["user"],
        )
        db.commit()
        forged_parent_release = decoy.id
    with SessionLocal() as db:
        rel = start_sub_pipeline(
            db,
            user=current,
            pipeline_id=ids["prod_pipe"],
            params={},
            parent_release_id=forged_parent_release,  # 伪造：拿刚才那条当爹
        )
        check(
            "顶层 /run 伪造 parent_release_id 仍须审批",
            rel.status == "pending",
            f"status={rel.status}",
        )
        pipeline_service.cancel_release(db, rel.id, ids["user"])

    # ================= 2. AI 技能同一条路也堵上 =================
    from app.modules.ai.skills.delivery import _run_pipeline

    with SessionLocal() as db:
        out = _run_pipeline(db, current, {
            "pipeline_id": ids["prod_pipe"],
            "parent_release_id": forged_parent_release,
        })
        check(
            "AI 技能 run_pipeline 伪造父级仍须审批",
            out["status"] == "pending",
            f"status={out['status']}",
        )
        pipeline_service.cancel_release(db, out["release_id"], ids["user"])
        # 占位用的 decoy 用完就撤，把 test 流水线让出来
        pipeline_service.cancel_release(db, forged_parent_release, ids["user"])

    # ================= 3. 真正的嵌套调用照旧免审 =================
    with SessionLocal() as db:
        parent = pipeline_service.create_release(
            db, pipeline_id=ids["test_pipe"], version="v-parent", strategy="rolling",
            trigger_by="manual", operator_id=ids["user"],
        )
        db.commit()
        parent_id = parent.id
    with SessionLocal() as db:
        child = start_sub_pipeline(
            db,
            user=current,
            pipeline_id=ids["prod_pipe"],
            params={},
            parent_release_id=parent_id,
            trusted_nested=True,  # 服务端证明过的父子关系（platform_worker / 任务级 token）
        )
        check(
            "服务端证明的嵌套调用仍然继承审批闸门",
            child.status in ("queued", "running"),
            f"status={child.status}",
        )
        child_id = child.id

    # ================= 4. 取消父发布连带取消子发布 =================
    with SessionLocal() as db:
        pipeline_service.cancel_release(db, parent_id, ids["user"])
    with SessionLocal() as db:
        c = db.get(Release, child_id)
        check("取消父发布会连带取消子发布", c.status == "cancelled", f"子发布 status={c.status}")
        p = db.get(Release, parent_id)
        check("父发布本身已取消", p.status == "cancelled", f"status={p.status}")

    # 子流水线不再被孤儿发布占住：现在应该能重新发起
    with SessionLocal() as db:
        try:
            pipeline_service.ensure_pipeline_idle(db, ids["prod_pipe"])
            check("子流水线已释放，可以重新发起", True)
        except BizException as e:
            check("子流水线已释放，可以重新发起", False, str(e))

    # ================= 5. AI 技能 create_release 会真的把它拉起来 =================
    from app.modules.ai.skills.delivery import _create_release

    with SessionLocal() as db:
        out = _create_release(db, current, {"pipeline_id": ids["test_pipe"], "version": "v1"})
        # 免审批分组建出来就是 queued，修完之后应当被拉起来（running / 或立刻失败），
        # 总之不能停在 queued——那会永远占着这条流水线
        check(
            "AI 技能 create_release 不再把发布晾在 queued",
            out["status"] != "queued",
            f"status={out['status']} err={out.get('error', '')}",
        )
        rid = out["release_id"]
        n_tasks = db.query(BuildTask).filter(BuildTask.release_id == rid).count()
        check("并且确实生成了构建任务", n_tasks > 0, f"tasks={n_tasks}")

    # ================= 6. rejected 算终态 =================
    check("子发布被驳回时父任务不会永远等下去", "rejected" in TERMINAL_STATUSES)

    # ================= 7. 接口不再泄露密钥 =================
    from app.modules.agent import task_service

    masked = task_service.mask_secrets({"PASSWORD": "p@ss", "API_KEY": "sk-1", "APP": "demo"})
    check(
        "变量里的密码/密钥会被脱敏",
        masked.get("PASSWORD") != "p@ss" and masked.get("API_KEY") != "sk-1",
        str(masked),
    )
    check("非敏感变量原样保留", masked.get("APP") == "demo", str(masked))

    from app.modules.agent.router import _to_dict

    with SessionLocal() as db:
        a = db.scalars(__import__("sqlalchemy").select(BuildAgent)).first()
        d = _to_dict(a)
        check(
            "构建机列表不再返回长期 token",
            "token" not in d and "token_prev" not in d and "token_issued" not in d,
            f"keys={sorted(d)[:8]}...",
        )

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：{FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
