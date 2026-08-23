"""Codex command harness adapter with structured JSONL normalization."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from .harness_capabilities import resolve_capabilities
from .harness_process import (ProcessOutcome, ProcessSpec, ensure_executable,
                              require_effort, run_jsonl_process)
from .harness_types import (HarnessConfigError, HarnessEvent, HarnessName,
                            HarnessProcessError, HarnessProfile, HarnessProtocolError,
                            HarnessRequest, HarnessResult, ProcessCallback,
                            UsageBreakdown, validate_session_id)

CODEX_PATH = os.environ.get("CODEX_PATH", "codex")
CODEX_EFFORTS: frozenset[str] = frozenset({"minimal", "low", "medium", "high", "xhigh"})
RESULT_SNIPPET_CHARS = 20_000
MAPPING_SCHEMA_KEYWORDS = frozenset({"properties", "$defs", "definitions", "patternProperties",
                                     "dependentSchemas"})
SINGLE_SCHEMA_KEYWORDS = frozenset({"contains", "not", "if", "then", "else", "propertyNames"})
LIST_SCHEMA_KEYWORDS = frozenset({"prefixItems", "allOf", "anyOf", "oneOf"})


def _object_map(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _int_value(value: object) -> int:
    if not isinstance(value, int | float):
        return 0
    return int(value)


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _item_text(item: Mapping[str, object]) -> str:
    text = item.get("text")
    if not isinstance(text, str):
        return ""
    return text


def _failure_detail(event: Mapping[str, object]) -> str:
    error = _object_map(event.get("error"))
    message = error.get("message")
    if isinstance(message, str) and message:
        return message
    return _failure_identity(error)


def _failure_identity(error: Mapping[str, object]) -> str:
    values = [error.get("code"), error.get("type")]
    labels = [str(value) for value in values if isinstance(value, str) and value]
    return ": ".join(labels) or "Codex reported a failed turn"


def _event_name(item: Mapping[str, object]) -> str:
    item_type = str(item.get("type") or "tool")
    command = item.get("command")
    if item_type == "command_execution" and isinstance(command, str):
        return f"command_execution: {_clip(command, 80)}"
    return item_type


def _tool_event(item: Mapping[str, object]) -> HarnessEvent | None:
    item_type = item.get("type")
    if item_type not in {"command_execution", "file_change"}:
        return None
    payload = _tool_payload(item, str(item_type))
    return HarnessEvent(name=_event_name(item), payload=payload)


def _tool_payload(item: Mapping[str, object], item_type: str) -> dict[str, object]:
    payload: dict[str, object] = {"tool": item_type, "tool_call_id": str(item.get("id") or "")}
    if item_type == "command_execution":
        _add_command_payload(payload, item)
    else:
        _add_file_change_payload(payload, item)
    return payload


def _add_command_payload(payload: dict[str, object], item: Mapping[str, object]) -> None:
    command = item.get("command")
    exit_code = item.get("exit_code")
    payload["command"] = command if isinstance(command, str) else ""
    payload["ok"] = _int_value(exit_code) == 0
    output = item.get("aggregated_output")
    if isinstance(output, str) and output:
        payload["result_snippet"] = _clip(output, RESULT_SNIPPET_CHARS)


def _add_file_change_payload(payload: dict[str, object], item: Mapping[str, object]) -> None:
    changes = item.get("changes")
    payload["ok"] = item.get("status") != "failed"
    payload["changes"] = _file_changes(changes)


def _file_changes(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object_map(change) for change in value]


def _set_usage(usage: UsageBreakdown, source: Mapping[str, object]) -> None:
    raw_input = _int_value(source.get("input_tokens"))
    usage.output_tokens = _int_value(source.get("output_tokens"))
    usage.cache_read_tokens = _int_value(source.get("cached_input_tokens"))
    usage.cache_write_tokens = _cache_write_tokens(source)
    usage.reasoning_tokens = _int_value(source.get("reasoning_output_tokens"))
    usage.input_tokens = max(raw_input - usage.cache_read_tokens - usage.cache_write_tokens, 0)
    usage.total_tokens = _usage_total(usage, source)


def _cache_write_tokens(source: Mapping[str, object]) -> int:
    value = source.get("cache_write_input_tokens")
    if value is None:
        value = source.get("cache_write_tokens")
    return _int_value(value)


def _usage_total(usage: UsageBreakdown, source: Mapping[str, object]) -> int:
    reported = _int_value(source.get("total_tokens"))
    if reported:
        return reported
    raw_input = _int_value(source.get("input_tokens"))
    return raw_input + usage.output_tokens


class CodexEventParser:
    """Collect one Codex thread result and forward completed tool items.

    Example: ``parser.observe({"type": "thread.started"})`` stores its thread ID.
    """

    def __init__(self, result: HarnessResult,
                 on_event: Callable[[HarnessEvent], None] | None) -> None:
        self._result = result
        self._on_event = on_event
        self.has_final_message = False
        self.failure_detail = ""

    def observe(self, event: dict[str, object]) -> None:
        """Normalize one protocol event from the command stream.

        Example: ``parser.observe({"type": "turn.completed"})`` records usage.
        """
        event_type = event.get("type")
        if event_type == "thread.started":
            self._record_thread(event)
        if event_type == "item.completed":
            self._record_item(event)
        if event_type == "turn.completed":
            _set_usage(self._result.usage, _object_map(event.get("usage")))
        if event_type == "turn.failed":
            self.failure_detail = _failure_detail(event)

    def _record_thread(self, event: Mapping[str, object]) -> None:
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            self._result.session_id = validate_session_id("codex", thread_id)

    def _record_item(self, event: Mapping[str, object]) -> None:
        item = _object_map(event.get("item"))
        if item.get("type") == "agent_message":
            self.has_final_message = True
            self._result.text = _item_text(item)
        tool_event = _tool_event(item)
        if tool_event and self._on_event:
            self._on_event(tool_event)


class CodexAdapter:
    """Implement the shared adapter contract for sandboxed Codex CLI turns.

    Example: ``CodexAdapter().run(request)`` executes one structured conversation turn.
    """

    name: HarnessName = "codex"

    def validate_profile(self, profile: HarnessProfile) -> None:
        """Check the executable, effort, and exact portable capability profile.

        Example: ``adapter.validate_profile(profile)`` rejects unsupported settings.
        """
        _require_codex_profile(profile)
        ensure_executable(CODEX_PATH, self.name)
        require_effort(profile.effort, CODEX_EFFORTS, self.name)
        resolve_capabilities(profile)

    def new_session_id(self, adw_id: str, agent_name: str) -> None:
        """Defer session creation until Codex reports its thread identifier.

        Example: ``adapter.new_session_id("run", "builder")`` returns ``None``.
        """
        del adw_id, agent_name
        return None

    def run(self, request: HarnessRequest,
            on_event: Callable[[HarnessEvent], None] | None = None,
            on_spawn: ProcessCallback | None = None,
            on_exit: ProcessCallback | None = None) -> HarnessResult:
        """Run one Codex turn and normalize its thread, tools, and usage.

        Example: ``adapter.run(request, on_event=record_event)`` streams tool events.
        """
        session_id = (validate_session_id("codex", request.session_id)
                      if request.session_id else "")
        result = HarnessResult(session_id=session_id)
        parser = CodexEventParser(result, on_event)
        outcome = run_jsonl_process(_process_spec(request), parser.observe, on_spawn, on_exit)
        result.returncode = outcome.returncode
        _require_usable_result(result, outcome, parser.has_final_message, parser.failure_detail)
        return result


def _require_codex_profile(profile: HarnessProfile) -> None:
    if profile.harness == "codex":
        return
    raise HarnessConfigError("codex", profile.harness, "a profile with harness='codex'")


def _process_spec(request: HarnessRequest) -> ProcessSpec:
    schema_path = _write_schema(request)
    stderr_path = Path(request.raw_output_path).with_suffix(".stderr.log")
    return ProcessSpec(command=_codex_command(request, schema_path), cwd=request.cwd,
                       raw_output_path=request.raw_output_path, stderr_output_path=str(stderr_path),
                       stdin_text=request.prompt)


def _write_schema(request: HarnessRequest) -> Path:
    runtime_dir = Path(request.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    schema_path = runtime_dir / "output_schema.json"
    normalized_schema = _normalize_output_schema(request.output_schema)
    schema_path.write_text(json.dumps(normalized_schema, ensure_ascii=False), encoding="utf-8")
    return schema_path


def _normalize_output_schema(schema: dict[str, object]) -> dict[str, object]:
    """Copy a schema before adding strict structured-output constraints."""
    normalized = _normalize_schema_node(schema)
    return normalized


def _copy_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _copy_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    return value


def _normalize_schema_node(schema: Mapping[str, object]) -> dict[str, object]:
    normalized = {key: _copy_json_value(value) for key, value in schema.items()}
    _normalize_mapping_schemas(normalized)
    _normalize_single_schemas(normalized)
    _normalize_list_schemas(normalized)
    _normalize_items_schema(normalized)
    _normalize_object_schema(normalized)
    return normalized


def _normalize_mapping_schemas(schema: dict[str, object]) -> None:
    for keyword in MAPPING_SCHEMA_KEYWORDS:
        values = schema.get(keyword)
        if isinstance(values, dict):
            schema[keyword] = {str(key): _copy_or_normalize_schema(value)
                               for key, value in values.items()}


def _normalize_single_schemas(schema: dict[str, object]) -> None:
    for keyword in SINGLE_SCHEMA_KEYWORDS:
        normalized = _normalize_schema_value(schema.get(keyword))
        if normalized is not None:
            schema[keyword] = normalized


def _normalize_list_schemas(schema: dict[str, object]) -> None:
    for keyword in LIST_SCHEMA_KEYWORDS:
        values = schema.get(keyword)
        if isinstance(values, list):
            schema[keyword] = [_copy_or_normalize_schema(value) for value in values]


def _normalize_items_schema(schema: dict[str, object]) -> None:
    items = schema.get("items")
    if isinstance(items, list):
        schema["items"] = [_copy_or_normalize_schema(value) for value in items]
        return
    normalized = _normalize_schema_value(items)
    if normalized is not None:
        schema["items"] = normalized


def _normalize_schema_value(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    copied = {str(key): item for key, item in value.items()}
    return _normalize_schema_node(copied)


def _copy_or_normalize_schema(value: object) -> object:
    normalized = _normalize_schema_value(value)
    if normalized is not None:
        return normalized
    return _copy_json_value(value)


def _normalize_object_schema(schema: dict[str, object]) -> None:
    properties = schema.get("properties")
    if schema.get("type") != "object" and not isinstance(properties, dict):
        return
    schema["additionalProperties"] = False
    schema["required"] = list(properties) if isinstance(properties, dict) else []


def _codex_command(request: HarnessRequest, schema_path: Path) -> list[str]:
    sandbox_access = resolve_capabilities(request.profile).sandbox
    command = [
        CODEX_PATH, "exec", "--json", "--color", "never",
        "--cd", request.cwd,
        "--model", request.profile.model,
        "--sandbox", sandbox_access,
        "--config", 'approval_policy="never"',
        "--config", f"model_reasoning_effort={json.dumps(request.profile.effort)}",
        "--config", f"developer_instructions={json.dumps(request.system_prompt)}",
        "--output-schema", str(schema_path),
    ]
    _add_thread_command(command, request.session_id)
    return command


def _add_thread_command(command: list[str], session_id: str | None) -> None:
    if session_id:
        command.extend(["resume", session_id, "-"])
        return
    command.append("-")


def _require_usable_result(result: HarnessResult, outcome: ProcessOutcome,
                           has_final_message: bool, failure_detail: str) -> None:
    if failure_detail:
        raise HarnessProcessError("codex", failure_detail,
                                  "a completed turn without turn.failed")
    if outcome.returncode != 0:
        _raise_process_error(result.returncode, outcome.stderr_tail, failure_detail)
    if not result.session_id:
        raise HarnessProtocolError("codex", "thread.started was absent", "a thread ID")
    if has_final_message:
        return
    raise HarnessProtocolError("codex", "completed agent_message was absent", "a final message")


def _raise_process_error(returncode: int, stderr_tail: str, failure_detail: str) -> None:
    detail = failure_detail or stderr_tail.strip()
    value = f"{CODEX_PATH} exited {returncode}: {detail}"
    raise HarnessProcessError("codex", value, "exit code 0 with a completed turn")


ADAPTER = CodexAdapter()
