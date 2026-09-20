"""LLM configuration, task routing and invocation observability models."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class LlmProvider(Base, TimestampMixin):
    __tablename__ = "llm_provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    api_base_url: Mapped[str] = mapped_column(String(512), default="")
    credential_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("credential.id"), nullable=True, index=True
    )
    adapter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("llm_adapter.id"), nullable=True, index=True
    )
    # Compatibility only. New writes go to Credential; migration clears this.
    api_key: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    website: Mapped[str] = mapped_column(String(256), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    models: Mapped[list["LlmModel"]] = relationship("LlmModel", back_populates="provider")


class LlmModel(Base, TimestampMixin):
    __tablename__ = "llm_model"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_id: Mapped[int] = mapped_column(Integer, ForeignKey("llm_provider.id"), nullable=False, index=True)
    credential_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("credential.id"), nullable=True, index=True
    )
    adapter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("llm_adapter.id"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(Text, default="")
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    context_window: Mapped[int] = mapped_column(Integer, default=0)
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    # JSON 数组，目前只认 "vision"。空串表示没人工标过，由 model_id 推断；
    # 写成 "[]" 才是明确声明「不支持」，两者要分得开
    capabilities: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    provider: Mapped[LlmProvider] = relationship("LlmProvider", back_populates="models")


class LlmAdapterConfig(Base, TimestampMixin):
    __tablename__ = "llm_adapter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    protocol: Mapped[str] = mapped_column(String(64), default="openai-compatible")
    api_base_url: Mapped[str] = mapped_column(String(512), default="")
    credential_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("credential.id"), nullable=True, index=True
    )
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def settings(self) -> dict[str, Any]:
        try:
            value = json.loads(self.settings_json or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


class LlmTaskRoute(Base, TimestampMixin):
    __tablename__ = "llm_task_route"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    model_id: Mapped[int] = mapped_column(Integer, ForeignKey("llm_model.id"), nullable=False)
    adapter_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_adapter.id"))
    timeout_sec: Mapped[int] = mapped_column(Integer, default=90)
    retries: Mapped[int] = mapped_column(Integer, default=1)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fallback_model_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def fallback_model_ids(self) -> list[int]:
        try:
            values = json.loads(self.fallback_model_ids_json or "[]")
            return [int(value) for value in values] if isinstance(values, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []


class LlmObservation(Base, TimestampMixin):
    __tablename__ = "llm_observation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(32), default="", index=True)
    provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider_code: Mapped[str] = mapped_column(String(64), default="")
    model_pk: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    model_id: Mapped[str] = mapped_column(String(128), default="")
    adapter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adapter_code: Mapped[str] = mapped_column(String(64), default="")
    request_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    fallback_from_model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="success", index=True)
    error_kind: Mapped[str] = mapped_column(String(48), default="")
    error_code: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    usage_source: Mapped[str] = mapped_column(String(16), default="provider")


_PATCH_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "llm_provider": {
        "credential_id": ("BIGINT NULL", "INTEGER"),
        "adapter_id": ("BIGINT NULL", "INTEGER"),
    },
    "llm_model": {
        "credential_id": ("BIGINT NULL", "INTEGER"),
        "adapter_id": ("BIGINT NULL", "INTEGER"),
        "context_window": ("INT NOT NULL DEFAULT 0", "INTEGER DEFAULT 0"),
        "capabilities": ("TEXT NULL", "TEXT DEFAULT ''"),
    },
}


def ensure_llm_schema(engine: Engine, *, migrate_legacy_keys: bool = True) -> int:
    """Create new tables, safely patch old tables, then optionally migrate keys.

    This is the single startup hook intended to be called after the application's
    generic ``Base.metadata.create_all`` / schema patches.
    """

    for table in (LlmAdapterConfig.__table__, LlmTaskRoute.__table__, LlmObservation.__table__):
        table.create(bind=engine, checkfirst=True)
    dialect = engine.dialect.name
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table_name, columns in _PATCH_COLUMNS.items():
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for name, definitions in columns.items():
                if name in existing:
                    continue
                ddl = definitions[0] if dialect == "mysql" else definitions[1]
                quoted_table = f"`{table_name}`" if dialect == "mysql" else table_name
                connection.execute(text(f"ALTER TABLE {quoted_table} ADD COLUMN {name} {ddl}"))
    return migrate_legacy_api_keys(engine) if migrate_legacy_keys else 0


def migrate_legacy_api_keys(engine: Engine) -> int:
    """Move legacy plaintext provider keys into encrypted global Credentials."""

    from sqlalchemy.orm import Session

    from app.core.security import encrypt
    from app.modules.credential.models import Credential

    migrated = 0
    with Session(engine) as session:
        providers = session.query(LlmProvider).filter(
            LlmProvider.credential_id.is_(None),
            LlmProvider.api_key.is_not(None),
            LlmProvider.api_key != "",
        ).all()
        for provider in providers:
            plain = (provider.api_key or "").strip()
            if not plain:
                continue
            ciphertext, iv = encrypt(plain)
            credential = Credential(
                project_id=None,
                name=f"LLM/{provider.name}",
                type="api_key",
                ciphertext=ciphertext,
                iv=iv,
                description=f"由 LLM 厂商 {provider.code} 的旧 API Key 安全迁移",
                created_by=None,
            )
            session.add(credential)
            session.flush()
            provider.credential_id = credential.id
            provider.api_key = ""
            migrated += 1
        session.commit()
    return migrated
