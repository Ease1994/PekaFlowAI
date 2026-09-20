"""对话框只能挑模型管理里真正能跑的模型。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.credential.models import Credential  # noqa: F401
from app.modules.llm.models import LlmModel, LlmProvider
from app.modules.llm.service import list_usable_models, usable_model_id

TABLES = ("credential", "llm_provider", "llm_adapter", "llm_model")


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[table for name, table in Base.metadata.tables.items() if name in TABLES],
    )
    return Session(engine)


def test_only_active_ready_models_appear() -> None:
    db = _db()
    ready = LlmProvider(name="可用厂商", code="ok", api_base_url="http://x/v1", api_key="sk-x", is_active=True)
    dead = LlmProvider(name="没钥匙", code="dead", api_base_url="http://x/v1", api_key="", is_active=True)
    db.add_all([ready, dead])
    db.commit()
    keep = LlmModel(name="主力", model_id="keep", provider_id=ready.id, is_active=True, is_default=True)
    off = LlmModel(name="停用", model_id="off", provider_id=ready.id, is_active=False)
    broken = LlmModel(name="缺钥", model_id="broken", provider_id=dead.id, is_active=True)
    db.add_all([keep, off, broken])
    db.commit()

    items = list_usable_models(db)
    assert [m["name"] for m in items] == ["主力"]
    assert usable_model_id(db, keep.id) == keep.id
    assert usable_model_id(db, off.id) is None
    assert usable_model_id(db, broken.id) is None
    db.close()


def test_vision_flag_follows_manual_then_name() -> None:
    db = _db()
    p = LlmProvider(name="P", code="p", api_base_url="http://x/v1", api_key="sk-x")
    db.add(p)
    db.commit()
    guessed = LlmModel(name="GPT-4o", model_id="gpt-4o", provider_id=p.id, capabilities="")
    forced_off = LlmModel(name="看起来像视觉", model_id="gpt-4o-mini", provider_id=p.id, capabilities="[]")
    forced_on = LlmModel(name="纯文本名", model_id="plain-chat", provider_id=p.id, capabilities='["vision"]')
    db.add_all([guessed, forced_off, forced_on])
    db.commit()

    by_name = {m["name"]: m for m in list_usable_models(db)}
    assert by_name["GPT-4o"]["supports_vision"] is True
    assert by_name["看起来像视觉"]["supports_vision"] is False
    assert by_name["纯文本名"]["supports_vision"] is True
    db.close()
