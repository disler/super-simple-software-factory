"""Antigravity CLI adapter — the HarnessAdapter behind coding_agent: antigravity.

Runs `agy -p <prompt> --output-format stream-json` and tails its NDJSON stdout
line by line, same shape as agent_pi.run(): raw lines land in raw_output_path
verbatim, and normalized tool_call records are the only thing forwarded to the
caller via on_event.

Event schema is Antigravity CLI 1.1.26's headless stream, documented at
antigravity.google/docs/cli/headless and confirmed against the binary: one
`init` event, any number of `step_update` events, and exactly one `result`
event, each line an object whose `event` field names its type and whose payload
hangs off a key of the same name. A `step_update` carries `step_type`
(`user_input`, `agent_response`, `tool`, `checkpoint`), `state` (`ACTIVE` while
running, `DONE` when finished), and on tool steps a `tool_info` of
`{name, parameters, output, error}`. Like the Codex adapter this targets one CLI
version and is not defensive against other schemas beyond ignoring unrecognized
events.

Effort: `agy` sells effort as part of the model slug — `gemini-3.8-flash-low`,
`-medium` and `-high` are three catalog entries, not one model with a knob — and
passing `--effort` alongside such a slug is a hard CLI error rather than a
preference it reconciles. So `--effort` is sent only for a tier-less slug, and
`validate()` rejects a `thinking` that contradicts the slug's own tier instead
of letting the slug quietly win and the roster misreport what ran. See
`_model_effort`.

Flag order is deliberate and was verified, not assumed: `agy`'s help output is
the Go `flag` package's, which stops parsing at the first positional argument.
Because `--print`/`-p` takes the prompt as its VALUE, the flags that follow it
are still parsed — a real run confirms `--output-format stream-json` and
`--model` both take effect when they trail the prompt (`init.model` echoes the
requested model, `init.permission_mode` echoes `always-proceed`). Had `-p` been
a boolean, every flag after the prompt would have been silently dropped.

Usage is the richest of any adapter here: completed `step_update.usage` values
report the tokens spent by this CLI invocation, so they are summed into the
call's usage. The terminal `result.usage` is cumulative across the resumed
conversation, so it is neither billed again nor presented as context occupancy:
it includes previous turns rather than measuring the current window. Antigravity
reports no dollars — it bills AI credits — so `usage.total_cost` stays 0 rather
than carry a number in the wrong unit.

Tools: `tools:` cannot be honored, and nothing is invented to pretend
otherwise. Antigravity's headless mode has no tool-allowlist flag; scoping is
done with `permissions.allow` rules in `~/.gemini/antigravity-cli/settings.json`,
which SSSF does not own and must not rewrite. Silently ignoring `tools` would
let the agent use every tool regardless of what the config asked for, so
`validate()` rejects any effective list — exactly as the Codex adapter does.

Every turn runs with `--dangerously-skip-permissions`, which is a deliberate
choice, not a shortcut. Headless mode SOFT-DENIES a tool it cannot get approval
for: the run continues, exits 0, and only prints a notice to stderr. Shell
commands default to Ask, so without the flag a builder would report success on
a phase whose commands were quietly refused — a green trace over work that
never happened, which is the one failure this system is built to make
impossible. Repo write scope stays where it always is, enforced after the call
by permissions.enforce() against `writes` and `protected_files`. An operator
who wants narrower grants than "all tools, then check the diff" writes
`permissions.allow` rules in the CLI's own settings and they still apply,
because they are consulted before this flag is.

`read_only` is not used to pick a native sandbox. `--sandbox` here means
terminal sandbox restrictions, not Codex's `--sandbox read-only` filesystem
mode, and equating the two would claim a guarantee the flag does not give.

Effort: `--effort` accepts `low | medium | high` only, so `validate()` rejects
the rest of Pi's ladder (`off`, `minimal`, `xhigh`, `max`) at config time
instead of letting the CLI reject it mid-run.

Model: not pre-validated. `agy models` prints a two-column human table whose
exact shape this adapter has not observed, and a parser that misreads it would
fail a valid roster at validate() time. Headless mode already fails loudly on
an unknown `--model` (non-zero exit, `status: ERROR`) rather than silently
falling back, so the CLI is the better judge. `context_window` is therefore
reported as 0 — unknown — rather than guessed.

`--print-timeout` is raised well above the CLI's 5-minute default: a builder
phase implementing a plan routinely runs longer, and the default would kill it
mid-edit.

Session continuation: `--conversation <id>` resumes a specific conversation by
the `conversation_id` a previous run reported. agents.py always offers a
session_id (its own placeholder when there is no prior mapped session for this
coding_agent + model), so that value is treated as resumable only when it
parses as a UUID, exactly like the Claude Code and Codex adapters. `--continue`
is deliberately unused: it resumes "the most recent conversation" in the
directory, which is whichever agent happened to run last, not this one.

Antigravity has no system-prompt flag, so the system prompt is folded into the
prompt text — but only on a FRESH turn. A resumed turn already carries it as the
first thing in that conversation's history, and agents.py only resumes a
conversation created by this same agent, so re-sending it would pay for the
whole system prompt again on every JSON retry and gate correction.

Auth is whatever `agy` is already logged in as on this machine — the adapter
never reads or stores a credential. An unauthenticated non-interactive run
exits with `authentication required` instead of hanging.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .data_types import AgentConfig, HarnessRequest, HarnessResult
from .utils import drain_stderr, now_iso, operator_env

AGY_PATH = os.environ.get("AGY_PATH", "agy")

# The CLI's own default is 5m, which is shorter than real builder phases.
PRINT_TIMEOUT = os.environ.get("AGY_PRINT_TIMEOUT", "60m")

RESULT_SNIPPET_CHARS = 20_000
LABEL_CHARS = 80
# Antigravity's tool parameters are PascalCase (`CommandLine`); the lowercase
# names are here so a tool that uses them still gets a readable label.
PRIMARY_ARGS = ("CommandLine", "command", "AbsolutePath", "path", "file_path",
                "TargetFile", "pattern", "Query", "query", "url")

# `agy --effort`'s own documented range; the rest of Pi's ladder has no equivalent.
SUPPORTED_EFFORTS = {"low", "medium", "high"}

# Every terminal status the CLI documents. Only one of them is a completed run.
OK_STATUS = "SUCCESS"


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


def _label(tool: str, args: dict) -> str:
    """One-line human name for a tool call: `run_command: echo hello`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool}: {_clip(value, LABEL_CHARS)}" if value else tool


