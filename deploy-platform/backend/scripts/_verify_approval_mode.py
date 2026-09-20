"""流水线级审批模式（approval_mode）的回归验证。

设计要点是「收紧免授权、放松要授权」：
force 有编辑权就能设，exempt 必须另外授予 approval_exempt。
这条不对称一旦破了，被审批的人就能自己给自己开绿灯，整个审批闸门形同虚设。

覆盖：
  1. inherit（默认）行为和改造前一致：生产要审、测试不审
  2. force 让免审批环境里的流水线也走审批
  3. exempt 让生产流水线直接排队执行
  4. 没有 approval_exempt 权限的人设 exempt 被拒，设 force 放行
  5. pipeline:* 通配符不顺带给出豁免权（allow_wildcard=False）
  6. 存量流水线（列里是 NULL/空）按 inherit 走，不会被当成豁免
  7. 复制流水线时 exempt 退回 inherit、force 保留
  8. 应急跳审仍由环境开关决定，force 不会顺带把它打开
  9. 页面展示口径（approval_required）和真正的闸门判定一致
 10. 缺审批人时报错指向真实分组名
 11. 改审批模式留下审计

第 2 组顺带钉住一个容易想歪的点：免审批的环境往往没人被显式授过 approve，
但管理员对所有分组天然有审批权，所以设成强制审批不会把流水线变成谁也发不了。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 硬覆盖：这个脚本会造数据，绝不能落到测试 MySQL 上
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_approval_mode.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


PIPELINE_YAML = """
pipeline:
  name: demo
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

    from app.core.deps import CurrentUser, check_permission
    from app.core.response import BizException
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.agent.models import BuildAgent  # noqa: F401
    from app.modules.audit.models import AuditLog
    from app.modules.auth.models import Permission, User
    from app.modules.pipeline import service as pipeline_service
    from app.modules.pipeline.models import Pipeline
    from app.modules.project.models import Group, Project

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：本脚本会造数据，只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_approval_mode.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

    # ---- 造数据：生产组（要审）+ 测试组（免审）+ 一个普通开发者 ----
    with SessionLocal() as db:
        admin = User(username="admin", password_hash="x", display_name="管理员", is_admin=True)
        dev = User(username="dev", password_hash="x", display_name="开发", is_admin=False)
        boss = User(username="boss", password_hash="x", display_name="负责人", is_admin=False)
        db.add_all([admin, dev, boss])
        db.flush()
        proj = Project(name="demo", code="demo")
        db.add(proj)
        db.flush()
        prod = Group(
            project_id=proj.id, name="生产", type="prod",
            approval_required=True, allow_emergency_bypass=False,
        )
        test = Group(project_id=proj.id, name="测试", type="test", approval_required=False)
        db.add_all([prod, test])
        db.flush()

        prod_pipe = Pipeline(
            project_id=proj.id, group_id=prod.id, name="prd-app", yaml=PIPELINE_YAML,
            status="active", created_by=admin.id, workspace_uuid="ws1",
        )
        test_pipe = Pipeline(
            project_id=proj.id, group_id=test.id, name="test-app", yaml=PIPELINE_YAML,
            status="active", created_by=admin.id, workspace_uuid="ws2",
        )
        db.add_all([prod_pipe, test_pipe])
        db.flush()

        # dev：对两条线有全部权限（含 "*"），但没有 approval_exempt
        for pid in (prod_pipe.id, test_pipe.id):
            db.add(
                Permission(
                    user_id=dev.id, resource_type="pipeline", resource_id=pid,
                    action="*", effect="allow",
                )
            )
        # boss：只在项目上拿到 approval_exempt
        db.add(
            Permission(
                user_id=boss.id, resource_type="project", resource_id=proj.id,
                action="approval_exempt", effect="allow",
            )
        )
        db.commit()
        ids = {
            "proj": proj.id, "prod_grp": prod.id, "test_grp": test.id,
            "prod_pipe": prod_pipe.id, "test_pipe": test_pipe.id,
            "admin": admin.id, "dev": dev.id, "boss": boss.id,
        }

    def required(pipe_id: int) -> bool:
        """走真正的闸门判定：这条流水线现在要不要审批。"""
        with SessionLocal() as db:
            p = db.get(Pipeline, pipe_id)
            g = db.get(Group, p.group_id)
            return pipeline_service.approval_required_for(g, p)

    def set_mode(pipe_id: int, mode: str, user_id: int) -> str:
        """按某个用户的身份改审批模式，返回 'ok' 或异常消息。"""
        with SessionLocal() as db:
            u = db.get(User, user_id)
            cur = CurrentUser(id=u.id, username=u.username, is_admin=u.is_admin)
            can = check_permission(
                db, cur, "pipeline", pipe_id, "approval_exempt", allow_wildcard=False
            )
            try:
                pipeline_service.update_pipeline(
                    db, pipe_id, {"approval_mode": mode},
                    is_admin=u.is_admin, can_exempt=can, operator_id=u.id,
                )
                return "ok"
            except BizException as e:
                return str(getattr(e, "message", None) or e)

    # ---- 1. 默认 inherit：行为和改造前一致 ----
    with SessionLocal() as db:
        p = db.get(Pipeline, ids["prod_pipe"])
        check("1a 新建流水线默认 inherit", (p.approval_mode or "") == "inherit", p.approval_mode)
    check("1b inherit + 生产组 → 要审批", required(ids["prod_pipe"]) is True)
    check("1c inherit + 测试组 → 免审批", required(ids["test_pipe"]) is False)

    # ---- 2. force：把免审批环境里的线抬成要审批 ----
    r = set_mode(ids["test_pipe"], "force", ids["dev"])
    check("2a 普通开发者能设 force（收紧不需要授权）", r == "ok", r)
    check("2b force 后测试流水线要审批", required(ids["test_pipe"]) is True)

    # 免审批的环境通常没人被显式授过 approve，但管理员对所有分组天然有审批权，
    # 所以 force 之后仍然找得到人批，不会把流水线设成谁也发不了
    with SessionLocal() as db:
        from app.modules.approval.service import resolve_group_approvers

        approvers = resolve_group_approvers(db, ids["test_grp"])
        check(
            "2c 免审批环境设 force 后仍有人可批（管理员兜底），不会把线设死",
            [u.username for u in approvers] == ["admin"],
            str([u.username for u in approvers]),
        )

    # ---- 3/4/5. exempt 的授权边界 ----
    r = set_mode(ids["prod_pipe"], "exempt", ids["dev"])
    check(
        "3a 有 pipeline:* 但无豁免权限的人设 exempt 被拒（通配符不顺带给出豁免）",
        r != "ok" and "豁免审批" in r,
        r,
    )
    check("3b 被拒之后模式没被改动", required(ids["prod_pipe"]) is True)

    with SessionLocal() as db:
        u = db.get(User, ids["dev"])
        cur = CurrentUser(id=u.id, username=u.username, is_admin=False)
        check(
            "3c check_permission 里 * 能命中普通 action、命不中豁免",
            check_permission(db, cur, "pipeline", ids["prod_pipe"], "update") is True
            and check_permission(
                db, cur, "pipeline", ids["prod_pipe"], "approval_exempt", allow_wildcard=False
            )
            is False,
        )

    r = set_mode(ids["prod_pipe"], "exempt", ids["boss"])
    check("4a 有 approval_exempt 的人能设豁免", r == "ok", r)
    check("4b exempt 后生产流水线不再要审批", required(ids["prod_pipe"]) is False)

    # ---- 6. 存量数据：补列后值是空串时按 inherit 走，绝不能当成豁免 ----
    with SessionLocal() as db:
        from sqlalchemy import text

        db.execute(
            text("UPDATE pipeline SET approval_mode = '' WHERE id = :i"),
            {"i": ids["prod_pipe"]},
        )
        db.commit()
    check(
        "6 存量流水线（approval_mode 为空）按 inherit 走，仍然要审批",
        required(ids["prod_pipe"]) is True,
    )
    set_mode(ids["prod_pipe"], "exempt", ids["boss"])  # 复原，给后面的复制用例

    # ---- 7. 复制：exempt 退回 inherit，force 保留 ----
    with SessionLocal() as db:
        copied_exempt = pipeline_service.duplicate_pipeline(
            db, ids["prod_pipe"], "prd-app-copy", None, None, ids["dev"], False
        )
        copied_force = pipeline_service.duplicate_pipeline(
            db, ids["test_pipe"], "test-app-copy", None, None, ids["dev"], False
        )
        check(
            "7a 复制豁免流水线 → 退回 inherit（不给没豁免权的人现成的免审批线）",
            copied_exempt.approval_mode == "inherit",
            copied_exempt.approval_mode,
        )
        check("7b 复制强制审批流水线 → 保留 force", copied_force.approval_mode == "force")
        check("7c 复制出来的生产线恢复成要审批", required(copied_exempt.id) is True)

    # ---- 8. force 不会顺带打开应急跳审 ----
    with SessionLocal() as db:
        p = db.get(Pipeline, ids["test_pipe"])  # force + 测试组（未开应急跳审）
        g = db.get(Group, p.group_id)
        try:
            pipeline_service._approval_gate(
                db, g, p, emergency_bypass=True, emergency_bypass_reason="紧急"
            )
            ok8 = False
            detail8 = "居然放行了"
        except BizException as e:
            detail8 = str(getattr(e, "message", None) or e)
            ok8 = "未开启应急跳审" in detail8
        check("8 force 流水线申请应急跳审，仍被环境开关挡住", ok8, detail8)

    # ---- 9. 展示口径和闸门判定一致 ----
    with SessionLocal() as db:
        from app.modules.pipeline.router import _pipeline_caps

        u = db.get(User, ids["boss"])
        cur = CurrentUser(id=u.id, username=u.username, is_admin=False)
        caps_prod = _pipeline_caps(db, cur, db.get(Pipeline, ids["prod_pipe"]))
        caps_test = _pipeline_caps(db, cur, db.get(Pipeline, ids["test_pipe"]))
        check(
            "9a 豁免的生产线：页面显示免审批，且和闸门一致",
            caps_prod["approval_required"] is False
            and caps_prod["approval_required"] == required(ids["prod_pipe"]),
        )
        check(
            "9b 同时保留环境本身的默认值供设置页展示",
            caps_prod["group_approval_required"] is True,
        )
        check(
            "9c 强制审批的测试线：页面显示要审批",
            caps_test["approval_required"] is True
            and caps_test["group_approval_required"] is False,
        )
        check("9d 有豁免权限的人 can_exempt_approval=True", caps_prod["can_exempt_approval"] is True)

        u2 = db.get(User, ids["dev"])
        cur2 = CurrentUser(id=u2.id, username=u2.username, is_admin=False)
        caps_dev = _pipeline_caps(db, cur2, db.get(Pipeline, ids["prod_pipe"]))
        check("9e 无豁免权限的人 can_exempt_approval=False", caps_dev["can_exempt_approval"] is False)

    # ---- 10. 找不到审批人时，报出真实分组名 ----
    # 强制审批可以落在任何环境上，报错再写死「生产分组」就会把人指错方向
    with SessionLocal() as db:
        from sqlalchemy import select as _select

        orphan = db.scalars(
            _select(Pipeline).where(Pipeline.name == "prd-app-copy")
        ).first()  # 生产组 + inherit
        try:
            # 由管理员自己发起：生产组里除他之外没人有 approve 权，自审又没开，
            # 这才是真正会「找不到审批人」的场景
            pipeline_service.create_release(
                db, orphan.id, "v1", "rolling", "manual", ids["admin"]
            )
            ok10, detail10 = False, "居然放行了"
        except BizException as e:
            detail10 = str(getattr(e, "message", None) or e)
            ok10 = "「生产」" in detail10 and "生产分组未找到" not in detail10
        check("10 缺审批人的报错指向真实分组名，而不是写死的「生产分组」", ok10, detail10)

    # ---- 11. 审计留痕 ----
    with SessionLocal() as db:
        from sqlalchemy import select

        logs = db.scalars(
            select(AuditLog).where(AuditLog.action == "pipeline.approval_mode")
        ).all()
        check(
            "11 改审批模式写了审计，能查出是谁什么时候免掉的",
            len(logs) >= 2 and any("exempt" in (r.detail or "") for r in logs),
            f"{len(logs)} 条",
        )

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
