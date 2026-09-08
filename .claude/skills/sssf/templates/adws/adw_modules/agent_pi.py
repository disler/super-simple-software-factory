"""Pi coding agent adapter — the HarnessAdapter behind coding_agent: pi.

Runs `pi -p --mode json` and tails its JSONL stdout line by line, folding the
raw stream into ONE normalized tool_call record per completed call (via
ToolCallTracker) and forwarding only those to the caller — the same
normalization every adapter is responsible for doing on its own raw wire
format, per harnesses.HarnessAdapter. `--session-id` creates-or-continues, so
running and continuing an agent are the same call: same session id = same
context window.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .data_types import AgentConfig, HarnessRequest, HarnessResult
from .utils import drain_stderr, new_id, now_iso, operator_env

PI_PATH = os.environ.get("PI_PATH", "pi")
MODELS_JSON = os.environ.get("PI_MODELS_PATH",
                             str(Path.home() / ".pi" / "agent" / "models.json"))

RESULT_SNIPPET_CHARS = 20_000   # tool output rides along whole; clip only guards pathological cases
ARG_VALUE_CHARS = 20_000        # args too — the UI scrolls, it must not be handed cut-off data
LABEL_CHARS = 80                # "bash: <command>" shown as the event name

# The arg that identifies a call at a glance, in the order tools tend to use.
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")


def _count(value: str) -> int:
    """Parse pi's compact model-list counts (`272K`, `1.0M`)."""
    suffixes = {"K": 1_000, "M": 1_000_000}
    suffix = value[-1:].upper()
    if suffix in suffixes:
        return int(float(value[:-1]) * suffixes[suffix])
    return int(value)


@lru_cache(maxsize=1)
def _pi_catalog() -> list[tuple[str, str, int]]:
    """Read pi's merged catalog, including built-in providers and custom models."""
    try:
        result = subprocess.run(
            [PI_PATH, "--list-models"], capture_output=True, text=True,
            timeout=30, env=operator_env(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) < 3:
            continue
        try:
            rows.append((columns[0], columns[1], _count(columns[2])))
        except ValueError:
            continue
    return rows


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve a model pattern to an explicit ``(provider, model_id)`` pair.

    Pi's catalog merges built-in models with ``~/.pi/agent/models.json``. Using
    that same merged view lets SSSF target direct providers such as
    ``openai/gpt-5.6-terra`` without re-registering built-in models locally.
    """
    catalog = [(provider, model_id) for provider, model_id, _ in _pi_catalog()]
    if "/" in pattern:
        provider, model_id = pattern.split("/", 1)
        if (provider, model_id) in catalog:
            return provider, model_id
    matches = [(provider, model_id) for provider, model_id in catalog
               if pattern == model_id or pattern in model_id]
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


def _context_tokens(usage: dict) -> int:
    """Tokens occupying the window after a turn.

    Mirrors pi's own `calculateContextTokens` (coding-agent
    `core/compaction/compaction.ts`), which is what pi compacts against and
    shows in its footer: prefer the provider's `totalTokens`, else sum the
    parts. Cache reads count — cached prompt is still prompt.
    """
    total = usage.get("totalTokens") or 0
    if total:
        return int(total)
    return int(sum(usage.get(part) or 0
                   for part in ("input", "output", "cacheRead", "cacheWrite")))


def context_window(provider: str, model_id: str) -> int:
    """The model's context ceiling from pi's merged model catalog."""
    registry = json.loads(Path(MODELS_JSON).read_text())
    for model in registry.get("providers", {}).get(provider, {}).get("models", []):
        if model.get("id") == model_id:
            return int(model.get("contextWindow") or 0)
    for listed_provider, listed_model, window in _pi_catalog():
        if listed_provider == provider and listed_model == model_id:
            return window
    return 0


def _text_of(container: dict) -> str:
    """Join the text blocks of anything pi shapes as {content: [...]} — a
    message or a tool result."""
    return "".join(part.get("text", "") for part in container.get("content", []) or []
                   if isinstance(part, dict) and part.get("type") == "text")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _label(tool: str, args: dict) -> str:
    """One-line human name for a tool call: `bash: ls -la src`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool}: {_clip(value, LABEL_CHARS)}" if value else tool


