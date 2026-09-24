"""阶段 23 单测：OpenAI 兼容 Provider（真实模型接入）。

全部使用 httpx.MockTransport 离线验证：请求编码、响应归一、
错误映射（429/5xx/4xx/超时/结构不符）与 cancel 语义；并验证
「真实 Provider 形状的响应可驱动完整 Runtime」。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from coding_agent.bootstrap import build_runtime
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.ports.provider import (
    ModelRequest,
    ProviderError,
    ProviderErrorKind,
    ProviderMessage,
    ProviderMessageRole,
    ProviderToolCallPart,
    ToolDefinition,
    TokenUsage,
)
from coding_agent.providers.openai_compat import OpenAICompatibleProvider

Handler = Callable[[httpx.Request], httpx.Response]


def make_provider(handler: Handler, **kwargs) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://model.invalid/v1/",
    )
    return OpenAICompatibleProvider(
        base_url="https://model.invalid/v1",
        api_key="test-key",
        model="frozen-model",
        client=client,
        **kwargs,
    )


def json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def completion(
    *,
    content: str | None = "done",
    finish_reason: str = "stop",
    tool_calls: list[dict] | None = None,
    usage: dict | None = None,
) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict = {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def function_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def request_with_tools() -> ModelRequest:
    return ModelRequest(
        messages=(
            ProviderMessage(role=ProviderMessageRole.SYSTEM, content="sys"),
            ProviderMessage(role=ProviderMessageRole.USER, content="task"),
            ProviderMessage(
                role=ProviderMessageRole.ASSISTANT,
                content="",
                tool_calls=(ProviderToolCallPart(id="call_1", name="read", arguments={"path": "a.py"}),),
            ),
            ProviderMessage(
                role=ProviderMessageRole.TOOL,
                content="file body",
                tool_call_id="call_1",
            ),
        ),
        tools=(
            ToolDefinition(
                name="read",
                description="Read a file",
                json_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        model="frozen-model",
        max_output_tokens=512,
    )


class TestRequestEncoding:
    async def test_payload_shape(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["json"] = json.loads(request.content.decode("utf-8"))
            return json_response(completion(content="ok"))

        provider = make_provider(handler)
        response = await provider.complete(request_with_tools())
        assert response.content == "ok"
        assert response.stop_reason is StopReason.END_TURN

        assert captured["url"] == "https://model.invalid/v1/chat/completions"
        assert captured["auth"] == "Bearer test-key"
        payload = captured["json"]
        assert payload["model"] == "frozen-model"
        assert payload["max_tokens"] == 512
        assert payload["tools"][0]["function"]["name"] == "read"
        messages = payload["messages"]
        assert messages[0] == {"role": "system", "content": "sys"}
        assert messages[1] == {"role": "user", "content": "task"}
        assistant = messages[2]
        assert assistant["role"] == "assistant"
        assert assistant["content"] is None
        call = assistant["tool_calls"][0]
        assert call["id"] == "call_1"
        assert json.loads(call["function"]["arguments"]) == {"path": "a.py"}
        assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "file body"}


class TestResponseDecoding:
    async def test_tool_calls_normalized_with_usage(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(
                completion(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[
                        function_call("call_a", "read", {"path": "a.py"}),
                        function_call("call_b", "bash", {"command": "pytest"}),
                    ],
                    usage={"prompt_tokens": 120, "completion_tokens": 30},
                )
            )

        provider = make_provider(handler)
        response = await provider.complete(ModelRequest(messages=()))
        assert response.stop_reason is StopReason.TOOL_CALLS
        assert [str(call.id) for call in response.tool_calls] == ["call_a", "call_b"]
        assert [call.ordinal for call in response.tool_calls] == [0, 1]
        assert response.tool_calls[1].arguments == {"command": "pytest"}
        assert response.usage == TokenUsage(input_tokens=120, output_tokens=30)

    async def test_length_maps_to_max_tokens(self) -> None:
        provider = make_provider(
            lambda request: json_response(completion(content="half", finish_reason="length"))
        )
        response = await provider.complete(ModelRequest(messages=()))
        assert response.stop_reason is StopReason.MAX_TOKENS

    async def test_usage_missing_stays_none(self) -> None:
        provider = make_provider(lambda request: json_response(completion()))
        response = await provider.complete(ModelRequest(messages=()))
        assert response.usage is None


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, ProviderErrorKind.RATE_LIMIT),
            (500, ProviderErrorKind.UNAVAILABLE),
            (503, ProviderErrorKind.UNAVAILABLE),
            (400, ProviderErrorKind.INVALID_REQUEST),
            (401, ProviderErrorKind.INVALID_REQUEST),
        ],
    )
    async def test_http_status_mapping(self, status: int, expected: ProviderErrorKind) -> None:
        provider = make_provider(
            lambda request: httpx.Response(status, text="upstream detail")
        )
        with pytest.raises(ProviderError) as info:
            await provider.complete(ModelRequest(messages=()))
        assert info.value.kind is expected
        assert "upstream detail" in str(info.value)

    async def test_timeout_mapping(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("read timeout")

        provider = make_provider(handler)
        with pytest.raises(ProviderError) as info:
            await provider.complete(ModelRequest(messages=()))
        assert info.value.kind is ProviderErrorKind.TIMEOUT

    async def test_malformed_body_invalid_response(self) -> None:
        provider = make_provider(
            lambda request: httpx.Response(200, text="not json")
        )
        with pytest.raises(ProviderError) as info:
            await provider.complete(ModelRequest(messages=()))
        assert info.value.kind is ProviderErrorKind.INVALID_RESPONSE

    async def test_unknown_finish_reason_invalid_response(self) -> None:
        provider = make_provider(
            lambda request: json_response(completion(finish_reason="content_filter"))
        )
        with pytest.raises(ProviderError) as info:
            await provider.complete(ModelRequest(messages=()))
        assert info.value.kind is ProviderErrorKind.INVALID_RESPONSE

    async def test_non_json_tool_arguments_invalid_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(
                completion(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{not json"},
                        }
                    ],
                )
            )

        provider = make_provider(handler)
        with pytest.raises(ProviderError) as info:
            await provider.complete(ModelRequest(messages=()))
        assert info.value.kind is ProviderErrorKind.INVALID_RESPONSE

    async def test_tool_calls_with_stop_finish_rejected(self) -> None:
        # stop_reason=stop 但带 tool_calls：同契约校验拒绝（与 Fake 一致的语义）
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(
                completion(
                    finish_reason="stop",
                    tool_calls=[function_call("call_1", "read", {"path": "a.py"})],
                )
            )

        provider = make_provider(handler)
        with pytest.raises(ProviderError) as info:
            await provider.complete(ModelRequest(messages=()))
        assert info.value.kind is ProviderErrorKind.INVALID_RESPONSE


class TestCancelAndStream:
    async def test_cancelled_before_dispatch(self) -> None:
        class Cancelled:
            @property
            def is_cancelled(self) -> bool:
                return True

        provider = make_provider(lambda request: json_response(completion()))
        with pytest.raises(ProviderError) as info:
            await provider.complete(ModelRequest(messages=()), Cancelled())
        assert info.value.kind is ProviderErrorKind.CANCELLED

    async def test_stream_explicitly_unsupported(self) -> None:
        provider = make_provider(lambda request: json_response(completion()))
        with pytest.raises(ProviderError) as info:
            provider.stream(ModelRequest(messages=()))
        assert info.value.kind is ProviderErrorKind.INVALID_REQUEST
        assert "streaming is not implemented" in str(info.value)


class TestRuntimeIntegration:
    async def test_real_provider_shape_drives_full_runtime(self, tmp_path: Path) -> None:
        """真实 Provider 形态（chat.completions JSON）驱动完整 Runtime 与编码工具。"""
        (tmp_path / "a.py").write_text("print('hi')\n", encoding="utf-8")
        step = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            step["count"] += 1
            if step["count"] == 1:
                return json_response(
                    completion(
                        content=None,
                        finish_reason="tool_calls",
                        tool_calls=[function_call("call_1", "read", {"path": "a.py"})],
                        usage={"prompt_tokens": 50, "completion_tokens": 10},
                    )
                )
            return json_response(
                completion(content="file contains a print statement", finish_reason="stop")
            )

        provider = make_provider(handler)
        runtime = build_runtime(provider=provider)
        result = await runtime.run("inspect a.py", tmp_path)

        assert result.status is RunStatus.FINISHED
        assert result.final_text == "file contains a print statement"
        assert result.tool_calls == 1
        assert step["count"] == 2
