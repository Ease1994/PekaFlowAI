"""请求级审计上下文：操作人是当前用户，source 标明入口（web/ai/api/cron）。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_source: ContextVar[str] = ContextVar("audit_source", default="web")
_skill: ContextVar[str] = ContextVar("audit_skill", default="")
_user_id: ContextVar[int | None] = ContextVar("audit_user_id", default=None)
_username: ContextVar[str] = ContextVar("audit_username", default="")
_ip: ContextVar[str] = ContextVar("audit_ip", default="")


def bind_request(*, source: str, user_id: int | None, username: str, ip: str = "") -> None:
    _source.set(source)
    _user_id.set(user_id)
    _username.set(username or "")
    _ip.set(ip or "")


def bind_skill(name: str):
    return _skill.set(name or "")


def reset_skill(token) -> None:
    _skill.reset(token)


def current_source() -> str:
    return _source.get() or "web"


def current_skill() -> str:
    return _skill.get() or ""


def current_user_id() -> int | None:
    return _user_id.get()


def current_username() -> str:
    return _username.get() or ""


def current_ip() -> str:
    return _ip.get() or ""


@contextmanager
def use_source(source: str, *, user_id: int | None = None, username: str = ""):
    """非 HTTP 场景（如 cron）临时绑定来源。"""
    t_s = _source.set(source)
    t_u = _user_id.set(user_id)
    t_n = _username.set(username)
    try:
        yield
    finally:
        _source.reset(t_s)
        _user_id.reset(t_u)
        _username.reset(t_n)