def _tool_record(step: dict, started_at: Optional[str]) -> dict:
    """One completed tool step, normalized into the shape every adapter forwards."""
    info = step.get("tool_info") or {}
    tool = str(info.get("name") or step.get("tool_name") or "tool")
    args = info.get("parameters")
    args = args if isinstance(args, dict) else {}
    error = info.get("error") or {}
    record = {
        "tool": tool,
        # Antigravity identifies a step by its index in the conversation, not by
        # a call id, so that index is what makes the row addressable.
        "tool_call_id": f"step_{step.get('step_index')}",
        "args": args,
        "ok": not error,
        "label": _label(tool, args),
        "started_at": started_at or now_iso(),
        "ended_at": now_iso(),
    }
    duration = step.get("duration_seconds")
    if isinstance(duration, (int, float)):
        record["duration_ms"] = int(duration * 1000)
    output = info.get("output") or ""
    if error:
        message = error.get("message") or json.dumps(error)
        output = f"{output}\n{error.get('type', 'error')}: {message}".strip()
    if output:
        record["result_snippet"] = _clip(str(output), RESULT_SNIPPET_CHARS)
    return record


def _add_step_usage(result: HarnessResult, usage: dict) -> None:
    """Add one completed step's usage, which is local to this invocation."""
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    result.usage.input_tokens += input_tokens
    result.usage.output_tokens += output_tokens
    result.usage.cache_read_tokens += usage.get("cache_read_tokens") or 0
    result.usage.reasoning_tokens += usage.get("thinking_tokens") or 0
    # Cache reads and thinking are subsets of input and output respectively.
    result.usage.total_tokens += (usage.get("total_tokens")
                                  or input_tokens + output_tokens)


