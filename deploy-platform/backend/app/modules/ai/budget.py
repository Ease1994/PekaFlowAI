"""上下文预算与压缩，对齐 DeepSeek Harness 的两段式 compaction。

1. 无模型裁剪：超长工具结果改成 head + 省略标记 + tail；本轮更早的工具结果收成摘要。
2. 按窗口压历史：system 和本轮用户输入钉住，从最近往前塞，超预算的整组丢掉。

没有接真正的 tokenizer，用启发式估算：CJK 约 1 token，其余约 4 字符 1 token。
估算偏保守（宁可少喂）。思考内容禁止回灌下一轮，那是上下文膨胀的主因。
"""
from __future__ import annotations

import json
from typing import Any

# 模型没填 context_window 时的默认窗口。真正预算按 0.8 * window - 输出预留 - tools schema
DEFAULT_CONTEXT_WINDOW = 32000
THRESHOLD_RATIO = 0.8
# 留给模型输出（含思考）的下限
MIN_OUTPUT_RESERVE = 1024
MIN_INPUT_BUDGET = 4000
# 单条工具结果进入模型历史的上限；诊断类结果本身就是摘要，给得宽一些
TOOL_RESULT_MAX_TOKENS = 1200
# 从会话日志回放的历史工具结果，只需要够模型回忆“查过什么”
HISTORY_TOOL_RESULT_MAX_TOKENS = 280
# 本轮里，更早几轮工具结果压成摘要后的上限
STALE_TOOL_RESULT_MAX_TOKENS = 180
# 单条历史文本消息上限
HISTORY_TEXT_MAX_TOKENS = 400
# skill 正文单独限额：playbook 够用，用户上传的 2 万字说明书不能整篇塞进去
SKILL_RESULT_MAX_TOKENS = 2500
# 兼容旧引用
CONTEXT_BUDGET_TOKENS = 24000

_OMISSION = "\n\n[... tool result middle pruned ...]\n\n"


def estimate_tokens(text: str | None) -> int:
    """粗估 token 数。中文按字计，其余按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef":
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def input_budget(
    context_window: int | None,
    max_tokens: int | None = None,
    tools_tokens: int = 0,
) -> int:
    """能塞进 messages 的 token 上限。思考/输出和 tools schema 不占这段预算。"""
    window = int(context_window or 0) or DEFAULT_CONTEXT_WINDOW
    window = max(window, 8000)
    reserved = max(int(max_tokens or 0), MIN_OUTPUT_RESERVE)
    budget = int(window * THRESHOLD_RATIO) - reserved - max(int(tools_tokens or 0), 0)
    return max(budget, MIN_INPUT_BUDGET)


def clip_to_tokens(text: str, max_tokens: int, suffix: str = "…[已截断]") -> str:
    """把文本裁到 token 预算内。二分找切点，避免中英混排时截得过狠。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid] + suffix) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + suffix


