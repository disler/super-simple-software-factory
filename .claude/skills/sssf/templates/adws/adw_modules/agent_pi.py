"""Pi command harness adapter.

Runs the non-interactive JSON mode and normalizes completed tool calls and
usage. A session ID creates or continues the same context window.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

from .harness_capabilities import resolve_capabilities
from .harness_process import (ProcessOutcome, ProcessSpec, ensure_executable,
                              require_effort, run_jsonl_process)
from .harness_types import (HarnessConfigError, HarnessEvent, HarnessName,
                            HarnessProcessError, HarnessProfile, HarnessRequest,
                            HarnessResult, ProcessCallback, UsageBreakdown,
                            validate_session_id)
from .utils import new_id, now_iso, operator_env

PI_PATH = os.environ.get("PI_PATH", "pi")
MODELS_JSON = os.environ.get("PI_MODELS_PATH",
                             str(Path.home() / ".pi" / "agent" / "models.json"))
PI_EFFORTS: frozenset[str] = frozenset({
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
})

RESULT_SNIPPET_CHARS = 20_000
ARG_VALUE_CHARS = 20_000
LABEL_CHARS = 80
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")


class OpenToolCall(TypedDict):
    tool: str
    args: dict[str, object]
    started_at: str
    clock: float


def _count(value: str) -> int:
    """Parse a compact model-list count.

    Example: ``_count("272K")`` returns ``272000``.
    """
    suffixes = {"K": 1_000, "M": 1_000_000}
    suffix = value[-1:].upper()
    if suffix in suffixes:
        return int(float(value[:-1]) * suffixes[suffix])
    return int(value)


@lru_cache(maxsize=1)
def _pi_catalog() -> list[tuple[str, str, int]]:
    """Read the merged model catalog.

    Example: ``_pi_catalog()`` returns provider, model, and window rows.
    """
    try:
        result = subprocess.run([PI_PATH, "--list-models"], capture_output=True,
                                text=True, timeout=30, env=operator_env(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return _catalog_rows(result.stdout)


def _catalog_rows(output: str) -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    for line in output.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 3:
            _append_catalog_row(rows, columns)
    return rows


def _append_catalog_row(rows: list[tuple[str, str, int]], columns: list[str]) -> None:
    try:
        rows.append((columns[0], columns[1], _count(columns[2])))
    except ValueError:
        return


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve a model pattern to one explicit provider and model ID.

    Example: ``resolve_model("provider/model")`` returns its catalog pair.
    """
    catalog = [(provider, model_id) for provider, model_id, _ in _pi_catalog()]
    if "/" in pattern:
        provider, model_id = pattern.split("/", 1)
        if (provider, model_id) in catalog:
            return provider, model_id
    matches = [(provider, model_id) for provider, model_id in catalog
               if pattern == model_id or pattern in model_id]
    return _matched_model(pattern, matches)


def _matched_model(pattern: str, matches: list[tuple[str, str]]) -> tuple[str, str]:
    exact = [match for match in matches
             if match[1] == pattern or match[1].endswith("/" + pattern)]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"model pattern {pattern!r} not found in pi --list-models — "
                         "authenticate/register it or fix the config")
    raise ValueError(f"model pattern {pattern!r} is ambiguous: {matches}")


def _context_tokens(usage: Mapping[str, object]) -> int:
    """Return the context occupancy reported after a turn.

    Example: ``_context_tokens({"totalTokens": 12})`` returns ``12``.
    """
    total = _usage_count(usage, "totalTokens")
    if total:
        return total
    fields = ("input", "output", "cacheRead", "cacheWrite")
    return sum(_usage_count(usage, field) for field in fields)


def _usage_count(usage: Mapping[str, object], field: str) -> int:
    """Return a numeric token field or zero for malformed input.

    Example: ``_usage_count({}, "input")`` returns ``0``.
    """
    value = usage.get(field, 0)
    return int(value) if isinstance(value, int | float) else 0


def _usage_cost(cost: Mapping[str, object], field: str) -> float:
    """Return a numeric cost field or zero for malformed input.

    Example: ``_usage_cost({}, "total")`` returns ``0.0``.
    """
    value = cost.get(field, 0.0)
    return float(value) if isinstance(value, int | float) else 0.0


def _add_usage(result: HarnessResult, source: Mapping[str, object], total: int) -> None:
    """Fold one completion usage object into a normalized result.

    Example: ``_add_usage(result, source, 100)`` adds the final turn usage.
    """
    breakdown = result.usage
    _add_token_usage(breakdown, source, total)
    _add_cost_usage(breakdown, _object_map(source.get("cost")))


def _add_token_usage(usage: UsageBreakdown, source: Mapping[str, object], total: int) -> None:
    usage.input_tokens += _usage_count(source, "input")
    usage.output_tokens += _usage_count(source, "output")
    usage.cache_read_tokens += _usage_count(source, "cacheRead")
    usage.cache_write_tokens += _usage_count(source, "cacheWrite")
    usage.reasoning_tokens += _usage_count(source, "reasoning")
    usage.total_tokens += total


