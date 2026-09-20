"""加载全部内置 Tool。这里的 Python 是可执行工具，不是 SKILL.md。

说明书在 playbooks/*.md（以及用户上传的技能包）。Agent 先读匹配的 md，再调这里的 handler。
工具 description 写做什么、何时调用、和易混工具差在哪；前 140 字进模型 schema。
"""
from __future__ import annotations

from . import access, authoring, catalog, delivery, inbox, observe, platform, pm, skill_authoring, skill_lifecycle, skill_load

_LOADED = False


def load_all() -> None:
    global _LOADED
    if _LOADED:
        return
    skill_load.load()
    catalog.load()
    delivery.load()
    observe.load()
    inbox.load()
    platform.load()
    pm.load()
    access.load()
    authoring.load()
    skill_authoring.load()
    skill_lifecycle.load()
    _LOADED = True
