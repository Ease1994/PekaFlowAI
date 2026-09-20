"""编辑用户：不能取消自己的管理员，也不能把最后一名启用管理员降掉。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.db.base import Base
from app.modules.account.service import update_user
from app.modules.auth.models import User


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__])
    return Session(engine)


def _add(db: Session, username: str, *, is_admin: bool) -> User:
    """造一个可编辑的本地账号。本测不校验密码。"""
    row = User(
        username=username,
        display_name=username,
        email=f"{username}@mail.test",
        password_hash="x",
        is_admin=is_admin,
        source="local",
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _as(row: User) -> CurrentUser:
    return CurrentUser(id=row.id, username=row.username, is_admin=bool(row.is_admin))


def test_admin_cannot_demote_self() -> None:
    db = _db()
    admin = _add(db, "admin", is_admin=True)
    _add(db, "ops", is_admin=True)
    try:
        update_user(db, _as(admin), admin.id, is_admin=False)
        raise AssertionError("自己取消管理员必须失败")
    except BizException as exc:
        assert exc.code == 400
        assert "不能取消自己的管理员" in str(exc.message)
    db.refresh(admin)
    assert admin.is_admin is True


def test_admin_can_update_own_profile_without_demoting() -> None:
    db = _db()
    admin = _add(db, "admin", is_admin=True)
    out = update_user(db, _as(admin), admin.id, display_name="系统管理员", is_admin=True)
    assert out["is_admin"] is True
    assert out["display_name"] == "系统管理员"


def test_other_admin_can_demote_when_another_remains() -> None:
    db = _db()
    admin = _add(db, "admin", is_admin=True)
    ops = _add(db, "ops", is_admin=True)
    out = update_user(db, _as(admin), ops.id, is_admin=False)
    assert out["is_admin"] is False


def test_cannot_demote_last_active_admin() -> None:
    db = _db()
    admin = _add(db, "admin", is_admin=True)
    ops = _add(db, "ops", is_admin=True)
    update_user(db, _as(admin), ops.id, status="disabled")
    try:
        update_user(db, _as(ops), admin.id, is_admin=False)
        raise AssertionError("最后一名启用管理员不能被降权")
    except BizException as exc:
        assert exc.code == 400
        assert "至少保留一名" in str(exc.message)
    db.refresh(admin)
    assert admin.is_admin is True
