"""Lookup configured command harness adapters by their stable names."""

from __future__ import annotations

from . import agent_cc, agent_codex, agent_pi
from .harness_types import HarnessAdapter, HarnessConfigError, HarnessName

ADAPTERS: dict[HarnessName, HarnessAdapter] = {
    "pi": agent_pi.ADAPTER,
    "claude_code": agent_cc.ADAPTER,
    "codex": agent_codex.ADAPTER,
}


def get_adapter(name: HarnessName) -> HarnessAdapter:
    """Return the adapter registered for one configured harness.

    Example: ``get_adapter("codex").run(request)`` executes a Codex turn.
    """
    adapter = ADAPTERS.get(name)
    if adapter is not None:
        return adapter
    raise HarnessConfigError("registry", name, f"one of {sorted(ADAPTERS)}")