def _add_cost_usage(usage: UsageBreakdown, source: Mapping[str, object]) -> None:
    usage.input_cost += _usage_cost(source, "input")
    usage.output_cost += _usage_cost(source, "output")
    usage.cache_read_cost += _usage_cost(source, "cacheRead")
    usage.cache_write_cost += _usage_cost(source, "cacheWrite")
    usage.total_cost += _usage_cost(source, "total")


def context_window(provider: str, model_id: str) -> int:
    """Return the selected model's declared context window.

    Example: ``context_window("provider", "model")`` returns its token ceiling.
    """
    registry = json.loads(Path(MODELS_JSON).read_text())
    for model in _provider_models(registry, provider):
        if model.get("id") == model_id:
            return _usage_count(model, "contextWindow")
    return _catalog_context_window(provider, model_id)


def _provider_models(registry: object, provider: str) -> list[dict[str, object]]:
    root = _object_map(registry)
    providers = _object_map(root.get("providers"))
    selected = _object_map(providers.get(provider))
    models = selected.get("models", [])
    return [_object_map(model) for model in models] if isinstance(models, list) else []


def _catalog_context_window(provider: str, model_id: str) -> int:
    for listed_provider, listed_model, window in _pi_catalog():
        if listed_provider == provider and listed_model == model_id:
            return window
    return 0


