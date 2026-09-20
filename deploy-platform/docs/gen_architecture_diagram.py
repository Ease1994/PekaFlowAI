# -*- coding: utf-8 -*-
"""生成发布业务架构流程图（精简主链，已定稿）。

全模块业务总图、技术架构图见 gen_full_diagrams.py。
重新出图：py docs/gen_architecture_diagram.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle

OUT_DIR = Path(__file__).resolve().parent
PNG = OUT_DIR / "系统架构图.png"
PDF = OUT_DIR / "系统架构图.pdf"

# 按功能模块配色（边框 / 底 / 顶条），同一模块同一色。
MOD = {
    "web": ("#1D4E89", "#E8F1FA", "#1D4E89"),
    "ai": ("#C05621", "#FFF1E4", "#C05621"),
    "pipe": ("#0F766E", "#E6F4F1", "#0F766E"),
    "rel": ("#B45309", "#FEF3C7", "#B45309"),
    "exec": ("#277548", "#E6F4EA", "#277548"),
    "store": ("#5B4B8A", "#F0E9FA", "#5B4B8A"),
    "iam": ("#9A6B1F", "#F8F0DD", "#9A6B1F"),
    "obs": ("#6B46C1", "#F3EEFC", "#6B46C1"),
}

C = {
    "bg": "#F3F5F8",
    "paper": "#FFFFFF",
    "ink": "#1A2332",
    "muted": "#5A6574",
    "line": "#C5CDD8",
    "band": "#EEF2F6",
    "white": "#FFFFFF",
}

FONT = "Microsoft YaHei"


def fp(size, weight="normal", color=None):
    """统一中文字体。"""
    return {
        "fontfamily": FONT,
        "fontsize": size,
        "fontweight": weight,
        "color": color or C["ink"],
    }


def box(ax, x, y, w, h, fc, ec="none", lw=0.9, r=0.22, z=3):
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


def txt(ax, x, y, s, size=9, weight="normal", color=None, ha="center", va="center", z=6):
    """画一行字。"""
    ax.text(x, y, s, ha=ha, va=va, zorder=z, **fp(size, weight, color))


def card(ax, x, y, w, h, title, line, mod):
    """一张功能卡：底色和边框用模块色，标题与说明作为一组垂直居中。"""
    ec, fill, bar = MOD[mod]
    box(ax, x, y, w, h, fill, ec, 1.15, 0.2, 4)
    ax.add_patch(Rectangle((x, y + h - 0.13), w, 0.13, facecolor=bar, edgecolor="none", zorder=5))
    cy = y + h / 2 + 0.08
    txt(ax, x + w / 2, cy + 0.62, title, 9.3, "bold", ec)
    txt(ax, x + w / 2, cy - 0.78, line, 7.4, "normal", C["muted"])


def band(ax, x, y, w, h):
    """流程大段浅底。"""
    box(ax, x, y, w, h, C["band"], C["line"], 0.6, 0.28, 1)


def arr(ax, x1, y1, x2, y2, color, lw=1.3):
    """实线箭头。"""
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle="-|>",
            mutation_scale=11,
            linewidth=lw,
            color=color,
            zorder=6,
            shrinkA=0.2,
            shrinkB=0.2,
        )
    )


def diamond(ax, cx, cy, w, h, title, sub, mod="rel"):
    """审批分流判断。"""
    ec, fill, _ = MOD[mod]
    pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
    ax.add_patch(Polygon(pts, closed=True, facecolor=fill, edgecolor=ec, linewidth=1.2, zorder=4))
    txt(ax, cx, cy + 0.28, title, 8.6, "bold", ec)
    txt(ax, cx, cy - 0.42, sub, 6.8, "normal", C["muted"])


def col_head(ax, x, y, w, n, title, accent):
    """主链一列的序号标题。"""
    ax.add_patch(Circle((x + 0.85, y), 0.48, facecolor=accent, edgecolor="none", zorder=4))
    txt(ax, x + 0.85, y, str(n), 8.5, "bold", C["white"])
    txt(ax, x + 1.55, y, title, 10.2, "bold", accent, ha="left")


def draw():
    """按发布业务主链出一张架构流程图。"""
    plt.rcParams["font.sans-serif"] = [FONT, "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(22.0, 10.8), dpi=160)
    ax.set_xlim(0, 100)
    ax.set_ylim(10.6, 68)
    ax.axis("off")
    fig.patch.set_facecolor(C["bg"])
    ax.set_facecolor(C["bg"])

    txt(ax, 50, 66.35, "PekaFlowAI  ·  架构流程图", 20, "bold")
    txt(
        ax,
        50,
        64.55,
        "页面人工操作与 AI 助手并列接入，汇成同一张发布单；测试直跑、生产审批；构建机 / 节点拉任务落地",
        10,
        "normal",
        C["muted"],
    )

    legend = [
        ("web", "页面"),
        ("ai", "AI 助手"),
        ("pipe", "项目与流水线"),
        ("rel", "发布与审批"),
        ("exec", "构建 / 节点 / 制品"),
        ("store", "技能库 / 凭证"),
        ("iam", "用户与权限"),
        ("obs", "观测与审计"),
    ]
    lx = 8.2
    for key, label in legend:
        ec, _fill, _bar = MOD[key]
        ax.add_patch(Rectangle((lx, 62.72), 0.72, 0.42, facecolor=ec, edgecolor="none", zorder=4))
        txt(ax, lx + 0.92, 62.93, label, 7.6, "normal", C["muted"], ha="left")
        lx += 10.8

    # 接入层：三个入口并列，不是 Web→AI→API 串行
    band(ax, 1.6, 51.15, 96.8, 10.55)
    txt(ax, 3.0, 60.15, "接入层  ·  同一账号、同一权限，模型挂了页面照跑", 9.5, "bold", C["ink"], ha="left")

    entry = [
        (3.4, "Web 页面", "编排画布 · 点执行 · 发布提交\n审批页 · 盯日志 · 管构建机和节点", "web"),
        (35.2, "AI 助手", "对话查询 / 发布 / 回滚 / 诊断\n执行权只能在这里申请 · 确认卡后执行", "ai"),
        (67.0, "API / 定时", "API Token 权限与本人相同\ncron 自动发，来源记为定时", "obs"),
    ]
    for x, title, line, mod in entry:
        card(ax, x, 51.55, 29.6, 7.35, title, line, mod)
    txt(ax, 34.1, 55.22, "或", 8.2, "normal", C["muted"])
    txt(ax, 65.9, 55.22, "或", 8.2, "normal", C["muted"])
    txt(ax, 50, 50.35, "三种入口都落到同一张正式发布单，不另开特权通道", 8.0, "normal", C["muted"])

    arr(ax, 50, 50.0, 50, 48.55, C["muted"], 1.5)

    # 发布主链：左到右，和常见持续交付架构图同一读法
    band(ax, 1.6, 23.15, 96.8, 25.05)
    txt(ax, 3.0, 46.55, "发布主流程", 9.5, "bold", C["ink"], ha="left")

    cols = [
        (3.2, 1, "配置", MOD["pipe"][0]),
        (22.4, 2, "发起发布", MOD["rel"][0]),
        (41.6, 3, "审批放行", MOD["rel"][0]),
        (60.8, 4, "执行落地", MOD["exec"][0]),
        (80.0, 5, "运维闭环", MOD["obs"][0]),
    ]
    for x, n, title, accent in cols:
        col_head(ax, x, 45.35, 18.0, n, title, accent)
    for i in range(4):
        arr(ax, cols[i][0] + 18.0, 45.35, cols[i + 1][0], 45.35, C["line"], 1.15)

    cw = 18.0
    ys = (40.55, 36.15, 31.75, 27.35)
    ch = 3.95

    x = 3.2
    for y, (title, line, mod) in zip(
        ys,
        [
            ("项目与环境", "系统 + 测试/生产分组、审批策略", "pipe"),
            ("流水线编排", "页面画布 Stage → Job → Step", "web"),
            ("代码仓库 / 凭证", "Git 绑定；密钥加密、步骤按引用", "store"),
            ("技能库", "流水线插件；助手技能包另算一套", "store"),
        ],
    ):
        card(ax, x, y, cw, ch, title, line, mod)

    x = 22.4
    for y, (title, line, mod) in zip(
        ys,
        [
            ("页面点执行", "有执行权后在流水线页直接发", "web"),
            ("发布提交", "填分支和文件清单，核对后触发", "rel"),
            ("AI 确认卡", "对话发起，点黄按钮才真正执行", "ai"),
            ("定时触发", "按 cron 入队，来源记为定时", "obs"),
        ],
    ):
        card(ax, x, y, cw, ch, title, line, mod)

    x = 41.6
    diamond(ax, x + cw / 2, ys[0] + ch / 2, 12.2, 3.7, "测试环境？", "看环境分组策略", "rel")
    card(ax, x, ys[1], cw, ch, "是 → 直接排队", "测试分组可免技术审批", "exec")
    card(ax, x, ys[2], cw, ch, "否 → 发布审批页", "页面通过或驳回，驳回必填原因", "web")
    card(ax, x, ys[3], cw, ch, "执行权不足", "AI 申请 → 权限管理页审核", "iam")

    x = 60.8
    for y, (title, line, mod) in zip(
        ys,
        [
            ("构建机", "拉代码、编译、打镜像；不开入站", "exec"),
            ("制品库", "按环境保留，回滚时包还在", "exec"),
            ("部署节点", "发文件 / 启停 / K8s / Docker / IIS", "exec"),
            ("失败即停", "步骤报错级联取消，取消真杀进程", "exec"),
        ],
    ):
        card(ax, x, y, cw, ch, title, line, mod)

    x = 80.0
    for y, (title, line, mod) in zip(
        ys,
        [
            ("执行详情 / 日志", "页面实时日志、失败定位", "web"),
            ("回滚 / Rebuild", "页面或助手发起，还原备份或重跑", "rel"),
            ("AI 失败诊断", "读失败步骤日志说明原因，不代替回滚", "ai"),
            ("通知 / 审计 / 指标", "铃铛、操作人留痕、部署频率", "obs"),
        ],
    ):
        card(ax, x, y, cw, ch, title, line, mod)

    txt(
        ax,
        50,
        23.85,
        "发起的四路汇成同一张发布单。平台不往内网推任务，构建机与节点主动来拉；环境码对不上当场拦住。",
        7.6,
        "normal",
        C["muted"],
    )

    # 支撑：不进主链的后台模块，收在底下一条
    band(ax, 1.6, 11.85, 96.8, 9.9)
    txt(ax, 3.0, 20.15, "支撑能力（不插入发布主链）", 9.5, "bold", C["ink"], ha="left")
    support = [
        (3.4, "用户管理", "本地账号、LDAP 导入、启停", "iam"),
        (22.4, "权限管理", "项目/环境/流水线/节点授权\n页面审核执行权申请", "iam"),
        (41.6, "模型管理", "给助手配模型与密钥\n用户侧不露 Key", "ai"),
        (60.8, "平台设置", "LDAP、企微、隔离执行器地址", "obs"),
        (80.0, "数据存储", "业务 MySQL · 队列 Redis · 日志 ES", "obs"),
    ]
    for x, title, line, mod in support:
        card(ax, x, 12.2, cw, 7.35, title, line, mod)

    txt(
        ax,
        50,
        11.05,
        "PekaFlowAI  ·  颜色按模块  ·  重新出图：py docs/gen_architecture_diagram.py",
        7.4,
        "normal",
        C["muted"],
    )

    fig.subplots_adjust(left=0.012, right=0.988, top=0.988, bottom=0.012)
    fig.savefig(PNG, dpi=160, facecolor=C["bg"])
    fig.savefig(PDF, facecolor=C["bg"])
    plt.close(fig)
    print(f"PNG  {PNG}")
    print(f"PDF  {PDF}")


if __name__ == "__main__":
    draw()