def _model_effort(model: str) -> str:
    """The effort tier baked into a model slug, or "" when it carries none.

    `agy models` sells effort as part of the model: `gemini-3.8-flash-low`,
    `-medium`, `-high` are three catalog entries, not one model with a knob.
    Passing `--effort` alongside such a slug is a hard error, not a preference
    the CLI reconciles:

        agy exited 1: invalid model selection
        (--model "gemini-3.6-flash-medium" --effort "low"):
        --model gemini-3.6-flash-medium conflicts with --effort=low

    Every slug in the catalog observed on 1.1.26 carries a tier, so in practice
    `--effort` is never sent. It is still sent for a tier-less slug, because
    that is what the documented flag is for.
    """
    for effort in SUPPORTED_EFFORTS:
        if model.endswith(f"-{effort}"):
            return effort
    return ""


@lru_cache(maxsize=1)
def _model_slugs() -> frozenset[str]:
    """The model slugs `agy models` offers, or empty when it cannot be read.

    Output is one model per line as `slug<TAB>Display Name`, preceded by a
    "Fetching available models..." status line — the fetch is per-account, so
    the set is whatever THIS login is entitled to, not a static list.

    An empty set means the CLI is missing, unauthenticated, or offline, and is
    treated as "unknown" rather than "invalid": failing a valid roster because
    the network was down would send the operator after the wrong bug.
    """
    try:
        proc = subprocess.run([AGY_PATH, "models"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60,
                              env=operator_env())
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    slugs = set()
    for line in proc.stdout.splitlines():
        # Only tab-delimited rows are models; the status line carries no tab.
        if "\t" not in line:
            continue
        slug = line.split("\t", 1)[0].strip()
        if slug:
            slugs.add(slug)
    return frozenset(slugs)


def run(request: HarnessRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
    resumable = request.session_id if _is_uuid(request.session_id) else None
    # The system prompt rides in the prompt text only on a fresh turn; a resumed
    # conversation already holds it. See the module docstring.
    prompt = (request.prompt if resumable
              else f"{request.system_prompt}\n\n{request.prompt}")

    cmd = [AGY_PATH, "-p", prompt,
           "--output-format", "stream-json",
           "--print-timeout", PRINT_TIMEOUT,
           "--model", request.model,
           "--dangerously-skip-permissions"]
    # The slug's own tier wins. Sending both is a hard CLI error, and
    # validate() has already rejected a thinking that contradicts the slug.
    if not _model_effort(request.model):
        cmd += ["--effort", request.thinking]
    if resumable:
        cmd += ["--conversation", resumable]

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = HarnessResult(session_id=resumable or "")
    streamed = []                    # agent_response text_delta, in arrival order
    open_steps: dict[int, str] = {}  # step_index -> started_at, from its ACTIVE sighting
    status = ""
    error_message = ""

    # stdin is DEVNULL: the prompt travels in argv, and `--input-format
    # stream-json` (the only mode that reads stdin) is not in use. Inheriting the
    # parent's stdin is what makes a headless `agy` sit waiting for input that
    # never arrives, which looks exactly like a hang.
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    # Drained on a background thread from the moment the child exists: an
    # unread stderr pipe fills and blocks the child's write, which stalls
    # stdout too and looks exactly like a hang (see utils.drain_stderr). This
    # is also where a soft-denied tool's notice arrives.
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
            name = event.get("event")
            if name == "init":
                result.session_id = event.get("conversation_id") or result.session_id
            elif name == "step_update":
                step = event.get("step_update") or {}
                result.session_id = step.get("conversation_id") or result.session_id
                step_type = step.get("step_type")
                if step.get("state") == "DONE" and isinstance(step.get("usage"), dict):
                    _add_step_usage(result, step["usage"])
                if step_type == "agent_response":
                    streamed.append(step.get("text_delta") or "")
                elif step_type == "tool":
                    index = step.get("step_index")
                    if step.get("state") != "DONE":
                        open_steps.setdefault(index, now_iso())
                        continue
                    record = _tool_record(step, open_steps.pop(index, None))
                    if on_event:
                        on_event(record)
            elif name == "result":
                payload = event.get("result") or {}
                result.session_id = payload.get("conversation_id") or result.session_id
                status = str(payload.get("status") or "")
                error_message = str(payload.get("error") or "")
                # `response` is authoritative for the turn that emitted it; the
                # streamed deltas are the fallback when it comes back empty.
                result.text = payload.get("response") or "".join(streamed)
                # Terminal usage is cumulative for the whole conversation, so
                # it is neither this invocation's spend nor context occupancy.

    result.returncode = process.wait()
    stderr = stderr_getter()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0:
        detail = error_message or stderr.strip()[-800:]
        raise RuntimeError(f"agy exited {result.returncode}: {detail}")
    # A documented non-SUCCESS terminal state (CANCELED, INTERRUPTED, INVALID,
    # WAITING, RUNNING) is a failed turn even when the process exits 0, and the
    # envelope it produced must not be treated as an answer.
    if status and status != OK_STATUS:
        raise RuntimeError(f"agy finished with status {status}: "
                           f"{error_message or stderr.strip()[-800:]}")
    return result


class AntigravityAdapter:
    """The HarnessAdapter for coding_agent: antigravity. See harnesses.HarnessAdapter."""

    def validate(self, agent: AgentConfig) -> list[str]:
        problems = []
        if agent.harness_engineering:
            problems.append(
                "harness_engineering is Pi-only in this MVP — coding_agent "
                "'antigravity' cannot use it; clear the list or set coding_agent: pi")
        if agent.thinking not in SUPPORTED_EFFORTS:
            problems.append(
                f"thinking {agent.thinking!r} is not an Antigravity effort level — "
                f"supported: {sorted(SUPPORTED_EFFORTS)}")
        if agent.tools is not None:
            problems.append(
                f"tools {agent.tools!r} cannot be honored — Antigravity's headless "
                "mode has no tool-allowlist flag to map onto; scoping lives in "
                "`permissions.allow` in ~/.gemini/antigravity-cli/settings.json "
                "(see agent_antigravity.py). Set tools: null explicitly on this "
                "agent (it would otherwise inherit defaults.tools), or set "
                "defaults.tools: null when the whole roster runs on kiro_cli "
                "and/or antigravity, or switch to coding_agent: pi/claude_code")
        catalog = _model_slugs()
        if catalog and agent.model not in catalog:
            problems.append(
                f"model {agent.model!r} is not in `agy models` for this account — "
                f"available: {sorted(catalog)}")
        # The tier is part of the slug, so thinking and model can disagree.
        # Letting the slug quietly win would make the roster lie about what ran.
        baked = _model_effort(agent.model)
        if baked and agent.thinking != baked:
            problems.append(
                f"thinking {agent.thinking!r} contradicts model {agent.model!r}, whose "
                f"own effort tier is {baked!r} — Antigravity sells effort as part of "
                f"the model slug, and `--effort` cannot be passed alongside one "
                f"(the CLI rejects the pair). Set thinking: {baked} to match, or pick "
                f"the {agent.thinking} variant of the model")
        return problems

    def run(self, request: HarnessRequest,
            on_event: Optional[Callable[[dict], None]] = None,
            on_spawn: Optional[Callable[[int], None]] = None,
            on_exit: Optional[Callable[[int], None]] = None) -> HarnessResult:
        return run(request, on_event, on_spawn, on_exit)
