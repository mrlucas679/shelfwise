from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from inspect import Parameter, isawaitable, signature
from ipaddress import ip_address
from time import perf_counter
from typing import Any, Protocol, get_type_hints
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOOL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_GEMMA_FUNCTION_TAG = re.compile(
    r"<start_function_call>\s*call:([A-Za-z0-9_-]+)\s*(\{.*?\})"
    r"\s*<end_function_call>",
    re.DOTALL,
)
_SCHEMA_KEYS = {
    "$anchor",
    "$defs",
    "$id",
    "$ref",
    "$schema",
    "additionalProperties",
    "allOf",
    "anyOf",
    "const",
    "default",
    "definitions",
    "deprecated",
    "description",
    "enum",
    "examples",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "items",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "pattern",
    "properties",
    "readOnly",
    "required",
    "title",
    "type",
    "uniqueItems",
    "writeOnly",
}
_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_STRING_FORMATS = {"date", "date-time", "email", "hostname", "ipv4", "ipv6", "time", "uri", "uuid"}


class ToolCallingError(RuntimeError):
    """Base failure for schema generation, parsing, or guarded execution."""


class ToolSchemaError(ToolCallingError):
    """Raised when a registered callable cannot produce a trustworthy schema."""


class ToolCallParseError(ToolCallingError):
    """Raised when a provider emits a malformed tool call."""


class ToolPolicyError(ToolCallingError):
    """Raised when a tool violates the read-only registry policy."""


class ToolExecutionError(ToolCallingError):
    """Raised when validated dispatch cannot complete successfully."""


class FinalAnswerValidationError(ToolCallingError):
    """Raised when the final model content is not schema-valid JSON."""


class UngroundedAnswerError(ToolCallingError):
    """Raised when a conclusion cites none of the real numbers its own tools returned.

    "Never invent numbers" in a system prompt is a request, not a guarantee - a model can
    call the calculator tool and then still write a conclusion that doesn't actually cite
    what it computed (either ignoring the result or restating it vaguely). This is the
    enforced check: if a tool returned a salient number, that number - not a paraphrase -
    must appear in the final conclusion text, or the answer is rejected.
    """


