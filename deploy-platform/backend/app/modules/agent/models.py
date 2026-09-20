"""构建机模块的数据模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class BuildAgent(Base, TimestampMixin):
    __tablename__ = "build_agent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(256), default="")
    port: Mapped[int] = mapped_column(Integer, default=22)
    os: Mapped[str] = mapped_column(String(32), default="linux")  # linux/windows
    # builder=构建机（编译打包）；node=部署节点（生产机上的受限 Agent，只做发布动作）
    # 两者机制完全一样，分开只是为了「构建机管理」和「节点管理」两个页面各看各的
    role: Mapped[str] = mapped_column(String(16), default="builder", index=True)
    # 节点允许被操作的根目录（JSON 数组），落在它之外的写操作由节点自己拒绝
    allow_paths: Mapped[str] = mapped_column(Text, default="[]")
    # Linux 节点允许启停的服务（JSON 数组），形如 systemd:nginx、docker:web。
    # 仅供页面展示和编排时对照；真正的边界在节点本地的 sudoers 白名单上，
    # 这里的值是 Agent 自报的，不作为放行依据
    allow_services: Mapped[str] = mapped_column(Text, default="[]")
    # 节点环境：prod 的节点往上传文件要走审批。新注册的一律按 prod 对待，
    # 管理员确认是测试机再改过来——认错方向的代价不对等，把生产当测试是要出事的
    env: Mapped[str] = mapped_column(String(16), default="prod", index=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组
    status: Mapped[str] = mapped_column(String(16), default="offline")  # online/offline
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 当前凭证的 sha256（前缀 s256:）。明文只在注册/轮换响应里给 Agent。
    token: Mapped[str] = mapped_column(String(128), default="")
    # 轮换宽限：旧凭证的哈希。Agent 还拿着上一版明文进来时仍放行。
    token_prev: Mapped[str] = mapped_column(String(128), default="")
    # 刚签发、尚未被心跳确认的明文。只在宽限期内有值，用来补发给还没切过来的 Agent。
    token_issued: Mapped[str] = mapped_column(String(64), default="")
    # 上次轮换时间；到期（AGENT_TOKEN_ROTATE_DAYS）后下一次心跳触发换发新 key
    token_rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Agent 自报的 jar 指纹（sha256 前 12 位）。和平台当前 jar 比对即可判断谁没升级，
    # 不用另外维护版本号，改了代码忘改版本号的问题天然不存在
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    # 手动重试开关：版本落后时 Agent 本来就会自己升级，这个开关是给「自动升级失败后
    # 想再推一把」用的，能越过 Agent 自己设的失败熔断。Agent 换完版本重新注册时清掉
    upgrade_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # 下发时刻。Agent 离线或换版本失败时开关会一直挂着，光看开关无法区分
    # 「正在升级」和「卡住了」，页面上按钮也就永远点不动，所以记下时间做超时兜底
    upgrade_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Agent 自报的升级失败原因。升级横跨「平台下发 → Agent 下载 → 守护进程换包」三段，
    # 中间两段都在平台视野之外，不让 Agent 说一声，页面上就只能一直转圈
    upgrade_error: Mapped[str] = mapped_column(String(500), default="")
    # 历史列，不再读写。JDK / Maven 安装目录写在对应插件步骤里。
    tool_homes: Mapped[str] = mapped_column(Text, default="{}")


class NodeGroup(Base, TimestampMixin):
    """节点分组，权限授在组上。

    按台授权在机器多起来之后是维护不住的：扩容要给每个相关的人补一遍，漏一台
    就是发布跑到一半才发现没权限；机器下线了授权还挂着没人清；到最后「谁能碰
    生产机」这个问题得靠翻几百行授权记录才能回答。授在组上，机器进出组权限自
    动跟着走，一个人的授权记录也从几百条变回几条。
    """

    __tablename__ = "node_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")


class NodeGroupMember(Base):
    """节点与分组的多对多关系。

    一台机器往往同时属于好几个维度——「O2O 生产」和「杭州机房」可能都要圈到它，
    所以没做成 build_agent 上的一列。
    """

    __tablename__ = "node_group_member"
    __table_args__ = (UniqueConstraint("group_id", "agent_id", name="uq_node_group_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class BuildTask(Base, TimestampMixin):
    """构建任务（Agent 拉模式执行单元，按 Job 粒度）。"""

    __tablename__ = "build_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    stage_name: Mapped[str] = mapped_column(String(128), default="")
    job_id: Mapped[str] = mapped_column(String(64), default="")
    job_name: Mapped[str] = mapped_column(String(128), default="")
    # Job 所需的标签（用于匹配构建机）；platform = 平台编排步骤（不占用构建机）
    agent_tag: Mapped[str] = mapped_column(String(64), default="linux")
    # 环境隔离：生产流水线的任务只能落到生产构建机/节点，测试的只能落到测试资源。
    # 取值随流水线所属分组的 type（prod/test）。空串按 prod 处理（存量任务）
    env: Mapped[str] = mapped_column(String(16), default="prod")
    # 同 Job 拆段后的前置任务（Agent 段 ↔ 平台段串行）
    depends_on_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 同步等待的子流水线 release
    wait_release_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 该 Job 的所有步骤（插件 + 参数），JSON
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    # 构建环境变量，JSON
    variables_json: Mapped[str] = mapped_column(Text, default="{}")
    # pending / assigned / running / success / failed / timeout
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 指定只能由某个节点领取（部署步骤在插件参数里选了节点时写入）
    target_agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 工作空间目录名：p-{pipeline.workspace_uuid}，同流水线复用；同 release 的所有 job 共享
    workspace: Mapped[str] = mapped_column(String(64), default="")
    # 历史列：曾经当 ES 不可用时把日志塞这里。现在禁止回写，列保留以免老库迁表。
    logs: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT, "mysql"), default="")
    # 每个步骤各自的状态与耗时，Agent 逐步上报，JSON：
    # [{"index":0,"plugin":"git-checkout","status":"success","started_at":"...","duration":3.2}]
    # 没有它的话页面只能拿 Job 总耗时冒充每一步的耗时。
    step_results_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BuildTaskLog(Base, TimestampMixin):
    """历史日志分块表。新日志不再写入；表保留以免老库迁表失败。"""

    __tablename__ = "build_task_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 这一块的起始行号与行数，用来按行偏移定位，避免为了跳过前面的内容把它们全读出来
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT, "mysql"), default="")
