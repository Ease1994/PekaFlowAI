"""企业微信 OAuth2 登录服务（参考 AI 陪练项目 wecom_auth.py）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy.orm import Session

from app.modules.auth.models import User

QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"
OAUTH_AUTHORIZE = "https://open.weixin.qq.com/connect/oauth2/authorize"


def _get_json(url: str, timeout: int = 30) -> dict:
    try:
        r = httpx.get(url, timeout=timeout)
        data = r.json()
        return data if isinstance(data, dict) else {"errcode": -1, "errmsg": str(data)}
    except Exception as e:  # noqa: BLE001
        return {"errcode": -1, "errmsg": f"请求企微 API 失败: {e}"}


def wecom_ready(cfg: dict) -> bool:
    return bool(
        cfg.get("wecom_enabled") == "true"
        and cfg.get("wecom_corp_id")
        and cfg.get("wecom_secret")
        and str(cfg.get("wecom_agent_id", "")).strip()
        and cfg.get("wecom_redirect_uri")
    )


def _get_access_token(cfg: dict) -> str:
    q = urlencode({"corpid": cfg["wecom_corp_id"], "corpsecret": cfg["wecom_secret"]})
    data = _get_json(f"{QYAPI}/gettoken?{q}")
    if data.get("errcode"):
        raise RuntimeError(f"企微 gettoken 失败: {data.get('errmsg', data)}")
    return str(data["access_token"])


def _auth_get_userinfo(access_token: str, code: str) -> dict:
    q = urlencode({"access_token": access_token, "code": code})
    return _get_json(f"{QYAPI}/auth/getuserinfo?{q}")


def _auth_get_userdetail(access_token: str, user_ticket: str) -> dict:
    url = f"{QYAPI}/auth/getuserdetail?access_token={access_token}"
    try:
        r = httpx.post(url, json={"user_ticket": user_ticket}, timeout=30)
        data = r.json()
        return data if isinstance(data, dict) else {"errcode": -1}
    except Exception:  # noqa: BLE001
        return {"errcode": -1}


def _user_get(access_token: str, userid: str) -> dict:
    q = urlencode({"access_token": access_token, "userid": userid})
    return _get_json(f"{QYAPI}/user/get?{q}")


def make_state(secret_key: str) -> str:
    """生成防 CSRF 的 state（带时间戳 + HMAC 签名）。"""
    ts = str(int(time.time()))
    sig = hmac.new(
        (secret_key + ":wecom").encode(), ts.encode(), hashlib.sha256
    ).hexdigest()[:20]
    raw = f"{ts}.{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_state(state: str | None, secret_key: str) -> bool:
    if not state:
        return False
    try:
        pad = "=" * (-len(state) % 4)
        raw = base64.urlsafe_b64decode(state + pad).decode()
        ts, sig = raw.split(".", 1)
        if abs(int(time.time()) - int(ts)) > 600:
            return False
        expect = hmac.new(
            (secret_key + ":wecom").encode(), ts.encode(), hashlib.sha256
        ).hexdigest()[:20]
        return hmac.compare_digest(expect, sig)
    except Exception:  # noqa: BLE001
        return False


def build_authorize_url(cfg: dict, state: str, scope: str) -> str:
    redirect_uri = quote(cfg["wecom_redirect_uri"], safe="")
    agent = str(cfg.get("wecom_agent_id", "")).strip()
    qs = (
        f"appid={quote(cfg['wecom_corp_id'])}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&state={quote(state, safe='')}"
        f"&agentid={quote(agent)}"
    )
    return f"{OAUTH_AUTHORIZE}?{qs}#wechat_redirect"


def login_user_via_wecom_code(db: Session, code: str, cfg: dict) -> User:
    """用 OAuth code 换企微身份，绑定到唯一本地用户（可与 LDAP 是同一人）。"""
    from datetime import datetime

    from app.modules.auth.identity import lookup_ldap_hints, resolve_or_provision

    access_token = _get_access_token(cfg)
    info = _auth_get_userinfo(access_token, code)
    if info.get("errcode"):
        raise RuntimeError(f"获取用户身份失败: {info.get('errmsg', info)}")

    userid = (info.get("UserId") or info.get("userid") or "").strip()
    if not userid:
        raise RuntimeError("未返回 userid")

    emails: list[str] = []
    user_ticket = info.get("user_ticket")
    if user_ticket:
        detail = _auth_get_userdetail(access_token, user_ticket)
        for key in ("email", "biz_mail"):
            val = (detail.get(key) or "").strip()
            if val:
                emails.append(val)

    display_name = ""
    profile = _user_get(access_token, userid)
    if profile and not profile.get("errcode"):
        display_name = str(profile.get("name") or profile.get("alias") or "").strip()
        for key in ("email", "biz_mail"):
            val = str(profile.get(key) or "").strip()
            if val:
                emails.append(val)

    ldap_entry = lookup_ldap_hints(cfg, [userid, *emails])
    usernames = [userid]
    if ldap_entry:
        if ldap_entry.get("username"):
            usernames.insert(0, ldap_entry["username"])
        if ldap_entry.get("email"):
            emails.append(ldap_entry["email"])
        if ldap_entry.get("upn"):
            emails.append(ldap_entry["upn"])
            usernames.append(ldap_entry["upn"])
        if ldap_entry.get("display_name") and not display_name:
            display_name = ldap_entry["display_name"]

    user = resolve_or_provision(
        db,
        wecom_userid=userid,
        usernames=usernames,
        emails=emails,
        display_name=display_name or userid,
        source="ldap" if ldap_entry else "wecom",
        auto_provision=cfg.get("wecom_auto_provision", "true") == "true",
    )
    if user is None:
        raise RuntimeError("系统中无该企微账号绑定，且未开启自动建档")
    if (user.status or "active") != "active":
        raise RuntimeError("账号已禁用，请联系管理员")
    user.last_login_at = datetime.now()
    db.commit()
    db.refresh(user)
    return user