def extract_salient_numbers(value: Any) -> list[str]:
    """Collect numeric-looking leaf values worth requiring a citation for.

    Skips small integers (counts, flags, short IDs) that are too generic to prove a
    conclusion is actually grounded in this specific tool result rather than a lucky guess.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            if abs(node) >= 10 or (isinstance(node, float) and node != int(node)):
                found.append(str(node))
        elif isinstance(node, str):
            try:
                parsed = float(node)
            except ValueError:
                return
            if abs(parsed) >= 10 or parsed != int(parsed):
                found.append(node)

    walk(value)
    return found


def assert_conclusion_grounded_in_tool_results(
    conclusion: str,
    tool_executions: Sequence[Any],
) -> None:
    """Require the conclusion to cite at least one real *computed* number per tool call.

    Numbers that merely echo a call argument (e.g. the SKU the caller already passed in,
    like `get_supplier_ranking(sku="generated-sku")` echoing the same identifier back) are
    excluded - citing an
    identifier the caller already knew isn't evidence of real calculation, only citing a
    value the tool actually computed is. Found live: Gemma correctly grounded its verdict
    in genuine outputs (reorder quantities, profit figures) but was rejected for not also
    repeating the bare SKU digit, which was never the point of this check.

    Raises UngroundedAnswerError naming the first tool whose computed output went uncited.
    """
    for execution in tool_executions:
        input_numbers = set(extract_salient_numbers(execution.arguments))
        output_numbers = [
            number
            for number in extract_salient_numbers(execution.result)
            if number not in input_numbers
        ]
        if not output_numbers:
            continue
        if not any(number in conclusion for number in output_numbers):
            raise UngroundedAnswerError(
                f"conclusion never cites any computed value from {execution.name}'s result "
                f"(expected one of {output_numbers[:5]}) - answer may be ungrounded, not "
                "calculator-backed"
            )


class PlatformToolLike(Protocol):
    """Structural view of the existing backend PlatformTool registry entry."""

    name: str
    description: str
    read_only: bool
    fn: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Normalized OpenAI-compatible function call emitted by Gemma/vLLM."""

    call_id: str
    name: str
    arguments: dict[str, Any]

    def to_openai_dict(self) -> dict[str, Any]:
        """Return the assistant-message representation needed for the next turn."""
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(
                    self.arguments,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """One audited tool result with timing and correlation metadata."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    result: Any
    latency_ms: int
    correlation_id: str

    def to_tool_message(self) -> dict[str, Any]:
        """Return the OpenAI tool message fed back to the model."""
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "name": self.name,
            "content": json.dumps(
                self.result,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        """Expose bounded execution telemetry to orchestration callers."""
        return {
            "call_id": self.call_id,
            "name": self.name,
            "arguments": deepcopy(self.arguments),
            "result": deepcopy(self.result),
            "latency_ms": self.latency_ms,
            "correlation_id": self.correlation_id,
        }


class PlatformToolRegistry:
    """Schema and dispatch adapter over authoritative PlatformTool entries."""

    def __init__(self, tools: Sequence[PlatformToolLike]) -> None:
        entries: dict[str, PlatformToolLike] = {}
        for tool in tools:
            if not _TOOL_NAME.fullmatch(tool.name):
                raise ToolSchemaError(f"invalid OpenAI tool name: {tool.name!r}")
            if tool.name in entries:
                raise ToolSchemaError(f"duplicate registered tool: {tool.name}")
            if not tool.read_only:
                raise ToolPolicyError(f"refusing write-capable tool: {tool.name}")
            if not callable(tool.fn):
                raise ToolSchemaError(f"registered tool is not callable: {tool.name}")
            entries[tool.name] = tool
        self._tools = entries

    @property
    def names(self) -> tuple[str, ...]:
        """Return registered names in deterministic insertion order."""
        return tuple(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        """Generate provider schemas directly from current registry callables."""
        return [platform_tool_to_openai_schema(tool) for tool in self._tools.values()]

    async def execute(
        self,
        call: ToolCall,
        *,
        correlation_id: str,
        trusted_overrides: Mapping[str, Any] | None = None,
    ) -> ToolExecution:
        """Validate and execute one registered read-only tool call.

        trusted_overrides carries caller-authenticated context (e.g. tenant_id) that
        must win over whatever the model put in the call arguments. Found live against
        Gemma-4-E4B-it: the model invented tenant_id="default_tenant" for a tool whose
        signature exposed the parameter, which both broke the lookup and - worse - would
        have let a prompt-injected model read across tenants if the value were honored.
        Overrides only apply to parameters the tool signature actually declares.
        """
        if not correlation_id:
            raise ToolExecutionError("tool execution requires a correlation ID")
        tool = self._tools.get(call.name)
        if tool is None:
            raise ToolPolicyError(f"tool is not registered: {call.name}")
        if not tool.read_only:
            raise ToolPolicyError(f"refusing write-capable tool: {call.name}")
        merged_arguments = dict(call.arguments)
        if trusted_overrides:
            declared = set(signature(tool.fn).parameters)
            for key, value in trusted_overrides.items():
                if key in declared:
                    merged_arguments[key] = value
        arguments = _validated_arguments(tool, merged_arguments)
        started = perf_counter()
        try:
            result = tool.fn(**arguments)
            if isawaitable(result):
                result = await result
            json_result = _json_value(result)
            json.dumps(json_result, allow_nan=False)
        except ToolCallingError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"registered tool failed: {call.name}") from exc
        trace_arguments = _json_value(arguments)
        if not isinstance(trace_arguments, dict):
            raise ToolExecutionError(f"registered tool arguments are invalid: {call.name}")
        return ToolExecution(
            call_id=call.call_id,
            name=call.name,
            arguments=trace_arguments,
            result=json_result,
            latency_ms=_elapsed_ms(started),
            correlation_id=correlation_id,
        )


def platform_tool_to_openai_schema(tool: PlatformToolLike) -> dict[str, Any]:
    """Derive one OpenAI function schema from a PlatformTool function signature."""
    if not tool.read_only:
        raise ToolPolicyError(f"refusing write-capable tool: {tool.name}")
    try:
        type_hints = get_type_hints(tool.fn)
        fn_signature = signature(tool.fn)
    except (NameError, TypeError, ValueError) as exc:
        raise ToolSchemaError(f"cannot inspect registered tool: {tool.name}") from exc

    properties: dict[str, Any] = {}
    definitions: dict[str, Any] = {}
    required: list[str] = []
    for parameter in fn_signature.parameters.values():
        if parameter.kind in {Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD}:
            raise ToolSchemaError(f"variadic parameters are not supported: {tool.name}")
        if parameter.kind is Parameter.POSITIONAL_ONLY:
            raise ToolSchemaError(f"positional-only parameters are not supported: {tool.name}")
        annotation = type_hints.get(parameter.name, parameter.annotation)
        if annotation is Parameter.empty:
            raise ToolSchemaError(
                f"registered tool parameter lacks a type annotation: {tool.name}.{parameter.name}"
            )
        try:
            property_schema = TypeAdapter(annotation).json_schema(mode="validation")
        except (TypeError, ValueError) as exc:
            raise ToolSchemaError(
                f"cannot generate schema for: {tool.name}.{parameter.name}"
            ) from exc
        nested_definitions = property_schema.pop("$defs", {})
        for definition_name, definition_schema in nested_definitions.items():
            if (
                definition_name in definitions
                and definitions[definition_name] != definition_schema
            ):
                raise ToolSchemaError(
                    f"conflicting schema definition in registered tool: {tool.name}"
                )
            definitions[definition_name] = definition_schema
        if parameter.default is Parameter.empty:
            required.append(parameter.name)
        else:
            property_schema["default"] = _json_value(parameter.default)
        properties[parameter.name] = property_schema

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    if definitions:
        parameters["$defs"] = definitions
    try:
        json.dumps(parameters, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolSchemaError(f"tool schema is not JSON serializable: {tool.name}") from exc
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def parse_tool_calls(message: Mapping[str, Any]) -> list[ToolCall]:
    """Parse OpenAI, legacy vLLM, and tagged Gemma tool-call messages."""
    raw_calls = message.get("tool_calls")
    if raw_calls is not None and not isinstance(raw_calls, list):
        raise ToolCallParseError("tool_calls must be a list")
    if raw_calls:
        return [_parse_call(item, index) for index, item in enumerate(raw_calls)]

    legacy_call = message.get("function_call")
    if legacy_call is not None:
        return [_parse_call({"function": legacy_call}, 0)]

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return []
    tagged = _TOOL_TAG.findall(content)
    if tagged:
        calls: list[ToolCall] = []
        for payload in tagged:
            parsed = _parse_json(payload, context="tagged Gemma tool call")
            values = parsed if isinstance(parsed, list) else [parsed]
            calls.extend(_parse_call(item, len(calls)) for item in values)
        return calls

    gemma_calls = _GEMMA_FUNCTION_TAG.findall(content)
    return [
        ToolCall(
            call_id=f"call_{index + 1}",
            name=name,
            arguments=_arguments_object(arguments),
        )
        for index, (name, arguments) in enumerate(gemma_calls)
    ]


def openai_json_schema_response_format(
    *,
    name: str,
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the strict response_format sent with final-answer requests."""
    if not _SCHEMA_NAME.fullmatch(name):
        raise ToolSchemaError(f"invalid response schema name: {name!r}")
    if not isinstance(schema, Mapping):
        raise ToolSchemaError("final answer schema must be an object")
    normalized_schema = deepcopy(dict(schema))
    _validate_schema_definition(normalized_schema)
    try:
        json.dumps(normalized_schema, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolSchemaError("final answer schema is not JSON serializable") from exc
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": normalized_schema,
        },
    }


