# -*- coding: utf-8 -*-
"""发布邮件应是可扫读的 HTML，正文仍是通知里的摘要和建议。"""
from app.modules.notify.email_html import parse_notice_sections, render_email_html

SAMPLE = """【发布结果】失败
流水线：msbuild
构建号：#22
版本：-
耗时：2 分 26 秒

【错误摘要】
· stage-1/构建环境-Windows：应用池 AppPool 不存在。请核对 IIS 里的名称（大小写要一致）

【建议】
1. 打开 IIS 管理器，在应用程序池中核对 AppPool
2. 若不存在则按该名称新建应用池
3. 确认流水线填写的名称与 IIS 完全一致
"""


def test_parse_notice_keeps_diagnosis_sections():
    secs = parse_notice_sections(SAMPLE)
    titles = [t for t, _lead, _lines in secs]
    assert titles == ["发布结果", "错误摘要", "建议"]
    result = next(s for s in secs if s[0] == "发布结果")
    assert result[1] == "失败"
    assert any("msbuild" in x for x in result[2])
    err = next(s for s in secs if s[0] == "错误摘要")
    assert "AppPool" in " ".join(err[2])


def test_html_failure_uses_red_header_and_escapes():
    html = render_email_html(
        "发布失败：msbuild #22",
        SAMPLE + "\n· 含 <script>alert(1)</script> 的日志",
        brand="PekaFlowAI",
        link="/executions/1/22",
        origin="https://deploy.example.com",
    )
    assert "<html" in html.lower()
    assert "#c62828" in html
    assert "AppPool" in html
    assert "查看执行详情" in html
    assert "https://deploy.example.com/executions/1/22" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_omits_button_without_origin():
    html = render_email_html("发布失败：x", SAMPLE, link="/executions/1/2", origin="")
    assert "查看执行详情" not in html


def test_html_success_uses_green():
    body = "【发布结果】成功\n流水线：msbuild\n构建号：#1\n说明：已成功结束，可在执行历史查看产物与日志。"
    html = render_email_html("发布成功：msbuild #1", body)
    assert "#2e7d32" in html
    assert "成功" in html