class ToolCallTracker:
    """Folds pi's tool stream into ONE normalized record per completed call.

    pi announces a call as a `toolCall` content block, then emits
    tool_execution_start / _update / _end for it. Only the end carries the
    result, so that is where a record is emitted — one trace event per real
    tool call, the moment it returns, instead of three shapeless ones.

    The record carries the call's real span (`started_at`/`ended_at`), which the
    tracer writes to columns so the UI can lay tool calls on a time axis without
    parsing every payload.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        """Returns the record for a finished tool call, else None."""
        etype = event.get("type", "")
        if etype == "message_end":
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._announce(block.get("id"), block.get("name"),
                                   block.get("arguments"))
            return None
        if etype == "tool_execution_start":
            self._announce(event.get("toolCallId"), event.get("toolName"),
                           event.get("args"))
            return None
        if etype != "tool_execution_end":
            return None

        call_id = str(event.get("toolCallId") or "")
        opened = self._open.pop(call_id, {})
        tool = str(event.get("toolName") or opened.get("tool") or "tool")
        args = event.get("args") or opened.get("args") or {}
        record = {
            "tool": tool,
            "tool_call_id": call_id,
            "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": not event.get("isError", False),
            "label": _label(tool, args),
        }
        result_text = _text_of(event.get("result") or {})
        if result_text:
            record["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
        record["ended_at"] = now_iso()
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        if opened.get("started_at"):
            record["started_at"] = opened["started_at"]
        return record

    def _announce(self, call_id, tool, args) -> None:
        """First sighting starts the clock; a later sighting only fills gaps."""
        if not call_id:
            return
        known = self._open.get(str(call_id), {})
        self._open[str(call_id)] = {
            "tool": tool or known.get("tool", ""),
            "args": args or known.get("args", {}),
            "started_at": known.get("started_at") or now_iso(),   # wall clock, for the row
            "clock": known.get("clock") or time.monotonic(),      # monotonic, for duration
        }


def run(request: HarnessRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
    """Run one non-interactive pi turn.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable — a hung coding agent is otherwise a pid you have
    to hunt for in `ps` while the run sits there.

    `request.session_id` is normally supplied by agents.py, but pi's CLI needs
    an explicit `--session-id` no matter what, so a missing one is minted here
    rather than pushed back onto the caller.
    """
    session_id = request.session_id or f"pi-{new_id(8)}"
    provider, model_id = resolve_model(request.model)
    cmd = [
        PI_PATH, "-p", "--mode", "json",
        "--provider", provider, "--model", model_id,
        "--thinking", request.thinking,
        "--session-id", session_id,
        "--session-dir", request.state_dir,
        "--system-prompt", request.system_prompt,
    ]
    if request.tools:
        cmd += ["--tools", ",".join(request.tools)]
    for extension in request.harness_engineering:
        cmd += ["-e", extension]
    cmd.append(request.prompt)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = HarnessResult(session_id=session_id,
                           context_window=context_window(provider, model_id))
    tracker = ToolCallTracker()
    # stdin is DEVNULL, deliberately. The prompt travels in argv, so the child
    # never needs stdin — but inheriting the parent's means pi sees a non-TTY
    # and can sit forever waiting for piped input that will never arrive or
    # EOF. That failure is silent and total: no request goes out, no bytes come
    # back, and the ADW blocks on a read loop with nothing to read. Observed as
    # a run that sat idle at 0% CPU with an empty raw_output.jsonl.
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
            raw.flush()                      # events land on disk as they happen
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                message = event.get("message", {})
                if message.get("role") == "assistant":
                    text = _text_of(message)
                    if text:
                        result.text = text   # last assistant message wins
                    usage = message.get("usage", {}) or {}
                    turn = _context_tokens(usage)
                    result.usage.add_turn(usage, turn)
                    # Occupancy is read off the last VALID assistant turn, the
                    # way pi does it — an aborted or errored turn reports usage
                    # you can't trust, so it must not overwrite a good reading.
                    if turn and message.get("stopReason") not in ("aborted", "error"):
                        result.context_tokens = turn
            # Fold pi's raw tool_execution_start/_update/_end stream into ONE
            # normalized record per completed call, and forward only that —
            # on_event never sees pi's own wire shape, so agents.py stays
            # ignorant of it too.
            record = tracker.observe(event)
            if record and on_event:
                on_event(record)

    result.returncode = process.wait()
    stderr = stderr_getter()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0 and not result.text:
        raise RuntimeError(f"pi exited {result.returncode}: {stderr.strip()[-800:]}")
    return result


class PiAdapter:
    """The HarnessAdapter for coding_agent: pi. See harnesses.HarnessAdapter."""

    def validate(self, agent: AgentConfig) -> list[str]:
        try:
            resolve_model(agent.model)
        except ValueError as error:
            return [str(error)]
        return []

    def run(self, request: HarnessRequest,
            on_event: Optional[Callable[[dict], None]] = None,
            on_spawn: Optional[Callable[[int], None]] = None,
            on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
        return run(request, on_event, on_spawn, on_exit)
