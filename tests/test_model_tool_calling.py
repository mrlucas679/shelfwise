from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from shelfwise_inference.client import OpenAICompatibleInferenceClient
from shelfwise_inference.config import InferenceConfig, ProviderKind
from shelfwise_inference.tool_calling import (
    FinalAnswerValidationError,
    PlatformToolRegistry,
    ToolCall,
    ToolCallParseError,
    ToolPolicyError,
    ToolSchemaError,
    parse_and_validate_json_answer,
    parse_tool_calls,
)


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    read_only: bool
    fn: Any


@dataclass(frozen=True)
class _Warehouse:
    warehouse_id: int


@dataclass(frozen=True)
class _StockQuery:
    sku: str
    warehouses: list[_Warehouse]


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


async def _read_stock(
    sku: str,
    limit: int = 2,
    include_expired: bool = False,
) -> dict[str, Any]:
    return {"sku": sku, "limit": limit, "include_expired": include_expired}


async def _read_nested_stock(query: _StockQuery) -> dict[str, Any]:
    return {
        "sku": query.sku,
        "warehouses": [warehouse.warehouse_id for warehouse in query.warehouses],
    }


def _read_tool() -> _Tool:
    return _Tool("get_stock", "Read stock without writing.", True, _read_stock)


def _config() -> InferenceConfig:
    return InferenceConfig(
        provider=ProviderKind.VLLM_MI300X,
        base_url="https://vllm.example/v1",
        routine_model="gemma-routine",
        strong_model="gemma-strong",
        api_key="test-key",
        api_key_present=True,
    )


def test_platform_registry_generates_openai_schema_from_signature() -> None:
    schema = PlatformToolRegistry([_read_tool()]).openai_tools()[0]
    parameters = schema["function"]["parameters"]

    assert schema["function"]["name"] == "get_stock"
    assert parameters["required"] == ["sku"]
    assert parameters["properties"]["sku"]["type"] == "string"
    assert parameters["properties"]["limit"] == {"default": 2, "type": "integer"}
    assert parameters["additionalProperties"] is False


def test_nested_signature_schema_keeps_root_refs_and_executes_typed_arguments() -> None:
    tool = _Tool("query_stock", "Read stock by warehouse.", True, _read_nested_stock)
    registry = PlatformToolRegistry([tool])
    parameters = registry.openai_tools()[0]["function"]["parameters"]
    execution = asyncio.run(
        registry.execute(
            ToolCall(
                "call_nested",
                "query_stock",
                {
                    "query": {
                        "sku": "4011",
                        "warehouses": [{"warehouse_id": 1}, {"warehouse_id": 3}],
                    }
                },
            ),
            correlation_id="corr-nested",
        )
    )

    warehouse_items = parameters["properties"]["query"]["properties"]["warehouses"]["items"]
    assert warehouse_items["$ref"].startswith("#/$defs/")
    assert parameters["$defs"]
    assert execution.result == {"sku": "4011", "warehouses": [1, 3]}
    assert execution.arguments["query"] == {
        "sku": "4011",
        "warehouses": [{"warehouse_id": 1}, {"warehouse_id": 3}],
    }


def test_registry_executes_only_registered_read_only_tools() -> None:
    registry = PlatformToolRegistry([_read_tool()])
    execution = asyncio.run(
        registry.execute(
            ToolCall("call_1", "get_stock", {"sku": "4011"}),
            correlation_id="corr-1",
        )
    )

    assert execution.result == {"sku": "4011", "limit": 2, "include_expired": False}
    assert execution.correlation_id == "corr-1"
    assert json.loads(execution.to_tool_message()["content"])["sku"] == "4011"

    with pytest.raises(ToolPolicyError, match="not registered"):
        asyncio.run(
            registry.execute(
                ToolCall("call_2", "delete_stock", {}),
                correlation_id="corr-1",
            )
        )

    with pytest.raises(ToolPolicyError, match="write-capable"):
        PlatformToolRegistry([_Tool("delete_stock", "Write stock.", False, _read_stock)])


def test_parser_accepts_vllm_and_tagged_gemma_calls_but_rejects_bad_json() -> None:
    vllm = parse_tool_calls(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "provider-call",
                    "type": "function",
                    "function": {"name": "get_stock", "arguments": '{"sku":"4011"}'},
                }
            ],
        }
    )
    tagged = parse_tool_calls(
        {
            "role": "assistant",
            "content": '<tool_call>{"name":"get_stock","arguments":{"sku":"4012"}}'
            "</tool_call>",
        }
    )

    assert vllm == [ToolCall("provider-call", "get_stock", {"sku": "4011"})]
    assert tagged == [ToolCall("call_1", "get_stock", {"sku": "4012"})]
    with pytest.raises(ToolCallParseError, match="invalid JSON"):
        parse_tool_calls(
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "get_stock", "arguments": "{"}}],
            }
        )


def test_final_answer_must_be_strict_schema_valid_json() -> None:
    schema = {
        "type": "object",
        "properties": {
            "risk": {"type": "string", "enum": ["low", "high"]},
            "units": {"type": "integer", "minimum": 0},
        },
        "required": ["risk", "units"],
        "additionalProperties": False,
    }

    assert parse_and_validate_json_answer('{"risk":"high","units":4}', schema) == {
        "risk": "high",
        "units": 4,
    }
    with pytest.raises(FinalAnswerValidationError, match="not allowed"):
        parse_and_validate_json_answer(
            '{"risk":"high","units":4,"unknown":true}',
            schema,
        )
    with pytest.raises(FinalAnswerValidationError, match="not valid JSON"):
        parse_and_validate_json_answer('```json\n{"risk":"high","units":4}\n```', schema)
    with pytest.raises(ToolSchemaError, match="unsupported final answer schema keyword"):
        parse_and_validate_json_answer("[]", {"type": "array", "contains": {"type": "string"}})


def test_generic_client_submits_messages_tools_and_preserves_recorder(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    body = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_stock",
                                    "arguments": '{"sku":"4011"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 17, "completion_tokens": 5},
        }
    ).encode()

    def fake_urlopen(request, timeout=30):
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return _FakeHttpResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    recorded: list[dict[str, Any]] = []
    client = OpenAICompatibleInferenceClient(_config(), recorder=recorded.append)
    tools = PlatformToolRegistry([_read_tool()]).openai_tools()
    result = client.chat_completions(
        agent="inventory",
        messages=[{"role": "user", "content": "Check 4011"}],
        tools=tools,
        response_format={"type": "json_object"},
        correlation_id="corr-client",
    )

    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == "auto"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert result.message is not None and result.message["tool_calls"][0]["id"] == "call_1"
    assert result.finish_reason == "tool_calls"
    assert result.input_tokens == 17
    assert recorded[0]["correlation_id"] == "corr-client"
