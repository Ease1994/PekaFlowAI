"""SMTP 发信：发布结束通知发给当前发布人，HTML 与纯文本各一份。"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

logger = logging.getLogger(__name__)


def _real_email(addr: str) -> bool:
    """占位邮箱和空地址不发信，避免 SMTP 被无效收件人打满日志。"""
    from app.modules.auth.identity import _is_placeholder_email

    e = (addr or "").strip()
    return bool(e) and "@" in e and not _is_placeholder_email(e)


def _public_origin(cfg: dict) -> str:
    """邮件里的绝对地址：平台对外根地址优先，其次企微卡片根，再从回调 URL 推。"""
    for key in ("public_app_base", "wecom_app_base"):
        base = (cfg.get(key) or "").strip().rstrip("/")
        if base:
            return base
    redir = (cfg.get("wecom_redirect_uri") or "").strip()
    if redir.startswith("http"):
        from urllib.parse import urlparse

        parsed = urlparse(redir)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def send_mail(to_addr: str, subject: str, body: str, link: str = "") -> bool:
    """按平台配置发一封 HTML 邮件，并附纯文本给不支持 HTML 的客户端。"""
    if not _real_email(to_addr):
        return False
    from app.db.session import SessionLocal
    from app.modules.notify.email_html import render_email_html
    from app.modules.settings import get_all_settings

    with SessionLocal() as db:
        cfg = get_all_settings(db)
    if cfg.get("smtp_enabled", "false") != "true":
        return False
    host = (cfg.get("smtp_host") or "").strip()
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    if not host or not user or not password:
        logger.warning("SMTP 未配齐 host/user/password，跳过发信")
        return False
    port = int(cfg.get("smtp_port") or "465")
    from_addr = (cfg.get("smtp_from") or user).strip()
    from app.modules.settings.branding import DEFAULT_FROM_NAME

    from_name = (cfg.get("smtp_from_name") or cfg.get("platform_display_name") or DEFAULT_FROM_NAME).strip() or DEFAULT_FROM_NAME
    use_ssl = cfg.get("smtp_ssl", "true") == "true" or port == 465
    origin = _public_origin(cfg)
    html_body = render_email_html(
        subject, body, brand=from_name, link=link, origin=origin
    )

    msg = MIMEMultipart("alternative")
    # 不写显示名的话，邮箱客户端会把账号部分显示成发件人
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as smtp:
                smtp.login(user, password)
                smtp.sendmail(from_addr, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, password)
                smtp.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info("已发邮件给 %s：%s", to_addr, subject)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("发邮件失败 to=%s：%s", to_addr, e)
        return False
