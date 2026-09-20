# -*- coding: utf-8 -*-
"""空库启动必须能登录、有默认配置，再跑一遍不能重复建管理员。"""
from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.db.models  # noqa: F401
from app.core.security import verify_password
from app.db.base import Base
from app.db.bootstrap import ensure_admin, ensure_platform_settings, ensure_runtime_ready, wait_for_database
from app.db.models import Group, Pipeline, Project, User
from app.main import _backfill_prod_self_approval, _seed_if_empty
from app.modules.settings import get_all_settings, get_setting, update_settings
from app.modules.settings.defaults import DEFAULT_ES_INDEX_PREFIX


def _bind(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    monkeypatch.setattr("app.db.session.engine", engine)
    monkeypatch.setattr("app.db.session.SessionLocal", sessionmaker(bind=engine))
    return engine


def test_fresh_database_is_ready_to_login(tmp_path, monkeypatch):
    engine = _bind(tmp_path, monkeypatch)
    wait_for_database(engine, attempts=3, delay=0.01)
    Base.metadata.create_all(engine)
    _seed_if_empty()
    _backfill_prod_self_approval()
    ensure_runtime_ready()

    with Session(engine) as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        assert admin.is_admin
        assert verify_password("admin123", admin.password_hash)
        assert db.scalar(select(func.count()).select_from(Project)) == 1
        demo = db.scalar(select(Project).where(Project.code == "DEMO"))
        assert demo is not None and demo.name == "发布示例项目"
        names = set(db.scalars(select(Pipeline.name)).all())
        assert names == {"容器化发布", "Windows增量发布", "前端发布"}
        assert get_setting(db, "agent_enroll_token").strip()
        assert get_setting(db, "harness_runner_token").strip()
        prods = db.scalars(select(Group).where(Group.type == "prod")).all()
        assert prods
        assert all(g.allow_self_approval for g in prods)

    _seed_if_empty()
    ensure_runtime_ready()
    with Session(engine) as db:
        n = db.scalar(select(func.count()).select_from(User).where(User.username == "admin"))
        assert n == 1


def test_ensure_admin_when_only_ordinary_users(tmp_path, monkeypatch):
    engine = _bind(tmp_path, monkeypatch)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="dev", display_name="开发", password_hash="x", is_admin=False))
        db.commit()
        ensure_admin(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None and admin.is_admin
        assert verify_password("admin123", admin.password_hash)
        assert db.scalar(select(User).where(User.username == "dev")) is not None


def test_compose_es_hosts_seeded_then_settings_page_wins(tmp_path, monkeypatch):
    """Compose 把本栈 ES 写入库；之后设置页改地址，不被 ES_HOSTS 环境变量锁死。"""
    monkeypatch.setenv("ES_HOSTS", "http://elasticsearch:9200")
    engine = _bind(tmp_path, monkeypatch)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        ensure_platform_settings(db)
        assert get_setting(db, "es_hosts") == "http://elasticsearch:9200"
        assert get_setting(db, "es_index") == DEFAULT_ES_INDEX_PREFIX
        update_settings(db, {"es_hosts": "http://es.example.com:9200"})
        assert get_setting(db, "es_hosts") == "http://es.example.com:9200"
        assert get_all_settings(db)["es_hosts"] == "http://es.example.com:9200"


def test_compose_replaces_loopback_es_hosts(tmp_path, monkeypatch):
    """已有库若还是 localhost:9200，进 Compose 后改成本栈 ES；用户已填其它地址则不动。"""
    engine = _bind(tmp_path, monkeypatch)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        ensure_platform_settings(db)
        assert get_setting(db, "es_hosts") == "http://localhost:9200"
    monkeypatch.setenv("ES_HOSTS", "http://elasticsearch:9200")
    with Session(engine) as db:
        ensure_platform_settings(db)
        assert get_setting(db, "es_hosts") == "http://elasticsearch:9200"
        update_settings(db, {"es_hosts": "http://es.example.com:9200"})
    with Session(engine) as db:
        ensure_platform_settings(db)
        assert get_setting(db, "es_hosts") == "http://es.example.com:9200"
