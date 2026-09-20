"""上下文压缩必须能单独测：思考回灌和连环 get_pipeline 会把窗口吃光。"""
from __future__ import annotations

from app.modules.ai.budget import (
    collapse_stale_tool_results,
    compact_messages,
    estimate_tokens,
    input_budget,
    prune_tool_result,
    stub_tool_content,
)


def test_input_budget_reserves_output_and_tools() -> None:
    budget = input_budget(32000, max_tokens=8192, tools_tokens=4000)
    assert budget == int(32000 * 0.8) - 8192 - 4000
    assert input_budget(None, None, 0) >= 4000


def test_prune_keeps_head_and_tail() -> None:
    payload = {"log": "HEAD-" + ("x" * 8000) + "-TAIL-UNIQUE"}
    text = prune_tool_result(payload, max_tokens=200)
    assert "HEAD-" in text
    assert "TAIL-UNIQUE" in text
    assert "pruned" in text
    assert estimate_tokens(text) <= 220


def test_collapse_keeps_latest_tool_round_only() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "全部流水线"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_pipeline", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"id": 6, "name": "a", "description": "很长的说明"}'},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "get_pipeline", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": '{"id": 7, "name": "b"}'},
    ]
    out = collapse_stale_tool_results(messages)
    first = next(m for m in out if m.get("tool_call_id") == "c1")
    last = next(m for m in out if m.get("tool_call_id") == "c2")
    assert "_compacted" in first["content"]
    assert "很长的说明" not in first["content"]
    assert last["content"] == '{"id": 7, "name": "b"}'


def test_compact_messages_drops_old_turns_before_latest_user() -> None:
    old_user = {"role": "user", "content": "很久以前"}
    old_assistant = {"role": "assistant", "content": "旧答复 " + ("哈" * 5000)}
    messages = [
        {"role": "system", "content": "identity"},
        old_user,
        old_assistant,
        {"role": "user", "content": "现在问"},
    ]
    out = compact_messages(messages, budget=800)
    roles = [m["role"] for m in out]
    assert roles[0] == "system"
    assert roles[-1] == "user"
    assert out[-1]["content"] == "现在问"
    assert old_assistant not in out


def test_stub_keeps_pipeline_ids() -> None:
    raw = '{"pipelines":[{"id":1,"name":"a","project":"p"},{"id":2,"name":"b"}],"total":2}'
    stub = stub_tool_content(raw)
    assert "1" in stub and "a" in stub
    assert "_compacted" in stub


def test_collapse_does_not_stub_skill_content() -> None:
    body = "<skill_content name=\"rp-release\">\n<skill_instructions>\n点名流水线就出确认卡\n</skill_instructions>\n</skill_content>"
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "发布"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "skill", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "s1", "content": body},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "propose_release", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": '{"ok": true}'},
    ]
    out = collapse_stale_tool_results(messages)
    skill_msg = next(m for m in out if m.get("tool_call_id") == "s1")
    assert "点名流水线就出确认卡" in skill_msg["content"]
    assert "_compacted" not in skill_msg["content"]
