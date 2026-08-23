"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import yaml

from . import permissions, prompts
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, Phase, SSSFConfig)
from .harness_types import (HarnessError, HarnessEvent, HarnessProfile,
                            HarnessAdapter, HarnessName, HarnessRequest, HarnessResult,
                            UsageBreakdown)
from .harness_registry import get_adapter
from .repository_snapshot import RepositorySnapshot

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON

if TYPE_CHECKING:
    from .runner import Run


class GateFailure(RuntimeError):
    pass


@dataclass
class AgentExecution:
    """State shared by every send in one agent phase.

    Example: ``_send(execution, "continue")`` retains its harness session.
    """

    run: "Run"
    phase: Phase
    agent: AgentConfig
    adapter: HarnessAdapter
    user_text: str
    session_id: str | None
    request: HarnessRequest
    latest: HarnessResult | None = None
    spent: UsageBreakdown = field(default_factory=UsageBreakdown)


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        _inherit_effort(agent, defaults)
        for key in ("coding_agent", "model", "color", "tools", "writes"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
    return SSSFConfig(**raw)


def _inherit_effort(agent: dict[str, object], defaults: dict[str, object]) -> None:
    if "effort" in agent or "thinking" in agent:
        return
    if "effort" in defaults:
        agent["effort"] = defaults["effort"]
        return
    if "thinking" in defaults:
        agent["thinking"] = defaults["thinking"]


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent."""
    problems = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        try:
            get_adapter(agent.coding_agent).validate_profile(_harness_profile(agent))
        except (HarnessError, ValueError) as error:
            problems.append(f"agent {name!r}: {error}")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run: "Run", phase: Phase, call: AgentCall) -> EnvelopeBase:
    """Run one agent call through prompts, harness, typed parse, and gates.

    Example: ``envelope = execute(run, phase, call)``.
    """
    agent = resolve(run.cfg, phase.params.owner)
    execution = _prepare_execution(run, phase, call, agent)
    tree_before = permissions.snapshot(run)
    try:
        envelope, attempt = _run_exchange(execution, call)
    except BaseException:
        _enforce_permissions(execution, tree_before)
        raise
    touched = _enforce_permissions(execution, tree_before)
    _trace_touched(execution, touched)
    return _finish_execution(execution, call, envelope, attempt)


def _prepare_execution(run: "Run", phase: Phase, call: AgentCall,
                       agent: AgentConfig) -> AgentExecution:
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)
    system_text, user_text = _render_prompts(run, call, agent, agent_dir)
    adapter = get_adapter(agent.coding_agent)
    session_id = _session_id(run, agent, adapter)
    request = _base_request(run, call, agent, agent_dir, system_text)
    execution = AgentExecution(run, phase, agent, adapter, user_text, session_id, request)
    _trace_agent_start(execution)
    return execution


def _render_prompts(run: "Run", call: AgentCall, agent: AgentConfig,
                    agent_dir: Path) -> tuple[str, str]:
    variables = {"prompt": call.prompt,
                 "previous_envelope": call.previous.model_dump_json(indent=2)
                 if call.previous else "(none)",
                 "context_handoff_dir": str(run.context_handoff_dir)}
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)
    return system_text, user_text


def _session_id(run: "Run", agent: AgentConfig,
                adapter: HarnessAdapter) -> str | None:
    existing = reusable_session(run.agent_map.get(agent.name), agent)
    if existing:
        return existing
    return adapter.new_session_id(run.adw_id, agent.name)


def _base_request(run: "Run", call: AgentCall, agent: AgentConfig,
                  agent_dir: Path, system_text: str) -> HarnessRequest:
    return HarnessRequest(
        profile=_harness_profile(agent), prompt="", system_prompt=system_text,
        session_id=None, runtime_dir=_runtime_dir(agent_dir, agent.coding_agent),
        raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
        cwd=str(run.repo_root), output_schema=call.output_type.model_json_schema(),
    )


def _trace_agent_start(execution: AgentExecution) -> None:
    agent, run, phase = execution.agent, execution.run, execution.phase
    run.tracer.event(EventRecord(
        adw_id=run.adw_id, phase_id=phase.phase_id, type="agent_start", name=agent.name,
        payload={"model": agent.model, "effort": agent.effort, "thinking": agent.thinking,
                 "color": agent.color, "session_id": execution.session_id,
                 "harness": agent.coding_agent, "coding_agent": agent.coding_agent,
                 "purpose": agent.purpose, "tools": agent.tools,
                 "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.coding_agent, agent.model, execution.session_id)


def _run_exchange(execution: AgentExecution,
                  call: AgentCall) -> tuple[EnvelopeBase, int]:
    result = _send(execution, execution.user_text)
    envelope, attempt = _parse_with_retries(
        execution.run, execution.phase, call, result, lambda text: _send(execution, text))
    return _run_gates(execution, call, envelope, attempt)


def _send(execution: AgentExecution, prompt_text: str) -> HarnessResult:
    request = execution.request.model_copy(update={"prompt": prompt_text,
                                                   "session_id": execution.session_id})
    result = _run_adapter(execution, request)
    execution.session_id = result.session_id
    save_session(execution.run, execution.agent, result.session_id)
    execution.run.add_usage(result.usage.total_tokens, result.usage.total_cost)
    execution.spent.merge(result.usage)
    execution.latest = result
    return result


def _run_adapter(execution: AgentExecution, request: HarnessRequest) -> HarnessResult:
    run, phase, agent = execution.run, execution.phase, execution.agent
    return execution.adapter.run(
        request, on_event=_event_forwarder(run, phase, agent),
        on_spawn=lambda pid: run.tracer.process_start(
            run.adw_id, "agent", agent.name, pid,
            f"{agent.coding_agent} {agent.name} {agent.model}"),
        on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))


def _run_gates(execution: AgentExecution, call: AgentCall,
               envelope: EnvelopeBase, attempt: int) -> tuple[EnvelopeBase, int]:
    for gate_attempt in range(1, max(1, execution.phase.params.retries + 1) + 1):
        violations = _gate_violations(execution, call, envelope, gate_attempt)
        if not violations:
            return envelope, attempt
        _raise_exhausted_gates(execution, gate_attempt, violations)
        _send_gate_correction(execution, gate_attempt, violations)
        result = _send(execution, _gate_correction(violations))
        envelope, attempt = _parse_with_retries(
            execution.run, execution.phase, call, result, lambda text: _send(execution, text))
    raise AssertionError("gate retry loop ended without a result")


def _gate_violations(execution: AgentExecution, call: AgentCall,
                     envelope: EnvelopeBase, attempt: int) -> list[str]:
    violations: list[str] = []
    for gate in call.gates:
        report = _as_report(gate(envelope, execution.run))
        found = report.violations
        execution.run.tracer.gate_row(execution.phase, gate.__name__, report, attempt)
        _trace_gate_result(execution, gate.__name__, report, attempt, found)
        execution.run.console.gate_result(gate.__name__, report)
        violations.extend(found)
    return violations


def _trace_gate_result(execution: AgentExecution, gate_name: str,
                       report: GateReport, attempt: int, found: list[str]) -> None:
    execution.run.tracer.event(EventRecord(
        adw_id=execution.run.adw_id, phase_id=execution.phase.phase_id,
        type="gate_fail" if found else "gate_pass", name=gate_name,
        payload={"attempt": attempt, "violations": found,
                 "checks": [check.model_dump() for check in report.checks]}))


def _raise_exhausted_gates(execution: AgentExecution, attempt: int,
                           violations: list[str]) -> None:
    if attempt <= execution.phase.params.retries:
        return
    raise GateFailure(f"{execution.agent.name} failed gates after {attempt} attempt(s):\n- "
                      + "\n- ".join(violations))


def _send_gate_correction(execution: AgentExecution, attempt: int,
                          violations: list[str]) -> None:
    execution.phase.attempt = attempt
    execution.run.console.retry(execution.agent.name, attempt, execution.phase.params.retries,
                                f"{len(violations)} gate violation(s)")


def _gate_correction(violations: list[str]) -> str:
    return ("Your previous response failed validation:\n- " + "\n- ".join(violations)
            + "\n\nFix these problems, then re-emit ONLY your Report JSON.")


def _enforce_permissions(execution: AgentExecution,
                         tree_before: RepositorySnapshot) -> list[str]:
    try:
        return permissions.enforce(execution.run, execution.phase, execution.agent, tree_before)
    except permissions.PermissionBreach as breach:
        _trace_permission_breach(execution, breach)
        raise


def _trace_permission_breach(execution: AgentExecution,
                             breach: permissions.PermissionBreach) -> None:
    agent, run, phase = execution.agent, execution.run, execution.phase
    run.tracer.event(EventRecord(
        adw_id=run.adw_id, phase_id=phase.phase_id, type="error", name="permission_breach",
        payload={"agent": agent.name, "error": str(breach), "writes": agent.writes,
                 "protected_files": run.cfg.defaults.protected_files}))


def _trace_touched(execution: AgentExecution, touched: list[str]) -> None:
    if touched:
        execution.run.tracer.event(EventRecord(
            adw_id=execution.run.adw_id, phase_id=execution.phase.phase_id,
            type="log", name="paths_touched",
            payload={"agent": execution.agent.name, "paths": touched}))


def _finish_execution(execution: AgentExecution, call: AgentCall,
                      envelope: EnvelopeBase, attempt: int) -> EnvelopeBase:
    _persist_envelope(execution.run, execution.phase, execution.agent.name, call, envelope,
                      attempt, valid=True)
    _trace_completion(execution, envelope)
    if envelope.status != "success":
        raise RuntimeError(f"{execution.agent.name} reported status={envelope.status!r}: "
                           f"{envelope.summary}")
    return envelope


def _trace_completion(execution: AgentExecution, envelope: EnvelopeBase) -> None:
    context = execution.latest or HarnessResult(session_id=execution.session_id or "")
    _record_agent_session(execution, context)
    _trace_handoff(execution, envelope)
    _trace_agent_end(execution, context)
    execution.run.console.envelope_summary(envelope)
    execution.run.console.agent_finished(execution.agent.name, execution.spent.total_tokens,
                                          execution.spent.total_cost)


def _record_agent_session(execution: AgentExecution, context: HarnessResult) -> None:
    execution.run.tracer.agent_session_row(
        execution.run.adw_id, execution.agent, execution.session_id or "",
        context_tokens=context.context_tokens, context_window=context.context_window)


def _trace_handoff(execution: AgentExecution, envelope: EnvelopeBase) -> None:
    execution.run.tracer.event(EventRecord(
        adw_id=execution.run.adw_id, phase_id=execution.phase.phase_id,
        type="handoff", name=execution.agent.name,
        payload={"artifacts": envelope.artifacts, "summary": envelope.summary}))


def _trace_agent_end(execution: AgentExecution, context: HarnessResult) -> None:
    agent, run, phase, spent = execution.agent, execution.run, execution.phase, execution.spent
    run.tracer.event(EventRecord(
        adw_id=run.adw_id, phase_id=phase.phase_id, type="agent_end", name=agent.name,
        tokens=spent.total_tokens,
        payload={"cost": spent.total_cost, "usage": spent.model_dump(),
                 "harness": agent.coding_agent, "coding_agent": agent.coding_agent,
                 "session_id": execution.session_id, "effort": agent.effort,
                 "thinking": agent.thinking, "returncode": context.returncode,
                 "context_tokens": context.context_tokens,
                 "context_window": context.context_window}))


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def reusable_session(entry: dict[str, object] | None, agent: AgentConfig) -> str | None:
    """Return an existing session only when its harness and model still match.

    Example: ``reusable_session({"session_id": "old", "model": "m"}, agent)`` reuses Pi.
    """
    if not entry:
        return None
    recorded_harness = str(entry.get("coding_agent") or "pi")
    if recorded_harness != agent.coding_agent or entry.get("model") != agent.model:
        return None
    session_id = entry.get("session_id")
    return str(session_id) if session_id else None


def save_session(run: "Run", agent: AgentConfig, session_id: str) -> None:
    """Persist a completed harness session for a later phase or resumed run.

    Example: ``save_session(run, agent, "thread-1")`` records the active thread.
    """
    run.save_agent_map(agent.name, {
        "session_id": session_id,
        "model": agent.model,
        "coding_agent": agent.coding_agent,
        "effort": agent.effort,
    })


def _harness_profile(agent: AgentConfig) -> HarnessProfile:
    return HarnessProfile(harness=agent.coding_agent, model=agent.model,
                          effort=agent.effort or "medium", tools=agent.tools,
                          extensions=agent.harness_engineering, writes=agent.writes)


def _runtime_dir(agent_dir: Path, harness: HarnessName) -> str:
    """Return the storage location expected by a harness.

    Example: ``_runtime_dir(Path("run"), "pi")`` preserves Pi's session store.
    """
    if harness == "pi":
        return str((agent_dir / "pi_sessions").resolve())
    return str((agent_dir / "harness_sessions").resolve())


def _event_forwarder(run: "Run", phase: Phase,
                     agent: AgentConfig) -> Callable[[HarnessEvent], None]:
    """Forward a completed normalized tool event into the current phase trace."""
    def forward(event: HarnessEvent) -> None:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="tool_call", name=event.name,
                                     started_at=event.started_at, ended_at=event.ended_at,
                                     payload={**event.payload, "agent": agent.name,
                                              "harness": agent.coding_agent,
                                              "coding_agent": agent.coding_agent}))
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response; on failure, continue the same session (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(json.dumps(record, indent=2))