def clip_head_tail(text: str, max_tokens: int) -> str:
    """保留头尾、丢掉中间。对齐 harness 的 tool-result-pruner，错误栈尾部往往更有用。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    marker_tokens = estimate_tokens(_OMISSION)
    if max_tokens <= marker_tokens + 32:
        return clip_to_tokens(text, max_tokens)
    remain = max_tokens - marker_tokens
    head_budget = max(remain * 3 // 4, 16)
    tail_budget = max(remain - head_budget, 16)
    head = clip_to_tokens(text, head_budget, suffix="")
    # 从尾部往前扩，直到贴上 tail_budget
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if estimate_tokens(text[mid:]) <= tail_budget:
            hi = mid
        else:
            lo = mid + 1
    tail = text[lo:]
    combined = head + _OMISSION + tail
    if estimate_tokens(combined) > max_tokens:
        return clip_to_tokens(text, max_tokens)
    return combined


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def tool_result_for_model(
    name: str, result: Any, max_tokens: int = TOOL_RESULT_MAX_TOKENS
) -> str:
    """skill 正文比普通工具结果宽，但不能把 2 万字 SKILL.md 整篇塞进窗口。"""
    if (name or "").strip() in {"skill", "load_skill"} and isinstance(result, dict):
        text = result.get("skill_content") or result.get("content")
        if text:
            return clip_head_tail(str(text), SKILL_RESULT_MAX_TOKENS)
        return prune_tool_result(result, SKILL_RESULT_MAX_TOKENS)
    return prune_tool_result(result, max_tokens)


def prune_tool_result(result: Any, max_tokens: int = TOOL_RESULT_MAX_TOKENS) -> str:
    """序列化工具结果并压进预算。

    先砍列表（发布列表、流水线目录这类超预算多半是行数太多），
    砍到还超才退化成头尾截断，尽量保住 JSON 结构可解析。
    """
    text = _dumps(result)
    if estimate_tokens(text) <= max_tokens:
        return text

    if isinstance(result, dict):
        trimmed: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in result.items()}
        omitted: dict[str, int] = {}
        for _ in range(24):
            key = max(
                (k for k, v in trimmed.items() if isinstance(v, list) and v),
                key=lambda k: len(trimmed[k]),
                default=None,
            )
            if key is None:
                break
            items = trimmed[key]
            keep = max(len(items) // 2, 1)
            if keep >= len(items):
                break
            omitted[key] = omitted.get(key, 0) + (len(items) - keep)
            trimmed[key] = items[:keep]
            trimmed["_omitted"] = omitted
            trimmed["_hint"] = "结果过长已裁剪，需要完整数据请缩小范围（指定 pipeline_id 或关键词）后再查"
            text = _dumps(trimmed)
            if estimate_tokens(text) <= max_tokens:
                return text

    return clip_head_tail(text, max_tokens)


def stub_tool_content(content: str, max_tokens: int = STALE_TOOL_RESULT_MAX_TOKENS) -> str:
    """本轮更早的工具结果只留检索键，完整内容仍在会话日志里。"""
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return clip_to_tokens(content or "", max_tokens)
    if not isinstance(data, dict):
        return clip_to_tokens(content or "", max_tokens)
    keep: dict[str, Any] = {"_compacted": True}
    for key in ("id", "name", "error", "total", "hint", "status", "project", "env", "group"):
        if key in data:
            keep[key] = data[key]
    pipes = data.get("pipelines")
    if isinstance(pipes, list):
        keep["pipelines"] = [
            {k: row.get(k) for k in ("id", "name", "project", "env") if isinstance(row, dict) and k in row}
            for row in pipes[:12]
        ]
        keep["total"] = data.get("total", len(pipes))
    keep["_hint"] = "完整结果已从上下文撤下，需要细节请再调用工具"
    return clip_to_tokens(_dumps(keep), max_tokens)


# 一张图在各家计费里大致就是这个量级。按 base64 长度估会直接把预算算爆，
# 然后压缩逻辑会把带图的那条用户消息判成超预算扔掉
IMAGE_TOKENS = 800


def content_tokens(content) -> int:
    """content 可能是字符串，也可能是带图的数组。"""
    if isinstance(content, list):
        total = 0
        for item in content:
            if not isinstance(item, dict):
                total += estimate_tokens(str(item))
            elif item.get("type") == "image_url":
                total += IMAGE_TOKENS
            else:
                total += estimate_tokens(item.get("text") or "")
        return total
    return estimate_tokens(content or "")


def _message_tokens(msg: dict) -> int:
    total = content_tokens(msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        total += estimate_tokens(fn.get("name") or "") + estimate_tokens(fn.get("arguments") or "")
    return total + 4  # role / 分隔符开销


def _group(messages: list[dict]) -> list[list[dict]]:
    """把 assistant(tool_calls) 和它的 tool 结果绑成一组。

    拆散会导致 OpenAI 兼容接口报 tool_call_id 无对应调用，所以裁剪必须以组为单位。
    """
    groups: list[list[dict]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool" and groups and groups[-1][0].get("role") == "assistant" and groups[-1][0].get("tool_calls"):
            groups[-1].append(msg)
            continue
        groups.append([msg])
    return groups


def collapse_stale_tool_results(messages: list[dict], keep_latest: int = 1) -> list[dict]:
    """本轮只保留最近一组工具结果的全文，更早的改成摘要。

    模型常对 list 里每一条再 get_pipeline，不收掉的话思考会把窗口吃光然后突然断掉。
    """
    groups = _group(messages)
    if not groups:
        return messages
    last_user = max((i for i, g in enumerate(groups) if g[0].get("role") == "user"), default=None)
    live = [
        i
        for i, g in enumerate(groups)
        if last_user is not None
        and i > last_user
        and g[0].get("role") == "assistant"
        and g[0].get("tool_calls")
    ]
    stale = set(live[:-keep_latest] if keep_latest else live)
    if not stale:
        return messages
    out: list[dict] = []
    for i, group in enumerate(groups):
        if i not in stale:
            out.extend(group)
            continue
        for msg in group:
            if msg.get("role") != "tool":
                out.append(msg)
                continue
            cloned = dict(msg)
            content = str(msg.get("content") or "")
            if content.lstrip().startswith("<skill_content"):
                out.append(cloned)
                continue
            cloned["content"] = stub_tool_content(content)
            out.append(cloned)
    return out


def fit_messages(messages: list[dict], budget: int = CONTEXT_BUDGET_TOKENS) -> list[dict]:
    """按预算保留最近的对话，system 与本轮用户输入永远保留。"""
    groups = _group(messages)
    if not groups:
        return messages

    pinned: set[int] = {i for i, g in enumerate(groups) if g[0].get("role") == "system"}
    last_user = max((i for i, g in enumerate(groups) if g[0].get("role") == "user"), default=None)
    if last_user is not None:
        pinned.add(last_user)

    sizes = [sum(_message_tokens(m) for m in g) for g in groups]
    keep = set(pinned)
    total = sum(sizes[i] for i in pinned)
    for i in range(len(groups) - 1, -1, -1):
        if i in keep:
            continue
        if total + sizes[i] > budget:
            break
        keep.add(i)
        total += sizes[i]

    if len(keep) == len(groups):
        return messages

    out: list[dict] = []
    noted = False
    for i, g in enumerate(groups):
        if i not in keep:
            continue
        if not noted and g[0].get("role") != "system":
            out.append({"role": "system", "content": "[系统] 较早的对话因上下文长度已省略，需要旧信息请重新调用技能查询。"})
            noted = True
        out.extend(g)
    return out


def compact_messages(
    messages: list[dict],
    *,
    budget: int | None = None,
    context_window: int | None = None,
    max_tokens: int | None = None,
    tools_tokens: int = 0,
) -> list[dict]:
    """先收本轮旧工具结果，再按窗口丢更早的对话组。"""
    limit = budget if budget is not None else input_budget(context_window, max_tokens, tools_tokens)
    return fit_messages(collapse_stale_tool_results(messages), limit)
