"""Concrete data types for the SSSF ADW system.

RULE (four-param rule): any function that takes more than 4 parameters takes
ONE of these objects instead. AgentCall and PhaseParams are the pattern.

Every agent call declares a concrete output type — an EnvelopeBase subclass —
that its final JSON response is parsed against. No untyped handoffs.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional, Type

from pydantic import BaseModel, Field, ValidationInfo, field_validator

PhaseKind = Literal["engineer", "agent", "code"]
PhaseStatus = Literal["queued", "running", "success", "fail"]


# ── Phases ────────────────────────────────────────────────────────────────────

class PhaseParams(BaseModel):
    """Everything run.phase() needs. Passed as one object, never loose params."""

    name: str                       # short id, unique within the run: "plan", "build"
    kind: PhaseKind                 # which lane the block renders in
    owner: str                      # engineer's name, "git", or an agent name from config
    description: str                # REQUIRED: what this phase does and why — see below
    retries: int = 0                # agent phases: gate-failure retries via continue

    @field_validator("description")
    @classmethod
    def _description_must_be_earned(cls, value: str, info: ValidationInfo) -> str:
        """A phase name identifies; a description explains. Both are required.

        The description is the only sentence the trace, the console, and the
        phase block in the UI ever show about intent — everything else is ids,
        statuses, and timings. `commit_plan: "Commit the plan"` tells a reader
        nothing they could not already see, so an echo is rejected the same way
        a blank one is. This is a construction-time error on purpose: it fires
        before the phase opens, not after a run is already in the trace.
        """
        text = " ".join(value.split())
        name = str(info.data.get("name", "?"))
        if not text:
            raise ValueError(
                f"phase {name!r}: description is required — one sentence on what this "
                f"phase does and why. It is what the trace and the UI show.")
        if text.rstrip(".").casefold() == name.replace("_", " ").casefold():
            raise ValueError(
                f"phase {name!r}: description {text!r} only restates the phase name — "
                f"say what it does and why instead.")
        return text


class Phase(BaseModel):
    """The persisted phase record — PhaseParams plus lifecycle."""

    phase_id: str
    adw_id: str
    seq: int
    params: PhaseParams
    status: PhaseStatus = "fail"    # success must be earned
    attempt: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Envelopes (agent output types) ───────────────────────────────────────────

class EnvelopeBase(BaseModel):
    """Base of every agent's final JSON response. Output types extend this."""

    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""


class GenericOutput(EnvelopeBase):
    pass


class PlanOutput(EnvelopeBase):
    # Subject for committing the PLAN — the spec file the planner wrote, not the
    # implementation it describes. Each agent's commit_message covers its own
    # work product, so a chain that commits per step never reuses one agent's
    # words for another agent's diff.
    commit_message: str = ""


class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""        # consumed by the git commit phase


class ScoutFinding(BaseModel):
    file: str
    note: str = ""


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    """One thing the request (or plan) asked for, and whether it is there."""

    requirement: str                # the ask, in the requester's words
    met: bool
    evidence: str = ""              # where it lives, or what is missing


class ReviewOutput(EnvelopeBase):
    """Confirmation that what was built is what was asked for — not a test run."""

    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)   # what must change before approval


class DocumentOutput(EnvelopeBase):
    """Where the write-up of a completed change landed."""

    document_path: str = ""         # the doc in the repo, e.g. app_docs/<adw_id>_<slug>.md
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


# ── Deterministic quality blocks ─────────────────────────────────────────────

QualityArea = Literal["frontend", "backend"]
QualityOperation = Literal["lint", "typecheck", "build"]


class QualityCheckSpec(BaseModel):
    """One deterministic quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    argv: list[str]
    timeout_seconds: int = 120


class QualityCheckResult(BaseModel):
    """Captured evidence from one quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_artifact: str
    # The tail of stdout+stderr, verbatim and unparsed. A failure has to travel
    # back to the builder as an envelope, and the builder cannot open a log file
    # it was never handed — so the evidence rides along. Deliberately raw: every
    # runner formats failures differently and a generic parser would be
    # confidently wrong. The full log is always at output_artifact.
    output_tail: str = ""


class QualityResult(BaseModel):
    """Aggregate result from a quality block: every check it ran, and the verdict."""

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ── Change capture (git diff, deterministic) ─────────────────────────────────

class ChangeCapture(BaseModel):
    """Everything documentation.capture() needs. One object, never loose params."""

    base: str = "main"              # the ref the work is measured against
    max_diff_lines: int = 2000      # the diff artifact is truncated past this
    include_untracked: bool = True  # a brand-new file is part of the change


