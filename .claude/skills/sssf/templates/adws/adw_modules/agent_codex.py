"""Codex adapter — the HarnessAdapter behind coding_agent: codex.

Runs `codex exec --json` (or `codex exec resume <id> --json` to continue) and
tails its JSONL stdout line by line, same shape as agent_pi.run(): raw lines
land in raw_output_path verbatim, and normalized tool_call records are the
only thing forwarded to the caller via on_event.

Event schema is Codex CLI 0.153.0's `exec --json` "thread event" stream —
top-level events `thread.started` (carries `thread_id`), `turn.started`,
`turn.completed` (carries `usage`), and `item.started` / `item.completed`
(carry an `item` whose `type` is `agent_message` or `command_execution`,
among others this adapter does not need). Older/newer Codex releases may
shape this differently; this adapter targets 0.153.0 specifically and is not
defensive against other schemas beyond ignoring unrecognized event types.

Session continuation: Codex assigns its own thread id. agents.py always
offers a session_id (its own placeholder when there is no prior mapped
session for this coding_agent + model); this adapter treats that value as
resumable only when it parses as a UUID, exactly like the Claude Code
adapter — anything else means "start fresh", and the real thread_id Codex
reports back becomes the session_id on the HarnessResult.

Codex has no dedicated system-prompt flag, so the system prompt is folded
into the composed prompt text sent to the CLI. Codex also has no `--tools`
allowlist flag — unlike Claude Code, there is nothing here to map Pi's
`tools:` onto, and none is invented; `validate()` rejects any agent whose
effective `tools` isn't `None` (unset), since silently ignoring it would let
an agent use every Codex tool regardless of what the config asked for. Set
`tools: null` explicitly on a codex agent to opt out of an inherited
`defaults.tools` list.

`request.read_only` (true exactly when `agent.writes == []`) selects Codex's
own `--sandbox read-only` on a FRESH turn, as defense in depth alongside
permissions.enforce() — never instead of it. `codex exec resume` does not
expose `--sandbox` (confirmed via `codex exec resume --help`), so a resumed
turn cannot re-assert it. agents.py's session-identity rule (see
`_agent_session_id` in agents.py) is what actually keeps this safe: a session
is only resumed when its mapped `read_only` still matches the agent's
current one, so a session that started writable is never hand-waved into a
resumed "read-only" turn that Codex has no way to actually sandbox — it gets
a fresh thread instead, which DOES take `--sandbox read-only`.

Auth is whatever `codex` is already logged in as on this machine — the
adapter never reads or stores a credential.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

from .data_types import AgentConfig, HarnessRequest, HarnessResult
from .utils import drain_stderr, now_iso, operator_env

CODEX_PATH = os.environ.get("CODEX_PATH", "codex")

RESULT_SNIPPET_CHARS = 20_000
LABEL_CHARS = 80


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _is_uuid(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def run(request: HarnessRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
    resumable = request.session_id if _is_uuid(request.session_id) else None
    prompt = f"{request.system_prompt}\n\n{request.prompt}"
    cmd = [CODEX_PATH, "exec"]
    if resumable:
        cmd += ["resume", resumable]
    cmd += ["--json", "--model", request.model,
            "-c", f"model_reasoning_effort={request.thinking}"]
    if request.read_only and not resumable:
        cmd += ["--sandbox", "read-only"]
    cmd.append(prompt)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = HarnessResult(session_id=resumable or "")
    pending: dict[str, dict] = {}   # item id -> {command, started_at}

    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    # Drained on a background thread from the moment the child exists: an
    # unread stderr pipe fills and blocks the child's write, which stalls
    # stdout too and looks exactly like a hang (see utils.drain_stderr).
    stderr_getter = drain_stderr(process.stderr)
    with raw_path.open("a") as raw:
        assert process.stdout is not None
        for line in process.stdout:
            raw.write(line)
            raw.flush()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "thread.started":
                result.session_id = event.get("thread_id") or result.session_id
            elif etype == "item.started":
                item = event.get("item") or {}
                if item.get("type") == "command_execution":
                    pending[str(item.get("id") or "")] = {
                        "command": item.get("command"), "started_at": now_iso()}
            elif etype == "item.completed":
                item = event.get("item") or {}
                item_type = item.get("type")
                if item_type == "command_execution":
                    item_id = str(item.get("id") or "")
                    opened = pending.pop(item_id, {})
                    command = item.get("command") or opened.get("command")
                    label_value = command if isinstance(command, str) else " ".join(command or [])
                    record = {
                        "tool": "command_execution", "tool_call_id": item_id,
                        "args": {"command": command},
                        "ok": (item.get("exit_code") or 0) == 0,
                        "label": f"exec: {_clip(str(label_value), LABEL_CHARS)}",
                        "started_at": opened.get("started_at"),
                        "ended_at": now_iso(),
                    }
                    output = item.get("aggregated_output")
                    if output:
                        record["result_snippet"] = _clip(output, RESULT_SNIPPET_CHARS)
                    if on_event:
                        on_event(record)
                elif item_type == "agent_message":
                    text = item.get("text")
                    if text:
                        result.text = text   # authoritative final text
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                if usage:
                    result.usage.input_tokens = usage.get("input_tokens") or 0
                    result.usage.output_tokens = usage.get("output_tokens") or 0
                    result.usage.cache_read_tokens = usage.get("cached_input_tokens") or 0
                    result.usage.reasoning_tokens = usage.get("reasoning_output_tokens") or 0
                    # Not carried in turn.completed's usage — cached tokens are a
                    # subset of input, not additional, so summing avoids double count.
                    result.usage.total_tokens = (result.usage.input_tokens
                                                 + result.usage.output_tokens)
                    result.context_tokens = result.usage.total_tokens

    result.returncode = process.wait()
    stderr = stderr_getter()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0:
        raise RuntimeError(f"codex exited {result.returncode}: {stderr.strip()[-800:]}")
    return result


class CodexAdapter:
    """The HarnessAdapter for coding_agent: codex. See harnesses.HarnessAdapter."""

    def validate(self, agent: AgentConfig) -> list[str]:
        problems = []
        if agent.harness_engineering:
            problems.append(
                "harness_engineering is Pi-only in this MVP — coding_agent "
                "'codex' cannot use it; clear the list or set coding_agent: pi")
        if agent.tools is not None:
            problems.append(
                f"tools {agent.tools!r} cannot be honored — Codex's CLI has no "
                "tool-allowlist flag to map onto (see agent_codex.py). Set "
                "tools: null explicitly on this agent (it would otherwise inherit "
                "defaults.tools) or switch to coding_agent: pi/claude_code")
        return problems

    def run(self, request: HarnessRequest,
            on_event: Optional[Callable[[dict], None]] = None,
            on_spawn: Optional[Callable[[int], None]] = None,
            on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
        return run(request, on_event, on_spawn, on_exit)
