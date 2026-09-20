"""对话附图：data URL 落盘成短链，避免撑爆会话事件。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.ai import chat_images
from app.modules.ai.models import AiConversation, AiMessage, AiSessionEvent
from app.modules.ai.sessions import append_event, create_session, project_events, read_events

# 1×1 透明 PNG
_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_DATA_URL = f"data:image/png;base64,{_PNG}"


@pytest.fixture()
def image_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每条测试用独立目录，避免写进仓库的 data/chat-images。"""
    monkeypatch.setattr(chat_images, "CHAT_IMAGE_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            AiConversation.__table__,
            AiMessage.__table__,
            *[
                table
                for name, table in Base.metadata.tables.items()
                if name in {"ai_session", "ai_session_event"}
            ],
        ],
    )
    with Session(engine) as session:
        yield session


def test_persist_data_url_becomes_short_link(image_dir: Path) -> None:
    urls = chat_images.persist(12, [_DATA_URL, "https://example.com/a.png"])
    assert urls[1] == "https://example.com/a.png"
    assert urls[0].startswith("/ai/sessions/12/images/")
    name = urls[0].rsplit("/", 1)[-1]
    path, media = chat_images.resolve(12, name)
    assert path.is_file()
    assert media == "image/png"
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_resolve_rejects_path_traversal(image_dir: Path) -> None:
    chat_images.persist(12, [_DATA_URL])
    with pytest.raises(BizException) as exc:
        chat_images.resolve(12, "../secrets.png")
    assert exc.value.code == 404


def test_public_urls_only_this_session(image_dir: Path) -> None:
    stored = chat_images.persist(12, [_DATA_URL])[0]
    leaked = stored.replace("/12/", "/99/")
    assert chat_images.public_urls(12, [stored, leaked, "javascript:alert(1)", _DATA_URL]) == [stored]


def test_user_message_projection_keeps_images(db: Session, image_dir: Path) -> None:
    session = create_session(db, 7, title="附图")
    stored = chat_images.persist(session.id, [_DATA_URL])
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "把这个文件发到节点", "kind": "chat", "images": stored},
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    messages = project_events(read_events(db, session.id))["messages"]
    assert messages[0]["content"] == "把这个文件发到节点"
    assert messages[0]["images"] == stored
    raw = json.loads(read_events(db, session.id)[-1].payload_json)
    assert all(not str(item).startswith("data:") for item in raw["images"])
    assert len(raw["images"][0]) < 80
