"""可扩展 Harness 内核与统一生命周期公共 API。"""

from app.modules.harness.context import (
    AccessDecision,
    AccessRequest,
    ScopeContext,
    bind_scope,
)
from app.modules.harness.events import EventBus
from app.modules.harness.kernel import HarnessKernel, kernel
from app.modules.harness.packages import Manifest, ManifestDependency, resolve_dependency_dag
from app.modules.harness.services import (
    EffectStack,
    ServiceConsumer,
    ServiceDefinition,
    ServiceKey,
    ServiceProvider,
)

__all__ = [
    "AccessDecision",
    "AccessRequest",
    "EffectStack",
    "EventBus",
    "HarnessKernel",
    "Manifest",
    "ManifestDependency",
    "ScopeContext",
    "ServiceConsumer",
    "ServiceDefinition",
    "ServiceKey",
    "ServiceProvider",
    "bind_scope",
    "kernel",
    "resolve_dependency_dag",
]
