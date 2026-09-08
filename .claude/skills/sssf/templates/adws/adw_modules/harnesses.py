"""The one place that knows which coding_agent maps to which adapter.

agents.py resolves an adapter here ONCE per phase and calls only the
HarnessAdapter contract from then on — no `if agent.coding_agent == ...`
anywhere outside this file. Adding a harness later means adding one entry to
ADAPTERS, not touching agents.py.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from .agent_antigravity import AntigravityAdapter
from .agent_claudecode import ClaudeCodeAdapter
from .agent_codex import CodexAdapter
from .agent_kirocli import KiroCliAdapter
from .agent_pi import PiAdapter
from .data_types import AgentConfig, HarnessRequest, HarnessResult


class HarnessAdapter(Protocol):
    """What every coding-agent adapter must implement.

    `validate` returns objective problem strings (empty = fine) — it never
    raises, so agents.py can collect every agent's problems before failing.
    `run` executes exactly one turn: it does not retry, gate, or commit — that
    stays in agents.py, identically for every coding_agent.
    """

    def validate(self, agent: AgentConfig) -> list[str]: ...

    def run(
        self,
        request: HarnessRequest,
        on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
    ) -> HarnessResult: ...


ADAPTERS: dict[str, HarnessAdapter] = {
    "pi": PiAdapter(),
    "claude_code": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
    "kiro_cli": KiroCliAdapter(),
    "antigravity": AntigravityAdapter(),
}


def resolve(coding_agent: str) -> HarnessAdapter:
    try:
        return ADAPTERS[coding_agent]
    except KeyError:
        raise ValueError(
            f"coding_agent {coding_agent!r} has no adapter — "
            f"available: {sorted(ADAPTERS)}") from None
