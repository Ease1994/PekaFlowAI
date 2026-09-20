"""登录失败锁定：同一用户名（大小写不敏感）+ IP 计次。

计数优先放 Redis，没有 Redis 则进程内字典（和现网「Redis 挂了后端仍能起」一致）。
对外文案不区分用户不存在、密码错、已锁定，避免给攻击者额外信号。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 密码错几次锁、锁多久；TOTP 错几次作废 pending。
PASSWORD_LIMIT = 5
PASSWORD_LOCK_SECONDS = 15 * 60
TOTP_LIMIT = 8
TOTP_WINDOW_SECONDS = 15 * 60

# 进程内兜底：key -> (次数, 过期时间戳)
_mem: dict[str, tuple[int, float]] = {}
_mem_lock = threading.Lock()


def _redis():
    """拿到 Redis；连不上返回 None，调用方改走内存。"""
    try:
        from app.core.redis_client import get_redis, redis_available

        if not redis_available():
            return None
        return get_redis()
    except Exception:  # noqa: BLE001
        return None


def _incr(key: str, ttl: int) -> int:
    """给 key 加一并保证有过期时间。返回加完后的次数。"""
    client = _redis()
    if client is not None:
        try:
            n = int(client.incr(key))
            if n == 1:
                client.expire(key, ttl)
            return n
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    with _mem_lock:
        count, exp = _mem.get(key, (0, 0.0))
        if exp and now > exp:
            count = 0
        count += 1
        _mem[key] = (count, now + ttl)
        return count


def _count(key: str) -> int:
    """读当前次数，过期视为 0。"""
    client = _redis()
    if client is not None:
        try:
            raw = client.get(key)
            return int(raw) if raw else 0
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    with _mem_lock:
        count, exp = _mem.get(key, (0, 0.0))
        if exp and now > exp:
            _mem.pop(key, None)
            return 0
        return count


def _clear(key: str) -> None:
    """成功后清零。"""
    client = _redis()
    if client is not None:
        try:
            client.delete(key)
        except Exception:  # noqa: BLE001
            pass
    with _mem_lock:
        _mem.pop(key, None)


def _set_flag(key: str, ttl: int) -> None:
    """打一个存在即生效的标记（作废 pending）。"""
    client = _redis()
    if client is not None:
        try:
            client.setex(key, ttl, "1")
            return
        except Exception:  # noqa: BLE001
            pass
    with _mem_lock:
        _mem[key] = (1, time.time() + ttl)


def _has_flag(key: str) -> bool:
    """标记是否仍有效。"""
    return _count(key) > 0


def password_key(username: str, ip: str) -> str:
    """密码失败计数的 Redis/内存键。"""
    name = (username or "").strip().lower()
    return f"login:pw:{name}:{ip or '-'}"


def totp_key(username: str, ip: str) -> str:
    """TOTP 失败计数键。"""
    name = (username or "").strip().lower()
    return f"login:totp:{name}:{ip or '-'}"


def pending_void_key(pending_token: str) -> str:
    """作废某张 pending 票。只存哈希，避免把票本身再写进 Redis。"""
    digest = hashlib.sha256((pending_token or "").encode("utf-8")).hexdigest()
    return f"login:mfa_void:{digest}"


def password_locked(username: str, ip: str) -> bool:
    """该用户名+IP 是否已因密码错误被锁。"""
    return _count(password_key(username, ip)) >= PASSWORD_LIMIT


def record_password_failure(username: str, ip: str) -> int:
    """记一次密码错。达到上限时打审计日志。"""
    n = _incr(password_key(username, ip), PASSWORD_LOCK_SECONDS)
    if n >= PASSWORD_LIMIT:
        logger.warning("登录已锁定 username=%s ip=%s fails=%s", username, ip, n)
    return n


def clear_password_failures(username: str, ip: str) -> None:
    """密码正确后清零。"""
    _clear(password_key(username, ip))


def totp_locked(username: str, ip: str) -> bool:
    """该用户名+IP 的 TOTP 是否已锁。"""
    return _count(totp_key(username, ip)) >= TOTP_LIMIT


def record_totp_failure(username: str, ip: str, pending_token: str) -> int:
    """记一次 TOTP 错。满 8 次作废 pending，调用方应让用户重新登录。"""
    n = _incr(totp_key(username, ip), TOTP_WINDOW_SECONDS)
    if n >= TOTP_LIMIT:
        _set_flag(pending_void_key(pending_token), TOTP_WINDOW_SECONDS)
        logger.warning("双因子已锁定 username=%s ip=%s fails=%s", username, ip, n)
    return n


def pending_voided(pending_token: str) -> bool:
    """这张 pending 票是否已被失败次数作废。"""
    return _has_flag(pending_void_key(pending_token))


def clear_totp_failures(username: str, ip: str) -> None:
    """TOTP 正确后清零。"""
    _clear(totp_key(username, ip))


def reset_for_tests() -> None:
    """单测用：清掉进程内计数。"""
    with _mem_lock:
        _mem.clear()
