"""Agent 长期凭证：库里只存哈希，明文只在签发时返回给那台机器。

明文 token 是 32 字节随机的 hex（64 位），sha256 hex 也是 64 位，不能靠长度区分。
因此入库时加前缀 s256:。启动时把没有前缀的旧明文就地哈希，已登记的机器磁盘上
仍是原来那份明文，不必重装。
轮换宽限期内要把「刚发出去、Agent 可能还没落到盘上」的新明文暂存在 token_issued，
等心跳带着新 token 回来再清掉——只存哈希的话补发不了。
"""
from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.agent.models import BuildAgent

# 已哈希凭证的前缀。后面跟 sha256 hex（64 位），整段约 69 字符，落在 String(128) 内。
HASH_PREFIX = "s256:"


def hash_agent_token(plain: str) -> str:
    """把明文 token 收成入库形态。空串保持空，避免把「还没发过凭证」哈希进去。"""
    if not plain:
        return ""
    digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return HASH_PREFIX + digest


def is_stored_hash(stored: str) -> bool:
    """库里这一格是不是已经是哈希。启动迁移靠它决定要不要再哈希一次。"""
    return (stored or "").startswith(HASH_PREFIX)


def token_matches(stored: str, presented: str) -> bool:
    """比对请求里的明文和库里的值。

    已哈希：对请求做同样哈希再 compare_digest。
    尚未迁移的明文：直接比，兼容滚动升级那几秒。
    """
    if not stored or not presented:
        return False
    if is_stored_hash(stored):
        return hmac.compare_digest(stored, hash_agent_token(presented))
    return hmac.compare_digest(stored, presented)


def find_agent_by_presented_token(db: Session, presented: str) -> BuildAgent | None:
    """用请求里的明文找到已登记的 Agent。

    库里存的是哈希，不能 SQL 等值查询。机器数量就几十台上百台，
    自升级拉 jar、构建机拉插件都走这条，扫一遍可以接受。
    轮换宽限期内旧凭证还在 token_prev，两边都认。
    """
    if not presented:
        return None
    for agent in db.scalars(select(BuildAgent)).all():
        if token_matches(agent.token, presented) or token_matches(agent.token_prev, presented):
            return agent
    return None


def migrate_plaintext_tokens(db: Session) -> int:
    """把旧明文 token / token_prev 就地收成哈希。返回改了多少台机器。

    已是 s256: 的跳过。Agent 磁盘上的明文不变，下次心跳仍能过。
    """
    changed = 0
    agents = db.scalars(select(BuildAgent)).all()
    for agent in agents:
        dirty = False
        if agent.token and not is_stored_hash(agent.token):
            agent.token = hash_agent_token(agent.token)
            dirty = True
        if agent.token_prev and not is_stored_hash(agent.token_prev):
            agent.token_prev = hash_agent_token(agent.token_prev)
            dirty = True
        if dirty:
            changed += 1
    if changed:
        db.commit()
    return changed
