"""进程内 Harness 内核：服务 seam + waterfall 事件 + allow/deny 策略。

持久化的安装/启停/卸载在 lifecycle.py，本文件只管进程内注册表。
"""
from __future__ import annotations

from app.modules.harness.context import PolicyRegistry
from app.modules.harness.events import EventBus
from app.modules.harness.services import ServiceRegistry


class HarnessKernel(ServiceRegistry, EventBus, PolicyRegistry):
    def __init__(self) -> None:
        ServiceRegistry.__init__(self)
        EventBus.__init__(self)
        PolicyRegistry.__init__(self)


kernel = HarnessKernel()
