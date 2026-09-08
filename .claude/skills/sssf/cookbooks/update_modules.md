# Update Modules

Extend `adws/adw_modules/` with new low-level logic.

## The rule

**ALL low-level logic lives in `adw_modules/`; ADW scripts stay thin.** An `adw_*.py` file declares agents, sequences phases, and returns an exit code. Anything else — subprocess handling, parsing, retry mechanics, git plumbing, reusable predicates — goes in a module.

## Where things go

| Module | Owns |
|---|---|
| `data_types.py` | Every Pydantic model: `AgentCall`, `PhaseParams`, `Phase`, `EnvelopeBase` + one output type per agent call, the config models (`AgentConfig`, `SSSFConfig`), `EventRecord`, and the harness contract (`HarnessRequest`/`HarnessResult`) |
| `agents.py` | `load_config`, `validate`, resolving an entry → adapter (via `harnesses.resolve`) + model + thinking + harness extensions. Never branches on a specific `coding_agent` value itself |
| `harnesses.py` | the `HarnessAdapter` Protocol and the `ADAPTERS` registry (`pi` / `claude_code` / `codex` / `kiro_cli` / `antigravity` → adapter instance) |
| `runner.py` | the `Run` object; `run.phase(PhaseParams)` context manager; `ph.call(AgentCall)` |
| `agent_pi.py` | the Pi adapter — non-interactive `pi -p --mode json`, JSONL stream tailed live, model resolved against `~/.pi/agent/models.json`; `--session-id` creates-or-continues, so running and continuing an agent are the same call |
| `agent_claudecode.py` | the Claude Code adapter — `claude -p --output-format stream-json`, `--resume <uuid>` to continue a real session |
| `agent_codex.py` | the Codex adapter — `codex exec --json`, `codex exec resume <thread_id> --json` to continue |
| `agent_kirocli.py` | the Kiro CLI adapter — `kiro-cli chat --agent-engine v3 --output-format stream-json` (ACP events), `--resume-id <id>` to continue. Engine pinned because v2 silently ignores `--model` |
| `agent_antigravity.py` | the Antigravity adapter — `agy -p --output-format stream-json` (init / step_update / result), `--conversation <uuid>` to continue |
| `gates.py` | validation gates over envelope claims |
| `changes.py` | deterministic change capture: resolve the base ref, `git diff` into `context_handoff/changes.diff`, adapt the `ChangeSet` into an envelope an agent can be handed |
| `prompts.py` | load system/user prompt refs from config, render placeholders |
| `session.py` | mint or join `adw_id`, maintain `agent_map.json`, create session dirs incl. `context_handoff/` |
| `tracer.py` | append JSONL **and** insert every event into `sssf.db` as it happens |
| `console.py` | the terminal narrative — every line printed also lands in the db as a `log` event, so the UI reads the same story; plain sequential lines, no spinners |
| `console.py` | the rich stdout reporter — every line printed is ALSO traced as a `log` event (`{message, level}`) so the terminal and the swim-lane UI tell the same story |
| `git_helper.py` | branch, status, diff, commit — the raw plumbing `changes.py` composes |
| `utils.py` | safe subprocess env, logging, `resolve_prompt` |

## Never `print()`

Modules report through `run.console` — never a bare `print()`. Each console method prints a rich line **and** writes it to `sssf.db` as a `log` event with payload `{message, level}`, both from one `_emit` helper, so the terminal narrative and the swim-lane UI can't drift. New output means a new method on `Console`, not a print at the call site.

## The four-param rule

**Any function taking more than 4 parameters gets them converted into a concrete data type in `data_types.py`.** `AgentCall` and `PhaseParams` are the pattern — `run.phase()` and `ph.call()` each take exactly one object. This is skill-wide: every module the factory generates obeys it.

```python
class ReviewParams(BaseModel):
    """Everything review_changes() needs. Passed as one object, never loose params."""
    base_ref: str
    paths: list[str]
    max_diff_lines: int = 2000
    ignore_generated: bool = True
    reviewer: str = "scout"
```

## Adding an output type

Every agent call parses against a concrete type. Extend `EnvelopeBase` — `status`, `summary`, `artifacts`, `notes_for_next_agent` — with only the fields that call actually needs:

```python
class ReviewOutput(EnvelopeBase):
    approved: bool
    blocking: list[str] = []
```

**The output contract is a synced triad — one change means three edits, always together:**

1. The type in `data_types.py` (the enforcer).
2. The agent's `user.md` `## Report` section showing exactly that JSON (the ask).
3. Every call site passing `output_type=` (the binding) — `grep -rn "ReviewOutput" adws/` to find them all.

If the type and the Report example drift, the agent produces what the prompt asked for, the parser rejects what the type expects, and every call burns correction round-trips before landing — a slow, silent tax. Renaming or removing a field is the same triad edit. Schema details: `references/handoff.md`.

## Adding a gate

A gate is a callable — `gate(envelope, run) -> GateReport`. You record **one check per item you look at**, and the harness derives the verdict: any failed check is a violation, and no failed checks means pass.

```python
from adw_modules.data_types import GateReport

def tests_declared_passed(envelope, run) -> GateReport:
    """Verify the envelope's own test claims, after the fact."""
    report = GateReport()
    for f in envelope.failures:
        report.check(f.test, False, f.error)
    report.check("suite", envelope.passed,
                 "all declared tests passed" if envelope.passed
                 else f"{len(envelope.failures)} declared failure(s)")
    return report
```

`report.check(item, ok, note)` appends and returns the report, so a single-item gate is one line: `return GateReport().check(command, ok, f"exit {code}")`.

**Write a note on passing checks too, not just failures.** The note is the evidence, and it is what makes a green gate worth reading — `artifacts_exist ✓ 1 checked · plan.md — exists, 454B` tells you what was verified, where a bare ✓ tells you nothing. Notes on failed checks double as the reason and are what the agent is told, so phrase them as the problem: `"claimed changed file does not exist"`.

Rules that keep gates honest:

- **Verify claims, never predict.** File names and counts are unknowable before the agent finishes; gates check what the envelope declared.
- **Quantity as properties, not counts.** "at least one artifact", "ALL declared paths exist" — never `len(artifacts) == 3`.
- **Record checks, don't raise.** The harness feeds the derived violations back into the same session as a correction — context intact, bounded by the phase's `retries` — and traces every check, passed or failed, to `gate_results.checks_json` and the `gate_pass`/`gate_fail` event payload.
- **Check every item, even after one fails.** Don't early-return on the first problem; the agent fixes more per correction round when it sees every failure at once, and the trace shows the full picture.
- **Don't gate the ungateable.** Plan quality and code taste are a reviewer agent's job or a human's.

A gate that returns a plain `list[str]` of violations still works — the harness adapts it — but it records no evidence for the items that passed, so prefer a `GateReport`.

Reusable gates live in `gates.py`; genuine one-offs can be defined inline at the ADW call site and passed in `gates=[...]`.

## Before you finish

Run the smoke ADW — `uv run adws/adw_prompt.py "ping"` — since every module change rides the same path a real run does.