def _object_map(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _text_of(container: Mapping[str, object]) -> str:
    """Join text blocks from a JSON message or tool result.

    Example: ``_text_of({"content": []})`` returns an empty string.
    """
    content = container.get("content", [])
    if not isinstance(content, list):
        return ""
    return "".join(_text_block(block) for block in content)


def _text_block(block: object) -> str:
    item = _object_map(block)
    text = item.get("text", "")
    return text if item.get("type") == "text" and isinstance(text, str) else ""


def _clip(text: str, limit: int) -> str:
    """Truncate long event content while leaving short strings unchanged.

    Example: ``_clip("text", 10)`` returns ``"text"``.
    """
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _label(tool: str, args: Mapping[str, object]) -> str:
    """Build a one-line name for a completed tool call.

    Example: ``_label("bash", {"command": "pwd"})`` returns ``"bash: pwd"``.
    """
    value = _primary_arg(args)
    if not value:
        return tool
    return f"{tool}: {_clip(value, LABEL_CHARS)}"


def _primary_arg(args: Mapping[str, object]) -> str:
    values = [args.get(key) for key in PRIMARY_ARGS]
    values.extend(args.values())
    for value in values:
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


class ToolCallTracker:
    """Fold the tool stream into one event per completed call.

    Example: ``ToolCallTracker().observe(event)`` returns a completed call event.
    """

    def __init__(self) -> None:
        """Start tracking tool calls for one harness run.

        Example: ``ToolCallTracker()`` prepares an empty call registry.
        """
        self._open: dict[str, OpenToolCall] = {}

    def observe(self, event: dict[str, object]) -> HarnessEvent | None:
        """Return a normalized event after a tool call completes.

        Example: ``tracker.observe(event)`` returns ``None`` before completion.
        """
        event_type = event.get("type", "")
        if event_type == "message_end":
            self._observe_message_end(event)
            return None
        if event_type == "tool_execution_start":
            self._announce(event.get("toolCallId"), event.get("toolName"), event.get("args"))
            return None
        return self._finish(event) if event_type == "tool_execution_end" else None

    def _observe_message_end(self, event: Mapping[str, object]) -> None:
        message = _object_map(event.get("message"))
        content = message.get("content", [])
        if not isinstance(content, list):
            return
        for block in content:
            item = _object_map(block)
            if item.get("type") == "toolCall":
                self._announce(item.get("id"), item.get("name"), item.get("arguments"))

    def _finish(self, event: Mapping[str, object]) -> HarnessEvent:
        call_id = str(event.get("toolCallId") or "")
        opened = self._open.pop(call_id, None)
        tool = str(event.get("toolName") or (opened or {}).get("tool") or "tool")
        args = _object_map(event.get("args")) or (opened or {}).get("args", {})
        payload = _tool_payload(tool, call_id, args, event)
        _add_duration(payload, opened)
        return HarnessEvent(name=_label(tool, args), payload=payload,
                            started_at=(opened or {}).get("started_at"), ended_at=now_iso())

    def _announce(self, call_id: object, tool: object, args: object) -> None:
        identifier = str(call_id or "")
        if not identifier:
            return
        known = self._open.get(identifier)
        self._open[identifier] = {"tool": str(tool or (known or {}).get("tool", "")),
                                  "args": _object_map(args) or (known or {}).get("args", {}),
                                  "started_at": (known or {}).get("started_at") or now_iso(),
                                  "clock": (known or {}).get("clock") or time.monotonic()}


def _tool_payload(tool: str, call_id: str, args: dict[str, object],
                  event: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {"tool": tool, "tool_call_id": call_id,
                                  "args": _clip_args(args),
                                  "ok": not bool(event.get("isError", False))}
    result_text = _text_of(_object_map(event.get("result")))
    if result_text:
        payload["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
    return payload


def _clip_args(args: Mapping[str, object]) -> dict[str, object]:
    clipped: dict[str, object] = {}
    for key, value in args.items():
        clipped[key] = _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
    return clipped


def _add_duration(payload: dict[str, object], opened: OpenToolCall | None) -> None:
    if opened:
        payload["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        return
    return None


class PiEventParser:
    def __init__(self, result: HarnessResult,
                 on_event: Callable[[HarnessEvent], None] | None) -> None:
        self._result = result
        self._on_event = on_event
        self._tools = ToolCallTracker()

    def observe(self, event: dict[str, object]) -> None:
        self._observe_assistant_message(event)
        tool_event = self._tools.observe(event)
        if tool_event and self._on_event:
            self._on_event(tool_event)

    def _observe_assistant_message(self, event: Mapping[str, object]) -> None:
        if event.get("type") != "message_end":
            return
        message = _object_map(event.get("message"))
        if message.get("role") == "assistant":
            self._record_message(message)

    def _record_message(self, message: Mapping[str, object]) -> None:
        text = _text_of(message)
        if text:
            self._result.text = text
        usage = _object_map(message.get("usage"))
        turn = _context_tokens(usage)
        _add_usage(self._result, usage, turn)
        if turn and message.get("stopReason") not in ("aborted", "error"):
            self._result.context_tokens = turn


class PiAdapter:
    """Implement the shared adapter contract for Pi CLI sessions.

    Example: ``PiAdapter().run(request)`` executes one configured harness turn.
    """

    name: HarnessName = "pi"

    def validate_profile(self, profile: HarnessProfile) -> None:
        """Check the executable, model, effort, and native capabilities.

        Example: ``adapter.validate_profile(profile)`` fails before a process starts.
        """
        _require_pi_profile(profile)
        ensure_executable(PI_PATH, self.name)
        require_effort(profile.effort, PI_EFFORTS, self.name)
        resolve_model(profile.model)
        resolve_capabilities(profile)

    def new_session_id(self, adw_id: str, agent_name: str) -> str:
        """Create the stable session ID for a fresh Pi conversation.

        Example: ``adapter.new_session_id("run", "builder")`` creates a session ID.
        """
        return f"sssf-{adw_id}-{agent_name}-{new_id(4)}"

    def run(self, request: HarnessRequest,
            on_event: Callable[[HarnessEvent], None] | None = None,
            on_spawn: ProcessCallback | None = None,
            on_exit: ProcessCallback | None = None) -> HarnessResult:
        """Run one Pi turn and normalize its final message and usage.

        Example: ``adapter.run(request, on_event=record_event)`` streams tool events.
        """
        session_id = _require_session_id(request)
        provider, model_id = resolve_model(request.profile.model)
        result = HarnessResult(session_id=session_id,
                               context_window=context_window(provider, model_id))
        parser = PiEventParser(result, on_event)
        outcome = run_jsonl_process(_process_spec(request, provider, model_id, session_id),
                                    parser.observe, on_spawn, on_exit)
        result.returncode = outcome.returncode
        _require_usable_result(result, outcome)
        return result


def _require_pi_profile(profile: HarnessProfile) -> None:
    if profile.harness == "pi":
        return
    raise HarnessConfigError("pi", profile.harness, "a profile with harness='pi'")


def _require_session_id(request: HarnessRequest) -> str:
    if request.session_id:
        return validate_session_id("pi", request.session_id)
    raise HarnessConfigError("pi", request.session_id, "a non-empty Pi session ID")


def _process_spec(request: HarnessRequest, provider: str, model_id: str,
                  session_id: str) -> ProcessSpec:
    Path(request.runtime_dir).mkdir(parents=True, exist_ok=True)
    return ProcessSpec(command=_pi_command(request, provider, model_id, session_id), cwd=request.cwd,
                       raw_output_path=request.raw_output_path,
                       stderr_output_path=str(Path(request.raw_output_path).with_suffix(".stderr.log")))


def _pi_command(request: HarnessRequest, provider: str, model_id: str,
                session_id: str) -> list[str]:
    capabilities = resolve_capabilities(request.profile)
    command = [PI_PATH, "-p", "--mode", "json", "--provider", provider, "--model", model_id,
               "--thinking", request.profile.effort, "--session-id", session_id,
               "--session-dir", request.runtime_dir, "--system-prompt", request.system_prompt]
    if capabilities.native_tools:
        command.extend(["--tools", ",".join(capabilities.native_tools)])
    for extension in request.profile.extensions:
        command.extend(["-e", extension])
    command.append(request.prompt)
    return command


def _require_usable_result(result: HarnessResult, outcome: ProcessOutcome) -> None:
    if result.returncode == 0 or result.text:
        return
    stderr_tail = outcome.stderr_tail.strip()
    value = f"{PI_PATH} exited {result.returncode}: {stderr_tail}"
    raise HarnessProcessError("pi", value, "exit code 0 or a final assistant result")


ADAPTER = PiAdapter()
