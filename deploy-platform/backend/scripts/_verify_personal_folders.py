"""个人分组：看得到项目就能建，不需要创建流水线的权限。

个人分组是「我的工作篮」，不是资源授权。如果建组还要 project:create，
普通执行者就只能看一堆流水线、没法归拢，这个功能等于没做。

覆盖：
  1. 没有 create 权限的人也能建空组、重命名、删除
  2. 空组在没有流水线时仍然存在
  3. 把流水线移进组 / 移出组
  4. 删除分组不会删流水线，只是移出
  5. 重命名会带着组里的流水线一起改名
  6. 新建流水线带 folder 时自动归入，且写入目录
  7. 复制流水线默认跟着源在「我这边」的分组
  8. 系统保留名不能用
  9. 看不见的项目建不了组
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_personal_folders.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    import app.main  # noqa: F401

    from app.core.deps import CurrentUser, require_project_visible
    from app.core.response import BizException
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.auth.models import Permission, User
    from app.modules.pipeline import service as pipeline_service
    from app.modules.pipeline.models import Pipeline, UserPipelinePref
    from app.modules.project.models import Group, Project
    from sqlalchemy import select

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_personal_folders.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        admin = User(username="admin", password_hash="x", display_name="管理员", is_admin=True)
        runner = User(username="runner", password_hash="x", display_name="执行者", is_admin=False)
        stranger = User(username="stranger", password_hash="x", display_name="外人", is_admin=False)
        db.add_all([admin, runner, stranger])
        db.flush()
        proj = Project(name="demo", code="demo")
        other = Project(name="other", code="other")
        db.add_all([proj, other])
        db.flush()
        grp = Group(project_id=proj.id, name="测试", type="test", approval_required=False)
        db.add(grp)
        db.flush()
        pipe = Pipeline(
            project_id=proj.id, group_id=grp.id, name="app", yaml="",
            status="active", created_by=admin.id, workspace_uuid="ws1",
        )
        db.add(pipe)
        db.flush()
        db.add(
            Permission(
                user_id=runner.id, resource_type="pipeline", resource_id=pipe.id,
                action="execute", effect="allow",
            )
        )
        db.add(
            Permission(
                user_id=runner.id, resource_type="pipeline", resource_id=pipe.id,
                action="read", effect="allow",
            )
        )
        db.commit()
        ids = {
            "proj": proj.id, "other": other.id, "grp": grp.id, "pipe": pipe.id,
            "admin": admin.id, "runner": runner.id, "stranger": stranger.id,
        }

    def as_user(uid: int) -> CurrentUser:
        with SessionLocal() as db:
            u = db.get(User, uid)
            return CurrentUser(id=u.id, username=u.username, is_admin=u.is_admin)

    runner_u = as_user(ids["runner"])

    # ---- 1. 没有 create 也能建组 ----
    with SessionLocal() as db:
        can_create = False
        from app.core.deps import check_permission

        can_create = check_permission(db, runner_u, "project", ids["proj"], "create")
        check("1a 执行者没有项目创建权", can_create is False)
        names = pipeline_service.create_personal_folder(db, ids["runner"], ids["proj"], "日常发布")
        check("1b 没有创建权也能建个人分组", "日常发布" in names, str(names))

    # ---- 2. 空组在目录里 ----
    with SessionLocal() as db:
        names = pipeline_service.list_personal_folders(db, ids["runner"], ids["proj"])
        check("2 空组没有流水线时仍然在目录里", names == ["日常发布"], str(names))

    # ---- 3. 移进 / 移出 ----
    with SessionLocal() as db:
        pipeline_service.set_pipeline_folder(db, ids["runner"], ids["pipe"], "日常发布")
        pref = db.scalar(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == ids["runner"],
                UserPipelinePref.pipeline_id == ids["pipe"],
            )
        )
        check("3a 流水线进组", pref is not None and pref.folder == "日常发布")
        pipeline_service.set_pipeline_folder(db, ids["runner"], ids["pipe"], "")
        db.refresh(pref)
        check("3b 移出后 folder 为空", pref.folder == "")

    # ---- 4. 删组不删流水线 ----
    with SessionLocal() as db:
        pipeline_service.set_pipeline_folder(db, ids["runner"], ids["pipe"], "日常发布")
        pipeline_service.delete_personal_folder(db, ids["runner"], ids["proj"], "日常发布")
        p = db.get(Pipeline, ids["pipe"])
        pref = db.scalar(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == ids["runner"],
                UserPipelinePref.pipeline_id == ids["pipe"],
            )
        )
        check("4a 流水线还在", p is not None and p.status == "active")
        check("4b 只是移出了分组", pref.folder == "")
        names = pipeline_service.list_personal_folders(db, ids["runner"], ids["proj"])
        check("4c 目录里没有这个组了", "日常发布" not in names, str(names))

    # ---- 5. 重命名带着流水线走 ----
    with SessionLocal() as db:
        pipeline_service.create_personal_folder(db, ids["runner"], ids["proj"], "旧名")
        pipeline_service.set_pipeline_folder(db, ids["runner"], ids["pipe"], "旧名")
        names = pipeline_service.rename_personal_folder(
            db, ids["runner"], ids["proj"], "旧名", "新名"
        )
        pref = db.scalar(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == ids["runner"],
                UserPipelinePref.pipeline_id == ids["pipe"],
            )
        )
        check("5a 目录改名", names == ["新名"], str(names))
        check("5b 流水线跟着改名", pref.folder == "新名")

    # ---- 6. 新建流水线带 folder ----
    with SessionLocal() as db:
        created = pipeline_service.create_pipeline(
            db,
            {
                "project_id": ids["proj"],
                "group_id": ids["grp"],
                "name": "new-app",
                "folder": "我负责的",
            },
            created_by=ids["admin"],
        )
        pref = db.scalar(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == ids["admin"],
                UserPipelinePref.pipeline_id == created.id,
            )
        )
        names = pipeline_service.list_personal_folders(db, ids["admin"], ids["proj"])
        check("6a 新线归入指定组", pref is not None and pref.folder == "我负责的")
        check("6b 指定组写进了目录", "我负责的" in names, str(names))

    # ---- 7. 复制跟着源分组 ----
    with SessionLocal() as db:
        src_id = db.scalars(select(Pipeline.id).where(Pipeline.name == "new-app")).first()
        copied = pipeline_service.duplicate_pipeline(
            db, src_id, "new-app_copy", None, None, ids["admin"], True
        )
        pref = db.scalar(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == ids["admin"],
                UserPipelinePref.pipeline_id == copied.id,
            )
        )
        check("7 复制默认跟着源在我这边的分组", pref is not None and pref.folder == "我负责的")

    # ---- 8. 保留名 ----
    with SessionLocal() as db:
        try:
            pipeline_service.create_personal_folder(db, ids["runner"], ids["proj"], "我的收藏")
            ok8, d8 = False, "居然放行了"
        except BizException as e:
            d8 = str(getattr(e, "message", None) or e)
            ok8 = "保留" in d8
        check("8 系统保留名不能用", ok8, d8)

    # ---- 9. 看不见的项目 ----
    with SessionLocal() as db:
        stranger_u = as_user(ids["stranger"])
        try:
            require_project_visible(db, stranger_u, ids["proj"])
            ok9, d9 = False, "居然可见"
        except BizException:
            ok9, d9 = True, "不可见"
        check("9 外人看不见项目，上层会拦住建组", ok9, d9)

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
