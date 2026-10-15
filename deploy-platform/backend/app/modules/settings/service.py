"""平台配置读写。一般是环境变量 > 数据库 > 默认值；ES 地址和索引以设置页为准。

导入导出只搬运营配置。启动项（数据库连接、JWT 密钥）和顶栏图片文件不在这份 JSON 里。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.settings.defaults import DEFAULT_ES_INDEX_PREFIX, DEFAULT_SETTINGS
from app.modules.settings.models import PlatformSetting

# 文件落盘的项只能走专用上传/删除接口，整表 PUT 带上会把 MIME 冲掉、图还在盘上对不上
FILE_BACKED_KEYS = frozenset({"header_image_mime"})

# 这几项以设置页保存的库值为准。Compose 只在首次写入时用环境变量，避免把自备集群锁死。
UI_OWNED_KEYS = frozenset({"es_hosts", "es_index", "es_username", "es_password"})

# 导出文件种类。导入时对不上就整单拒绝，避免把别的 JSON 灌进平台配置。
EXPORT_KIND = "pekaflow.platform-settings"
# 当前导出格式。不认识的版本拒绝，不猜测字段含义。
EXPORT_VERSION = 1
# 单文件上限。运营配置只有几十个短字段，超过这个量就不是本页导出的文件。
MAX_IMPORT_BYTES = 256 * 1024
# 允许比默认可写项多出的未知键数量。再多就当异常文件拒绝，不进更新循环。
_IMPORT_KEY_SLACK = 32


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


def export_document(db: Session) -> dict:
    """导出当前生效的运营配置。

    只收录默认表里的可写项，值是此刻读到的字符串（含环境变量覆盖后的结果）。
    顶栏图 MIME 不带上：图片文件不在 JSON 里，单独搬 MIME 会和磁盘对不上。
    密码、企微 Secret、构建机接入凭证都在 settings 里，只应交给管理员下载。
    """
    current = get_all_settings(db)
    settings: dict[str, str] = {}
    for key in DEFAULT_SETTINGS:
        if key in FILE_BACKED_KEYS:
            continue
        raw = current.get(key, "")
        settings[key] = "" if raw is None else str(raw)
    return {
        "kind": EXPORT_KIND,
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings": settings,
    }


def settings_from_import(payload: object) -> dict[str, str]:
    """把导出文件还原成可写入的键值。不合规则抛业务异常，调用方不得写库。

    空对象、种类不对、版本不对、一个可写项都没有，都在这里停下。
    未知键和顶栏图 MIME 直接丢掉。值只接受文本、整数和布尔，布尔写成 true/false，与页面保存一致。
    """
    if not isinstance(payload, dict):
        raise BizException.bad_request("配置文件格式不正确")
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise BizException.bad_request("配置文件过大，已拒绝导入")
    if payload.get("kind") != EXPORT_KIND:
        raise BizException.bad_request("不是平台配置导出文件")
    if payload.get("version") != EXPORT_VERSION:
        raise BizException.bad_request("不支持的导出文件版本")
    raw = payload.get("settings")
    if not isinstance(raw, dict) or len(raw) == 0:
        raise BizException.bad_request("文件里没有可导入的配置项")
    if len(raw) > len(DEFAULT_SETTINGS) + _IMPORT_KEY_SLACK:
        raise BizException.bad_request("配置项过多，已拒绝导入")
    updates: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in DEFAULT_SETTINGS or key in FILE_BACKED_KEYS:
            continue
        updates[key] = _import_value(key, value)
    if not updates:
        raise BizException.bad_request("文件里没有可导入的配置项")
    return updates


def _import_value(key: str, value: object) -> str:
    """把文件里的单个值收成平台配置使用的字符串。其它类型整单拒绝，不猜。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    raise BizException.bad_request(f"配置项 {key} 的值必须是文本、整数或布尔")


def import_settings(
    db: Session,
    payload: object,
    *,
    user_id: int | None = None,
    username: str = "",
) -> tuple[dict, int]:
    """用导出文件覆盖其中写明的运营配置，返回生效配置和实际写入的项数。

    先整单校验，通过后才写库。空文件、错误种类不会改任何项。
    文件里没出现的项保持原值。夹值、Redis / ES 重连与页面上点保存相同。
    传入操作人时先记审计，只记键名，和配置在同一次提交里落地。
    """
    updates = settings_from_import(payload)
    if user_id is not None:
        from app.modules.audit.service import write as write_audit

        write_audit(
            db,
            "settings.import",
            resource_type="platform_setting",
            detail="keys=" + ",".join(sorted(updates)),
            user_id=user_id,
            username=username,
        )
    applied = update_settings(db, updates)
    return applied, len(updates)
