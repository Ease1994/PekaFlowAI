"""部署记录的数据模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class DeploymentRecord(Base, TimestampMixin):
    """一次部署动作在目标环境留下的痕迹，以及撤销它所需的全部信息。

    回滚不该重新拉代码重新构建：老代码未必还构建得出来（依赖源、SDK 版本都会变），
    而且故障时最缺的就是时间。真正需要的是「上一次部署改了什么、原样在哪」，
    这条记录就是干这个的——部署步骤跑完由 Agent 上报，回滚时平台读它生成撤销任务。

    kind 决定 payload 的形状，也决定回滚怎么执行：
      file-backup   增量发布：payload 里是备份目录和站点目录，撤销 = 还原备份 + 删新增
      docker-image  容器发布：payload 里是上一个镜像 tag，撤销 = 用旧 tag 重新起
      k8s-revision  K8s 发布：payload 里是 deployment 和 revision，撤销 = rollout undo
    """

    __tablename__ = "deployment_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    # 执行这次部署的 Agent（节点或构建机）。回滚必须回到同一台机器上执行，
    # 备份文件只在那台机器的磁盘上
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 撤销所需的参数，JSON。形状由 kind 决定
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    # 部署目标的标识（站点目录 / 容器名 / deployment 名），用于判断
    # 「这条记录是不是该目标上最新的一次部署」
    target: Mapped[str] = mapped_column(String(512), default="", index=True)
    # 人话摘要，直接显示在回滚确认框里，例如「覆盖 12 个文件，新增 3 个」
    summary: Mapped[str] = mapped_column(String(512), default="")
    # 撤销状态：空=未撤销；undone=已被回滚掉。同一条记录只允许撤销一次，
    # 撤销后备份目录里的东西已经盖回去了，再撤一次只会把现场搞乱
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undone_by_release_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
