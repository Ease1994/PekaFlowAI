"""存量库缺列时，启动必须先按模型补齐，再走 ORM 回填。

场景：pipeline 表还没有 approval_mode，启动却 SELECT Pipeline。
SQLAlchemy 把模型上所有列都带上，MySQL 报 1054，整站起不来。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_schema_patch.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session

    from app.db.schema_sync import sync_model_columns
    from app.db.session import engine
    from app.main import _backfill_pipeline_workspace_uuid
    from app.modules.pipeline.models import Pipeline

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2

    db_file = "./data/verify_schema_patch.db"
    if os.path.exists(db_file):
        os.remove(db_file)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS pipeline"))
        conn.execute(
            text(
                """
                CREATE TABLE pipeline (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    description TEXT DEFAULT '',
                    yaml TEXT DEFAULT '',
                    version INTEGER DEFAULT 1,
                    status VARCHAR(16) DEFAULT 'active',
                    created_by INTEGER,
                    workspace_uuid VARCHAR(64) DEFAULT '',
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO pipeline (id, project_id, group_id, name, workspace_uuid) "
                "VALUES (1, 1, 1, 'old', '')"
            )
        )

    try:
        added = sync_model_columns(engine)
        check("1 按模型补列不报错", True, "、".join(added) or "无新列")
    except Exception as e:  # noqa: BLE001
        check("1 按模型补列不报错", False, str(e))
        return 1

    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(pipeline)")).fetchall()}
    check("2a 有 approval_mode", "approval_mode" in cols)
    check("2b 有 deleted_at", "deleted_at" in cols)

    try:
        _backfill_pipeline_workspace_uuid()
        check("3 ORM 回填 UUID", True)
    except Exception as e:  # noqa: BLE001
        check("3 ORM 回填 UUID", False, str(e))
        return 1

    with engine.begin() as conn:
        ws = conn.execute(text("SELECT workspace_uuid FROM pipeline WHERE id = 1")).scalar()
    check("4 旧流水线补上了 UUID", bool(ws), repr(ws))

    try:
        with Session(engine) as db:
            p = db.scalars(select(Pipeline).where(Pipeline.id == 1)).first()
            check(
                "5 ORM 能读到 approval_mode",
                p is not None and (p.approval_mode or "inherit") == "inherit",
            )
    except Exception as e:  # noqa: BLE001
        check("5 ORM 能读到 approval_mode", False, str(e))

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
