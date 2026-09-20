"""研发商店模块的数据模型（插件 + 流水线模板）。"""
from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Plugin(Base, TimestampMixin):
    __tablename__ = "plugin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), default="exec")  # source/build/deploy/notify/trigger/exec/artifact
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    description: Mapped[str] = mapped_column(Text, default="")
    config_schema: Mapped[str] = mapped_column(Text, default="{}")  # JSON Schema
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 拔插包：语言 / 入口命令 / 本地 zip 路径（Agent 下载后执行，不内嵌业务）
    language: Mapped[str] = mapped_column(String(16), default="python")
    entrypoint: Mapped[str] = mapped_column(String(256), default="python task.py")
    package_path: Mapped[str] = mapped_column(String(512), default="")
    package_sha: Mapped[str] = mapped_column(String(64), default="")
    installed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="catalog")  # catalog/uploaded/installed


class PluginDraft(Base, TimestampMixin):
    """待审插件草稿。

    AI 生成或人工提交的插件源码先落在这里，不进插件仓库、不出现在编排器里。
    要管理员看过代码和校验结果、点了发布，才会打包成 zip 走正常的上传/安装流程。
    """

    __tablename__ = "plugin_draft"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), default="exec")
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    description: Mapped[str] = mapped_column(Text, default="")
    config_schema: Mapped[str] = mapped_column(Text, default="{}")
    language: Mapped[str] = mapped_column(String(16), default="python")
    entrypoint: Mapped[str] = mapped_column(String(256), default="python3 task.py")
    # 源码：{"task.py": "...", "lib/util.py": "..."}
    files_json: Mapped[str] = mapped_column(Text, default="{}")
    # 来源与诉求，用于回溯这段代码当初是为了解决什么问题
    source: Mapped[str] = mapped_column(String(16), default="ai")  # ai/manual
    intent: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # pending（待审）/ published（已发布到仓库）/ rejected（驳回）
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lint_json: Mapped[str] = mapped_column(Text, default="{}")
    # 沙箱试跑：借一条真实的构建任务跑一次，看它到底干了什么
    trial_release_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trial_status: Mapped[str] = mapped_column(String(16), default="")  # ""/queued/running/success/failed


class PipelineTemplate(Base, TimestampMixin):
    __tablename__ = "pipeline_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    yaml: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    scope: Mapped[str] = mapped_column(String(16), default="public")  # public/private
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
