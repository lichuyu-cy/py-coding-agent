"""OpenAI 兼容 Provider（Chat Completions）：真实模型接入（阶段 23 评测链路）。

- 只依赖 httpx；HTTP 客户端可注入（测试用 MockTransport，不触网）；
- 响应先归一为 ModelResponse 并走同一校验（validate_model_response）：
  「真实 Provider 与 Fake 产出同一种可验证响应」；
- 错误映射为 ProviderErrorKind；重试策略由 Runtime 决定（本类绝不重试）；
- 流式（stream）尚未实现：显式失败而不是假装支持（评测使用非流式路径）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from coding_agent.domain.messages import ToolCall
from coding_agent.domain.state import StopReason
from coding_agent.ports.provider import (
    CancelSignal,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    ProviderMessage,
    ProviderMessageRole,
    TokenUsage,
    validate_model_request,
    validate_model_response,
)

__all__ = ["OpenAICompatibleProvider"]

_FINISH_REASON_MAP: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_CALLS,
    "length": StopReason.MAX_TOKENS,
}


class OpenAICompatibleProvider:
    """POST {base_url}/chat/completions；api_key 为空时不带 Authorization 头。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not model:
            raise ValueError("model is required")
        self._model = model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
        )
        if api_key:
            # 注入 client 与自建 client 均携带认证头
            self._client.headers["Authorization"] = f"Bearer {api_key}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- Provider 协议 ----

    async def complete(
        self, request: ModelRequest, cancel: CancelSignal | None = None
    ) -> ModelResponse:
        if cancel is not None and cancel.is_cancelled:
            raise ProviderError(
                ProviderErrorKind.CANCELLED, "request cancelled before dispatch"
            )
        validate_model_request(request)
        payload = self._build_payload(request)
        try:
            response = await self._client.post("chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                ProviderErrorKind.TIMEOUT, f"request timed out: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE, f"http transport error: {exc}"
            ) from exc
        self._raise_for_status(response)
        return self._parse_response(response)

    def stream(
        self, request: ModelRequest, cancel: CancelSignal | None = None
    ) -> AsyncIterator[ModelDelta]:
        """显式不支持流式：避免把「未实现」伪装成可用能力。"""
        raise ProviderError(
            ProviderErrorKind.INVALID_REQUEST,
            "streaming is not implemented by OpenAICompatibleProvider;"
            " use complete() (runtime streaming=False)",
        )

    # ---- 请求编码 ----

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model if request.model and request.model != "default" else self._model,
            "messages": [self._encode_message(message) for message in request.messages],
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.json_schema),
                    },
                }
                for tool in request.tools
            ]
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    @staticmethod
    def _encode_message(message: ProviderMessage) -> dict[str, Any]:
        if message.role is ProviderMessageRole.ASSISTANT:
            encoded: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or None,
            }
            if message.tool_calls:
                encoded["tool_calls"] = [
                    {
                        "id": part.id,
                        "type": "function",
                        "function": {
                            "name": part.name,
                            "arguments": json.dumps(
                                dict(part.arguments), ensure_ascii=False, sort_keys=True
                            ),
                        },
                    }
                    for part in message.tool_calls
                ]
            return encoded
        if message.role is ProviderMessageRole.TOOL:
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        return {"role": str(message.role), "content": message.content}

    # ---- 响应解码 ----

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = response.text[:300]
        if status == 429:
            kind = ProviderErrorKind.RATE_LIMIT
        elif status == 408:
            kind = ProviderErrorKind.TIMEOUT
        elif status >= 500:
            kind = ProviderErrorKind.UNAVAILABLE
        else:
            kind = ProviderErrorKind.INVALID_REQUEST
        raise ProviderError(kind, f"http {status}: {detail}")

    def _parse_response(self, response: httpx.Response) -> ModelResponse:
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "response body is not valid JSON"
            ) from exc
        if not isinstance(body, Mapping):
            raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "response body must be an object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "response has no choices")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "choice must be an object")
        message = choice.get("message") or {}
        if not isinstance(message, Mapping):
            raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "choice.message must be an object")
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "choice.message.content must be a string"
            )
        tool_calls = self._parse_tool_calls(message.get("tool_calls") or ())
        finish_reason = choice.get("finish_reason")
        stop_reason = _FINISH_REASON_MAP.get(str(finish_reason))
        if stop_reason is None:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, f"unknown finish_reason {finish_reason!r}"
            )
        usage_raw = body.get("usage")
        usage: TokenUsage | None = None
        if isinstance(usage_raw, Mapping):
            usage = TokenUsage(
                input_tokens=_optional_int(usage_raw.get("prompt_tokens")),
                output_tokens=_optional_int(usage_raw.get("completion_tokens")),
            )
        response_model = ModelResponse(
            content=content,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            usage=usage,
        )
        validate_model_response(response_model)
        return response_model

    @staticmethod
    def _parse_tool_calls(raw_calls: Any) -> tuple[ToolCall, ...]:
        if not isinstance(raw_calls, (list, tuple)):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "message.tool_calls must be a list"
            )
        calls: list[ToolCall] = []
        for ordinal, raw in enumerate(raw_calls):
            if not isinstance(raw, Mapping):
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE, f"tool call #{ordinal} must be an object"
                )
            function = raw.get("function")
            if not isinstance(function, Mapping):
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE, f"tool call #{ordinal} has no function"
                )
            arguments_text = function.get("arguments") or "{}"
            try:
                arguments = json.loads(arguments_text)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE,
                    f"tool call #{ordinal} arguments are not valid JSON",
                ) from exc
            if not isinstance(arguments, dict):
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE,
                    f"tool call #{ordinal} arguments must be a JSON object",
                )
            call_id = raw.get("id")
            name = function.get("name")
            if not isinstance(call_id, str) or not call_id:
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE, f"tool call #{ordinal} has no id"
                )
            if not isinstance(name, str) or not name:
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE, f"tool call #{ordinal} has no name"
                )
            calls.append(ToolCall(id=call_id, name=name, arguments=arguments, ordinal=ordinal))
        return tuple(calls)


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
