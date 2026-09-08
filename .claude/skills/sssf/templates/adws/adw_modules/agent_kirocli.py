"""Kiro CLI adapter — the HarnessAdapter behind coding_agent: kiro_cli.

Runs `kiro-cli chat --agent-engine v3 --output-format stream-json` and tails
its JSONL stdout line by line, same shape as agent_pi.run(): raw lines land in
raw_output_path verbatim, and normalized tool_call records are the only thing
forwarded to the caller via on_event.

Event schema is Kiro CLI 2.21.0's `--output-format stream-json` stream, which
the CLI documents as "the run's ACP events as JSON Lines on stdout". Every
line is `{"type": ..., "data": {...}}` with four types this adapter reads:
`runStarted`, `sessionUpdate` (an ACP `session/update` notification, whose
`update.sessionUpdate` names the real event), `runFinished` (carries the
authoritative `finalText`), and `runError`. Anything else is skipped. Like the
Codex adapter, this targets one CLI version and is not defensive against other
schemas beyond ignoring unrecognized events.

WHY `--agent-engine v3` IS PINNED, and not left to the CLI's own default:
`--output-format stream-json` is rejected outright on the v1 engine, and on
v2 the two flags SSSF cares about most are accepted and then silently dropped —
`--model no-such-model` only logs `[warn] failed to set model ...: Method not
found` and runs the settings default anyway (exit 0), and `--effort` never
takes: low, high and max all report the value from `chat.modelDefaults`. An
adapter on v2 would make the roster a lie, since sssf.config.yaml's whole
premise is that config picks the model. On v3 an unknown model fails loudly
(`InvalidModelError`, exit 1), which is the behavior validate() and run() are
written against.

Tools: `tools:` cannot be honored here, and nothing is invented to pretend
otherwise. v3's `--trust-tools` takes KAS's own tool ids, not Pi's vocabulary,
and a name it does not recognize does not fall back to "allowed" — it DENIES.
Measured on 2.21.0: `--trust-tools=shell` makes the agent's shell call come
back `The user rejected this tool call`, while `--trust-tools=run_command`
runs it. A wrong mapping would therefore silently disarm the agent rather
than merely mis-describe it, so `validate()` rejects any effective `tools`
list instead of guessing at that table.

Consequently every turn runs with `--trust-all-tools`, which is a deliberate
choice, not a shortcut: non-interactive mode has nobody to answer a trust
prompt, an untrusted call comes back rejected, and the run still exits 0 — so
without it an agent reports success on a phase in which its tools were quietly
refused. Repo write scope stays where it always is, enforced after the call by
permissions.enforce() against `writes` and `protected_files`. `read_only` is
not used here: Kiro CLI has no read-only sandbox flag, and a read-only agent
still has to write its own report into context_handoff/.

Effort: `--effort` is passed and pre-validated against the range the CLI's own
help documents (low | medium | high | xhigh | max), because an unsupported
value is not rejected by the CLI — `--effort off` is accepted and silently
ignored. Whether v3 honors the flag at all could not be verified from the
stream: unlike v2's `metadata` event, v3 reports no effort back. Two further
limits are the model's, not the flag's: some models expose no effort at all,
and others accept a narrower ladder (claude-sonnet-4.6 has no `xhigh`).

Usage: Kiro CLI reports neither billed tokens nor dollars. It bills credits,
per turn and explicitly unit-labelled
(`_meta.kiro.promptTurnSummaries[] = {"unit": "credit", "usage": 0.0275}`), so
`usage.total_tokens` and `usage.total_cost` stay 0 rather than carry a number
in the wrong unit — a credit is not a dollar, and the trace's cost column must
not say it is. Kiro's own docs confirm there is nothing better to report:
per-session token counts are "not currently available", account-level only.
The credits go to `usage.credits`, a field whose whole purpose is to carry a
non-dollar billing unit; folding them into `total_cost` would be the lie this
avoids, and dropping them made a billed run read as "0 tokens · $0.0000".

Context occupancy IS real and exact: `_meta.kiro.breakdown` reports absolute
token counts per component (`contextFiles`, `kiroResponses`, `sessionFiles`,
`tools`, `yourPrompts`), which are summed into `context_tokens`. The window
ceiling comes from the model's `context_window_tokens` in `kiro-cli chat
--list-models --format json`. The sibling `usagePercentage` is deliberately
NOT used to derive occupancy — see `_context_tokens`.

Session continuation: Kiro assigns its own id, and v3's is `sess_<uuid>`
rather than a bare UUID, so the resumable test accepts both — agents.py always
offers a session_id (its own placeholder when there is no prior mapped session
for this coding_agent + model), and anything that is not a Kiro-shaped id means
"start fresh". `--resume-id <id>` continues an existing context window (verified:
a codeword set in one turn is recalled in the next).

Kiro CLI has no system-prompt flag, so the system prompt is folded into the
prompt text — but only on a FRESH turn. A resumed turn already carries it as
the first thing in that session's history, and agents.py only resumes a
session created by this same agent, so re-sending it would pay for the whole
system prompt again on every JSON retry and gate correction.

Auth is whatever `kiro-cli` is already logged in as on this machine — the
adapter never reads or stores a credential.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .data_types import AgentConfig, HarnessRequest, HarnessResult
from .utils import drain_stderr, now_iso, operator_env

KIRO_PATH = os.environ.get("KIRO_PATH", "kiro-cli")

# Pinned, never inherited from the CLI's default — see the module docstring.
AGENT_ENGINE = "v3"

RESULT_SNIPPET_CHARS = 20_000
LABEL_CHARS = 80
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")

# `kiro-cli chat --effort`'s own documented range; Pi's off/minimal fall outside it.
SUPPORTED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# Kiro mints ids like `sess_<uuid>` on v3 and bare `<uuid>` on v2. Both are
# resumable; agents.py's placeholder (`sssf-<adw>-<agent>-<rand>`) is not.
SESSION_PREFIX = "sess_"

# Marker Kiro embeds in a model-facing tool call's id: `run_command_tooluse_<id>`.
TOOL_ID_MARKER = "_tooluse_"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _is_resumable(value: Optional[str]) -> bool:
    """True only for an id Kiro itself minted, in either engine's shape."""
    if not value:
        return False
    candidate = value[len(SESSION_PREFIX):] if value.startswith(SESSION_PREFIX) else value
    try:
        uuid.UUID(candidate)
        return True
    except ValueError:
        return False