def parse_and_validate_json_answer(content: str, schema: Mapping[str, Any]) -> Any:
    """Parse strict JSON and validate it locally before accepting a final answer."""
    if not isinstance(content, str) or not content.strip():
        raise FinalAnswerValidationError("final answer is empty")
    try:
        value = _parse_json(content, context="final answer")
    except ToolCallParseError as exc:
        raise FinalAnswerValidationError("final answer is not valid JSON") from exc
    normalized_schema = dict(schema)
    _validate_schema_definition(normalized_schema)
    _validate_schema(value, normalized_schema, root=normalized_schema, path="$")
    return value


def _parse_call(raw_call: Any, index: int) -> ToolCall:
    """Normalize one provider call object and decode its JSON arguments."""
    if not isinstance(raw_call, Mapping):
        raise ToolCallParseError("tool call must be an object")
    function = raw_call.get("function", raw_call)
    if not isinstance(function, Mapping):
        raise ToolCallParseError("tool call function must be an object")
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise ToolCallParseError("tool call is missing a function name")
    if not _TOOL_NAME.fullmatch(name):
        raise ToolCallParseError("tool call contains an invalid function name")
    call_id = raw_call.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"call_{index + 1}"
    elif len(call_id) > 256 or not call_id.isprintable():
        raise ToolCallParseError("tool call contains an invalid call ID")
    arguments = function.get("arguments", function.get("parameters", {}))
    return ToolCall(call_id=call_id, name=name, arguments=_arguments_object(arguments))


