"""平台配置读写。一般是环境变量 > 数据库 > 默认值；ES 地址和索引以设置页为准。"""
from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.settings.defaults import DEFAULT_ES_INDEX_PREFIX, DEFAULT_SETTINGS
from app.modules.settings.models import PlatformSetting

# 文件落盘的项只能走专用上传/删除接口，整表 PUT 带上会把 MIME 冲掉、图还在盘上对不上
FILE_BACKED_KEYS = frozenset({"header_image_mime"})

# 这几项以设置页保存的库值为准。Compose 只在首次写入时用环境变量，避免把自备集群锁死。
UI_OWNED_KEYS = frozenset({"es_hosts", "es_index", "es_username", "es_password"})


def get_all_settings(db: Session) -> dict:
    """返回所有配置（默认值 + DB 覆盖 + 环境变量覆盖）。"""
    result = dict(DEFAULT_SETTINGS)
    db_keys: set[str] = set()
    for s in db.scalars(select(PlatformSetting)).all():
        if s.key in DEFAULT_SETTINGS:
            result[s.key] = s.value
            db_keys.add(s.key)
    for key in list(result.keys()):
        if key in UI_OWNED_KEYS and key in db_keys:
            continue
        env_val = os.environ.get(key.upper())
        if env_val is not None and env_val != "":
            result[key] = env_val
    if (result.get("log_storage") or "").strip().lower() in {"mysql", "redis"}:
        result["log_storage"] = "es"
    return result


def update_settings(db: Session, updates: dict) -> dict:
    """更新配置（未知 key 忽略）。保存后立即对后续请求生效。"""
    redis_changed = False
    es_changed = False
    payload = dict(updates)
    if str(payload.get("log_storage") or "").strip().lower() in {"mysql", "redis"}:
        payload["log_storage"] = "es"
    for key, value in payload.items():
        if key not in DEFAULT_SETTINGS or key in FILE_BACKED_KEYS:
            continue
        if key == "es_index" and (value is None or str(value).strip() == ""):
            value = DEFAULT_ES_INDEX_PREFIX
        if key.startswith("redis_") and str(value) != get_setting(db, key):
            redis_changed = True
        if key.startswith("es_") and str(value) != get_setting(db, key):
            es_changed = True
        if key == "platform_display_name":
            from app.modules.settings.branding import clamp_display_name

            value = clamp_display_name(value)
        if key == "smtp_from_name":
            from app.modules.settings.branding import clamp_from_name

            value = clamp_from_name(value)
        if key == "session_expire_days":
            from app.modules.settings.branding import clamp_session_expire_days

            value = str(clamp_session_expire_days(value))
        if key == "totp_2fa_enabled":
            value = "true" if str(value).strip().lower() in {"true", "1", "yes"} else "false"
        if key == "public_app_base":
            from app.modules.settings.branding import clamp_public_app_base

            value = clamp_public_app_base(value)
        if key == "audit_retention_days":
            from app.modules.audit.service import clamp_retention_days

            value = str(clamp_retention_days(value))
        if key == "artifact_prod_retention_days":
            from app.modules.artifact.service import clamp_prod_retention_days

            value = str(clamp_prod_retention_days(value))
        if key == "header_notice_text":
            from app.modules.settings.branding import clamp_notice_text

            value = clamp_notice_text(value)
        if key == "header_notice_color":
            from app.modules.settings.branding import clamp_notice_color

            value = clamp_notice_color(value)
        s = db.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        if s is None:
            s = PlatformSetting(key=key, value=str(value))
            db.add(s)
        else:
            s.value = str(value)
    db.commit()

    if redis_changed:
        from app.core.redis_client import reset_redis

        reset_redis()
    if redis_changed or es_changed or "log_storage" in payload:
        from app.modules.agent.log_store import reset_log_store
        from app.modules.ai.audit_store import reset_ai_audit_store

        reset_log_store()
        reset_ai_audit_store()
        from app.modules.health.service import reset_health_cache

        reset_health_cache()

    return get_all_settings(db)


def get_setting(db: Session, key: str, default: str = "") -> str:
    """读取单个配置项。环境变量名为 key 大写，如 redis_host → REDIS_HOST。"""
    stored = db.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
    if key in UI_OWNED_KEYS and stored is not None:
        return stored.value
    env_val = os.environ.get(key.upper())
    if env_val is not None and env_val != "":
        return env_val
    if stored is not None:
        return stored.value
    return DEFAULT_SETTINGS.get(key, default)