def _context_tokens(meta: dict) -> int:
    """Absolute token occupancy, summed from Kiro's own context breakdown.

    The sibling `contextUsage.usagePercentage` is deliberately NOT used to
    derive this. Measured on Kiro CLI 2.21.0, that percentage reconciles with
    neither the breakdown nor the catalog: two snapshots in a single run
    reported 9366 tokens at 0.90% and 7008 tokens at 10.83%, implying context
    ceilings of ~1.04M and ~65k tokens for a model the catalog calls 200k.
    Multiplying it by the window inflated occupancy roughly threefold. The
    per-component counts are absolute and mutually consistent, so they are
    summed and the percentage is ignored.

    Only the top level is summed: `tools` already totals its own `builtin` and
    `mcp` children, and `contextFiles` already totals its `items`, so
    descending would double count.
    """
    breakdown = meta.get("breakdown")
    if not isinstance(breakdown, dict):
        return 0
    return sum(int(part.get("tokens") or 0)
               for part in breakdown.values() if isinstance(part, dict))


@lru_cache(maxsize=1)
def _model_catalog() -> dict[str, int]:
    """`model_id -> context_window_tokens`, from Kiro's own model list.

    Empty when the CLI is missing, unauthenticated, or changes this output:
    callers must treat empty as "unknown", never as "no models exist", or a
    missing CLI would be reported as a bad model name.
    """
    try:
        result = subprocess.run(
            [KIRO_PATH, "chat", "--list-models", "--format", "json"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, env=operator_env(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    catalog = {}
    for model in payload.get("models", []) or []:
        model_id = model.get("model_id")
        if model_id:
            catalog[model_id] = int(model.get("context_window_tokens") or 0)
    return catalog


def _label(tool: str, args: dict, title: str) -> str:
    """One-line human name for a tool call: `run_command: echo hello`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value or title).split())
    return f"{tool}: {_clip(value, LABEL_CHARS)}" if value else tool


def _is_internal_call(update: dict) -> bool:
    """True when Kiro is reporting its OWN bookkeeping call, not the model's.

    v3 names its internal calls in `_meta.kiro.toolId` and leaves that key
    empty for a model-facing call, whose name survives only in the prose
    `title` (see `_tool_name`). Measured on 2.21.0, every model-facing call
    also carries a `tooluse`-prefixed `toolCallId` while internal ones carry a
    bare uuid. The `toolName` guard keeps v2 semantics, where that key holds a
    real tool name and must not be mistaken for bookkeeping.

    Without this, `fetch_cloud_config` lands in the trace as though the agent
    had chosen to call it, which inflates the tool count of every Kiro phase.
    """
    meta = (update.get("_meta") or {}).get("kiro") or {}
    return bool(meta.get("toolId")) and not meta.get("toolName")


def _turn_credits(meta: dict) -> float:
    """Credits billed for this turn, from `_meta.kiro.promptTurnSummaries`.

    Each `session_info_update` carries only its own turn's summary — measured
    across a two-turn builder session, turn one reported 0.2213 and turn two
    reported 0.2444 as single-element arrays, never cumulatively — so the
    caller sums occurrences rather than taking the last one.
    """
    total = 0.0
    for summary in meta.get("promptTurnSummaries") or []:
        if isinstance(summary, dict) and summary.get("unit") == "credit":
            total += float(summary.get("usage") or 0.0)
    return total


def _tool_name(update: dict) -> str:
    """The tool's id, by the most authoritative route available.

    Kiro does not put the tool name in one fixed place. v2 states it outright
    in `_meta.kiro.toolName`; v3 names its own internal calls in
    `_meta.kiro.toolId` but leaves a model-facing call's name only in the id
    it generated for it (`run_command_tooluse_<id>`). `title` is the last
    resort because it is prose ("Run Command"), not an id.
    """
    meta = ((update.get("_meta") or {}).get("kiro") or {})
    for key in ("toolName", "toolId"):
        if meta.get(key):
            return str(meta[key])
    call_id = str(update.get("toolCallId") or "")
    if TOOL_ID_MARKER in call_id:
        return call_id.split(TOOL_ID_MARKER, 1)[0]
    return str(update.get("title") or "tool")


def _content_text(content) -> str:
    """Text out of an ACP content list: `[{type: content, content: {text}}]`."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict) and inner.get("type") == "text":
            parts.append(inner.get("text", ""))
        elif block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _result_snippet(update: dict) -> str:
    """A tool's output, preferring the rendered content over the raw payload."""
    text = _content_text(update.get("content"))
    if text:
        return text
    raw = update.get("rawOutput")
    if raw in (None, {}, ""):
        return ""
    return raw if isinstance(raw, str) else json.dumps(raw)


class ToolCallTracker:
    """Folds Kiro's tool stream into ONE normalized record per completed call.

    A call is announced by a `tool_call` update and closed by a
    `tool_call_update` carrying a terminal status. Intermediate updates only
    stream output, so emitting on them would put the same call in the trace
    several times — the record is built when the status lands.
    """

    TERMINAL = {"completed", "failed"}

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}
        # Call ids Kiro owns. Only the OPENING update identifies them: measured
        # on 2.21.0, `fetch_cloud_config` announces itself with
        # `_meta.kiro.toolId` and its closing `tool_call_update` carries an
        # empty `_meta` and no title. Dropping updates one at a time therefore
        # filtered the opener and let the closer through, and the closer — with
        # no name anywhere and no open call to match — got traced as a call
        # literally named "tool". The id is the only thing both updates share.
        self._internal: set[str] = set()

    def observe(self, update: dict) -> Optional[dict]:
        kind = update.get("sessionUpdate")
        call_id = str(update.get("toolCallId") or "")
        if not call_id or kind not in ("tool_call", "tool_call_update"):
            return None
        if _is_internal_call(update):
            self._internal.add(call_id)
        if call_id in self._internal:
            return None

        if kind == "tool_call" or call_id not in self._open:
            known = self._open.get(call_id, {})
            self._open[call_id] = {
                "tool": known.get("tool") or _tool_name(update),
                "args": update.get("rawInput") or known.get("args") or {},
                "title": update.get("title") or known.get("title", ""),
                "started_at": known.get("started_at") or now_iso(),
                "clock": known.get("clock") or time.monotonic(),
            }
            if kind == "tool_call":
                return None

        opened = self._open[call_id]
        if update.get("rawInput"):
            opened["args"] = update["rawInput"]
        status = update.get("status")
        if status not in self.TERMINAL:
            return None

        opened = self._open.pop(call_id)
        args = opened["args"] if isinstance(opened["args"], dict) else {}
        record = {
            "tool": opened["tool"],
            "tool_call_id": call_id,
            "args": args,
            "ok": status == "completed",
            "label": _label(opened["tool"], args, opened["title"]),
            "started_at": opened["started_at"],
            "ended_at": now_iso(),
            "duration_ms": int((time.monotonic() - opened["clock"]) * 1000),
        }
        snippet = _result_snippet(update)
        if snippet:
            record["result_snippet"] = _clip(snippet, RESULT_SNIPPET_CHARS)
        return record


def run(request: HarnessRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
    resumable = request.session_id if _is_resumable(request.session_id) else None
    # The system prompt rides in the prompt text only on a fresh turn; a resumed
    # session already holds it. See the module docstring.
    prompt = (request.prompt if resumable
              else f"{request.system_prompt}\n\n{request.prompt}")

    cmd = [KIRO_PATH, "chat",
           "--agent-engine", AGENT_ENGINE,
           "--output-format", "stream-json",
           "--no-interactive",
           "--model", request.model,
           "--effort", request.thinking,
           "--trust-all-tools"]
    if resumable:
        cmd += ["--resume-id", resumable]
    # `--` or nothing: clap reads a prompt that happens to start with a dash as
    # an unknown flag and refuses to run ("error: unexpected argument
    # '--weird prompt' found"), and an agent's prompt is arbitrary text.
    cmd += ["--", prompt]

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = HarnessResult(session_id=resumable or "",
                           context_window=_model_catalog().get(request.model, 0))
    tracker = ToolCallTracker()
    streamed = []            # agent_message_chunk text, the fallback for finalText
    run_error = ""

    # stdin is DEVNULL: the prompt travels in argv, so the child never needs it,
    # and inheriting the parent's lets a non-TTY child wait forever on input
    # that never arrives (see agent_pi.run for the same reasoning).
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    # Drained on a background thread from the moment the child exists: an
    # unread stderr pipe fills and blocks the child's write, which stalls
    # stdout too and looks exactly like a hang (see utils.drain_stderr). The
    # v3 engine is a talkative one — KAS logs every startup step there.
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
                continue                 # v3 mixes plain log lines into stdout
            etype = event.get("type")
            data = event.get("data") or {}
            if data.get("sessionId"):
                result.session_id = data["sessionId"]
            if etype == "sessionUpdate":
                update = data.get("update") or {}
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    streamed.append((update.get("content") or {}).get("text", ""))
                elif kind == "session_info_update":
                    # Occupancy is reported repeatedly; the last one wins,
                    # because context_tokens is "how full is it now", not a sum.
                    meta = (update.get("_meta") or {}).get("kiro") or {}
                    tokens = _context_tokens(meta)
                    if tokens:
                        result.context_tokens = tokens
                    # Credits, unlike occupancy, accumulate: each update bills
                    # its own turn. Kiro reports no per-session token counts at
                    # all, so this is the only cost signal the run ever gets.
                    result.usage.credits += _turn_credits(meta)
                else:
                    # Kiro's own bookkeeping calls are filtered inside the
                    # tracker, which is the only place that sees every update
                    # of a given call id.
                    record = tracker.observe(update)
                    if record and on_event:
                        on_event(record)
            elif etype == "runFinished":
                final = data.get("finalText") or ""
                # A truncated finalText is a worse answer than the chunks that
                # were streamed in full, so it only wins when it is complete.
                result.text = ("".join(streamed) if data.get("finalTextTruncated")
                               and streamed else final or "".join(streamed))
            elif etype == "runError":
                run_error = str(data.get("message") or "")

    result.returncode = process.wait()
    stderr = stderr_getter()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0:
        detail = run_error or stderr.strip()[-800:]
        raise RuntimeError(f"kiro-cli exited {result.returncode}: {detail}")
    return result


class KiroCliAdapter:
    """The HarnessAdapter for coding_agent: kiro_cli. See harnesses.HarnessAdapter."""

    def validate(self, agent: AgentConfig) -> list[str]:
        problems = []
        if agent.harness_engineering:
            problems.append(
                "harness_engineering is Pi-only in this MVP — coding_agent "
                "'kiro_cli' cannot use it; clear the list or set coding_agent: pi")
        if agent.thinking not in SUPPORTED_EFFORTS:
            problems.append(
                f"thinking {agent.thinking!r} is not a Kiro CLI effort level — "
                f"supported: {sorted(SUPPORTED_EFFORTS)}")
        if agent.tools is not None:
            problems.append(
                f"tools {agent.tools!r} cannot be honored — Kiro CLI's "
                f"--trust-tools takes the v3 engine's own tool ids, not Pi's "
                "names, and an unrecognized name DENIES the tool rather than "
                "allowing it (see agent_kirocli.py). Set tools: null explicitly "
                "on this agent (it would otherwise inherit defaults.tools), or "
                "set defaults.tools: null when the whole roster runs on kiro_cli "
                "and/or antigravity, or switch to coding_agent: pi/claude_code")
        catalog = _model_catalog()
        if catalog and agent.model not in catalog:
            problems.append(
                f"model {agent.model!r} is not in `kiro-cli chat --list-models` — "
                f"available: {sorted(catalog)}")
        return problems

    def run(self, request: HarnessRequest,
            on_event: Optional[Callable[[dict], None]] = None,
            on_spawn: Optional[Callable[[int], None]] = None,
            on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
        return run(request, on_event, on_spawn, on_exit)