def _arguments_object(arguments: Any) -> dict[str, Any]:
    """Decode JSON argument text and require an object payload."""
    parsed = (
        _parse_json(arguments, context="tool arguments")
        if isinstance(arguments, str)
        else arguments
    )
    if not isinstance(parsed, Mapping):
        raise ToolCallParseError("tool arguments must be a JSON object")
    return dict(parsed)


def _validated_arguments(
    tool: PlatformToolLike,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind named arguments and validate values against callable annotations."""
    fn_signature = signature(tool.fn)
    try:
        type_hints = get_type_hints(tool.fn)
        bound = fn_signature.bind(**dict(arguments))
    except (NameError, TypeError) as exc:
        raise ToolExecutionError(f"invalid arguments for registered tool: {tool.name}") from exc
    bound.apply_defaults()
    provided_names = set(arguments)
    validated: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        parameter = fn_signature.parameters[name]
        annotation = type_hints.get(name, parameter.annotation)
        if annotation is Parameter.empty:
            raise ToolExecutionError(f"untyped registered tool parameter: {tool.name}.{name}")
        try:
            adapter = TypeAdapter(annotation)
            if name in provided_names:
                encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
                validated[name] = adapter.validate_json(encoded, strict=True)
            else:
                validated[name] = adapter.validate_python(value, strict=True)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ToolExecutionError(
                f"invalid arguments for registered tool: {tool.name}"
            ) from exc
    return validated


def _parse_json(value: str, *, context: str) -> Any:
    """Parse RFC-style JSON while rejecting duplicate keys and NaN constants."""
    try:
        return json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError) as exc:
        raise ToolCallParseError(f"invalid JSON in {context}") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject non-standard NaN and infinity tokens accepted by json.loads."""
    raise ValueError(f"invalid JSON constant: {value}")


def _json_value(value: Any) -> Any:
    """Convert common tool values to deterministic JSON-compatible data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _validate_schema_definition(schema: Any, *, path: str = "$") -> None:
    """Reject schema keywords that the local validator cannot enforce."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise ToolSchemaError(f"{path} must be a JSON Schema object or boolean")
    unsupported = [
        key for key in schema if key not in _SCHEMA_KEYS and not str(key).startswith("x-")
    ]
    if unsupported:
        raise ToolSchemaError(f"unsupported final answer schema keyword: {unsupported[0]}")
    expected_type = schema.get("type")
    expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
    if expected_type is not None and (
        not expected_types or any(item not in _JSON_TYPES for item in expected_types)
    ):
        raise ToolSchemaError(f"{path}.type contains an unsupported JSON type")
    reference = schema.get("$ref")
    if reference is not None and (not isinstance(reference, str) or not reference.startswith("#/")):
        raise ToolSchemaError(f"{path} contains an unsupported schema reference")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(item, str) for item in required)
    ):
        raise ToolSchemaError(f"{path}.required must be an array of strings")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ToolSchemaError(f"{path}.enum must be a non-empty array")
    string_format = schema.get("format")
    if string_format is not None and string_format not in _STRING_FORMATS:
        raise ToolSchemaError(f"unsupported final answer string format: {string_format}")
    pattern = schema.get("pattern")
    if pattern is not None:
        try:
            re.compile(pattern)
        except (re.error, TypeError) as exc:
            raise ToolSchemaError(f"{path}.pattern is not a valid regular expression") from exc

    for container_name in ("$defs", "definitions", "properties"):
        container = schema.get(container_name, {})
        if not isinstance(container, Mapping):
            raise ToolSchemaError(f"{path}.{container_name} must be an object")
        for name, child in container.items():
            _validate_schema_definition(child, path=f"{path}.{container_name}.{name}")
    for sequence_name in ("allOf", "anyOf", "oneOf"):
        sequence = schema.get(sequence_name, [])
        if not isinstance(sequence, list) or (sequence_name in schema and not sequence):
            raise ToolSchemaError(f"{path}.{sequence_name} must be a non-empty array")
        for index, child in enumerate(sequence):
            _validate_schema_definition(child, path=f"{path}.{sequence_name}[{index}]")
    for child_name in ("additionalProperties", "items", "not"):
        child = schema.get(child_name)
        if child is not None:
            _validate_schema_definition(child, path=f"{path}.{child_name}")


