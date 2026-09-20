"""Agent 日志上报体积闸：和 Java LogBatcher 的上限对齐。"""
from app.modules.agent.task_service import (
    MAX_LOG_BATCH,
    MAX_LOG_BATCH_BYTES,
    MAX_LOG_LINE_CHARS,
    clip_log_batch,
)


def test_clip_log_batch_keeps_agent_full_buffer() -> None:
    rows = [f"L{i}" for i in range(MAX_LOG_BATCH)]
    assert len(clip_log_batch(rows)) == MAX_LOG_BATCH


def test_clip_log_batch_drops_beyond_cap() -> None:
    rows = [f"L{i}" for i in range(MAX_LOG_BATCH + 500)]
    assert len(clip_log_batch(rows)) == MAX_LOG_BATCH


def test_clip_log_batch_truncates_long_line() -> None:
    huge = "x" * (MAX_LOG_LINE_CHARS + 2000)
    out = clip_log_batch([huge])
    assert len(out) == 1
    assert len(out[0]) < len(huge)
    assert out[0].endswith("已截断）")


def test_clip_log_batch_caps_total_bytes() -> None:
    line = "x" * 4000
    rows = [line] * MAX_LOG_BATCH
    out = clip_log_batch(rows)
    assert out
    assert sum(len(x.encode("utf-8")) for x in out) <= MAX_LOG_BATCH_BYTES
    assert len(out) < MAX_LOG_BATCH


def test_clip_log_batch_accepts_single_string() -> None:
    assert clip_log_batch("one") == ["one"]
    assert clip_log_batch(None) == []
