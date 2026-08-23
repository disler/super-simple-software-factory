"""The Run object: config + adw_id + agent_map + tracer + console, bound once.

`run.phase(PhaseParams(...))` is the ONE phase primitive — a context manager
for all three kinds (engineer, agent, code). Success must be earned: every
phase defaults to fail; only a clean exit flips it (agent phases additionally
require a parsed envelope + green gates, enforced inside ph.call).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import agents
from .console import Console
from .data_types import (AgentCall, EnvelopeBase, EventRecord, Phase, PhaseParams,
                         SSSFConfig)
from .diagnostics import sanitize_diagnostic, sanitize_text
from .runtime_paths import RuntimePaths
from .tracer import Tracer
from .utils import ensure_dir, now_iso


@dataclass(frozen=True)
class RunContext:
    """Dependencies and fixed filesystem paths needed to construct one run.

    Example: ``Run(RunContext(cfg, adw_id, tracer, engineer, paths))``.
    """

    cfg: SSSFConfig
    adw_id: str
    tracer: Tracer
    engineer: str
    runtime_paths: RuntimePaths


def _load_agent_map(path: Path) -> dict[str, dict[str, object]]:
    """Load persisted per-agent session records with a concrete mapping shape.

    Example: ``_load_agent_map(path)["builder"]["session_id"]`` reads a session.
    """
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise RuntimeError(f"agent map {path!s} expected an object, found {type(raw).__name__}")
    return _validate_agent_map(path, raw)


def _validate_agent_map(path: Path, raw: dict[object, object]) -> dict[str, dict[str, object]]:
    """Reject session-map entries that cannot safely participate in resumption.

    Example: ``_validate_agent_map(path, {"builder": {}})`` returns that entry.
    """
    entries: dict[str, dict[str, object]] = {}
    for agent, entry in raw.items():
        if not isinstance(agent, str) or not isinstance(entry, dict):
            raise RuntimeError(f"agent map {path!s} expected string keys with object values, found {agent!r}")
        entries[agent] = {str(key): value for key, value in entry.items()}
    return entries


class PhaseHandle:
    def __init__(self, run: "Run", phase: Phase):
        self.run = run
        self.phase = phase

    def log(self, **payload) -> None:
        self.run.tracer.event(EventRecord(adw_id=self.run.adw_id,
                                          phase_id=self.phase.phase_id,
                                          type="log", name=self.phase.params.name,
                                          payload=sanitize_diagnostic(payload)))
        line = ", ".join(f"{key}: {sanitize_diagnostic(value)}"
                         for key, value in payload.items())
        self.run.console.note(line)
        if self.phase.params.kind == "engineer" and "input" in payload:
            self.run.tracer.session_request(self.run.adw_id, str(payload["input"]))

    def call(self, call: AgentCall) -> EnvelopeBase:
        if self.phase.params.kind != "agent":
            raise RuntimeError("ph.call() is only valid inside an agent phase")
        return agents.execute(self.run, self.phase, call)


class Run:
    def __init__(self, context: RunContext) -> None:
        self.cfg = context.cfg
        self.adw_id = context.adw_id
        self.tracer = context.tracer
        self.console = Console(context.tracer, context.adw_id)
        self.engineer = context.engineer
        self.phases: list[Phase] = []
        self.tokens = 0
        self.cost = 0.0
        self._seq = context.tracer.max_phase_seq(context.adw_id)
        self.repo_root = context.runtime_paths.repo_root
        self.session_dir = ensure_dir(context.runtime_paths.session_dir)
        self.context_handoff_dir = ensure_dir(self.session_dir / "context_handoff")
        self._agent_map_path = self.session_dir / "agent_map.json"
        self.agent_map: dict[str, dict[str, object]] = _load_agent_map(self._agent_map_path)

    # ── agent map (adw_id -> per-agent coding-agent session ids) ────────────
    def save_agent_map(self, agent: str, entry: dict[str, object]) -> None:
        self.agent_map[agent] = entry
        self._agent_map_path.write_text(json.dumps(self.agent_map, indent=2))

    # ── usage (run totals mirror what the tracer accumulates in sqlite) ─────
    def add_usage(self, tokens: int, cost: float) -> None:
        self.tokens += tokens
        self.cost += cost
        self.tracer.session_add_usage(self.adw_id, tokens, cost)

    # ── the phase primitive ─────────────────────────────────────────────────
    @contextmanager
    def phase(self, params: PhaseParams):
        self._seq += 1
        phase = Phase(phase_id=f"{self.adw_id}_{self._seq:02d}_{params.name}",
                      adw_id=self.adw_id, seq=self._seq, params=params,
                      status="running", started_at=now_iso())
        self.phases.append(phase)
        self.tracer.phase_upsert(phase)
        self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                      type="phase_start", name=params.name,
                                      payload={"kind": params.kind, "owner": params.owner,
                                               "description": params.description}))
        self.console.phase_started(phase)
        clock = time.monotonic()
        try:
            yield PhaseHandle(self, phase)
        except BaseException as error:
            phase.status = "fail"                      # success must be earned
            phase.error = sanitize_text(str(error))[:1000]
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="error", name=params.name,
                                          payload={"error": phase.error}))
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "fail"}))
            self.tracer.phase_upsert(phase)
            self.tracer.session_finish(self.adw_id, ok=False)
            self.console.phase_ended(phase, time.monotonic() - clock)
            self.console.session_finished(False, self.tokens, self.cost,
                                          self.cfg.observability.db)
            raise
        else:
            phase.status = "success"
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "success"}))
            self.tracer.phase_upsert(phase)
            self.console.phase_ended(phase, time.monotonic() - clock)

    # ── run outcome ─────────────────────────────────────────────────────────
    def finish(self, accepted: bool = True, reason: str = "") -> int:
        """Finalize the run and return its exit code. Call this exactly once.

        Two criteria, not one. Every phase must have passed, AND the ADW's own
        acceptance test must hold. They are different questions on purpose: a
        test phase that ran the suite did its job even when the suite came back
        red, so the PHASE succeeds while the RUN must not.

        This replaces a `succeeded` property that answered only the first
        question — and, being a property with side effects, wrote the session
        status and printed the banner before the caller's `and test.passed` was
        ever evaluated. A run whose suite never passed was recorded green in the
        db, on the terminal, and in the UI while exiting 1. Anyone reading the
        trace saw success; only a CI job checking `$?` saw the truth. One call
        now settles the db, the banner, and the exit code together, so the three
        cannot disagree.
        """
        phases_ok = bool(self.phases) and all(p.status == "success" for p in self.phases)
        ok = phases_ok and accepted
        if phases_ok and not accepted:
            note = reason or "the run's acceptance criterion was not met"
            self.tracer.event(EventRecord(
                adw_id=self.adw_id,
                phase_id=self.phases[-1].phase_id if self.phases else "",
                type="error", name="not_accepted", payload={"reason": note}))
            self.console.note(f"not accepted: {note}")
        self.tracer.session_finish(self.adw_id, ok=ok)
        self.console.session_finished(ok, self.tokens, self.cost, self.cfg.observability.db)
        return 0 if ok else 1
