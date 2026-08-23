"""Pi JSONL terminal protocol state and validation."""

from __future__ import annotations

from collections.abc import Mapping

from .harness_process import ProcessOutcome
from .harness_types import HarnessProcessError, HarnessProtocolError, HarnessResult


class PiTerminalState:
    """Track the terminal markers from one Pi JSONL stream.

    Example: ``state.observe(event)`` records the stream's protocol completion.
    """

    def __init__(self) -> None:
        """Create an empty terminal state for one process stream.

        Example: ``PiTerminalState()`` awaits a final assistant result.
        """
        self.agent_end_count = 0
        self.final_stop_reason: str | None = None
        self.assistant_after_agent_end = False

    def observe(self, event: Mapping[str, object]) -> None:
        """Record the documented Pi terminal event.

        Example: ``state.observe({"type": "agent_end"})`` records completion.
        """
        if event.get("type") == "agent_end":
            self.agent_end_count += 1

    def record_assistant(self, message: Mapping[str, object]) -> bool:
        """Record an assistant stop reason before the terminal marker.

        Example: ``state.record_assistant(message)`` returns whether it is valid.
        """
        if self.agent_end_count:
            self.assistant_after_agent_end = True
            return False
        stop_reason = message.get("stopReason")
        self.final_stop_reason = stop_reason if isinstance(stop_reason, str) else None
        return True

    def require_usable_result(self, result: HarnessResult, outcome: ProcessOutcome,
                              executable: str) -> None:
        """Reject a process result without one valid terminal assistant completion.

        Example: ``state.require_usable_result(result, outcome, "pi")`` validates a turn.
        """
        _require_exit(outcome, executable)
        _require_agent_end(self)
        _require_assistant(result, self.final_stop_reason)


def _require_exit(outcome: ProcessOutcome, executable: str) -> None:
    if outcome.returncode == 0:
        return
    value = f"{executable} exited {outcome.returncode}: {outcome.stderr_tail.strip()}"
    raise HarnessProcessError("pi", value, "exit code 0 with a terminal assistant result")


def _require_agent_end(state: PiTerminalState) -> None:
    if state.agent_end_count != 1:
        raise HarnessProtocolError("pi", state.agent_end_count, "exactly one agent_end marker")
    if not state.assistant_after_agent_end:
        return
    raise HarnessProtocolError("pi", "assistant message after agent_end",
                               "the final assistant message before agent_end")


def _require_assistant(result: HarnessResult, stop_reason: str | None) -> None:
    if stop_reason in {"error", "aborted"}:
        raise HarnessProcessError("pi", stop_reason, "a terminal stopReason of stop or length")
    if stop_reason not in {"stop", "length"}:
        raise HarnessProtocolError("pi", stop_reason,
                                   "a final assistant stopReason of stop or length")
    if result.text.strip():
        return
    raise HarnessProtocolError("pi", result.text, "non-empty final assistant text")
