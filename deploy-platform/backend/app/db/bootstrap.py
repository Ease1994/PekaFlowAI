"""全新环境启动：等库、补管理员、把默认配置落到库里。

create_all / 补列之后调用。幂等，空库和旧库都能再跑一遍。
`python -m app.db.bootstrap` 只写密钥文件，不连库——给 compose 的 secrets-init 用。
"""
from __future__ import annotations

import os
import sys
import time

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.modules.settings.defaults import DEFAULT_SETTINGS

# 本机回环地址视为「还没用自备集群」。Compose 注入 ES_HOSTS 时改成容器内地址。
_LOOPBACK_ES_HOSTS = frozenset({
    "http://localhost:9200",
    "http://127.0.0.1:9200",
    "https://localhost:9200",
    "https://127.0.0.1:9200",
})


def wait_for_database(engine: Engine, *, attempts: int = 40, delay: float = 1.5) -> None:
    """库还没起来就建表会把整个进程打死。空环境里 MySQL 经常比应用晚几秒。"""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if i > 1:
                print(f"[startup] 数据库已连通（第 {i} 次）")
            return
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[startup] 等待数据库… ({i}/{attempts}) {e}", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"数据库连不上，已重试 {attempts} 次：{last}") from last


def init_secrets() -> str:
    """把 JWT / AES / Runner 令牌写到 data/.secrets.json。

    同级 .env 里配了 JWT_SECRET / AES_KEY / HARNESS_RUNNER_TOKEN 就用那份并写入文件，
    换机才能解库里的凭证。没配且文件里也没有，才生成随机值。
    """
    import json

    from app.core.config import _SECRETS_FILE, _load_or_create_secret

    data: dict = {}
    if _SECRETS_FILE.exists():
        try:
            loaded = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    env_map = {
        "jwt_secret": (os.environ.get("JWT_SECRET") or "").strip(),
        "aes_key": (os.environ.get("AES_KEY") or "").strip(),
        "harness_runner_token": (os.environ.get("HARNESS_RUNNER_TOKEN") or "").strip(),
    }
    wrote = False
    for key, val in env_map.items():
        if val:
            data[key] = val
            wrote = True
    if wrote:
        _SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRETS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            _SECRETS_FILE.chmod(0o600)
        except OSError:
            pass

    _load_or_create_secret("jwt_secret", 48)
    _load_or_create_secret("aes_key", 32)
    token = _load_or_create_secret("harness_runner_token", 48)
    print("[bootstrap] 密钥已写入 data/.secrets.json")
    return token


def harness_runner_token() -> str:
    env = (os.environ.get("HARNESS_RUNNER_TOKEN") or "").strip()
    if env:
        return env
    from app.core.config import _load_or_create_secret

    return _load_or_create_secret("harness_runner_token", 48)


def ensure_admin(db: Session) -> None:
    """没有管理员就建一个。口令优先 BOOTSTRAP_ADMIN_PASSWORD。

    未设置环境变量时仍用演示口令 admin123 以便空库能登录，但会打警告。
    库里已经有管理员且已经改过密的，不会被覆盖；若仍是演示哈希只告警不锁死。
    """
    from app.core.security import hash_password, verify_password
    from app.db.models import User
    from app.db.seed import DEMO_PASSWORD

    env_password = (os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or "").strip()
    bootstrap_password = env_password or DEMO_PASSWORD

    existing_admin = db.scalar(select(User).where(User.is_admin.is_(True)).limit(1))
    if existing_admin is not None:
        if not env_password and verify_password(DEMO_PASSWORD, existing_admin.password_hash or ""):
            print(
                "[startup] 警告：管理员仍是演示口令。生产请设置 BOOTSTRAP_ADMIN_PASSWORD 或立刻改密",
                file=sys.stderr,
            )
        return
    existing = db.scalar(select(User).where(User.username == "admin").limit(1))
    if existing is not None:
        existing.is_admin = True
        existing.status = "active"
        db.commit()
        print("[startup] 已把已有账号 admin 提升为管理员")
        return
    db.add(
        User(
            username="admin",
            display_name="系统管理员",
            email="",
            password_hash=hash_password(bootstrap_password),
            is_admin=True,
            source="local",
            status="active",
        )
    )
    db.commit()
    if env_password:
        print("[startup] 已创建管理员 admin（口令来自 BOOTSTRAP_ADMIN_PASSWORD）")
    else:
        print(
            "[startup] 已创建管理员 admin / 演示口令。生产必须设置 BOOTSTRAP_ADMIN_PASSWORD 并立刻改密",
            file=sys.stderr,
        )


def ensure_platform_settings(db: Session) -> None:
    """缺的配置项写入库。环境变量优先，这样 compose 里的 Redis / Runner 不用再手填。"""
    from app.modules.settings.models import PlatformSetting

    token = harness_runner_token()
    overrides = {
        "harness_runner_token": token,
        "harness_runner_url": (os.environ.get("HARNESS_RUNNER_URL") or "").strip(),
    }
    created = 0
    for key, default in DEFAULT_SETTINGS.items():
        row = db.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        env_val = (os.environ.get(key.upper()) or "").strip()
        value = env_val or overrides.get(key) or default
        if key == "harness_runner_url" and not value:
            value = default
        if row is None:
            db.add(PlatformSetting(key=key, value=str(value)))
            created += 1
        elif key == "es_hosts" and env_val and (row.value or "").strip() in _LOOPBACK_ES_HOSTS:
            row.value = env_val
        elif key in {"harness_runner_token", "harness_runner_url"} and not (row.value or "").strip() and value:
            row.value = str(value)
    if created:
        db.commit()
        print(f"[startup] 已写入 {created} 项平台默认配置")
    else:
        db.commit()


def ensure_runtime_ready() -> None:
    """管理员 + 平台配置。列已经齐了，走 ORM。"""
    from app.db.session import SessionLocal
    from app.modules.settings import get_setting, update_settings

    with SessionLocal() as db:
        ensure_platform_settings(db)
        ensure_admin(db)
        if not get_setting(db, "agent_enroll_token").strip():
            import secrets

            update_settings(db, {"agent_enroll_token": secrets.token_hex(24)})
            print("[startup] 已生成构建机接入凭证，请到「构建机」页面查看")


if __name__ == "__main__":
    init_secrets()