class BaseRef(BaseModel):
    """The commit a change is measured from, and why that one.

    `reason` is the line the trace shows. A diff is only as trustworthy as the
    thing it was taken against, so the ADW records that choice instead of
    leaving the reader to infer it.
    """

    ref: str                        # what was asked for: "main", or a pinned sha
    commit: str                     # the commit actually diffed against
    reason: str = ""

    @property
    def label(self) -> str:
        """Display form — a named ref as itself, a pinned raw sha shortened."""
        if len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref):
            return self.ref[:7]
        return self.ref


class ChangeSet(BaseModel):
    """What changed since the base commit — pure git facts, no judgement."""

    base: BaseRef
    files: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""                  # `git diff --stat` output, verbatim
    diff_path: str = ""             # the full diff, written into context_handoff/
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.files or self.untracked)


class ChangesOutput(EnvelopeBase):
    """A ChangeSet shaped as an envelope so an agent can be handed it directly.

    Same adapter idea as VerifyOutput: code computes the diff, the documenter
    consumes it through the one door every agent handoff uses.
    """

    base: str = ""                  # "<ref> @ <commit> — <reason>"
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""             # read this for the full diff


class VerifyOutput(EnvelopeBase):
    """A deterministic result, shaped as an envelope so an agent can consume it.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door a tester agent's report used to —
    the ADW script is the only thing that knows the difference.
    """

    passed: bool = False
    failures: list[str] = Field(default_factory=list)


# ── Agent calls ──────────────────────────────────────────────────────────────

class GateCheck(BaseModel):
    """One thing a gate looked at, and what it found.

    `note` is the evidence — "exists, 2.1KB", "exit 0", "not in the diff". On a
    failed check it doubles as the reason, so it is what the agent is told.
    """

    item: str                       # what was checked: a path, a command, a test
    ok: bool
    note: str = ""


class GateReport(BaseModel):
    """What every gate returns: the checks it ran. Violations are derived.

    Authoring stays a one-liner per item — `report.check(...)` appends and
    returns self, so a gate is a loop and a return.
    """

    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> "GateReport":
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note or 'failed'}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


class AgentCall(BaseModel):
    """One agent invocation: prompt in, typed envelope out, gates verified."""

    model_config = {"arbitrary_types_allowed": True}

    output_type: Type[EnvelopeBase]
    prompt: str
    previous: Optional[EnvelopeBase] = None
    gates: list[Callable] = Field(default_factory=list)   # gate(envelope, run) -> list[str]


# ── Config ───────────────────────────────────────────────────────────────────

class PromptEngineering(BaseModel):
    system: str                     # path to system.md
    user: str                       # path to user.md


# Every key in harnesses.ADAPTERS. Declared once so the defaults and the
# per-agent field cannot drift apart when an adapter is added.
CodingAgent = Literal["pi", "claude_code", "codex", "kiro_cli", "antigravity"]


class AgentConfig(BaseModel):
    name: str
    coding_agent: CodingAgent = "claude_code"
    model: str = "sonnet"
    thinking: str = "medium"        # off | minimal | low | medium | high | xhigh | max
    color: str = ""                 # hex swatch for this agent's lane in the UI
    purpose: str = ""
    prompt_engineering: PromptEngineering
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # allowlist; None = all tools usable
    # What this agent may MODIFY in the repo, enforced in code after every call
    # (see adw_modules/permissions.py). `tools` cannot express this: `bash` runs
    # anything and `write` reaches any path, so an agent's capability list is a
    # statement of intent that nothing checks.
    #   None  -> unrestricted, except `protected_files` and active prompts
    #   []    -> read-only outside the current session runtime
    #   [...] -> only these. A trailing "/" means a directory prefix; a "*"
    #            makes it a glob; anything else is an exact path.
    writes: Optional[list[str]] = None


class ConfigDefaults(BaseModel):
    coding_agent: CodingAgent = "claude_code"
    model: str = "sonnet"
    thinking: str = "medium"
    color: str = ""
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # roster-wide allowlist; None = all tools usable
    # Off-limits to every agent that has not named them in its own `writes`.
    # Active system/user prompts are protected dynamically as well; unlike
    # these patterns, only an exact `writes` entry may unlock a prompt.
    # The factory's own code is the default: an agent must not be able to edit
    # the machinery that decides whether its work passed.
    protected_files: list[str] = Field(default_factory=lambda: [
        "adws/adw_modules/", "adws/adw_sssf_config/", "adws/adw_*.py",
    ])
    data_dir: str = "adws/adw_data"


class ObservabilityConfig(BaseModel):
    db: str = "adws/adw_data/sssf.db"
    poll_ms: int = 500


class SSSFConfig(BaseModel):
    defaults: ConfigDefaults = Field(default_factory=ConfigDefaults)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    agents: list[AgentConfig] = Field(default_factory=list)


# ── Tracing ──────────────────────────────────────────────────────────────────

