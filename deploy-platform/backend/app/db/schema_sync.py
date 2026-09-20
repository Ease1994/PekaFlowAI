"""按 ORM 模型给已有表补列、补普通索引。

create_all 只建新表，不会 ALTER。模型上新加的字段，启动时从 metadata 对一遍库，
缺什么补什么。补齐之后启动补丁和业务代码才能安全走 ORM。

只加列、加非唯一索引，不改类型、不删列、不加唯一约束（存量脏数据会把启动卡死）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Boolean, Integer, String, Text, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import ClauseElement

from app.db.base import Base

logger = logging.getLogger(__name__)


def sync_model_columns(engine: Engine) -> list[str]:
    """对照 Base.metadata，给已有表补上模型里有、库里没有的列和普通索引。"""
    import app.db.models  # noqa: F401  确保全部模型已注册

    dialect = engine.dialect
    prep = dialect.identifier_preparer
    added: list[str] = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        quoted_table = prep.quote(table.name)
        for column in table.columns:
            if column.name in existing_cols:
                continue
            ddl = _add_column_ddl(column, dialect)
            # 一列一次提交：某一列失败时，已经加上的列不会被回滚。
            with engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE {quoted_table} ADD COLUMN {ddl}")
            added.append(f"{table.name}.{column.name}")
            existing_cols.add(column.name)
        if added:
            insp.clear_cache()
        existing_ix = {
            ix["name"] for ix in insp.get_indexes(table.name) if ix.get("name")
        }
        for ix in table.indexes:
            if not ix.name or ix.unique or ix.name in existing_ix:
                continue
            cols = ", ".join(prep.quote(c.name) for c in ix.columns)
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f"CREATE INDEX {prep.quote(ix.name)} ON {quoted_table} ({cols})"
                )
            existing_ix.add(ix.name)

    if added:
        logger.info("已按模型补列 %s", "、".join(added))
    return added


def _add_column_ddl(column, dialect) -> str:
    """拼一段 ADD COLUMN 后面的定义：名字、类型、NULL、DEFAULT。"""
    type_sql = str(column.type.compile(dialect=dialect))
    nullable = bool(column.nullable)
    default_sql = _column_default_sql(column, dialect)
    # SQLite 给已有表加列时，DEFAULT 必须是常量；CURRENT_TIMESTAMP 会直接报错。
    if dialect.name == "sqlite" and default_sql and _is_nonconstant_default(default_sql):
        default_sql = None
    # MySQL：TEXT/BLOB/JSON 不能带 DEFAULT（1101），有行时 NOT NULL 也加不上。
    if _mysql_forbids_column_default(type_sql, dialect):
        default_sql = None
        nullable = True
    if not nullable and default_sql is None:
        default_sql = _inferred_default(column, dialect)
        if default_sql is None:
            # 有行的表加 NOT NULL 且没有默认值会失败，宁可先做成可空。
            nullable = True
    parts = [dialect.identifier_preparer.quote(column.name), type_sql]
    parts.append("NULL" if nullable else "NOT NULL")
    if default_sql is not None:
        parts.append(f"DEFAULT {default_sql}")
    return " ".join(parts)


_MYSQL_NO_DEFAULT_TYPES = frozenset({
    "text", "tinytext", "mediumtext", "longtext",
    "blob", "tinyblob", "mediumblob", "longblob",
    "json", "geometry",
})


def _mysql_forbids_column_default(type_sql: str, dialect) -> bool:
    if dialect.name != "mysql":
        return False
    kind = type_sql.lower().split("(", 1)[0].strip()
    return kind in _MYSQL_NO_DEFAULT_TYPES


def _is_nonconstant_default(sql: str) -> bool:
    compact = sql.lower().replace(" ", "")
    return compact in {"current_timestamp", "current_timestamp()", "now()", "current_date"}


def _column_default_sql(column, dialect) -> str | None:
    if column.server_default is not None:
        compiled = _compile_default(column.server_default.arg, dialect)
        if compiled is not None:
            return compiled
    default = column.default
    if default is None:
        return None
    arg = getattr(default, "arg", default)
    if callable(arg):
        return None
    return _literal_default(arg)


def _compile_default(arg: Any, dialect) -> str | None:
    if arg is None:
        return None
    if isinstance(arg, ClauseElement):
        try:
            return str(arg.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        except Exception:  # noqa: BLE001
            return None
    if callable(arg):
        return None
    return _literal_default(arg)


def _literal_default(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _inferred_default(column, dialect=None) -> str | None:
    """模型只写了 Python default / 没写 default 时，给 NOT NULL 列一个能落地的库默认值。"""
    t = column.type
    if dialect is not None and _mysql_forbids_column_default(
        str(t.compile(dialect=dialect)), dialect
    ):
        return None
    if isinstance(t, Boolean):
        return "0"
    if isinstance(t, Integer):
        return "0"
    if isinstance(t, Text):
        return "''"
    if isinstance(t, String):
        return "''"
    return None
