"""这次产品改动还没被其它脚本钉住的部分。

已经有独立脚本的不再重复：
  - 审批模式三档 / 通配符不带豁免 → _verify_approval_mode.py
  - 个人分组建/改/删/归入 → _verify_personal_folders.py
  - 审批卡死 / 伪造父级 / OOM → 各自脚本

这里补：
  1. 权限申请默认拟授查看+执行
  2. 点名编辑等动作时，审核通过会按拟授落库
  3. 角色申请通过后写 UserRole，不写 Permission 行
  4. AI 内置「节点文件下发」流水线被改成豁免后，下次 ensure 会自愈回 inherit
  5. 复制流水线时显式指定个人分组会覆盖源分组
  6. 重名个人分组被拒绝；两个用户的分组互不可见
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_product_coverage.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


PIPELINE_YAML = """
pipeline:
  name: demo
  stages:
    - name: s1
      jobs:
        - name: j1
          agent: windows
          steps:
            - name: echo
              plugin: shell
              with:
                script: echo hi
"""


def main() -> int:  # noqa: C901
    import app.main  # noqa: F401

    from app.core.deps import CurrentUser, check_permission
    from app.core.response import BizException
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.access import service as access_service
    from app.modules.ai import nodepush
    from app.modules.auth.models import Permission, Role, User, UserRole
    from app.modules.pipeline import service as pipeline_service
    from app.modules.pipeline.models import Pipeline, UserPipelinePref
    from app.modules.project.models import Group, Project
    from sqlalchemy import select

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_product_coverage.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        admin = User(username="admin", password_hash="x", display_name="管理员", is_admin=True)
        applicant = User(username="app", password_hash="x", display_name="申请人", is_admin=False)
        other = User(username="other", password_hash="x", display_name="另一个人", is_admin=False)
        db.add_all([admin, applicant, other])
        db.flush()
        proj = Project(name="demo", code="demo")
        db.add(proj)
        db.flush()
        grp = Group(
            project_id=proj.id, name="生产", type="prod",
            approval_required=True, allow_self_approval=True,
        )
        db.add(grp)
        db.flush()
        pipe = Pipeline(
            project_id=proj.id, group_id=grp.id, name="prd-app", yaml=PIPELINE_YAML,
            status="active", created_by=admin.id, workspace_uuid="ws1",
        )
        db.add(pipe)
        db.flush()
        db.add(
            Permission(
                user_id=applicant.id, resource_type="pipeline", resource_id=pipe.id,
                action="read", effect="allow",
            )
        )
        db.commit()
        ids = {
            "proj": proj.id, "grp": grp.id, "pipe": pipe.id,
            "admin": admin.id, "app": applicant.id, "other": other.id,
        }

    def user(uid: int) -> CurrentUser:
        with SessionLocal() as db:
            u = db.get(User, uid)
            return CurrentUser(id=u.id, username=u.username, is_admin=u.is_admin)

    admin_u = user(ids["admin"])
    app_u = user(ids["app"])
    other_u = user(ids["other"])

    # ---- 1/2. 权限申请 ----
    with SessionLocal() as db:
        row = access_service.apply_execute(db, app_u, ids["pipe"], "要跑生产")
        check(
            "1a 申请单拟授只有查看+执行",
            row["granted_action_list"] == ["read", "execute"],
            str(row["granted_action_list"]),
        )
        reviewed = access_service.review(
            db,
            admin_u,
            row["id"],
            approved=True,
            comment="按拟授通过",
        )
        check(
            "1b 默认申请审核后仍是查看+执行",
            reviewed["granted_action_list"] == ["read", "execute"],
            str(reviewed["granted_action_list"]),
        )

    with SessionLocal() as db:
        cur = user(ids["app"])
        check(
            "2a 通过后有执行权",
            check_permission(db, cur, "pipeline", ids["pipe"], "execute") is True,
        )
        check(
            "2b 通过后仍然没有编辑权",
            check_permission(db, cur, "pipeline", ids["pipe"], "update") is False,
        )
        check(
            "2c 通过后仍然没有删除权",
            check_permission(db, cur, "pipeline", ids["pipe"], "delete") is False,
        )
        check(
            "2d 通过后仍然没有豁免审批权",
            check_permission(
                db, cur, "pipeline", ids["pipe"], "approval_exempt", allow_wildcard=False
            )
            is False,
        )

    with SessionLocal() as db:
        row = access_service.apply_execute(
            db, app_u, ids["pipe"], "还要改流水线", actions=["read", "update"]
        )
        check(
            "1c 已有执行权仍可再申请编辑",
            "update" in row["granted_action_list"],
            str(row["granted_action_list"]),
        )
        reviewed = access_service.review(db, admin_u, row["id"], approved=True, comment="可以改")
        check(
            "1d 点名编辑则实授含编辑",
            "update" in reviewed["granted_action_list"],
            str(reviewed["granted_action_list"]),
        )

    with SessionLocal() as db:
        cur = user(ids["app"])
        check(
            "1e 第二张单通过后有编辑权",
            check_permission(db, cur, "pipeline", ids["pipe"], "update") is True,
        )

    with SessionLocal() as db:
        import json

        role = Role(
            project_id=ids["proj"],
            name="开发者",
            description="",
            permissions=json.dumps({"pipeline": ["read", "execute"]}, ensure_ascii=False),
        )
        db.add(role)
        db.commit()
        db.refresh(role)
        other = user(ids["other"])
        row = access_service.apply_role(db, other, role_id=role.id, reason="要角色")
        check("3a 角色申请 scope 是 role", row["apply_type"] == "role", row.get("apply_type"))
        access_service.review(db, admin_u, row["id"], approved=True, comment="可以")
        member = db.scalar(
            select(UserRole).where(UserRole.user_id == other.id, UserRole.role_id == role.id)
        )
        check("3b 通过后成为角色成员", member is not None)
        check(
            "3c 角色申请不写直授行",
            not db.scalars(select(Permission).where(Permission.user_id == other.id)).all(),
        )

    # ---- 4. 内置下发流水线自愈 ----
    with SessionLocal() as db:
        sys_pipe = nodepush.ensure_pipeline(db)
        sys_pipe.approval_mode = "exempt"
        db.commit()
        healed = nodepush.ensure_pipeline(db)
        check(
            "4a 内置下发流水线被豁免后，下次 ensure 自愈回 inherit",
            (healed.approval_mode or "inherit") == "inherit",
            healed.approval_mode,
        )
        g = db.get(Group, healed.group_id)
        check(
            "4b 自愈后这条线仍然要审批（生产节点下发闸门还在）",
            pipeline_service.approval_required_for(g, healed) is True,
        )

    # ---- 4. 复制时显式指定个人分组 ----
    with SessionLocal() as db:
        pipeline_service.set_pipeline_folder(db, ids["admin"], ids["pipe"], "源组")
        copied = pipeline_service.duplicate_pipeline(
            db, ids["pipe"], "prd-app_copy", None, None, ids["admin"], True, folder="新组"
        )
        pref = db.scalar(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == ids["admin"],
                UserPipelinePref.pipeline_id == copied.id,
            )
        )
        check(
            "4 复制时指定 folder 覆盖源分组",
            pref is not None and pref.folder == "新组",
            pref.folder if pref else "无偏好",
        )

    # ---- 5. 重名拒绝；两人分组互不可见 ----
    with SessionLocal() as db:
        pipeline_service.create_personal_folder(db, ids["app"], ids["proj"], "日常")
        try:
            pipeline_service.rename_personal_folder(
                db, ids["app"], ids["proj"], "日常", "日常"
            )
            # 改成自己是幂等，允许
            ok_same = True
            d_same = "ok"
        except BizException as e:
            ok_same, d_same = False, str(getattr(e, "message", None) or e)
        check("5a 重命名成自己是幂等", ok_same, d_same)

        pipeline_service.create_personal_folder(db, ids["app"], ids["proj"], "另一组")
        try:
            pipeline_service.rename_personal_folder(
                db, ids["app"], ids["proj"], "日常", "另一组"
            )
            ok_dup, d_dup = False, "居然放行了"
        except BizException as e:
            d_dup = str(getattr(e, "message", None) or e)
            ok_dup = "已经有" in d_dup
        check("5b 重命名撞名被拒绝", ok_dup, d_dup)

        mine = pipeline_service.list_personal_folders(db, ids["app"], ids["proj"])
        theirs = pipeline_service.list_personal_folders(db, ids["other"], ids["proj"])
        check("5c 申请人能看到自己的组", "日常" in mine and "另一组" in mine, str(mine))
        check("5d 另一个人看不到别人的组", theirs == [], str(theirs))

        try:
            pipeline_service.create_personal_folder(db, ids["app"], ids["proj"], "  ")
            ok_empty, d_empty = False, "居然放行了"
        except BizException as e:
            d_empty = str(getattr(e, "message", None) or e)
            ok_empty = "不能为空" in d_empty
        check("5e 空分组名被拒绝", ok_empty, d_empty)

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
