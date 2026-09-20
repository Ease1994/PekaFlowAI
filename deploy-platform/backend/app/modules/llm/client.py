# -*- coding: utf-8 -*-
"""Provider-neutral LLM protocol and the OpenAI-compatible adapter.

The public protocol deliberately does not expose OpenAI ``choices`` or SSE
shapes.  Native DeepSeek/Anthropic/Bedrock protocols have reserved adapter
names, but are reported as unavailable until their wire protocols exist.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Literal

logger = logging.getLogger(__name__)


ContentKind = Literal["text", "reasoning", "tool_use", "tool_result", "image"]
Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ContentBlock:
    """A closed content union. Fields irrelevant to ``kind`` stay empty."""

    kind: ContentKind
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    media_type: str = ""
    data: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"text", "reasoning", "tool_use", "tool_result", "image"}:
            raise ValueError(f"unsupported content block: {self.kind}")


@dataclass(frozen=True)
class Message:
    role: Role
    content: tuple[ContentBlock, ...]
    name: str = ""

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported role: {self.role}")

    @classmethod
    def text(cls, role: Role, value: str) -> "Message":
        return cls(role=role, content=(ContentBlock(kind="text", text=value),))


class FailureKind(str, Enum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    CONTEXT_LENGTH = "context_length"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER = "provider"
    UNAVAILABLE = "unavailable"


class LlmFailure(RuntimeError):
    """Stable failure taxonomy used by retry/fallback policy."""

    def __init__(
        self,
        kind: FailureKind,
        message: str,
        *,
        code: str = "",
        status_code: int | None = None,
        retryable: bool = False,
        request_id: str = "",
    ):
        super().__init__(message)
        self.kind = kind
        self.code = code or kind.value
        self.status_code = status_code
        self.retryable = retryable
        self.request_id = request_id


ChunkKind = Literal[
    "message_start", "block_start", "block_delta", "block_stop", "message_stop", "error"
]


@dataclass(frozen=True)
class StreamChunk:
    """Closed stream event union shared by all adapters."""

    kind: ChunkKind
    index: int | None = None
    block: ContentBlock | None = None
    delta: str = ""
    finish_reason: str = ""
    request_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    failure: LlmFailure | None = None

    def __post_init__(self) -> None:
        allowed = {
            "message_start", "block_start", "block_delta",
            "block_stop", "message_stop", "error",
        }
        if self.kind not in allowed:
            raise ValueError(f"unsupported stream chunk: {self.kind}")


class BlockAssembler:
    """Deterministically assembles the closed stream into one assistant message."""

    def __init__(self) -> None:
        self._blocks: dict[int, ContentBlock] = {}

    def push(self, chunk: StreamChunk) -> None:
        if chunk.kind == "error":
            raise chunk.failure or LlmFailure(FailureKind.PROVIDER, "stream failed")
        if chunk.index is None:
            return
        if chunk.kind == "block_start" and chunk.block is not None:
            self._blocks[chunk.index] = chunk.block
        elif chunk.kind == "block_delta":
            previous = self._blocks.get(chunk.index, ContentBlock(kind="text"))
            self._blocks[chunk.index] = ContentBlock(
                kind=previous.kind,
                text=previous.text + chunk.delta,
                tool_call_id=previous.tool_call_id,
                tool_name=previous.tool_name,
                arguments=previous.arguments,
                media_type=previous.media_type,
                data=previous.data,
            )

    def message(self) -> Message:
        return Message(role="assistant", content=tuple(self._blocks[i] for i in sorted(self._blocks)))


@dataclass
class LlmInvokeConfig:
    api_base_url: str
    api_key: str
    model: str
    max_tokens: int = 2048
    temperature: float = 0.2
    timeout_sec: int = 90
    reasoning: bool | None = None
    context_window: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LlmTurn:
    content: str
    tool_calls: list[dict]
    raw_assistant: dict
    request_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    reasoning: str = ""


@dataclass(frozen=True)
class AdapterResponse:
    message: Message
    request_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    first_token_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LlmAdapter(ABC):
    """Seam for provider protocol implementations."""

    name: str
    available: bool = True

    @abstractmethod
    def invoke(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        timeout_sec: int | None = None,
    ) -> AdapterResponse:
        raise NotImplementedError

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        timeout_sec: int | None = None,
    ) -> Iterator[StreamChunk]:
        raise NotImplementedError


def _failure_from_http(status: int, detail: str, request_id: str = "") -> LlmFailure:
    lowered = detail.lower()
    if status == 401:
        kind, retryable = FailureKind.AUTHENTICATION, False
    elif status == 403:
        kind, retryable = FailureKind.PERMISSION, False
    elif status == 429:
        kind, retryable = FailureKind.RATE_LIMIT, True
    elif status in {408, 504}:
        kind, retryable = FailureKind.TIMEOUT, True
    elif status == 400 and ("context" in lowered or "token" in lowered):
        kind, retryable = FailureKind.CONTEXT_LENGTH, False
    elif 400 <= status < 500:
        kind, retryable = FailureKind.INVALID_REQUEST, False
    else:
        kind, retryable = FailureKind.PROVIDER, status >= 500
    code = ""
    try:
        error = (json.loads(detail).get("error") or {})
        code = str(error.get("code") or error.get("type") or "")
    except (json.JSONDecodeError, AttributeError):
        pass
    return LlmFailure(
        kind, f"模型调用失败 HTTP {status}: {detail[:800]}",
        code=code, status_code=status, retryable=retryable, request_id=request_id,
    )


class OpenAICompatibleAdapter(LlmAdapter):
    name = "openai-compatible"

    def __init__(self, cfg: LlmInvokeConfig):
        self.cfg = cfg

    def chat_url(self) -> str:
        url = (self.cfg.api_base_url or "").strip().rstrip("/")
        if not url:
            raise LlmFailure(FailureKind.INVALID_REQUEST, "未配置厂商 API 地址")
        return url if url.endswith("/chat/completions") else f"{url}/chat/completions"

    @staticmethod
    def _to_wire(messages: list[Message]) -> list[dict]:
        result: list[dict] = []
        for message in messages:
            # 思考不回灌。带上去下一轮模型会拿着几千字思考再想一遍，窗口很快被吃光
            texts = [b.text for b in message.content if b.kind == "text"]
            images = [b for b in message.content if b.kind == "image"]
            content: Any = "".join(texts)
            if images:
                # 带图时 content 必须是数组，纯文本时保持字符串——
                # 不少兼容层只认字符串，无脑用数组会让普通对话也跟着挂
                content = [{"type": "text", "text": content}] + [
                    {"type": "image_url", "image_url": {"url": b.data}} for b in images
                ]
            row: dict[str, Any] = {"role": message.role, "content": content}
            calls = [
                {
                    "id": b.tool_call_id,
                    "type": "function",
                    "function": {"name": b.tool_name, "arguments": json.dumps(b.arguments)},
                }
                for b in message.content if b.kind == "tool_use"
            ]
            if calls:
                row["tool_calls"] = calls
            tool_results = [b for b in message.content if b.kind == "tool_result"]
            if tool_results:
                row["tool_call_id"] = tool_results[0].tool_call_id
            if message.name:
                row["name"] = message.name
            result.append(row)
        return result

    def _payload(self, messages: list[Message], tools: list[dict] | None, stream: bool) -> dict:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": self._to_wire(messages),
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "stream": stream,
        }
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if self.cfg.reasoning is not None:
            lower_url = self.chat_url().lower()
            if "dashscope" in lower_url:
                payload["enable_thinking"] = self.cfg.reasoning
            elif "tokenhub" in lower_url or "lkeap.cloud.tencent.com" in lower_url:
                payload["thinking"] = {"type": "enabled" if self.cfg.reasoning else "disabled"}
        payload.update(self.cfg.extra)
        return payload

    def _request(self, payload: dict, timeout_sec: int | None) -> urllib.response.addinfourl:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
        if self.cfg.api_key.strip():
            headers["Authorization"] = f"Bearer {self.cfg.api_key.strip()}"
        request = urllib.request.Request(self.chat_url(), data=body, headers=headers, method="POST")
        timeout = timeout_sec if timeout_sec is not None else self.cfg.timeout_sec
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace") if exc.fp else str(exc)
            request_id = exc.headers.get("x-request-id", "") if exc.headers else ""
            raise _failure_from_http(exc.code, detail, request_id) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LlmFailure(FailureKind.TIMEOUT, f"模型调用超时: {exc}", retryable=True) from exc
        except urllib.error.URLError as exc:
            raise LlmFailure(FailureKind.NETWORK, f"模型网络错误: {exc}", retryable=True) from exc
        except Exception as exc:
            raise LlmFailure(FailureKind.PROVIDER, f"模型调用失败: {exc}") from exc

    @staticmethod
    def _parse_message(raw: dict) -> Message:
        blocks: list[ContentBlock] = []
        content = raw.get("content") or ""
        if isinstance(content, list):
            for item in content:
                blocks.append(ContentBlock(kind="text", text=str(item.get("text") or "")))
        elif content:
            blocks.append(ContentBlock(kind="text", text=str(content)))
        if raw.get("reasoning_content"):
            blocks.append(ContentBlock(kind="reasoning", text=str(raw["reasoning_content"])))
        for call in raw.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                parsed = {}
            blocks.append(ContentBlock(
                kind="tool_use", tool_call_id=str(call.get("id") or ""),
                tool_name=str(fn.get("name") or ""),
                arguments=parsed if isinstance(parsed, dict) else {},
            ))
        return Message(role="assistant", content=tuple(blocks))

    def invoke(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        timeout_sec: int | None = None,
    ) -> AdapterResponse:
        started = time.perf_counter()
        with self._request(self._payload(messages, tools, False), timeout_sec) as response:
            first_token_ms = int((time.perf_counter() - started) * 1000)
            request_id = response.headers.get("x-request-id", "")
            raw_text = response.read().decode(errors="replace")
        try:
            data = json.loads(raw_text)
            choice = (data.get("choices") or [])[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise LlmFailure(
                FailureKind.INVALID_RESPONSE, f"模型返回格式无效: {raw_text[:400]}",
                request_id=request_id,
            ) from exc
        return AdapterResponse(
            message=self._parse_message(choice.get("message") or {}),
            request_id=request_id or str(data.get("id") or ""),
            usage={k: int(v) for k, v in (data.get("usage") or {}).items() if isinstance(v, int)},
            finish_reason=str(choice.get("finish_reason") or ""),
            first_token_ms=first_token_ms,
            raw=data,
        )

    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        timeout_sec: int | None = None,
    ) -> Iterator[StreamChunk]:
        with self._request(self._payload(messages, tools, True), timeout_sec) as response:
            request_id = response.headers.get("x-request-id", "")
            yield StreamChunk(kind="message_start", request_id=request_id)
            # 思考和正文得各占一个块。以前共用一个 index，块类型按第一个增量定死，
            # 于是「先思考后回答」的模型把整段正文也标成了 reasoning，
            # 最后 content 为空、思考原文被当成回答吐到界面上。
            started: dict[tuple[int, ContentKind], int] = {}
            next_index = 0
            for raw_line in response:
                line = raw_line.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break
                try:
                    event = json.loads(data_text)
                    choice = (event.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    index = int(choice.get("index") or 0)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    yield StreamChunk(
                        kind="error",
                        failure=LlmFailure(FailureKind.INVALID_RESPONSE, f"无效 SSE: {line[:300]}"),
                    )
                    raise LlmFailure(FailureKind.INVALID_RESPONSE, "无效 SSE") from exc
                for kind, value in (
                    ("reasoning", delta.get("reasoning_content")),
                    ("text", delta.get("content")),
                ):
                    text = str(value or "")
                    if not text:
                        continue
                    block_kind: ContentKind = kind  # type: ignore[assignment]
                    key = (index, block_kind)
                    if key not in started:
                        started[key] = next_index
                        next_index += 1
                        yield StreamChunk(
                            kind="block_start", index=started[key],
                            block=ContentBlock(kind=block_kind), request_id=request_id,
                        )
                    yield StreamChunk(kind="block_delta", index=started[key], delta=text)
                if choice.get("finish_reason"):
                    for block_index in sorted(started.values()):
                        yield StreamChunk(kind="block_stop", index=block_index)
                    yield StreamChunk(
                        kind="message_stop",
                        finish_reason=str(choice["finish_reason"]),
                        request_id=request_id or str(event.get("id") or ""),
                        usage={
                            k: int(v) for k, v in (event.get("usage") or {}).items()
                            if isinstance(v, int)
                        },
                    )


class UnavailableAdapter(LlmAdapter):
    available = False

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason

    def _fail(self) -> None:
        raise LlmFailure(
            FailureKind.UNAVAILABLE,
            f"{self.name} adapter unavailable: {self.reason}",
            code="adapter_unavailable",
        )

    def invoke(self, messages: list[Message], **kwargs: Any) -> AdapterResponse:
        self._fail()
        raise AssertionError("unreachable")

    def stream(self, messages: list[Message], **kwargs: Any) -> Iterator[StreamChunk]:
        self._fail()
        yield StreamChunk(kind="message_stop")


class LlmClient:
    """Compatibility facade retained for pipeline/AI callers."""

    def __init__(self, cfg: LlmInvokeConfig, adapter: LlmAdapter | None = None):
        self.cfg = cfg
        self.adapter = adapter or OpenAICompatibleAdapter(cfg)

    def chat_url(self) -> str:
        if isinstance(self.adapter, OpenAICompatibleAdapter):
            return self.adapter.chat_url()
        raise LlmFailure(FailureKind.UNAVAILABLE, "当前 adapter 不提供 OpenAI chat URL")

    def _post(self, payload: dict, timeout_sec: int | None) -> dict:
        messages = _legacy_messages(payload.get("messages") or [])
        response = self.adapter.invoke(
            messages, tools=payload.get("tools"), timeout_sec=timeout_sec,
        )
        return response.raw

    @staticmethod
    def _message_text(msg: dict) -> str:
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(
                (x.get("text") or "") if isinstance(x, dict) else str(x) for x in text
            )
        if not str(text).strip():
            text = msg.get("reasoning_content") or ""
        return str(text).strip()

    def chat(self, messages: list[dict], *, timeout_sec: int | None = None) -> str:
        response = self.adapter.invoke(_legacy_messages(messages), timeout_sec=timeout_sec)
        text = "".join(
            block.text for block in response.message.content
            if block.kind in {"text", "reasoning"}
        )
        return text.strip()

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        timeout_sec: int | None = None,
    ) -> LlmTurn:
        response = self.adapter.invoke(
            _legacy_messages(messages), tools=tools, timeout_sec=timeout_sec,
        )
        msg = ((response.raw.get("choices") or [{}])[0].get("message") or {})
        parsed: list[dict] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            parsed.append(
                {
                    "id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": args,
                    "raw": tc,
                }
            )
        text = "".join(
            block.text for block in response.message.content if block.kind == "text"
        ).strip()
        reasoning = "".join(
            block.text for block in response.message.content if block.kind == "reasoning"
        ).strip()
        # 思考模型经常只在 reasoning 里写字、content 为空。有 tool_calls 时不要把思考回灌，
        # 否则下一轮会带着几千字思考再想一遍，直到窗口被吃光后突然断开。
        if not text and not parsed:
            text = reasoning
        return LlmTurn(
            content=text,
            tool_calls=parsed,
            raw_assistant=msg,
            request_id=response.request_id,
            usage=response.usage,
            finish_reason=response.finish_reason,
            reasoning=reasoning,
        )


def _legacy_messages(rows: list[dict]) -> list[Message]:
    messages: list[Message] = []
    for row in rows:
        role = str(row.get("role") or "user")
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        raw_content = row.get("content") or ""
        images: list[ContentBlock] = []
        if isinstance(raw_content, list):
            parts: list[str] = []
            for item in raw_content:
                if not isinstance(item, dict):
                    parts.append(str(item))
                    continue
                if item.get("type") == "image_url":
                    url = str((item.get("image_url") or {}).get("url") or "")
                    if url:
                        images.append(ContentBlock(kind="image", data=url))
                    continue
                parts.append(str(item.get("text") or ""))
            text = "".join(parts)
        else:
            text = str(raw_content)
        blocks: list[ContentBlock] = [ContentBlock(kind="text", text=text), *images]
        for call in row.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments") or "{}"
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                args = {}
            blocks.append(ContentBlock(
                kind="tool_use", tool_call_id=str(call.get("id") or ""),
                tool_name=str(fn.get("name") or ""),
                arguments=args if isinstance(args, dict) else {},
            ))
        if role == "tool":
            blocks = [ContentBlock(
                kind="tool_result", text=text,
                tool_call_id=str(row.get("tool_call_id") or ""),
            )]
        messages.append(Message(role=role, content=tuple(blocks), name=str(row.get("name") or "")))
    return messages
