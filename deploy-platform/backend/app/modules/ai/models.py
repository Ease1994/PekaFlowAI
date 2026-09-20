"""AI 助手持久化：事件表是真源，旧消息表保留为兼容投影。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AiConversation(Base, TimestampMixin):
    __tablename__ = "ai_conversation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), default="默认会话")
    working_json: Mapped[str] = mapped_column(Text, default="{}")  # 会话投影：当前目标/待选项，对齐 Harness session projection


class AiMessage(Base, TimestampMixin):
    __tablename__ = "ai_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_conversation.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    actions_json: Mapped[str] = mapped_column(Text, default="[]")
    traces_json: Mapped[str] = mapped_column(Text, default="[]")  # 本轮工具调用，下一轮从会话日志重建请求
    kind: Mapped[str] = mapped_column(String(16), default="chat")  # chat / followup / action


class AiSession(Base, TimestampMixin):
    """可恢复的 AI 会话；head_* 是并发 append 的 CAS 游标。"""

    __tablename__ = "ai_session"
    __table_args__ = (
        UniqueConstraint("legacy_conversation_id", name="uq_ai_session_legacy_conversation"),
        Index("ix_ai_session_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), default="默认会话")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    legacy_conversation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_conversation.id"), nullable=True
    )
    parent_session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_session.id"), nullable=True, index=True
    )
    forked_from_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 这个会话选的模型（llm_model.id）。为空表示跟随全局默认模型
    model_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    projection_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AiSessionEvent(Base):
    """Append-only 会话事件；业务代码不得 update/delete。"""

    __tablename__ = "ai_session_event"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_ai_session_event_seq"),
        Index("ix_ai_session_event_session_type", "session_id", "type"),
        Index("ix_ai_session_event_session_turn", "session_id", "turn", "step"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_session.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    model_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    surface: Mapped[str] = mapped_column(String(24), nullable=False, default="internal")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    prev_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class AiAttachment(Base, TimestampMixin):
    """对话框里拖进来的文件，等着被下发到节点。

    不复用 Artifact：制品必须挂在 release 下，而附件在还没决定发到哪、
    甚至还没生成发布单的时候就已经躺在这了。真下发时才打包成制品。
    """

    __tablename__ = "ai_attachment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 用户看到的原始文件名，可能带中文和空格，落盘时另有安全名
    name: Mapped[str] = mapped_column(String(255), default="")
    # 相对目录名，用来还原成 zip 里的路径（拖整个文件夹时保留层级）
    rel_path: Mapped[str] = mapped_column(String(512), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_key: Mapped[str] = mapped_column(String(512), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    # 已经被某次下发消费掉，不能再挑第二次
    consumed_release_id: Mapped[int] = mapped_column(Integer, default=0, index=True)


class AiWatch(Base, TimestampMixin):
    """助手发起的跟进：发布结束或权限审批结束后回写到同一会话。"""

    __tablename__ = "ai_watch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 发布跟进用 release_id；权限申请跟进用 application_id。另一边为 0。
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    application_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    status: Mapped[str] = mapped_column(String(16), default="watching", index=True)  # watching / done
    pending_hinted: Mapped[int] = mapped_column(Integer, default=0)