class EventRecord(BaseModel):
    """One traced event, always logged against adw_id + phase."""

    adw_id: str
    phase_id: str = ""
    type: str                       # phase_start | agent_start | tool_call | handoff | gate_pass | gate_fail | log | agent_end | phase_end | error
    name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: str = ""
    tokens: Optional[int] = None
    # Spans: set both when an event covers real elapsed time (a tool call), so
    # the UI lays it out on a time axis without parsing payload JSON. Left unset,
    # the tracer stamps started_at with the moment the event was recorded.
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Harness adapter contract ─────────────────────────────────────────────────
# One request/result shape shared by every coding-agent adapter (pi, Claude
# Code, Codex, ...). agents.py builds exactly one HarnessRequest per send and
# hands it to whichever adapter harnesses.resolve(agent.coding_agent) returns
# — it never inspects coding_agent itself beyond that one lookup.

class HarnessRequest(BaseModel):
    """Everything one non-interactive coding-agent turn needs."""

    prompt: str
    system_prompt: str
    model: str                      # adapter-specific pattern (pi registry id, Claude Code alias, ...)
    thinking: str = "medium"
    session_id: Optional[str] = None   # None = no prior session to continue
    cwd: str = "."                  # set from run.repo_root — the codebase root agents work in
    raw_output_path: str            # the adapter's raw stdout stream lands here, verbatim
    state_dir: str = ""             # adapter-owned on-disk session state, if it keeps one (pi)
    tools: Optional[list[str]] = None
    harness_engineering: list[str] = Field(default_factory=list)   # pi extensions; empty for Claude Code/Codex
    # True exactly when agent.writes == [] (declared read-only). The one piece
    # of permissions-shaped info an adapter may use to pick a NATIVE read-only
    # sandbox as defense in depth (currently: Codex). permissions.enforce()
    # still runs after every call regardless — this never replaces it.
    read_only: bool = False


class UsageBreakdown(BaseModel):
    """Tokens and the dollars they cost, per component, summed over a call.

    Mirrors pi's `usage` shape one-for-one so the numbers reconcile with what
    pi itself reports: `input` EXCLUDES cache reads, which bill at their own
    (cheaper) rate — add them to learn the size of the prompt that was sent.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Thinking tokens. NOT a fifth component: measured across every session on
    # disk, reasoning is always <= output and the four components above always
    # sum to totalTokens, so reasoning is the thinking SHARE of output, billed
    # at the output rate. Report it nested under output, never added to it.
    reasoning_tokens: int = 0
    total_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    total_cost: float = 0.0
    # Credits, for harnesses that bill in them instead of tokens and dollars.
    # Deliberately NOT folded into total_cost: a credit is not a dollar, and its
    # exchange rate is a per-model multiplier SSSF does not know. Kiro CLI is the
    # case that motivated this — its docs state per-session token counts are not
    # available at all, so credits are the only cost signal it emits, and without
    # this field a whole Kiro run reports "0 tokens · $0.0000" and reads as free.
    credits: float = 0.0

    def add_turn(self, usage: dict, total_tokens: int) -> None:
        """Fold in one pi `message_end` usage object.

        `total_tokens` is passed in rather than re-derived: the caller already
        computes it pi's way (totalTokens, else the sum of the parts).
        """
        cost = usage.get("cost") or {}
        self.input_tokens += usage.get("input") or 0
        self.output_tokens += usage.get("output") or 0
        self.cache_read_tokens += usage.get("cacheRead") or 0
        self.cache_write_tokens += usage.get("cacheWrite") or 0
        self.reasoning_tokens += usage.get("reasoning") or 0
        self.total_tokens += total_tokens
        self.input_cost += cost.get("input") or 0.0
        self.output_cost += cost.get("output") or 0.0
        self.cache_read_cost += cost.get("cacheRead") or 0.0
        self.cache_write_cost += cost.get("cacheWrite") or 0.0
        self.total_cost += cost.get("total") or 0.0

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage — a phase that retries spends more than once."""
        # Read off the class, not the instance: Pydantic 2.11 deprecated the
        # instance attribute and removes it in v3.
        for field in type(self).model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))


class HarnessResult(BaseModel):
    text: str = ""
    returncode: int = 0
    # The REAL session id the adapter used or was assigned — never the
    # placeholder agents.py may have offered in the request. agents.py adopts
    # this value for retries and for agent_map.json; it must not fall back to
    # its own pre-generated id once a real one comes back.
    session_id: str = ""
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    # Context occupancy after the LAST turn — not a sum. usage.total_tokens
    # bills every turn; this is how full the window is right now, which is
    # what the visualizer's context bar measures against `context_window`.
    context_tokens: int = 0
    context_window: int = 0         # 0 when the adapter can't report a ceiling
