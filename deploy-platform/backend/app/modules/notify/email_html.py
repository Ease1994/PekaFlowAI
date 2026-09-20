# -*- coding: utf-8 -*-
"""把站内通知的纯文本拼成适合邮箱客户端的 HTML。

Outlook 只认表格 + 内联样式，所以不用 flex。正文仍是 format_one 的摘要和建议，
只改呈现，不另跑模型。
"""
from __future__ import annotations

import html
import re

from app.modules.settings.branding import DEFAULT_FROM_NAME

_SECTION = re.compile(r"^【([^】]+)】(.*)$")
_NUM_ITEM = re.compile(r"^\d+[\.、)\]]\s*")

# 状态条颜色：失败醒目，成功冷静，其余中性
_THEME = {
    "失败": ("#c62828", "#fff5f5", "#c62828"),
    "成功": ("#2e7d32", "#f1f8f4", "#2e7d32"),
    "已取消": ("#5f6b7a", "#f4f6f8", "#5f6b7a"),
    "已回滚": ("#c05621", "#fff7ed", "#c05621"),
}
_DEFAULT_THEME = ("#1d4ed8", "#eff6ff", "#1d4ed8")


def parse_notice_sections(body: str) -> list[tuple[str, str, list[str]]]:
    """拆成 (栏目标题, 标题同行文字, 栏目内各行)。"""
    sections: list[tuple[str, str, list[str]]] = []
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        m = _SECTION.match(line.strip())
        if m:
            sections.append((m.group(1).strip(), m.group(2).strip(), []))
            continue
        if sections and line.strip():
            sections[-1][2].append(line.strip())
    return sections


def _status_of(subject: str, sections: list[tuple[str, str, list[str]]]) -> str:
    """从【发布结果】或邮件标题里取出成功/失败等状态字。"""
    for title, lead, _lines in sections:
        if title == "发布结果" and lead:
            return lead
    for key in ("失败", "成功", "已回滚", "已取消"):
        if key in (subject or ""):
            return key
    return ""


def _abs_link(link: str, origin: str) -> str:
    """相对路径拼上平台根地址；没有根地址就不放按钮，避免邮件里出现点不开的链接。"""
    href = (link or "").strip()
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    base = (origin or "").strip().rstrip("/")
    if not base:
        return ""
    if not href.startswith("/"):
        href = "/" + href
    return base + href


def _esc(text: str) -> str:
    """写入 HTML 前转义，防止日志里的 < 把版式拆掉。"""
    return html.escape(text or "", quote=True)


def _meta_table(lines: list[str]) -> str:
    """把「流水线：xxx」这类键值行排成两列，方便扫读。"""
    cells: list[tuple[str, str]] = []
    rest: list[str] = []
    for line in lines:
        if "：" in line:
            k, v = line.split("：", 1)
            cells.append((k.strip(), v.strip()))
        elif ":" in line and not line.strip().startswith("http"):
            k, v = line.split(":", 1)
            cells.append((k.strip(), v.strip()))
        else:
            rest.append(line)
    rows = []
    for i in range(0, len(cells), 2):
        left = cells[i]
        right = cells[i + 1] if i + 1 < len(cells) else ("", "")
        rows.append(
            "<tr>"
            f"<td style=\"padding:6px 16px 6px 0;width:50%;vertical-align:top;\">"
            f"<div style=\"color:#8c8c8c;font-size:12px;\">{_esc(left[0])}</div>"
            f"<div style=\"color:#1f2937;font-size:15px;font-weight:600;margin-top:2px;\">{_esc(left[1])}</div>"
            "</td>"
            f"<td style=\"padding:6px 0;width:50%;vertical-align:top;\">"
            f"<div style=\"color:#8c8c8c;font-size:12px;\">{_esc(right[0])}</div>"
            f"<div style=\"color:#1f2937;font-size:15px;font-weight:600;margin-top:2px;\">{_esc(right[1])}</div>"
            "</td>"
            "</tr>"
        )
    extra = "".join(
        f"<div style=\"color:#374151;font-size:14px;line-height:1.6;margin-top:8px;\">{_esc(x)}</div>"
        for x in rest
    )
    if not rows:
        return extra
    return (
        "<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;\">"
        + "".join(rows)
        + "</table>"
        + extra
    )


def _bullet_block(lines: list[str], *, danger: bool) -> str:
    """错误摘要用浅红底；普通条目用浅灰底。"""
    bg = "#fff5f5" if danger else "#f8fafc"
    border = "#fecaca" if danger else "#e5e7eb"
    color = "#7f1d1d" if danger else "#1f2937"
    items = []
    for line in lines:
        text = line.lstrip("·•- ").strip()
        items.append(
            "<tr>"
            f"<td style=\"padding:8px 0;color:{color};font-size:13px;line-height:1.7;"
            f"font-family:Consolas,'Sarasa Mono SC',monospace;\">{_esc(text)}</td>"
            "</tr>"
        )
    return (
        f"<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"background:{bg};border:1px solid {border};border-radius:8px;\">"
        f"<tr><td style=\"padding:12px 16px;\">"
        f"<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\">{''.join(items)}</table>"
        "</td></tr></table>"
    )


