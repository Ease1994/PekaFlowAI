"""Agent 在线判定。

库里的 status 只是心跳巡检上次写下的快照：节点刚掉线时仍可能标着 online。
列表页、派任务、回收卡住的部署任务必须用同一套规则——status 是 online，
且最近一次心跳没有超过阈值。否则「发送文件到节点」会一直 pending，页面只转圈。
"""
from __future__ import annotations

from datetime import datetime

from app.modules.agent.models import BuildAgent

# Agent 约 10 秒一次心跳。60 秒足以覆盖短暂抖动，又不会让离线节点任务一直挂着。
HEARTBEAT_TIMEOUT_SECONDS = 60


def agent_is_online(agent: BuildAgent | None) -> bool:
    """这台机器现在是否真正在线，能领走任务。

    实现：角色对象缺失、库里不是 online、从未报过心跳、心跳已经超时，都算离线。
    不单独看 status 字段，避免巡检还没把超时机器写回 offline 时误派任务。
    """
    if agent is None:
        return False
    if (agent.status or "") != "online":
        return False
    hb = agent.last_heartbeat
    if hb is None:
        return False
    return (datetime.now() - hb).total_seconds() <= HEARTBEAT_TIMEOUT_SECONDS


def effective_status(agent: BuildAgent) -> str:
    """给列表和接口用的展示状态，与派发任务时的在线判定同一套规则。"""
    return "online" if agent_is_online(agent) else "offline"
