# -*- coding: utf-8 -*-
"""执行画布偶发「步骤消失」：React Flow 11 的 fitView 只框已量尺寸的节点。

官方说明：受控模式下必须实现 onNodesChange，测量结果才会写回节点；
fitView 会跳过 width/height 为空的节点。执行中顶栏高度一变就触发 ResizeObserver
再 fitView 时，若只有第二行两步写回了尺寸，镜头就会缩成用户截图里那样。
刷新后整图重新测量，所以又恢复了。
"""
from __future__ import annotations


NODE_W = 220
NODE_H = 88
GAP_X = 40
GAP_Y = 36
ORIGIN_X = 40
ORIGIN_Y = 40
JOB_W = 120
STEPS_PER_ROW = 3


def _nodes_included_in_fit_view(nodes: list[dict]) -> list[dict]:
    """对齐 React Flow 11：没量到宽高的节点不进 fitView。"""
    return [n for n in nodes if n.get("width") and n.get("height")]


def _canvas_width_changed(prev: float, nxt: float, threshold: float = 48) -> bool:
    if nxt < 16:
        return False
    if not prev:
        return True
    return abs(nxt - prev) >= threshold


def _compact_step_pos(index: int) -> tuple[float, float]:
    """与 canvasLayout.computeCompactLayout 同套折行：每行 3 个。"""
    col = index % STEPS_PER_ROW
    row = index // STEPS_PER_ROW
    x = ORIGIN_X + JOB_W + GAP_X + col * (NODE_W + GAP_X)
    y = ORIGIN_Y + 72 + row * (NODE_H + GAP_Y)
    return x, y


def test_fit_view_skips_unmeasured_nodes_like_react_flow_11() -> None:
    names = ["Git 拉取代码", "Maven 构建", "Docker 镜像构建", "Docker 镜像推送", "Docker 容器部署"]
    nodes = []
    for i, name in enumerate(names):
        x, y = _compact_step_pos(i)
        measured = name in ("Docker 镜像推送", "Docker 容器部署")
        nodes.append({
            "name": name,
            "x": x,
            "y": y,
            "width": NODE_W if measured else None,
            "height": NODE_H if measured else None,
        })

    fitted = _nodes_included_in_fit_view(nodes)
    assert [n["name"] for n in fitted] == ["Docker 镜像推送", "Docker 容器部署"]

    min_x = min(n["x"] for n in fitted)
    min_y = min(n["y"] for n in fitted)
    max_x = max(n["x"] + n["width"] for n in fitted)
    max_y = max(n["y"] + n["height"] for n in fitted)

    git = nodes[0]
    assert git["y"] < min_y
    assert not (min_x <= git["x"] <= max_x and min_y <= git["y"] <= max_y)


def test_header_height_change_must_not_count_as_refit() -> None:
    """顶栏折行只改高度，宽度没变，不能当成打开日志去 fitView。"""
    assert not _canvas_width_changed(900, 900)
    assert not _canvas_width_changed(800, 820)
    assert _canvas_width_changed(900, 480)
