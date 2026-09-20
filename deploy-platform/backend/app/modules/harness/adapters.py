"""各包类型的生命周期适配器。

内置 Tool 只有启停，没有物理卸载：它是平台自己的代码，删掉就等于删功能。
第三方 Agent Tool 启用前必须确认隔离 Runner 可用——如果启用时不拦，模型第一次
调用它才会失败，而那时用户看到的只是一句「工具报错」。
"""
from __future__ import annotations

from app.modules.harness import runner
from app.modules.harness.context import ScopeContext
from app.modules.harness.packages import Manifest
from app.modules.harness.services import EffectStack


class SkillAdapter:
    """Skill 只是指令文本，安装即可用，唯一的运行时约束是启停。"""

    def install(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None:
        if not (manifest.metadata or {}).get("body"):
            raise ValueError("Skill 缺少正文")

    def enable(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None:
        return None

    def disable(self, manifest: Manifest, scope: ScopeContext) -> None:
        return None

    def uninstall(self, manifest: Manifest, scope: ScopeContext) -> None:
        return None

    def health(self, manifest: Manifest, scope: ScopeContext) -> tuple[str, str]:
        body = (manifest.metadata or {}).get("body") or ""
        return ("healthy", f"指令 {len(body)} 字符") if body else ("unhealthy", "Skill 正文缺失")


class IsolatedToolAdapter:
    """第三方工具：启用与健康检查都以隔离 Runner 为前提。"""

    def __init__(self, session_factory=None):
        from app.db.session import SessionLocal

        self._session_factory = session_factory or SessionLocal

    def _require_runner(self) -> dict:
        with self._session_factory() as db:
            return runner.health(db)

    def install(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None:
        if not (manifest.metadata or {}).get("signature_key_id"):
            raise ValueError("第三方工具必须通过签名校验后才能安装")

    def enable(self, manifest: Manifest, scope: ScopeContext, effects: EffectStack) -> None:
        self._require_runner()

    def disable(self, manifest: Manifest, scope: ScopeContext) -> None:
        return None

    def uninstall(self, manifest: Manifest, scope: ScopeContext) -> None:
        return None

    def health(self, manifest: Manifest, scope: ScopeContext) -> tuple[str, str]:
        try:
            self._require_runner()
        except runner.RunnerUnavailable as exc:
            return "unhealthy", str(exc)
        return "healthy", "隔离 Runner 就绪"


def register_all() -> None:
    """幂等注册；lifecycle.initialize_builtin_providers 已经占了 Noop 位就先让开。"""
    from app.modules.harness import lifecycle

    for kind, adapter in (
        ("agent-skill", SkillAdapter()),
        ("agent-tool", IsolatedToolAdapter()),
    ):
        existing = lifecycle.adapter_for(kind)
        if existing is None or isinstance(existing, lifecycle.NoopAdapter):
            lifecycle.replace_adapter(kind, adapter)
