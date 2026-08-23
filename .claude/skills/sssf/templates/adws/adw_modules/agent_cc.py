"""Claude Code command harness adapter with streaming schema enforcement."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypedDict

from .harness_capabilities import resolve_capabilities
from .harness_process import (ProcessOutcome, ProcessSpec, ensure_executable,
                              require_effort, run_jsonl_process)
from .harness_types import (HarnessConfigError, HarnessEvent, HarnessName,
                            HarnessProcessError, HarnessProfile, HarnessProtocolError,
                            HarnessRequest, HarnessResult, ProcessCallback, UsageBreakdown,
                            validate_session_id)
from .diagnostics import sanitize_diagnostic
from .utils import now_iso, operator_env

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
CLAUDE_EFFORTS: frozenset[str] = frozenset({
    "low", "medium", "high", "xhigh", "max", "ultracode",
})
MINIMUM_VERSION: tuple[int, int, int] = (2, 1, 205)
RESULT_SNIPPET_CHARS = 20_000
ARG_VALUE_CHARS = 20_000
LABEL_CHARS = 80
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")


class OpenToolUse(TypedDict):
    tool: str
    args: dict[str, object]
    started_at: str
    clock: float


def _object_map(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _content_blocks(message: Mapping[str, object]) -> list[dict[str, object]]:
    content = message.get("content", [])
    if not isinstance(content, list):
        return []
    return [_object_map(block) for block in content]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _primary_arg(args: Mapping[str, object]) -> str:
    values = [args.get(key) for key in PRIMARY_ARGS]
    values.extend(args.values())
    for value in values:
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _tool_label(tool: str, args: Mapping[str, object]) -> str:
    value = _primary_arg(args)
    if not value:
        return tool
    return f"{tool}: {_clip(value, LABEL_CHARS)}"


def _clip_args(args: Mapping[str, object]) -> dict[str, object]:
    return {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
            for key, value in args.items()}


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(_result_block_text(block) for block in content)


def _result_block_text(block: object) -> str:
    value = _object_map(block)
    text = value.get("text", "")
    return text if value.get("type") == "text" and isinstance(text, str) else ""


class ClaudeToolTracker:
    """Fold Claude tool-use and tool-result messages into completed calls.

    Example: ``ClaudeToolTracker().observe(event)`` yields a completed tool event.
    """

    def __init__(self) -> None:
        self._open: dict[str, OpenToolUse] = {}

    def observe(self, event: dict[str, object]) -> list[HarnessEvent]:
        """Return normalized events after corresponding results arrive.

        Example: ``tracker.observe(event)`` returns an empty list for a tool-use message.
        """
        message = _object_map(event.get("message"))
        if event.get("type") == "assistant":
            self._record_tool_uses(message)
            return []
        if event.get("type") != "user":
            return []
        return self._record_tool_result(message)

    def _record_tool_uses(self, message: Mapping[str, object]) -> None:
        for block in _content_blocks(message):
            if block.get("type") == "tool_use":
                self._open_tool(block)

    def _open_tool(self, block: Mapping[str, object]) -> None:
        identifier = str(block.get("id") or "")
        if not identifier:
            return
        self._open[identifier] = {"tool": str(block.get("name") or "tool"),
                                  "args": _object_map(block.get("input")),
                                  "started_at": now_iso(), "clock": time.monotonic()}

    def _record_tool_result(self, message: Mapping[str, object]) -> list[HarnessEvent]:
        completed: list[HarnessEvent] = []
        for block in _content_blocks(message):
            event = self._complete_tool(block) if block.get("type") == "tool_result" else None
            if event:
                completed.append(event)
        return completed

    def _complete_tool(self, block: Mapping[str, object]) -> HarnessEvent | None:
        identifier = str(block.get("tool_use_id") or "")
        opened = self._open.pop(identifier, None)
        if opened is None:
            return None
        payload = _tool_payload(identifier, opened, block)
        return HarnessEvent(name=_tool_label(opened["tool"], opened["args"]), payload=payload,
                            started_at=opened["started_at"], ended_at=now_iso())


def _tool_payload(identifier: str, opened: OpenToolUse,
                  result: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {"tool": opened["tool"], "tool_call_id": identifier,
                                  "args": _clip_args(opened["args"]),
                                  "ok": not bool(result.get("is_error", False)),
                                  "duration_ms": int((time.monotonic() - opened["clock"]) * 1000)}
    text = _result_text(result.get("content"))
    if text:
        payload["result_snippet"] = _clip(text, RESULT_SNIPPET_CHARS)
    return payload


class ClaudeEventParser:
    """Collect normalized result fields while forwarding completed tool events.

    Example: ``parser.observe(event)`` records the final structured response.
    """

    def __init__(self, result: HarnessResult,
                 on_event: Callable[[HarnessEvent], None] | None) -> None:
        self._result = result
        self._on_event = on_event
        self._tools = ClaudeToolTracker()
        self.result_events = 0
        self.has_success_result = False
        self.result_failure = ""

    def observe(self, event: dict[str, object]) -> None:
        """Process one Claude stream event.

        Example: ``parser.observe({"type": "result"})`` records final usage.
        """
        self._capture_session(event)
        if event.get("type") == "result":
            self._record_result(event)
        self._forward_tools(self._tools.observe(event))

    def _forward_tools(self, events: list[HarnessEvent]) -> None:
        if self._on_event is None:
            return
        for event in events:
            self._on_event(event)

    def _capture_session(self, event: Mapping[str, object]) -> None:
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._result.session_id = validate_session_id("claude_code", session_id)

    def _record_result(self, event: Mapping[str, object]) -> None:
        self.result_events += 1
        self.has_success_result = _is_success_result(event)
        if not self.has_success_result:
            self.result_failure = _result_failure(event)
        self._result.text = (json.dumps(event["structured_output"], ensure_ascii=False)
                             if "structured_output" in event else _final_result_text(event))
        _set_usage(self._result.usage, _object_map(event.get("usage")), event)
        _set_context_metrics(self._result, event)


def _final_result_text(event: Mapping[str, object]) -> str:
    result = event.get("result", "")
    return result if isinstance(result, str) else str(result)


def _is_success_result(event: Mapping[str, object]) -> bool:
    terminal_state = event.get("terminal_reason")
    api_error_status = event.get("api_error_status")
    return (event.get("is_error") is False and event.get("subtype") == "success"
            and terminal_state == "completed" and api_error_status is None)


def _result_failure(event: Mapping[str, object]) -> str:
    api_error_status = sanitize_diagnostic(event.get("api_error_status"))
    subtype = event.get("subtype")
    return (f"is_error={event.get('is_error')!r}, subtype={subtype!r}, "
            f"terminal_reason={event.get('terminal_reason')!r}, "
            f"api_error_status={api_error_status!r}")


def _set_usage(usage: UsageBreakdown, raw_usage: Mapping[str, object],
               result_event: Mapping[str, object]) -> None:
    usage.input_tokens = _usage_count(raw_usage, "input_tokens")
    usage.output_tokens = _usage_count(raw_usage, "output_tokens")
    usage.cache_read_tokens = _usage_count(raw_usage, "cache_read_input_tokens")
    usage.cache_write_tokens = _usage_count(raw_usage, "cache_creation_input_tokens")
    details = _object_map(raw_usage.get("output_tokens_details"))
    usage.reasoning_tokens = _usage_count(details, "thinking_tokens")
    usage.total_tokens = sum((usage.input_tokens, usage.output_tokens,
                              usage.cache_read_tokens, usage.cache_write_tokens))
    usage.total_cost = _usage_cost(result_event.get("total_cost_usd"))


def _set_context_metrics(result: HarnessResult, event: Mapping[str, object]) -> None:
    model_usage = _single_model_usage(event.get("modelUsage"))
    if not model_usage:
        return
    result.context_window = _usage_count(model_usage, "contextWindow")
    result.context_tokens = _model_usage_occupancy(model_usage)


def _single_model_usage(value: object) -> dict[str, object]:
    entries = [_object_map(item) for item in _object_map(value).values()]
    if len(entries) != 1:
        return {}
    return entries[0]


def _model_usage_occupancy(usage: Mapping[str, object]) -> int:
    # Current-turn occupancy is the verified input, output, cache-read, and
    # cache-write components reported for the one model that supplied its limit.
    fields = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
    return sum(_usage_count(usage, field) for field in fields)


def _usage_count(usage: Mapping[str, object], field: str) -> int:
    value = usage.get(field, 0)
    return int(value) if isinstance(value, int | float) else 0


def _usage_cost(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


class ClaudeCodeAdapter:
    """Implement the shared adapter contract for Claude Code CLI sessions.

    Example: ``ClaudeCodeAdapter().run(request)`` streams a structured response.
    """

    name: HarnessName = "claude_code"

    def validate_profile(self, profile: HarnessProfile) -> None:
        """Check native profile restrictions and the required CLI version.

        Example: ``adapter.validate_profile(profile)`` fails before a process starts.
        """
        _require_claude_profile(profile)
        ensure_executable(CLAUDE_PATH, self.name)
        require_effort(profile.effort, CLAUDE_EFFORTS, self.name)
        resolve_capabilities(profile)
        _require_supported_version()

    def new_session_id(self, adw_id: str, agent_name: str) -> None:
        """Defer new session creation until the CLI emits its effective UUID.

        Example: ``adapter.new_session_id("run", "builder")`` returns ``None``.
        """
        del adw_id, agent_name
        return None

    def run(self, request: HarnessRequest,
            on_event: Callable[[HarnessEvent], None] | None = None,
            on_spawn: ProcessCallback | None = None,
            on_exit: ProcessCallback | None = None) -> HarnessResult:
        """Run one Claude Code turn and normalize its streamed completion.

        Example: ``adapter.run(request, on_event=record_event)`` streams tool events.
        """
        requested_session = (validate_session_id("claude_code", request.session_id)
                             if request.session_id else str(uuid.uuid4()))
        result = HarnessResult(session_id=requested_session)
        parser = ClaudeEventParser(result, on_event)
        outcome = run_jsonl_process(_process_spec(request, requested_session), parser.observe,
                                    on_spawn, on_exit)
        result.returncode = outcome.returncode
        _require_usable_result(result, outcome, parser)
        return result


def _require_claude_profile(profile: HarnessProfile) -> None:
    if profile.harness == "claude_code":
        return
    raise HarnessConfigError("claude_code", profile.harness,
                             "a profile with harness='claude_code'")


def _require_supported_version() -> None:
    version = _installed_version()
    if version >= MINIMUM_VERSION:
        return
    expected = ".".join(str(part) for part in MINIMUM_VERSION) + " or newer"
    raise HarnessConfigError("claude_code", _version_text(version), expected)


def _installed_version() -> tuple[int, int, int]:
    completed = subprocess.run([CLAUDE_PATH, "--version"], capture_output=True, text=True,
                               timeout=10, check=False, env=operator_env())
    output = completed.stdout + completed.stderr
    matched = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    if completed.returncode == 0 and matched:
        return tuple(int(part) for part in matched.groups())
    raise HarnessConfigError("claude_code", output.strip() or completed.returncode,
                             "a parseable Claude Code CLI version")


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _process_spec(request: HarnessRequest, session_id: str) -> ProcessSpec:
    Path(request.runtime_dir).mkdir(parents=True, exist_ok=True)
    return ProcessSpec(command=_claude_command(request, session_id), cwd=request.cwd,
                       raw_output_path=request.raw_output_path,
                       stderr_output_path=str(
                           Path(request.raw_output_path).with_suffix(".stderr.log")))


def _claude_command(request: HarnessRequest, session_id: str) -> list[str]:
    capabilities = resolve_capabilities(request.profile)
    command = _command_prefix(request, _settings(request, capabilities.sandbox))
    command.extend(["--tools", ",".join(_native_tools(request, capabilities.native_tools)),
                    "--permission-mode", "dontAsk"])
    _add_session_command(command, request.session_id, session_id)
    return command


def _command_prefix(request: HarnessRequest, settings: str) -> list[str]:
    return [CLAUDE_PATH, "-p", request.prompt, "--output-format", "stream-json", "--verbose",
            "--model", request.profile.model, "--effort", request.profile.effort,
            "--system-prompt", request.system_prompt, "--json-schema",
            json.dumps(request.output_schema, ensure_ascii=False), "--settings", settings]


def _native_tools(request: HarnessRequest, tools: tuple[str, ...]) -> tuple[str, ...]:
    if request.profile.writes != []:
        return tools
    return tuple(tool for tool in tools if tool not in {"Write", "Edit"})


def _settings(request: HarnessRequest, sandbox: str) -> str:
    settings: dict[str, object] = {"sandbox": {"enabled": True, "failIfUnavailable": True,
                                                 "allowUnsandboxedCommands": False,
                                                 "autoAllowBashIfSandboxed": True}}
    if sandbox == "read-only":
        _deny_repository_writes(settings, request.cwd)
    return json.dumps(settings, ensure_ascii=False)


def _deny_repository_writes(settings: dict[str, object], cwd: str) -> None:
    sandbox = _object_map(settings["sandbox"])
    sandbox["filesystem"] = {"denyWrite": [str(Path(cwd).resolve())]}
    settings["sandbox"] = sandbox


def _add_session_command(command: list[str], existing: str | None, created: str) -> None:
    if existing:
        command.extend(["--resume", existing])
        return
    command.extend(["--session-id", created])


def _require_usable_result(result: HarnessResult, outcome: ProcessOutcome,
                           parser: ClaudeEventParser) -> None:
    if outcome.returncode != 0:
        stderr_tail = outcome.stderr_tail.strip()
        value = f"{CLAUDE_PATH} exited {outcome.returncode}: {stderr_tail}"
        raise HarnessProcessError("claude_code", value, "exit code 0 with one successful result")
    if parser.result_events != 1:
        raise HarnessProtocolError("claude_code", parser.result_events, "exactly one result event")
    if not parser.has_success_result:
        raise HarnessProtocolError("claude_code", parser.result_failure,
                                   "is_error=false, subtype='success', terminal_reason='completed'")
    validate_session_id("claude_code", result.session_id)
    return


ADAPTER = ClaudeCodeAdapter()
