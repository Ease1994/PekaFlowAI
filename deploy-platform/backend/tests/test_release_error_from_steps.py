# -*- coding: utf-8 -*-
"""步骤失败时列表必须展示任务日志里的原因，不能写「没有记录到失败原因」。"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.modules.pipeline.models import Release
from app.modules.pipeline.service import release_error_summary


POOL_LOG = [
    "节点: srv14, 允许操作的目录: [D:\\wwwroot\\testagent]",
    "应用池 AppPool 不存在，请核对 iis 里的名称 (大小写要一致)",
    "步骤失败, 停止本次发布",
]


def test_summary_without_db_still_has_fallback() -> None:
    r = Release(status="failed", error_message="")
    assert "没有记录到失败原因" in release_error_summary(r)


def test_summary_reads_failed_task_log() -> None:
    r = Release(id=22, status="failed", error_message="")
    task = SimpleNamespace(id=8)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [task]
    with patch(
        "app.modules.agent.task_service.failed_task_error_snippet",
        return_value="应用池 AppPool 不存在，请核对 iis 里的名称 (大小写要一致)",
    ):
        got = release_error_summary(r, db=db)
    assert "应用池 AppPool 不存在" in got
    assert "没有记录到失败原因" not in got


def test_list_summary_does_not_read_task_logs() -> None:
    """列表不传 db，不能为了补原因为每条失败去拉 ES。"""
    r = Release(id=22, status="failed", error_message="")
    with patch("app.modules.pipeline.service._error_from_failed_tasks") as spy:
        got = release_error_summary(r)
        spy.assert_not_called()
    assert "没有记录到失败原因" in got


def test_pick_iis_pool_missing_line() -> None:
    from app.modules.ai.followup import pick_error_snippet

    got = pick_error_snippet(POOL_LOG)
    assert got
    assert "应用池" in got[0]
    assert "不存在" in got[0]
    assert "步骤失败" not in got[0]
