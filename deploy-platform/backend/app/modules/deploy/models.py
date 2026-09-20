from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DeployRequest(Base):
    """发布提交单：开发人员报「这次要发哪些文件」，发布人员据此触发流水线。

    老 C# 项目只能增量发，而开发不知道生产上的目录结构，只知道自己改了哪些文件。
    这张单子就是两边的交接点：开发填清单和更新日志，发布人员核对后一键发布。
    单子本身不参与调度，触发时把清单当执行参数交给流水线，所以不影响现有发布流程。
    """

    __tablename__ = "deploy_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    # 发布走哪条流水线（编译 → 打包 → 发到节点）
    pipeline_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    title: Mapped[str] = mapped_column(String(200))
    # 关联的代码：仓库别名 + 分支/tag/commit
    repo: Mapped[str] = mapped_column(String(200), default="")
    source_ref: Mapped[str] = mapped_column(String(200), default="")
    # 更新日志：这次改了什么，出问题时回查用
    changelog: Mapped[str] = mapped_column(Text, default="")
    # 业务面：项目经理审批和对外同步用，开发可以先写，经理再改
    business_summary: Mapped[str] = mapped_column(Text, default="")
    impact_scope: Mapped[str] = mapped_column(Text, default="")
    iteration_tag: Mapped[str] = mapped_column(String(64), default="")
    planned_window: Mapped[str] = mapped_column(String(128), default="")
    audience: Mapped[str] = mapped_column(String(256), default="")
    need_user_notice: Mapped[bool] = mapped_column(Boolean, default=False)
    # 发布清单：一行一条相对路径 / 通配符，如 bin/*.dll、Areas/、!*.pdb
    manifest: Mapped[str] = mapped_column(Text, default="")

    # draft 起草 / submitted 待发布 / released 已发布 / rejected 已驳回 / closed 已关闭
    status: Mapped[str] = mapped_column(String(16), default="submitted", index=True)
    # 最近一次由本单触发的发布，点进去能看执行详情
    release_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")

    created_by: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
