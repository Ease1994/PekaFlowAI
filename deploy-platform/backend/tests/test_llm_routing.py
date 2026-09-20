"""任务路由的回归：重试、fallback，以及每次尝试都要留下可观测记录。

模型挂了最难查的不是「调用失败」，而是「到底打到哪个厂商、试了几次、
是不是已经降级到备用模型了」。这些必须落库，不能只在日志里。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.credential.models import Credential  # noqa: F401  建表需要
from app.modules.llm.client import AdapterResponse, FailureKind, LlmAdapter, LlmFailure, Message
from app.modules.llm.models import (
    LlmAdapterConfig,
    LlmModel,
    LlmObservation,
    LlmProvider,
    LlmTaskRoute,
)
from app.modules.llm.service import adapter_registry, invoke_task, resolve_route

TABLES = ("credential", "llm_provider", "llm_model", "llm_adapter", "llm_task_route", "llm_observation")


class ScriptedAdapter(LlmAdapter):
    """按模型名读脚本：给什么就抛什么/返回什么。"""

    name = "scripted"
    script: dict[str, list[object]] = {}
    calls: list[str] = []

    def __init__(self, config) -> None:
        self.config = config

    def invoke(self, messages, *, tools=None, timeout_sec=None) -> AdapterResponse:
        type(self).calls.append(self.config.model)
        queue = type(self).script.get(self.config.model) or []
        outcome = queue.pop(0) if queue else AdapterResponse(
            message=Message(role="assistant", content="ok"), usage={"total_tokens": 3}
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, messages, *, tools=None, timeout_sec=None):
        raise NotImplementedError


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[table for name, table in Base.metadata.tables.items() if name in TABLES],
    )
    ScriptedAdapter.script = {}
    ScriptedAdapter.calls = []
    adapter_registry.register("scripted", ScriptedAdapter)
    with Session(engine) as session:
        adapter = LlmAdapterConfig(
            name="脚本", code="scripted", protocol="scripted", api_base_url="http://x/v1"
        )
        provider = LlmProvider(name="Fake", code="fake", api_base_url="http://x/v1", api_key="sk-x")
        session.add_all([adapter, provider])
        session.commit()
        primary = LlmModel(
            name="主力", model_id="primary", provider_id=provider.id,
            adapter_id=adapter.id, is_default=True,
        )
        backup = LlmModel(
            name="备用", model_id="backup", provider_id=provider.id, adapter_id=adapter.id
        )
        session.add_all([primary, backup])
        session.commit()
        session.add(
            LlmTaskRoute(
                task="assistant",
                model_id=primary.id,
                adapter_id=adapter.id,
                retries=1,
                fallback_model_ids_json=f"[{backup.id}]",
            )
        )
        session.commit()
        yield session


def _messages() -> list[Message]:
    return [Message(role="user", content="hi")]


def test_route_lists_primary_then_fallback(db: Session) -> None:
    candidates = resolve_route(db, "assistant")
    assert [c.model.model_id for c in candidates] == ["primary", "backup"]
    assert candidates[0].fallback_from_model_id is None
    assert candidates[1].fallback_from_model_id == candidates[0].model.id


def test_retryable_failure_retries_then_falls_back(db: Session) -> None:
    ScriptedAdapter.script = {
        "primary": [
            LlmFailure(FailureKind.RATE_LIMIT, "429", retryable=True),
            LlmFailure(FailureKind.RATE_LIMIT, "429", retryable=True),
        ]
    }

    _, meta = invoke_task(db, "assistant", _messages())

    assert ScriptedAdapter.calls == ["primary", "primary", "backup"]
    assert meta["model_id"] == "backup"
    assert meta["attempt"] == 1

    rows = db.scalars(select(LlmObservation).order_by(LlmObservation.id)).all()
    assert [(r.model_id, r.attempt, r.status) for r in rows] == [
        ("primary", 1, "error"),
        ("primary", 2, "error"),
        ("backup", 1, "success"),
    ]
    assert rows[0].error_kind == FailureKind.RATE_LIMIT.value
    assert rows[-1].fallback_from_model_id is not None


def test_non_retryable_failure_skips_retry_but_still_falls_back(db: Session) -> None:
    ScriptedAdapter.script = {
        "primary": [LlmFailure(FailureKind.AUTHENTICATION, "401", retryable=False)]
    }

    _, meta = invoke_task(db, "assistant", _messages())

    # 401 重试多少次都还是 401，别拿用户的等待时间去撞墙
    assert ScriptedAdapter.calls == ["primary", "backup"]
    assert meta["model_id"] == "backup"


def test_all_candidates_down_raises_last_failure(db: Session) -> None:
    ScriptedAdapter.script = {
        "primary": [LlmFailure(FailureKind.PROVIDER, "500", retryable=True)] * 2,
        "backup": [LlmFailure(FailureKind.TIMEOUT, "超时", retryable=True)] * 2,
    }

    with pytest.raises(LlmFailure) as caught:
        invoke_task(db, "assistant", _messages())
    assert caught.value.kind is FailureKind.TIMEOUT
    assert db.scalar(select(LlmObservation).where(LlmObservation.status == "success")) is None


def test_explicit_model_pin_disables_fallback(db: Session) -> None:
    primary = db.scalar(select(LlmModel).where(LlmModel.model_id == "primary"))
    ScriptedAdapter.script = {"primary": [LlmFailure(FailureKind.PROVIDER, "500", retryable=False)]}

    with pytest.raises(LlmFailure):
        invoke_task(db, "assistant", _messages(), model_pk=primary.id)
    assert ScriptedAdapter.calls == ["primary"]
