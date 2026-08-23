"""Shared contracts used by every configured command harness."""

from __future__ import annotations

from typing import Callable, Literal, Protocol

from pydantic import BaseModel, Field

HarnessName = Literal["pi", "claude_code", "codex"]
PortableCapability = Literal["read", "search", "shell", "write", "edit"]
SandboxAccess = Literal["read-only", "workspace-write"]
ProcessCallback = Callable[[int], None]


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
        message = (f"{self.category} error for harness {harness!r}: value {value!r}; "
                   f"expected {expected}")
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