def _tips_block(lines: list[str]) -> str:
    """建议列表：左侧圆号，右侧可执行句子。"""
    rows = []
    for i, line in enumerate(lines, 1):
        text = _NUM_ITEM.sub("", line).strip()
        rows.append(
            "<tr>"
            "<td style=\"width:28px;vertical-align:top;padding:8px 8px 8px 0;\">"
            f"<div style=\"width:24px;height:24px;line-height:24px;text-align:center;"
            f"border-radius:12px;background:#eff6ff;color:#1d4ed8;font-size:12px;font-weight:700;\">{i}</div>"
            "</td>"
            f"<td style=\"vertical-align:top;padding:8px 0;color:#1f2937;font-size:14px;line-height:1.7;\">{_esc(text)}</td>"
            "</tr>"
        )
    return (
        "<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;\">"
        + "".join(rows)
        + "</table>"
    )


def render_email_html(
    subject: str,
    body: str,
    *,
    brand: str = DEFAULT_FROM_NAME,
    link: str = "",
    origin: str = "",
) -> str:
    """把通知正文渲染成一封可扫读的 HTML 信。"""
    sections = parse_notice_sections(body)
    status = _status_of(subject, sections)
    accent, _soft, badge_fg = _THEME.get(status, _DEFAULT_THEME)
    href = _abs_link(link, origin)
    heading = _esc(status or subject or "发布通知")
    brand_e = _esc(brand or DEFAULT_FROM_NAME)

    blocks: list[str] = []
    if not sections:
        blocks.append(
            f"<div style=\"color:#374151;font-size:14px;line-height:1.7;white-space:pre-wrap;\">{_esc(body)}</div>"
        )
    for title, lead, lines in sections:
        all_lines = ([lead] if lead and title != "发布结果" else []) + lines
        if title == "发布结果":
            inner = _meta_table(lines)
            if lead:
                inner = (
                    f"<div style=\"display:inline-block;padding:4px 10px;border-radius:999px;"
                    f"background:{_THEME.get(status, _DEFAULT_THEME)[1]};color:{badge_fg};"
                    f"font-size:13px;font-weight:700;margin-bottom:12px;\">{_esc(lead)}</div>"
                    + inner
                )
        elif title == "错误摘要":
            inner = _bullet_block(all_lines, danger=True)
        elif title == "建议":
            inner = _tips_block(all_lines)
        else:
            inner = _meta_table(all_lines) if any("：" in x or ":" in x for x in all_lines) else (
                f"<div style=\"color:#374151;font-size:14px;line-height:1.7;\">"
                + "<br/>".join(_esc(x) for x in all_lines)
                + "</div>"
            )
        blocks.append(
            f"<div style=\"margin:0 0 22px 0;\">"
            f"<div style=\"color:#6b7280;font-size:12px;letter-spacing:1px;margin-bottom:10px;\">{_esc(title)}</div>"
            f"{inner}</div>"
        )

    button = ""
    if href:
        button = (
            "<div style=\"margin:8px 0 4px 0;\">"
            f"<a href=\"{_esc(href)}\" style=\"display:inline-block;background:{accent};color:#ffffff;"
            "text-decoration:none;padding:12px 22px;border-radius:6px;font-size:14px;font-weight:600;\">"
            "查看执行详情</a></div>"
        )

    inner_html = "".join(blocks)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#eef1f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f6;">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb;">
<tr>
<td bgcolor="{accent}" style="background:{accent};padding:22px 28px;">
<div style="color:#ffffff;font-size:12px;letter-spacing:1px;opacity:.9;">{brand_e}</div>
<div style="color:#ffffff;font-size:22px;font-weight:700;margin-top:8px;">{heading}</div>
<div style="color:#ffffff;font-size:13px;margin-top:6px;opacity:.9;">{_esc(subject)}</div>
</td>
</tr>
<tr>
<td style="padding:28px 28px 8px 28px;font-family:'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;">
{inner_html}
{button}
</td>
</tr>
<tr>
<td style="padding:16px 28px 24px 28px;border-top:1px solid #f0f0f0;color:#9ca3af;font-size:12px;line-height:1.6;font-family:'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;">
本邮件由系统自动发送，请勿直接回复。
</td>
</tr>
</table>
</td></tr>
</table>
</body>
</html>
"""
