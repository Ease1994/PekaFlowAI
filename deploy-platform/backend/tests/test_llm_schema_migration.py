"""老库升级到适配器版 LLM 表结构的回归。

生产库是从重构前的版本长出来的：llm_provider 上没有 credential_id/adapter_id，
API Key 还是明文躺在表里。启动补丁必须把列补上、把明文换成加密凭证引用，
而且第二次启动不能再动一次——否则每重启一次就多造一条凭证。
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.core.security import decrypt
from app.modules.credential.models import Credential
from app.modules.llm.models import LlmProvider, ensure_llm_schema

LEGACY_PROVIDER = """
CREATE TABLE llm_provider (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    code VARCHAR(50) NOT NULL,
    api_base_url VARCHAR(512) DEFAULT '',
    api_key TEXT DEFAULT '',
    description TEXT DEFAULT '',
    website VARCHAR(256) DEFAULT '',
    is_active BOOLEAN DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at DATETIME,
    updated_at DATETIME
)
"""

LEGACY_MODEL = """
CREATE TABLE llm_model (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    provider_id INTEGER NOT NULL,
    description TEXT DEFAULT '',
    max_tokens INTEGER DEFAULT 4096,
    temperature FLOAT DEFAULT 0.2,
    is_active BOOLEAN DEFAULT 1,
    is_default BOOLEAN DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at DATETIME,
    updated_at DATETIME
)
"""


def _legacy_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Credential.__table__.create(bind=engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(LEGACY_PROVIDER))
        conn.execute(text(LEGACY_MODEL))
        conn.execute(
            text(
                "INSERT INTO llm_provider (name, code, api_base_url, api_key) "
                "VALUES ('OpenAI', 'openai', 'https://api.openai.com/v1', 'sk-legacy-plain')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO llm_model (name, model_id, provider_id) "
                "VALUES ('GPT-4o', 'gpt-4o', 1)"
            )
        )
    return engine


def test_legacy_db_gets_columns_new_tables_and_encrypted_key(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)

    assert ensure_llm_schema(engine) == 1

    inspector = inspect(engine)
    assert {"credential_id", "adapter_id"} <= {c["name"] for c in inspector.get_columns("llm_provider")}
    assert {"credential_id", "adapter_id", "context_window"} <= {
        c["name"] for c in inspector.get_columns("llm_model")
    }
    assert {"llm_adapter", "llm_task_route", "llm_observation"} <= set(inspector.get_table_names())

    with engine.begin() as conn:
        provider = conn.execute(
            text("SELECT api_key, credential_id FROM llm_provider WHERE code='openai'")
        ).one()
        assert provider.api_key == ""
        cipher = conn.execute(
            text("SELECT ciphertext, iv FROM credential WHERE id=:cid"),
            {"cid": provider.credential_id},
        ).one()
    assert decrypt(cipher.ciphertext, cipher.iv) == "sk-legacy-plain"


def test_schema_patch_is_idempotent(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    ensure_llm_schema(engine)

    # 第二次启动：没有明文可迁，也不该重复补列或重复造凭证
    assert ensure_llm_schema(engine) == 0
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM credential")).scalar() == 1


def test_provider_model_maps_to_patched_columns(tmp_path) -> None:
    """补完列之后 ORM 必须能直接查——生产事故就出在补列漏了一个字段上。"""
    engine = _legacy_engine(tmp_path)
    ensure_llm_schema(engine)

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        provider = session.query(LlmProvider).filter_by(code="openai").one()
        assert provider.credential_id is not None
        assert provider.adapter_id is None
