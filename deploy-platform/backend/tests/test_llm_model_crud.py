"""模型目录的改名、删除，以及默认/路由占用时的拦截。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.credential.models import Credential  # noqa: F401 建表需要
from app.modules.llm.models import LlmAdapterConfig, LlmModel, LlmProvider, LlmTaskRoute
from app.modules.llm.service import create_model, delete_model, update_model

TABLES = ("credential", "llm_provider", "llm_model", "llm_adapter", "llm_task_route", "llm_observation")


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[table for name, table in Base.metadata.tables.items() if name in TABLES],
    )
    with Session(engine) as session:
        adapter = LlmAdapterConfig(
            name="脚本", code="scripted", protocol="scripted", api_base_url="http://x/v1"
        )
        provider = LlmProvider(name="Fake", code="fake", api_base_url="http://x/v1", api_key="sk-x")
        session.add_all([adapter, provider])
        session.commit()
        primary = LlmModel(
            name="主力",
            model_id="primary",
            provider_id=provider.id,
            adapter_id=adapter.id,
            is_default=True,
        )
        backup = LlmModel(
            name="备用", model_id="backup", provider_id=provider.id, adapter_id=adapter.id
        )
        spare = LlmModel(
            name="闲置", model_id="spare", provider_id=provider.id, adapter_id=adapter.id
        )
        session.add_all([primary, backup, spare])
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


def test_update_model_renames(db: Session) -> None:
    spare = db.scalar(select(LlmModel).where(LlmModel.model_id == "spare"))
    assert spare is not None
    updated = update_model(
        db,
        spare.id,
        {"name": "闲置改名", "model_id": "spare-v2", "max_tokens": 8192},
    )
    assert updated["name"] == "闲置改名"
    assert updated["model_id"] == "spare-v2"
    assert updated["max_tokens"] == 8192
    db.refresh(spare)
    assert spare.name == "闲置改名"
    assert spare.model_id == "spare-v2"


def test_delete_unused_model(db: Session) -> None:
    spare = db.scalar(select(LlmModel).where(LlmModel.model_id == "spare"))
    assert spare is not None
    delete_model(db, spare.id)
    assert db.get(LlmModel, spare.id) is None


def test_delete_default_rejected(db: Session) -> None:
    primary = db.scalar(select(LlmModel).where(LlmModel.model_id == "primary"))
    assert primary is not None
    with pytest.raises(BizException) as caught:
        delete_model(db, primary.id)
    assert caught.value.code == 400
    assert "默认" in caught.value.message
    assert db.get(LlmModel, primary.id) is not None


def test_delete_route_primary_rejected(db: Session) -> None:
    backup = db.scalar(select(LlmModel).where(LlmModel.model_id == "backup"))
    spare = db.scalar(select(LlmModel).where(LlmModel.model_id == "spare"))
    assert backup is not None and spare is not None
    db.add(
        LlmTaskRoute(
            task="diagnose",
            model_id=spare.id,
            retries=0,
            fallback_model_ids_json="[]",
        )
    )
    db.commit()
    with pytest.raises(BizException) as caught:
        delete_model(db, spare.id)
    assert caught.value.code == 400
    assert "diagnose" in caught.value.message
    assert db.get(LlmModel, spare.id) is not None


def test_delete_strips_fallback_then_removes(db: Session) -> None:
    backup = db.scalar(select(LlmModel).where(LlmModel.model_id == "backup"))
    assert backup is not None
    delete_model(db, backup.id)
    assert db.get(LlmModel, backup.id) is None
    route = db.scalar(select(LlmTaskRoute).where(LlmTaskRoute.task == "assistant"))
    assert route is not None
    assert route.fallback_model_ids() == []


def test_create_then_delete_via_catalog(db: Session) -> None:
    provider = db.scalar(select(LlmProvider))
    assert provider is not None
    created = create_model(
        db,
        {
            "provider_id": provider.id,
            "name": "临时",
            "model_id": "tmp-chat",
            "max_tokens": 2048,
        },
    )
    delete_model(db, created["id"])
    assert db.get(LlmModel, created["id"]) is None
