# -*- coding: utf-8 -*-
"""回滚要重放 file-transfer 前后整段 IIS 启停，不能只记最近一条。"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.agent.models import BuildTask
from app.modules.deployment.models import DeploymentRecord
from app.modules.deployment.service import _file_backup_steps


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[BuildTask.__table__, DeploymentRecord.__table__])
    return Session(engine)


def test_rollback_replays_pool_and_site() -> None:
    db = _db()
    try:
        task = BuildTask(
            release_id=32,
            pipeline_id=1,
            steps_json=json.dumps(
                [
                    {"name": "停止应用池", "plugin": "iis-control", "with": {"action": "stop", "target": "apppool", "name": "AppPool"}},
                    {"name": "停止站点", "plugin": "iis-control", "with": {"action": "stop", "target": "site", "name": "Site_Uat"}},
                    {"name": "file-transfer", "plugin": "file-transfer", "with": {"targetDir": r"D:\wwwroot\testagent"}},
                    {"name": "启动应用池", "plugin": "iis-control", "with": {"action": "start", "target": "apppool", "name": "AppPool"}},
                    {"name": "启动站点", "plugin": "iis-control", "with": {"action": "start", "target": "site", "name": "Site_Uat"}},
                ],
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        row = DeploymentRecord(
            release_id=32,
            pipeline_id=1,
            task_id=task.id,
            step_index=2,
            agent_id=9,
            kind="file-backup",
            payload_json=json.dumps(
                {
                    "targetDir": r"D:\wwwroot\testagent",
                    "backupDir": r"D:\backup\x",
                    "stop_with": {"action": "stop", "name": "Site_Uat"},
                    "start_with": {"action": "start", "name": "AppPool"},
                },
                ensure_ascii=False,
            ),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        names = [s["name"] for s in _file_backup_steps(db, row, json.loads(row.payload_json))]
        assert names == [
            "停止应用池",
            "停止站点",
            r"还原备份到 D:\wwwroot\testagent",
            "启动应用池",
            "启动站点",
        ]
    finally:
        db.close()
