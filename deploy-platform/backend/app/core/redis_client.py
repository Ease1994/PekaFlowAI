"""Redis 客户端封装（惰性连接，从平台设置读配置）。"""
from __future__ import annotations

import time

import redis as redis_lib

_client: redis_lib.Redis | None = None
_skip_until = 0.0
_COOLDOWN = 15.0


def get_redis() -> redis_lib.Redis:
    """获取 Redis 连接（惰性创建，从平台设置读 host/port/password/db）。"""
    global _client
    if _client is not None:
        return _client

    from app.db.session import SessionLocal
    from app.modules.settings import get_setting

    with SessionLocal() as db:
        host = get_setting(db, "redis_host", "localhost")
        port = int(get_setting(db, "redis_port", "6379"))
        password = get_setting(db, "redis_password", "")
        dbnum = int(get_setting(db, "redis_db", "0"))

    _client = redis_lib.Redis(
        host=host,
        port=port,
        password=password or None,
        db=dbnum,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=2.0,
    )
    return _client


def get_redis_blocking(block_seconds: int = 5) -> redis_lib.Redis:
    """专用于 XREAD BLOCK 的连接（超时略大于 block 时间）。"""
    from app.db.session import SessionLocal
    from app.modules.settings import get_setting

    with SessionLocal() as db:
        host = get_setting(db, "redis_host", "localhost")
        port = int(get_setting(db, "redis_port", "6379"))
        password = get_setting(db, "redis_password", "")
        dbnum = int(get_setting(db, "redis_db", "0"))

    return redis_lib.Redis(
        host=host,
        port=port,
        password=password or None,
        db=dbnum,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=float(block_seconds) + 2.0,
    )


def reset_redis() -> None:
    """平台设置变更后重置连接，让新配置立即生效。"""
    global _client, _skip_until
    _client = None
    _skip_until = 0.0


def redis_available() -> bool:
    """探测 Redis 是否可用。失败后冷却，避免每个 SSE 循环都卡在 connect timeout。"""
    global _skip_until
    now = time.time()
    if now < _skip_until:
        return False
    try:
        return bool(get_redis().ping())
    except Exception:
        _skip_until = now + _COOLDOWN
        return False
