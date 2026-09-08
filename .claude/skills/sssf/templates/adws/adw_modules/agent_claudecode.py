"""Claude Code adapter — the HarnessAdapter behind coding_agent: claude_code.

Runs `claude -p --output-format stream-json` and tails its JSONL stdout line
by line, same shape as agent_pi.run(): raw lines land in raw_output_path
verbatim, and normalized tool_call records are the only thing forwarded to
the caller via on_event.

Session continuation: Claude Code assigns its own session UUID and will only
`--resume` a real one. agents.py always offers a session_id (its own
placeholder when there is no prior mapped session for this coding_agent +
model), so this adapter treats that value as resumable only when it parses as
a UUID; anything else means "start fresh", and the real UUID Claude Code
reports back becomes the session_id on the HarnessResult.

Tools: Pi's tool vocabulary (read, bash, edit, write, grep, find) is not
Claude Code's (Read, Bash, Edit, Write, Grep, Glob) — TOOL_MAP is the small,
direct translation, and `validate()` rejects any `agent.tools` entry that
isn't in it (Pi's `ls` has no Claude Code equivalent, so it fails loudly
rather than being dropped or passed through untranslated). Each mapped tool is
both exposed with `--tools` and pre-approved with `--allowedTools`: print mode
has nobody to answer an interactive permission prompt, so merely exposing
Write/Edit/Bash would leave them unusable. The prompt is sent through stdin
because both variadic options can consume a trailing positional prompt.

Effort: Claude Code's `--effort` only accepts low/medium/high/xhigh/max —
Pi's off/minimal have no equivalent, so `validate()` rejects them too.

Auth is whatever `claude` is already logged in as on this machine — the
adapter never reads or stores a credential.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

from .data_types import AgentConfig, HarnessRequest, HarnessResult, UsageBreakdown
from .utils import drain_stderr, now_iso, operator_env

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")

RESULT_SNIPPET_CHARS = 20_000
LABEL_CHARS = 80
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")

# Direct, small mapping — only the tools with an unambiguous Claude Code
# equivalent. Pi's `ls` has none (directory listing goes through Bash or
# Glob in Claude Code), so it is deliberately absent: validate() rejects it
# rather than guessing a translation.
TOOL_MAP = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "bash": "Bash",
    "grep": "Grep",
    "find": "Glob",
}

# `claude --effort`'s own accepted range (verified against `claude --help`);
# Pi's off/minimal fall outside it.
SUPPORTED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _map_tools(tools: list[str]) -> list[str]:
    """Translate Pi tool names to Claude Code's, or raise on the first one
    with no mapping — the same rule validate() and run() both apply."""
    unsupported = [t for t in tools if t not in TOOL_MAP]
    if unsupported:
        raise ValueError(
            f"tool(s) {unsupported} have no Claude Code mapping — supported: "
            f"{sorted(TOOL_MAP)}; drop them, rename them, or set coding_agent: pi")
    return [TOOL_MAP[t] for t in tools]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _label(tool: str, args: dict) -> str:
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool}: {_clip(value, LABEL_CHARS)}" if value else tool


def _is_uuid(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _text_blocks(content: list) -> str:
    return "".join(part.get("text", "") for part in content or []
                   if isinstance(part, dict) and part.get("type") == "text")


def run(request: HarnessRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
    resumable = request.session_id if _is_uuid(request.session_id) else None
    cmd = [
        CLAUDE_PATH, "-p", "--model", request.model, "--effort", request.thinking,
        "--output-format", "stream-json", "--verbose",
        "--system-prompt", request.system_prompt,
    ]
    if resumable:
        cmd += ["--resume", resumable]
    if request.tools:
        mapped_tools = ",".join(_map_tools(request.tools))
        # `--tools` controls availability; it does not approve permission
        # prompts. There is no interactive approver under `--print`, so grant
        # the same bounded list explicitly. permissions.enforce() still checks
        # the agent's narrower repo write scope after the process exits.
        cmd += ["--tools", mapped_tools, "--allowedTools", mapped_tools]
    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = HarnessResult(session_id=resumable or "")
    pending: dict[str, dict] = {}   # tool_use id -> {tool, args}, until its tool_result arrives

    # Claude documents stdin as a prompt source for --print. This also keeps the
    # variadic tool options from consuming a trailing positional prompt.
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    assert process.stdin is not None
    process.stdin.write(request.prompt)
    process.stdin.close()
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
            if etype == "system" and event.get("subtype") == "init":
                result.session_id = event.get("session_id") or result.session_id
            elif etype == "assistant":
                message = event.get("message", {}) or {}
                for block in message.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        pending[str(block.get("id"))] = {
                            "tool": block.get("name") or "tool",
                            "args": block.get("input") or {},
                        }
                text = _text_blocks(message.get("content"))
                if text:
                    result.text = text   # last assistant message wins until "result" overrides it
            elif etype == "user":
                message = event.get("message", {}) or {}
                for block in message.get("content", []) or []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    call_id = str(block.get("tool_use_id"))
                    opened = pending.pop(call_id, {})
                    tool = opened.get("tool", "tool")
                    args = opened.get("args", {})
                    content = block.get("content")
                    text = (content if isinstance(content, str)
                           else _text_blocks(content) if isinstance(content, list) else "")
                    record = {
                        "tool": tool, "tool_call_id": call_id, "args": args,
                        "ok": not block.get("is_error", False),
                        "label": _label(tool, args),
                        "ended_at": now_iso(),
                    }
                    if text:
                        record["result_snippet"] = _clip(text, RESULT_SNIPPET_CHARS)
                    if on_event:
                        on_event(record)
            elif etype == "result":
                result.session_id = event.get("session_id") or result.session_id
                if event.get("result"):
                    result.text = event["result"]   # authoritative final text
                usage = event.get("usage") or {}
                cost = event.get("total_cost_usd") or 0.0
                total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0) \
                    + (usage.get("cache_creation_input_tokens") or 0) \
                    + (usage.get("cache_read_input_tokens") or 0)
                result.usage.input_tokens += usage.get("input_tokens") or 0
                result.usage.output_tokens += usage.get("output_tokens") or 0
                result.usage.cache_write_tokens += usage.get("cache_creation_input_tokens") or 0
                result.usage.cache_read_tokens += usage.get("cache_read_input_tokens") or 0
                result.usage.total_tokens += total
                result.usage.total_cost += cost
                result.context_tokens = total

    result.returncode = process.wait()
    stderr = stderr_getter()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0:
        raise RuntimeError(f"claude exited {result.returncode}: {stderr.strip()[-800:]}")
    return result


class ClaudeCodeAdapter:
    """The HarnessAdapter for coding_agent: claude_code. See harnesses.HarnessAdapter."""

    def validate(self, agent: AgentConfig) -> list[str]:
        problems = []
        if agent.harness_engineering:
            problems.append(
                "harness_engineering is Pi-only in this MVP — coding_agent "
                "'claude_code' cannot use it; clear the list or set coding_agent: pi")
        if agent.thinking not in SUPPORTED_EFFORTS:
            problems.append(
                f"thinking {agent.thinking!r} is not a Claude Code effort level — "
                f"supported: {sorted(SUPPORTED_EFFORTS)}")
        if agent.tools:
            try:
                _map_tools(agent.tools)
            except ValueError as error:
                problems.append(str(error))
        return problems

    def run(self, request: HarnessRequest,
            on_event: Optional[Callable[[dict], None]] = None,
            on_spawn: Optional[Callable[[int], None]] = None,
            on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
        return run(request, on_event, on_spawn, on_exit)
