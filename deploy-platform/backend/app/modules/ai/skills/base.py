"""技能约定：高风险只返回确认卡片，不直接改生产。"""
from __future__ import annotations


def card(action: str, label: str, payload: dict, reply: str) -> dict:
    return {"_action": action, "label": label, "payload": payload, "reply": reply}
