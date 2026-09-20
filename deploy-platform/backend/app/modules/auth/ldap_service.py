"""LDAP 认证 / 搜索（对齐 AD：可用邮箱、UPN 或 sAMAccountName 登录）。"""
from __future__ import annotations

import ldap3
from ldap3 import ALL, SUBTREE, Connection, Server

FALLBACK_FILTER = "(|(mail={username})(userPrincipalName={username})(sAMAccountName={sam}))"


def _escape(value: str) -> str:
    return (
        (value or "")
        .replace("\\", r"\5c")
        .replace("*", r"\2a")
        .replace("(", r"\28")
        .replace(")", r"\29")
        .replace("\x00", r"\00")
    )


def _render_filter(template: str, username: str) -> str:
    raw = (username or "").strip()
    sam = raw.split("@", 1)[0] if "@" in raw else raw
    return (template or FALLBACK_FILTER).format(username=_escape(raw), sam=_escape(sam))


def _connect(cfg: dict) -> Connection:
    server_uri = (cfg.get("ldap_server_uri") or "").strip()
    if not server_uri:
        raise RuntimeError("未配置 LDAP 服务器地址")
    bind_dn = (cfg.get("ldap_bind_dn") or "").strip()
    bind_pw = cfg.get("ldap_bind_password") or ""
    server = Server(server_uri, get_info=ALL, connect_timeout=5)
    return Connection(server, user=bind_dn, password=bind_pw, auto_bind=True, receive_timeout=8)


def _attrs(cfg: dict) -> list[str]:
    return [
        cfg.get("ldap_attr_username") or "sAMAccountName",
        cfg.get("ldap_attr_email") or "mail",
        cfg.get("ldap_attr_display_name") or "displayName",
        "userPrincipalName",
        "distinguishedName",
    ]


def _entry_public(entry, cfg: dict) -> dict:
    attr_username = cfg.get("ldap_attr_username") or "sAMAccountName"
    attr_email = cfg.get("ldap_attr_email") or "mail"
    attr_display_name = cfg.get("ldap_attr_display_name") or "displayName"
    username_val = _first(entry, attr_username)
    email_val = _first(entry, attr_email) or _first(entry, "userPrincipalName")
    display_val = _first(entry, attr_display_name)
    return {
        "dn": entry.entry_dn,
        "username": username_val or (email_val.split("@")[0] if email_val else ""),
        "email": email_val,
        "display_name": display_val or username_val or email_val,
        "upn": _first(entry, "userPrincipalName"),
    }


def ldap_search(cfg: dict, username: str) -> dict | None:
    """按登录名搜索一条用户（配置过滤器失败时自动用 mail/UPN/sAMAccountName 兜底）。"""
    search_base = (cfg.get("ldap_user_search_base") or "").strip()
    if not search_base:
        raise RuntimeError("未配置 LDAP 用户搜索基准 DN")
    conn = _connect(cfg)
    try:
        filters = [_render_filter(cfg.get("ldap_user_search_filter") or FALLBACK_FILTER, username)]
        fallback = _render_filter(FALLBACK_FILTER, username)
        if fallback not in filters:
            filters.append(fallback)
        for flt in filters:
            conn.search(search_base, flt, SUBTREE, attributes=_attrs(cfg))
            if conn.entries:
                return _entry_public(conn.entries[0], cfg)
        return None
    finally:
        conn.unbind()


def ldap_search_users(cfg: dict, keyword: str, limit: int = 30) -> list[dict]:
    """管理端按关键字搜 AD 用户，供导入。"""
    kw = _escape((keyword or "").strip())
    if len(kw) < 2:
        return []
    search_base = (cfg.get("ldap_user_search_base") or "").strip()
    conn = _connect(cfg)
    try:
        flt = f"(&(objectClass=user)(|(mail=*{kw}*)(sAMAccountName=*{kw}*)(displayName=*{kw}*)(userPrincipalName=*{kw}*)))"
        try:
            conn.search(search_base, flt, SUBTREE, attributes=_attrs(cfg), size_limit=limit)
        except Exception:
            pass
        return [_entry_public(e, cfg) for e in (conn.entries or [])[:limit]]
    finally:
        conn.unbind()


def ldap_test(cfg: dict, username: str = "") -> dict:
    """测绑定账号连通；若给了 username 再搜该人（不校验登录密码）。"""
    conn = _connect(cfg)
    conn.unbind()
    found = None
    if username.strip():
        found = ldap_search(cfg, username.strip())
    return {"ok": True, "bind": True, "user": found, "user_found": found is not None}


def ldap_authenticate(username: str, password: str, cfg: dict) -> dict | None:
    """LDAP 认证：搜到用户后用其 DN 二次 bind 验密。失败返回 None。"""
    if not password:
        return None
    entry = ldap_search(cfg, username)
    if entry is None:
        return None
    server = Server((cfg.get("ldap_server_uri") or "").strip(), get_info=ALL, connect_timeout=5)
    try:
        user_conn = Connection(
            server, user=entry["dn"], password=password, auto_bind=True, receive_timeout=5,
        )
        user_conn.unbind()
    except ldap3.core.exceptions.LDAPInvalidCredentialsResult:
        return None
    except Exception:
        return None
    return {
        "username": entry["username"] or username,
        "email": entry["email"] or username,
        "display_name": entry["display_name"] or username,
        "dn": entry["dn"],
    }


def _first(entry, attr: str) -> str:
    try:
        v = entry[attr].value
        if isinstance(v, (list, tuple)):
            return str(v[0]) if v else ""
        return "" if v is None else str(v)
    except Exception:
        return ""
