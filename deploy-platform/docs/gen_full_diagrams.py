# -*- coding: utf-8 -*-
"""生成两张大图：全模块业务架构、全栈技术架构。

精简发布主链（已定稿）仍由 gen_architecture_diagram.py 输出 系统架构图。
本文件：
  全模块业务架构图.{png,pdf}
  技术架构图.{png,pdf}

重新出图：py docs/gen_full_diagrams.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Rectangle

OUT_DIR = Path(__file__).resolve().parent
BIZ_PNG = OUT_DIR / "全模块业务架构图.png"
BIZ_PDF = OUT_DIR / "全模块业务架构图.pdf"
TECH_PNG = OUT_DIR / "技术架构图.png"
TECH_PDF = OUT_DIR / "技术架构图.pdf"

FONT = "Microsoft YaHei"

# 业务图：按功能模块配色（边框 / 底）
BIZ = {
    "web": ("#1D4E89", "#E8F1FA"),
    "ai": ("#C05621", "#FFF1E4"),
    "pipe": ("#0F766E", "#E6F4F1"),
    "rel": ("#B45309", "#FEF3C7"),
    "exec": ("#277548", "#E6F4EA"),
    "store": ("#5B4B8A", "#F0E9FA"),
    "iam": ("#9A6B1F", "#F8F0DD"),
    "obs": ("#6B46C1", "#F3EEFC"),
}

# 技术图：按技术分层配色
TEC = {
    "client": ("#1D4E89", "#E8F1FA"),
    "edge": ("#0E4C66", "#E4F3F7"),
    "app": ("#0F766E", "#E6F4F1"),
    "ai": ("#C05621", "#FFF1E4"),
    "exec": ("#277548", "#E6F4EA"),
    "data": ("#5B4B8A", "#F0E9FA"),
    "iso": ("#9A3412", "#FFEDD5"),
    "cross": ("#6B46C1", "#F3EEFC"),
}

C = {
    "bg": "#F3F5F8",
    "ink": "#1A2332",
    "muted": "#4A5568",
    "line": "#C5CDD8",
    "band": "#EEF2F6",
    "white": "#FFFFFF",
}


def fp(size, weight="normal", color=None):
    """统一中文字体，字号按大图阅读距离设置。"""
    return {
        "fontfamily": FONT,
        "fontsize": size,
        "fontweight": weight,
        "color": color or C["ink"],
    }


def box(ax, x, y, w, h, fc, ec="none", lw=1.15, r=0.22, z=3):
    """圆角底板。"""
    p = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0.02,rounding_size={r}",
        facecolor=fc,
        edgecolor=ec,
        linewidth=lw,
        zorder=z,
        joinstyle="round",
    )
    ax.add_patch(p)
    return p


def txt(ax, x, y, s, size=11, weight="normal", color=None, ha="center", va="center", z=6, ls=1.32):
    """画字；含换行时用 ls 控制行距。"""
    ax.text(x, y, s, ha=ha, va=va, zorder=z, linespacing=ls, **fp(size, weight, color))


def card(ax, x, y, w, h, title, line, pal, title_size=14, line_size=11.2):
    """模块卡：顶条 + 标题 + 可换行说明。

    标题贴在色条下方，说明在剩余区域垂直居中，避免单行超宽被裁切。
    """
    ec, fill = pal
    box(ax, x, y, w, h, fill, ec, 1.25, 0.2, 4)
    ax.add_patch(Rectangle((x, y + h - 0.18), w, 0.18, facecolor=ec, edgecolor="none", zorder=5))
    txt(ax, x + w / 2, y + h * 0.68, title, title_size, "bold", ec)
    txt(ax, x + w / 2, y + h * 0.32, line, line_size, "normal", C["muted"])


def band(ax, x, y, w, h):
    """浅底分区。"""
    box(ax, x, y, w, h, C["band"], C["line"], 0.7, 0.28, 1)


def arr(ax, x1, y1, x2, y2, color, lw=1.45):
    """实线箭头。"""
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle="-|>",
            mutation_scale=14,
            linewidth=lw,
            color=color,
            zorder=6,
            shrinkA=0.12,
            shrinkB=0.12,
        )
    )


def col_head(ax, x, y, n, title, accent):
    """步骤序号 + 标题。"""
    ax.add_patch(Circle((x + 1.15, y), 0.72, facecolor=accent, edgecolor="none", zorder=4))
    txt(ax, x + 1.15, y, str(n), 13, "bold", C["white"])
    txt(ax, x + 2.1, y, title, 16, "bold", accent, ha="left")


def _setup(fig_w, fig_h, x1, x2, y1, y2):
    """空白画布。"""
    plt.rcParams["font.sans-serif"] = [FONT, "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
    ax.set_xlim(x1, x2)
    ax.set_ylim(y1, y2)
    ax.axis("off")
    fig.patch.set_facecolor(C["bg"])
    ax.set_facecolor(C["bg"])
    return fig, ax


def _legend(ax, items, palettes, y, x0, gap):
    """顶部色块图例。"""
    lx = x0
    for key, label in items:
        ec, _fill = palettes[key]
        ax.add_patch(Rectangle((lx, y - 0.22), 0.95, 0.58, facecolor=ec, edgecolor="none", zorder=4))
        txt(ax, lx + 1.2, y + 0.08, label, 12, "normal", C["muted"], ha="left")
        lx += gap


def draw_business():
    """全功能 / 全模块 / 全流程：按业务步骤落模块，字号按汇报阅读。"""
    fig, ax = _setup(32.0, 21.6, 0, 100, 0, 76)

    txt(ax, 50, 74.15, "PekaFlowAI  ·  全模块业务架构图", 28, "bold")
    txt(
        ax,
        50,
        71.55,
        "按业务步骤实现：登录配权 → 配资产与机器 → 页面 / AI / API 接入 → 申请执行权 → 发起 → 审批 → 落地 → 运维闭环",
        14.5,
        "normal",
        C["muted"],
    )

    _legend(
        ax,
        [
            ("web", "页面"),
            ("ai", "AI 助手"),
            ("pipe", "项目与流水线"),
            ("rel", "发布与审批"),
            ("exec", "构建 / 节点 / 制品"),
            ("store", "技能库 / 凭证"),
            ("iam", "用户与权限"),
            ("obs", "观测与审计"),
        ],
        BIZ,
        69.15,
        4.2,
        11.7,
    )

    # 两行五列。每列 4 张模块卡，覆盖功能说明书 16 个模块及实现步骤。
    phases = [
        (
            1,
            "登录准入",
            BIZ["iam"][0],
            [
                ("登录认证", "本地 / LDAP / 企微\nTOTP 双因子", "iam"),
                ("菜单可见性", "侧栏按角色裁剪\n不代替数据权限", "iam"),
                ("API Token", "权限与本人相同\n给脚本和外部系统调用", "obs"),
                ("平台设置", "LDAP、企微、Redis\nES、沙箱执行器地址", "obs"),
            ],
        ),
        (
            2,
            "组织与权限",
            BIZ["iam"][0],
            [
                ("用户管理", "本地账号、LDAP 导入\n启停与重置密码", "iam"),
                ("权限管理", "项目 / 环境 / 流水线\n节点授权", "iam"),
                ("执行权审核", "只审助手交来的申请\n网页不能自助开通", "iam"),
                ("角色与菜单", "治理动作与执行权分开\n管理员看全平台", "iam"),
            ],
        ),
        (
            3,
            "配置资产",
            BIZ["pipe"][0],
            [
                ("项目与环境", "系统 + 测试/生产分组\n审批策略、环境码", "pipe"),
                ("流水线编排", "页面画布\nStage → Job → Step", "web"),
                ("代码仓库", "GitLab / GitHub 绑定\n浅克隆 + mirror 缓存", "pipe"),
                ("凭证 / 技能库", "密钥加密；插件、技能包\n工具包三套不混", "store"),
            ],
        ),
        (
            4,
            "机器就绪",
            BIZ["exec"][0],
            [
                ("构建机", "编译打包，拉任务\n不开入站端口", "exec"),
                ("部署节点", "只发文件 / 启停 / 还原\n须声明可写目录", "exec"),
                ("环境码隔离", "构建机、节点、分组码\n对不上当场拦住", "pipe"),
                ("定时触发", "Redis 队列按 cron 入队\n来源记 source=cron", "obs"),
            ],
        ),
        (
            5,
            "接入办事",
            BIZ["web"][0],
            [
                ("Web 页面", "编排、点执行、审批\n盯日志、管机器", "web"),
                ("AI 助手", "查询 / 发布 / 诊断\n执行权只能在这里申请", "ai"),
                ("模型管理", "配通义等模型与密钥\n用户侧不露 Key", "ai"),
                ("项目工作台", "窗口、业务说明、待确认\n不替代技术审批", "obs"),
            ],
        ),
        (
            6,
            "申请执行权",
            BIZ["iam"][0],
            [
                ("AI 申请", "整项目 / 某个环境\n或某一条流水线", "ai"),
                ("范围不清先问", "命中项目后先问清\n整项目还是只要测试/生产", "ai"),
                ("权限页审核", "通过后写入执行权\n可改拟授权限", "iam"),
                ("站内通知", "申请与审批结论\n进顶栏铃铛", "obs"),
            ],
        ),
        (
            7,
            "发起发布",
            BIZ["rel"][0],
            [
                ("页面点执行", "有执行权后\n在流水线页直接发", "web"),
                ("发布提交", "填分支和文件清单\n核对后触发", "rel"),
                ("AI 确认卡", "对话发起\n点黄按钮才真正执行", "ai"),
                ("四路同一张单", "页面 / 提交 / 助手 / 定时\n都记发布人", "rel"),
            ],
        ),
        (
            8,
            "审批放行",
            BIZ["rel"][0],
            [
                ("测试直跑", "测试分组可免技术审批\n直接排队", "exec"),
                ("发布审批页", "生产必须审批\n驳回必填原因", "web"),
                ("业务确认", "窗口与说明\n不替代技术审批", "obs"),
                ("进入 queued", "只有放行后的单\n才会派给机器", "exec"),
            ],
        ),
        (
            9,
            "执行落地",
            BIZ["exec"][0],
            [
                ("发布状态机", "排队 → 执行\n成功 / 失败 / 取消 / 回滚", "exec"),
                ("构建机拉任务", "拉代码、编译、打镜像\nGit 二次秒拉", "exec"),
                ("制品库", "按环境保留\n回滚时包还在", "exec"),
                ("节点部署", "文件 / IIS / 服务 / K8s / Docker\n失败即停", "exec"),
            ],
        ),
        (
            10,
            "运维闭环",
            BIZ["obs"][0],
            [
                ("执行详情 / 日志", "页面实时日志\n失败定位", "web"),
                ("回滚 / Rebuild", "页面或助手发起\n还原备份或原 commit", "rel"),
                ("AI 失败诊断", "读失败步骤日志说明原因\n不代替回滚", "ai"),
                ("审计 / 指标 / 发布管理", "记真人；DORA\n管理员只看不改", "obs"),
            ],
        ),
    ]

    xs = [1.6, 21.4, 41.2, 61.0, 80.8]
    tops = [38.6, 2.85]
    bw, bh = 17.4, 29.4
    for i, phase in enumerate(phases):
        col, row = i % 5, i // 5
        x, y = xs[col], tops[row]
        n, title, accent, items = phase
        band(ax, x, y, bw, bh)
        col_head(ax, x + 0.25, y + bh - 1.7, n, title, accent)
        cw, ch, gap = bw - 1.0, 5.75, 0.28
        for j, (ct, cl, mod) in enumerate(items):
            cy = y + bh - 3.85 - (j + 1) * (ch + gap)
            card(ax, x + 0.5, cy, cw, ch, ct, cl, BIZ[mod], 14.2, 11.3)

    # 同排步骤之间只在标题高度画短箭头，避免穿过卡片
    for row, top in enumerate(tops):
        hy = top + bh - 1.7
        for col in range(4):
            x1 = xs[col] + bw + 0.08
            x2 = xs[col + 1] - 0.08
            arr(ax, x1, hy, x2, hy, C["line"], 1.7)

    arr(ax, 50, 38.45, 50, 32.55, C["muted"], 2.0)
    txt(
        ax,
        53.4,
        35.45,
        "人、权、资产、机器和入口就绪后，按发布步骤继续（⑥ — ⑩）",
        14.5,
        "bold",
        C["muted"],
        ha="left",
    )

    txt(
        ax,
        50,
        1.15,
        "PekaFlowAI  ·  覆盖功能说明书 16 个模块  ·  三种入口同一权限同一发布单  ·  py docs/gen_full_diagrams.py",
        11.5,
        "normal",
        C["muted"],
    )

    fig.subplots_adjust(left=0.008, right=0.992, top=0.992, bottom=0.008)
    fig.savefig(BIZ_PNG, dpi=150, facecolor=C["bg"])
    fig.savefig(BIZ_PDF, facecolor=C["bg"])
    plt.close(fig)
    print(f"PNG  {BIZ_PNG}")
    print(f"PDF  {BIZ_PDF}")


def draw_tech():
    """全技术栈 / 全技术模块 / 全技术流程。

    分层对齐华为 CAF 与 GitLab 一类图：接入 → 网关 → 应用 → 执行 → 数据存储。
    执行层只跑任务（Agent / 插件 / 沙箱），库表、缓存、检索单独做数据存储层，不和 Agent 混层。
    """
    fig, ax = _setup(32.0, 25.6, 0, 100, 0, 96)

    txt(ax, 50, 94.15, "PekaFlowAI  ·  技术架构图", 28, "bold")
    txt(
        ax,
        50,
        91.55,
        "分层：接入 → 网关 → 应用 → 执行 → 数据存储。请求一律 JWT；机器拉任务；库表与缓存不和执行节点混层。",
        14.2,
        "normal",
        C["muted"],
    )

    _legend(
        ax,
        [
            ("client", "接入端"),
            ("edge", "网关认证"),
            ("app", "应用层"),
            ("ai", "AI 运行时"),
            ("exec", "执行层"),
            ("iso", "隔离沙箱"),
            ("data", "数据存储层"),
            ("cross", "横切治理"),
        ],
        TEC,
        89.15,
        3.6,
        11.7,
    )

    # L1 接入层：谁来访问
    band(ax, 1.4, 80.15, 97.2, 7.55)
    txt(ax, 2.7, 86.45, "L1  接入层", 16, "bold", TEC["client"][0], ha="left")
    entry = [
        (3.0, "Web 控制台", "React 18 · TypeScript · Vite\nAnt Design 5 · React Flow\nTanStack Query · Zustand", "client"),
        (27.2, "AI Agent 对话框", "SSE 进度 · 确认卡片\n多会话 · 可选模型 · 看图\nAssistantPage", "ai"),
        (51.4, "API / MCP", "API Token 与本人同权\nMCP tools/list · tools/call\nOpenAPI /docs", "app"),
        (75.6, "定时 / 外部", "cron 触发入队\n来源记 source=cron\n平台调度器写入 Redis", "data"),
    ]
    for x, t, l, k in entry:
        card(ax, x, 80.4, 21.6, 5.5, t, l, TEC[k], 14.5, 11.2)
    txt(ax, 26.0, 83.15, "或", 13, "normal", C["muted"])
    txt(ax, 50.2, 83.15, "或", 13, "normal", C["muted"])
    txt(ax, 74.4, 83.15, "或", 13, "normal", C["muted"])

    arr(ax, 50, 80.15, 50, 78.55, C["muted"], 1.8)

    # L2 网关层：请求如何进来、如何验明身份
    band(ax, 1.4, 70.35, 97.2, 7.85)
    txt(ax, 2.7, 76.95, "L2  网关层", 16, "bold", TEC["edge"][0], ha="left")
    gate = [
        (3.0, "Nginx 反代", "前端 8000\n/api → 8080", "edge"),
        (18.8, "FastAPI / Uvicorn", "Python 3 入口\n聚合 20+ 模块路由", "app"),
        (34.6, "JWT / PyJWT", "会话与 API Token\n同一套校验", "edge"),
        (50.4, "登录实现", "本地 · LDAP mail/UPN/sAM\n企微 OAuth2 · TOTP", "edge"),
        (66.2, "RBAC", "check_permission 唯一入口\nAI / 页面 / API 同一套", "app"),
        (82.0, "审计上下文", "username 永远是人\nsource=web/ai/api/cron", "cross"),
    ]
    for x, t, l, k in gate:
        card(ax, x, 70.6, 14.6, 5.7, t, l, TEC[k], 13.6, 11.0)

    arr(ax, 50, 70.35, 50, 68.75, C["muted"], 1.8)

    # L3 应用层：业务逻辑在这里，对标华为 CAF 应用层、GitLab Rails
    band(ax, 1.4, 43.55, 97.2, 24.85)
    txt(
        ax,
        2.7,
        66.95,
        "L3  应用层  ·  FastAPI  +  SQLAlchemy 2  +  Pydantic v2",
        16,
        "bold",
        TEC["app"][0],
        ha="left",
    )

    biz_mods = [
        ("auth  认证鉴权", "JWT · LDAP · 企微 · Token"),
        ("account  用户账号", "本地 CRUD · LDAP 导入"),
        ("access  执行权", "仅 AI 可提交申请"),
        ("project  项目环境", "系统 · 测试/生产分组"),
        ("pipeline  流水线", "编排 · 状态机 · 调度 · 诊断"),
        ("repository  代码仓库", "GitLab / GitHub 绑定"),
        ("credential  凭证", "AES 密文 · 步骤按引用"),
        ("store  技能商店", "插件 · 模板 · 草稿"),
        ("deploy  发布提交", "分支 + 文件清单"),
        ("approval  发布审批", "生产门控 · 跳审留痕"),
        ("artifact  制品库", "按环境保留 · 回滚依赖"),
        ("deployment  部署回滚", "节点记录 · 备份凭据"),
        ("agent  机器任务", "注册心跳 · 拉任务 · 日志"),
        ("notify  通知", "铃铛 · 邮件 · 企微"),
        ("audit  操作审计", "记真人 · 记入口"),
        ("metric  指标 DORA", "频率 · 失败率"),
        ("settings  平台设置", "Redis / LDAP / ES / Runner"),
        ("health  健康检查", "进程与依赖探活"),
        ("pm  项目工作台", "窗口 · 待确认"),
        ("ai / llm / harness", "对话入口 · 模型目录 · 内核"),
    ]
    mx = [3.0, 22.2, 41.4, 60.6, 79.8]
    my = [61.15, 55.35, 49.55, 43.75]
    mw, mh = 17.6, 5.15
    for i, (t, l) in enumerate(biz_mods):
        pal = TEC["ai"] if i == 19 else (TEC["cross"] if i in (14, 15, 16, 17) else TEC["app"])
        card(ax, mx[i % 5], my[i // 5], mw, mh, t, l, pal, 13.4, 11.0)

    arr(ax, 50, 43.55, 50, 41.95, C["muted"], 1.8)

    # 应用层内的智能运行时：在 FastAPI 进程里，不是执行层
    band(ax, 1.4, 29.35, 97.2, 12.25)
    txt(
        ax,
        2.7,
        40.15,
        "应用层内 · AI-Harness 运行时（FastAPI 进程内，不是嵌 dsh 进程，也不等于执行层）",
        15.5,
        "bold",
        TEC["ai"][0],
        ha="left",
    )
    # 8 张卡铺满 L3 带宽，右侧 Skills/MCP 不得超出画布
    rt_w, rt_gap, rt_x0 = 11.15, 0.72, 2.55
    runtime = [
        "kernel",
        "session",
        "agent-loop",
        "system-prompt",
        "llm seam",
        "tools 管线",
        "compaction",
        "Skills / MCP",
    ]
    runtime_lines = [
        "ServiceRegistry\nEventBus waterfall",
        "AiSessionEvent 追加\n日志为真源",
        "turn/start → step×N\n最多 8 轮",
        "identity / persona\ntool-guide / 记忆",
        "openai-compatible\nContentBlock 分块",
        "pre → 确认卡\nexecute → post",
        "tool 结果修剪 + fit\n思考不回灌",
        "SKILL.md inject\n同一 Tool 目录",
    ]
    runtime_xs = [rt_x0 + i * (rt_w + rt_gap) for i in range(8)]
    for x, t, l in zip(runtime_xs, runtime, runtime_lines):
        card(ax, x, 29.6, rt_w, 9.55, t, l, TEC["ai"], 13.6, 10.8)
    for i in range(7):
        arr(ax, runtime_xs[i] + rt_w, 34.35, runtime_xs[i + 1], 34.35, TEC["ai"][0], 1.35)

    arr(ax, 50, 29.35, 50, 27.75, C["muted"], 1.8)

    # L4 执行层：对标 GitLab Runner，只干活，不存业务库
    band(ax, 1.4, 17.85, 97.2, 9.5)
    txt(ax, 2.7, 26.05, "L4  执行层", 16, "bold", TEC["exec"][0], ha="left")
    exec_cards = [
        (3.0, "Java Agent", "JDK 8 单 jar · 拉模式（对标 Runner）\nbuilder 编译 / node 只发文件\n注册 → 心跳 → 拉任务 → 上报", "exec"),
        (35.0, "流水线插件", "task.json + 入口脚本\n随任务在构建机 / 节点上跑\n不进助手 Tool 目录", "exec"),
        (67.0, "harness-runner", "隔离容器 8090 · JSON-RPC\n非 root、只读根、fail-closed\n只跑已签名第三方 Tool", "iso"),
    ]
    for x, t, l, k in exec_cards:
        card(ax, x, 18.1, 30.0, 8.25, t, l, TEC[k], 15, 11.4)

    arr(ax, 50, 17.85, 50, 16.25, C["muted"], 1.8)

    # L5 数据存储层：对标华为 CAF 数据层；Redis 承担缓存/队列，也常被单列中间件
    band(ax, 1.4, 8.35, 97.2, 7.5)
    txt(ax, 2.7, 14.65, "L5  数据存储层", 16, "bold", TEC["data"][0], ha="left")
    data_cards = [
        (3.0, "MySQL 8 / SQLite", "业务持久化\n用户、权限、发布单、编排", "data"),
        (35.0, "Redis 7", "缓存 · 定时 ZSET\n日志 Stream 队列", "data"),
        (67.0, "Elasticsearch", "执行日志检索（可选）\n业务库挂了日志检索仍可查", "data"),
    ]
    for x, t, l, k in data_cards:
        card(ax, x, 8.55, 30.0, 6.35, t, l, TEC[k], 15, 11.4)

    # 一次发布的技术路径：穿过应用 → 执行 → 数据
    band(ax, 1.4, 1.4, 97.2, 6.2)
    txt(ax, 2.7, 6.85, "一次发布的技术路径", 16, "bold", C["ink"], ha="left")
    steps = [
        ("① JWT", "接入 / 网关校验"),
        ("② RBAC", "应用层鉴权"),
        ("③ 业务 / Loop", "应用层写发布单"),
        ("④ queued", "写入 Redis / 状态机"),
        ("⑤ builder 拉", "执行层编译"),
        ("⑥ 制品入库", "写入数据层"),
        ("⑦ node 拉", "执行层覆盖"),
        ("⑧ 日志审计", "ES + username"),
    ]
    sw = 10.7
    sx0 = 3.0
    for i, (t, l) in enumerate(steps):
        x = sx0 + i * (sw + 0.55)
        if i < 3:
            pal = TEC["app"]
        elif i in (3, 5, 7):
            pal = TEC["data"]
        else:
            pal = TEC["exec"]
        card(ax, x, 1.6, sw, 4.45, t, l, pal, 13.4, 10.8)
        if i < 7:
            arr(ax, x + sw, 3.82, x + sw + 0.55, 3.82, C["muted"], 1.25)

    txt(
        ax,
        50,
        0.5,
        "PekaFlowAI  ·  模型只通过 Tool 办事，不直接改生产  ·  py docs/gen_full_diagrams.py",
        11.5,
        "normal",
        C["muted"],
    )

    fig.subplots_adjust(left=0.008, right=0.992, top=0.992, bottom=0.006)
    fig.savefig(TECH_PNG, dpi=150, facecolor=C["bg"])
    fig.savefig(TECH_PDF, facecolor=C["bg"])
    plt.close(fig)
    print(f"PNG  {TECH_PNG}")
    print(f"PDF  {TECH_PDF}")


if __name__ == "__main__":
    draw_business()
    draw_tech()
