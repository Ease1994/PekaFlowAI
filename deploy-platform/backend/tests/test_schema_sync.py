# -*- coding: utf-8 -*-
"""按模型给旧表补列：默认值要跟模型一致，第二次跑不能再改。"""
from __future__ import annotations

from sqlalchemy import Column, String, Text, create_engine, inspect, text
from sqlalchemy.dialects.mysql.base import MySQLDialect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect

from app.db.schema_sync import _add_column_ddl, sync_model_columns


def test_mysql_text_column_has_no_default():
    """MySQL 给 TEXT 写 DEFAULT 会 1101，启动直接失败。"""
    col = Column("business_summary", Text, nullable=False, default="")
    ddl = _add_column_ddl(col, MySQLDialect())
    assert "DEFAULT" not in ddl.upper()
    assert "TEXT" in ddl.upper()
    assert ddl.upper().endswith("NULL")
    assert "NOT NULL" not in ddl.upper()


def test_mysql_varchar_keeps_empty_default():
    col = Column("iteration_tag", String(64), nullable=False, default="")
    ddl = _add_column_ddl(col, MySQLDialect())
    assert "DEFAULT ''" in ddl
    assert "NOT NULL" in ddl


def test_sqlite_text_may_have_default():
    col = Column("business_summary", Text, nullable=False, default="")
    ddl = _add_column_ddl(col, SQLiteDialect())
    assert "business_summary" in ddl


def test_sync_adds_approval_mode_inherit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old_pipeline.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE pipeline (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    workspace_uuid VARCHAR(64) DEFAULT ''
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO pipeline (id, project_id, group_id, name) VALUES (1, 1, 1, 'old')"
            )
        )

    added = sync_model_columns(engine)
    assert "pipeline.approval_mode" in added
    assert "pipeline.deleted_at" in added

    with engine.connect() as conn:
        mode = conn.execute(text("SELECT approval_mode FROM pipeline WHERE id = 1")).scalar()
    assert mode == "inherit"

    again = sync_model_columns(engine)
    assert again == []

    cols = {c["name"] for c in inspect(engine).get_columns("pipeline")}
    assert "approval_mode" in cols
    assert "deleted_at" in cols
