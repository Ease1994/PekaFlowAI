"""流水线编排模块的数据模型（核心）。

对应文档 §8.4 的 Stage→Job→Step 编排模型，参考蓝盾。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Pipeline(Base, TimestampMixin):
    __tablename__ = "pipeline"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    yaml: Mapped[str] = mapped_column(Text, default="")  # Pipeline as Code
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/archived/deleted
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 回收站：删除只置状态，保留期内可一键恢复；超期后 status=purged，编排不可恢复，
    # 执行记录/制品留下。deleted = 回收站；purged = 超期归档。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 工作空间目录键：创建时生成，持久化；Agent 使用 workspace/p-{workspace_uuid}/src
    workspace_uuid: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 这条流水线自己的审批要求，覆盖所属环境的默认策略：
    #   inherit  跟环境走（默认，存量流水线都是这个，行为不变）
    #   force    不管环境怎么设，必须审批
    #   exempt   豁免审批
    # 授权上刻意做成不对称的：设 force 有编辑权就行（把门槛调高永远安全），
    # 设 exempt 要单独的 approval_exempt 权限——否则谁能改流水线谁就能
    # 给自己免审，等于把审批闸门交到被审批的人手里
    approval_mode: Mapped[str] = mapped_column(String(16), default="inherit")
    # 编辑器默认视图：form 列表式编排，canvas React Flow 画布。创建时选定，编辑页可再切。
    editor_view: Mapped[str] = mapped_column(String(16), default="form")
    # 触发方式：manual / cron，一条流水线只能选一种，不能并存。
    # 单独成列是为了定时扫描只查 cron 行，不必每 30 秒解析全部 YAML。
    trigger_type: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    cron_expr: Mapped[str] = mapped_column(String(128), default="")
    # 是否消费发布清单（pack-incremental / DEPLOY_MANIFEST）。列表筛选走列，不 parse YAML。
    uses_deploy_manifest: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # run-pipeline 指向的流水线 id，逗号分隔。环检测只读这一列。
    sub_pipeline_ids: Mapped[str] = mapped_column(String(1024), default="")
    # YAML 派生列的版本。启动只回填落后的行；保存时写成当前版本。
    yaml_features_ver: Mapped[int] = mapped_column(Integer, default=0)


# 从 YAML 抽出触发方式、清单标记、子流水线边。升这个数会让启动再扫一遍落后行。
YAML_FEATURES_VER = 1


class Release(Base, TimestampMixin):
    """一次发布任务实例（可审计、可回滚）。"""

    __tablename__ = "release"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 构建号：按流水线各自从 1 自增（页面展示的 #N、变量 BK_CI_BUILD_NUM）。
    # id 是全库自增的主键，拿它当构建号会让新流水线第一次执行就显示 #34
    build_number: Mapped[int] = mapped_column(Integer, default=0, index=True)
    artifact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[str] = mapped_column(String(64), default="")
    # 发布时的源代码版本（commit hash / branch / tag），Rebuild 时复用
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy: Mapped[str] = mapped_column(String(32), default="rolling")  # rolling/blue-green/gray/all
    # pending/queued/running/success/failed/rolling_back/rolled_back
    status: Mapped[str] = mapped_column(String(32), default="pending")
    snapshot: Mapped[str] = mapped_column(Text, default="{}")  # 版本快照，回滚用
    trigger_by: Mapped[str] = mapped_column(String(16), default="manual")  # manual/webhook/cron/ai/sub_pipeline
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 子流水线：父流水线 / 父发布（循环检测 + 追溯）
    parent_pipeline_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    parent_release_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    run_params_json: Mapped[str] = mapped_column(Text, default="{}")  # 启动参数（已与默认值合并）
    outputs_json: Mapped[str] = mapped_column(Text, default="{}")  # 完成后可供父流水线读取的变量
    # 本次执行专属的步骤计划，JSON。为空时按流水线 YAML 执行。
    # 回滚用它：撤销一次发布跑的是「停应用池 → 还原备份 → 起应用池」，
    # 这套步骤是按上次部署留下的记录现场生成的，YAML 里根本没有
    plan_json: Mapped[str] = mapped_column(Text, default="")
    # 被本次回滚撤销的那次发布，用于页面上把两条记录串起来
    rollback_of_release_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 历史列：曾经把构建 stdout 塞这里。现在禁止写入；启动失败原因走 error_message。
    logs: Mapped[str] = mapped_column(Text, default="")
    # 没有拆出构建任务时的失败原因（短文本，业务状态，不是日志）
    error_message: Mapped[str] = mapped_column(String(512), default="")
    step_status: Mapped[str] = mapped_column(Text, default="[]")  # 各步骤执行状态 JSON
    # 业务面（从提交单或发起时带过来）。空着不影响现有发布。
    deploy_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    business_summary: Mapped[str] = mapped_column(Text, default="")
    impact_scope: Mapped[str] = mapped_column(Text, default="")
    iteration_tag: Mapped[str] = mapped_column(String(64), default="")
    planned_window: Mapped[str] = mapped_column(String(128), default="")
    audience: Mapped[str] = mapped_column(String(256), default="")
    need_user_notice: Mapped[bool] = mapped_column(Boolean, default=False)
    announcement_text: Mapped[str] = mapped_column(Text, default="")
    announced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    announced_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 失败时与站内通知同一份摘要+建议，执行页 AI 诊断直接读，不再请求模型
    diagnosis_text: Mapped[str] = mapped_column(Text, default="")


class UserPipelinePref(Base, TimestampMixin):
    """流水线列表里，每个用户对某条流水线的个人偏好。

    收藏和置顶是同一个概念：starred=True 就是「⭐收藏置顶」，既进收藏组、
    又在主列表浮到最前。folder 是**个人的**自定义分组名（空=未分组），
    别人看不到。只有被个性化过的 (用户,流水线) 才有一行，没动过的不占行。
    """

    __tablename__ = "user_pipeline_pref"
    __table_args__ = (UniqueConstraint("user_id", "pipeline_id", name="uq_user_pipeline_pref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    folder: Mapped[str] = mapped_column(String(64), default="")


class UserViewPref(Base, TimestampMixin):
    """按用户 + 视图作用域存一段 JSON，用来记住列表的排序/筛选状态。

    scope 如 "pipeline_list"，data_json 存 {sort, envFilter, folderFilter...}，
    实现「下次进来还在」。作用域全局，换项目时前端对不上的筛选值自行忽略。
    """

    __tablename__ = "user_view_pref"
    __table_args__ = (UniqueConstraint("user_id", "scope", name="uq_user_view_pref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    data_json: Mapped[str] = mapped_column(Text, default="{}")
