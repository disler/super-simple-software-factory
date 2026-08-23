"""Shared contracts used by every configured command harness."""

from __future__ import annotations

import re
from typing import Callable, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .diagnostics import sanitize_diagnostic, sanitize_text

HarnessName = Literal["pi", "claude_code", "codex"]
PortableCapability = Literal["read", "search", "shell", "write", "edit"]
SandboxAccess = Literal["read-only", "workspace-write"]
ProcessCallback = Callable[[int], None]
PI_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class UsageBreakdown(BaseModel):
    """Tokens and the dollars they cost, per component, summed over a call.

    Mirrors pi's `usage` shape one-for-one so the numbers reconcile with what
    pi itself reports: `input` EXCLUDES cache reads, which bill at their own
    (cheaper) rate — add them to learn the size of the prompt that was sent.

    Example: ``UsageBreakdown().merge(retry_usage)`` records a retried call.
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

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage.

        Example: ``phase_usage.merge(call_usage)`` accumulates retries.
        """
        for field in self.model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))


class HarnessProfile(BaseModel):
    harness: HarnessName
    model: str
    effort: str
    tools: list[str] | None = None
    extensions: list[str] = Field(default_factory=list)
    writes: list[str] | None = None


class HarnessRequest(BaseModel):
    profile: HarnessProfile
    prompt: str
    system_prompt: str
    output_schema: dict[str, object]
    session_id: str | None = None
    runtime_dir: str
    raw_output_path: str
    cwd: str


class HarnessEvent(BaseModel):
    name: str
    payload: dict[str, object] = Field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None

    @model_validator(mode="after")
    def redact_diagnostic_payload(self) -> "HarnessEvent":
        """Redact event diagnostics while the adapter retains its parsed protocol event."""
        self.name = sanitize_text(self.name)
        sanitized = sanitize_diagnostic(self.payload)
        self.payload = sanitized if isinstance(sanitized, dict) else {}
        return self


class HarnessResult(BaseModel):
    text: str = ""
    returncode: int = 0
    session_id: str
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    context_tokens: int = 0
    context_window: int = 0


class ResolvedCapabilities(BaseModel):
    portable: frozenset[PortableCapability]
    native_tools: tuple[str, ...]
    sandbox: SandboxAccess


class HarnessError(RuntimeError):
    """Base error that identifies an invalid harness value and expected shape."""

    category = "harness"

    def __init__(self, harness: str, value: object, expected: str) -> None:
        safe_value = sanitize_diagnostic(value)
        message = (f"{self.category} error for harness {sanitize_text(harness)!r}: "
                   f"value {safe_value!r}; expected {sanitize_text(expected)}")
        super().__init__(message)


class HarnessConfigError(HarnessError):
    """Raised when a profile cannot be translated safely."""

    category = "configuration"


class HarnessUnavailableError(HarnessError):
    """Raised when a requested executable or model is unavailable."""

    category = "availability"


class HarnessProcessError(HarnessError):
    """Raised when a harness process exits without a usable result."""

    category = "process"


class HarnessProtocolError(HarnessError):
    """Raised when harness output does not match its documented protocol."""

    category = "protocol"


def validate_session_id(harness: HarnessName, session_id: str) -> str:
    """Validate an operational harness identifier before it can be resumed.

    Example: ``validate_session_id("codex", "123e4567-e89b-12d3-a456-426614174000")``.
    """
    valid = (_canonical_uuid(session_id)
             if harness in {"claude_code", "codex"} else _pi_id(session_id))
    if valid:
        return session_id
    raise HarnessProtocolError(harness, session_id, _session_id_shape(harness))


def _canonical_uuid(session_id: str) -> bool:
    try:
        return str(UUID(session_id)) == session_id
    except ValueError:
        return False


def _pi_id(session_id: str) -> bool:
    return bool(PI_SESSION_ID.fullmatch(session_id)) and sanitize_text(session_id) == session_id


def _session_id_shape(harness: HarnessName) -> str:
    if harness in {"claude_code", "codex"}:
        return "a canonical lowercase UUID session ID"
    return "a 1-128 character Pi identifier using letters, digits, underscores, and hyphens"


class HarnessAdapter(Protocol):
    """Adapter boundary for a harness-specific process implementation.

    Example: ``adapter.run(request)`` executes one normalized harness call.
    """

    name: HarnessName

    def validate_profile(self, profile: HarnessProfile) -> None: ...

    def new_session_id(self, adw_id: str, agent_name: str) -> str | None: ...

    def run(
        self,
        request: HarnessRequest,
        on_event: Callable[[HarnessEvent], None] | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> HarnessResult: ...