def _validate_schema(value: Any, schema: Any, *, root: Mapping[str, Any], path: str) -> None:
    """Validate the JSON Schema subset emitted and consumed by this runtime."""
    if schema is True:
        return
    if schema is False or not isinstance(schema, Mapping):
        raise FinalAnswerValidationError(f"{path} is rejected by the final answer schema")
    if "$ref" in schema:
        target = _resolve_local_ref(schema["$ref"], root)
        _validate_schema(value, target, root=root, path=path)

    for candidate in schema.get("allOf", []):
        _validate_schema(value, candidate, root=root, path=path)
    if "anyOf" in schema and not any(
        _schema_matches(value, candidate, root=root, path=path)
        for candidate in schema["anyOf"]
    ):
        raise FinalAnswerValidationError(f"{path} does not match any allowed schema")
    if "oneOf" in schema:
        matches = sum(
            _schema_matches(value, candidate, root=root, path=path)
            for candidate in schema["oneOf"]
        )
        if matches != 1:
            raise FinalAnswerValidationError(f"{path} must match exactly one schema")
    if "not" in schema and _schema_matches(value, schema["not"], root=root, path=path):
        raise FinalAnswerValidationError(f"{path} matches a forbidden schema")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise FinalAnswerValidationError(f"{path} does not match the required constant")
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        raise FinalAnswerValidationError(f"{path} is not an allowed value")

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        raise FinalAnswerValidationError(f"{path} has the wrong JSON type")
    if isinstance(value, dict):
        _validate_object(value, schema, root=root, path=path)
    elif isinstance(value, list):
        _validate_array(value, schema, root=root, path=path)
    elif isinstance(value, str):
        _validate_string(value, schema, path=path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _validate_number(value, schema, path=path)


def _schema_matches(value: Any, schema: Any, *, root: Mapping[str, Any], path: str) -> bool:
    """Return whether one schema branch accepts a value."""
    try:
        _validate_schema(value, schema, root=root, path=path)
    except FinalAnswerValidationError:
        return False
    return True


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without treating booleans as numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _resolve_local_ref(reference: Any, root: Mapping[str, Any]) -> Any:
    """Resolve local JSON pointers used by Pydantic-generated schemas."""
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise FinalAnswerValidationError("final answer schema contains an unsupported reference")
    current: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise FinalAnswerValidationError("final answer schema reference cannot be resolved")
        current = current[part]
    return current


def _matches_type(value: Any, expected: Any) -> bool:
    """Apply JSON type semantics without Python's bool-is-int ambiguity."""
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    checks = {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }
    return checks.get(expected, False)


def _validate_object(
    value: dict[str, Any],
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    path: str,
) -> None:
    """Validate object properties, requirements, and extra-field policy."""
    required = schema.get("required", [])
    missing = [name for name in required if name not in value]
    if missing:
        raise FinalAnswerValidationError(f"{path} is missing required field: {missing[0]}")
    properties = schema.get("properties", {})
    for name, item in value.items():
        child_path = f"{path}.{name}"
        if name in properties:
            _validate_schema(item, properties[name], root=root, path=child_path)
            continue
        additional = schema.get("additionalProperties", True)
        if additional is False:
            raise FinalAnswerValidationError(f"{child_path} is not allowed")
        if isinstance(additional, Mapping):
            _validate_schema(item, additional, root=root, path=child_path)
    if len(value) < schema.get("minProperties", 0):
        raise FinalAnswerValidationError(f"{path} has too few properties")
    if "maxProperties" in schema and len(value) > schema["maxProperties"]:
        raise FinalAnswerValidationError(f"{path} has too many properties")


def _validate_array(
    value: list[Any],
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    path: str,
) -> None:
    """Validate array length, uniqueness, and item schemas."""
    if len(value) < schema.get("minItems", 0):
        raise FinalAnswerValidationError(f"{path} has too few items")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise FinalAnswerValidationError(f"{path} has too many items")
    if schema.get("uniqueItems"):
        encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
        if len(encoded) != len(set(encoded)):
            raise FinalAnswerValidationError(f"{path} contains duplicate items")
    item_schema = schema.get("items")
    if item_schema is not None:
        for index, item in enumerate(value):
            _validate_schema(item, item_schema, root=root, path=f"{path}[{index}]")


def _validate_string(value: str, schema: Mapping[str, Any], *, path: str) -> None:
    """Validate string length and regular-expression constraints."""
    if len(value) < schema.get("minLength", 0):
        raise FinalAnswerValidationError(f"{path} is too short")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise FinalAnswerValidationError(f"{path} is too long")
    pattern = schema.get("pattern")
    if pattern is not None and re.search(pattern, value) is None:
        raise FinalAnswerValidationError(f"{path} does not match the required pattern")
    string_format = schema.get("format")
    if string_format is not None and not _matches_string_format(value, string_format):
        raise FinalAnswerValidationError(f"{path} does not match the required format")


def _matches_string_format(value: str, string_format: str) -> bool:
    """Validate common Pydantic/OpenAI string formats without another dependency."""
    try:
        if string_format == "date":
            date.fromisoformat(value)
        elif string_format == "date-time":
            if "T" not in value.upper():
                return False
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif string_format == "time":
            time.fromisoformat(value.replace("Z", "+00:00"))
        elif string_format == "uuid":
            UUID(value)
        elif string_format == "uri":
            if not urlsplit(value).scheme:
                return False
        elif string_format == "email":
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is None:
                return False
        elif string_format == "hostname":
            labels = value.rstrip(".").split(".")
            if not labels or any(not _valid_hostname_label(label) for label in labels):
                return False
        elif string_format in {"ipv4", "ipv6"}:
            expected_version = 4 if string_format == "ipv4" else 6
            if ip_address(value).version != expected_version:
                return False
    except (TypeError, ValueError):
        return False
    return True


def _valid_hostname_label(label: str) -> bool:
    """Return whether one DNS hostname label is syntactically safe."""
    return bool(
        label
        and len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
    )


def _validate_number(value: int | float, schema: Mapping[str, Any], *, path: str) -> None:
    """Validate numeric bounds and multiples."""
    if "minimum" in schema and value < schema["minimum"]:
        raise FinalAnswerValidationError(f"{path} is below the minimum")
    if "maximum" in schema and value > schema["maximum"]:
        raise FinalAnswerValidationError(f"{path} is above the maximum")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        raise FinalAnswerValidationError(f"{path} is below the exclusive minimum")
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        raise FinalAnswerValidationError(f"{path} is above the exclusive maximum")
    if "multipleOf" in schema:
        divisor = Decimal(str(schema["multipleOf"]))
        if divisor == 0 or Decimal(str(value)) % divisor != 0:
            raise FinalAnswerValidationError(f"{path} is not an allowed multiple")


def _elapsed_ms(started: float) -> int:
    """Return non-negative whole milliseconds for one operation."""
    return max(0, int((perf_counter() - started) * 1000))
