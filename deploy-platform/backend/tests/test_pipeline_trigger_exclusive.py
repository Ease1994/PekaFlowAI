"""一条流水线只能是手动或定时，不能并存。"""
from types import SimpleNamespace

from app.core.response import BizException
from app.modules.pipeline.graph import as_exclusive_triggers, exclusive_trigger_of


def test_exclusive_prefers_cron() -> None:
    items = [
        SimpleNamespace(type="manual", cron=None),
        SimpleNamespace(type="cron", cron="0 2 * * *"),
    ]
    kind, expr = exclusive_trigger_of(items)
    assert kind == "cron"
    assert expr == "0 2 * * *"
    got = as_exclusive_triggers(items)
    assert len(got) == 1
    assert got[0].type == "cron"


def test_exclusive_manual_when_no_cron() -> None:
    kind, expr = exclusive_trigger_of([SimpleNamespace(type="webhook", cron=None)])
    assert kind == "manual"
    assert expr == ""


def test_exclusive_empty_is_manual() -> None:
    kind, expr = exclusive_trigger_of([])
    assert kind == "manual"
    assert expr == ""


def test_save_rejects_empty_cron() -> None:
    try:
        exclusive_trigger_of(
            [SimpleNamespace(type="cron", cron="  ")],
            require_valid=True,
        )
        raise AssertionError("should reject")
    except BizException as exc:
        assert exc.code == 400


def test_save_rejects_bad_cron() -> None:
    try:
        exclusive_trigger_of(
            [SimpleNamespace(type="cron", cron="not-a-cron")],
            require_valid=True,
        )
        raise AssertionError("should reject")
    except BizException as exc:
        assert exc.code == 400
