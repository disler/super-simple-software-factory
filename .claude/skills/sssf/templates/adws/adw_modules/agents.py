"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml

from . import permissions, prompts
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, Phase, SSSFConfig)
from .harness_types import (HarnessError, HarnessEvent, HarnessProfile,
                            HarnessRequest, HarnessResult, UsageBreakdown)
from .harness_registry import get_adapter

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON


class GateFailure(RuntimeError):
    pass


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

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> harness run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    adapter = get_adapter(agent.coding_agent)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    active_session_id = reusable_session(run.agent_map.get(agent.name), agent)
    if active_session_id is None:
        active_session_id = adapter.new_session_id(run.adw_id, agent.name)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "effort": agent.effort,
                                          "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": active_session_id,
                                          "harness": agent.coding_agent,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.coding_agent, agent.model, active_session_id)

    # Parse retries and gate corrections re-enter the SAME harness session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: HarnessResult | None = None
    spent = UsageBreakdown()
    base_request = HarnessRequest(
        profile=_harness_profile(agent), prompt="", system_prompt=system_text,
        session_id=None, runtime_dir=str((agent_dir / "harness_sessions").resolve()),
        raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
        cwd=str(run.repo_root), output_schema=call.output_type.model_json_schema(),
    )

    def send(prompt_text: str) -> HarnessResult:
        nonlocal active_session_id, latest
        request = base_request.model_copy(update={"prompt": prompt_text,
                                                  "session_id": active_session_id})
        result = adapter.run(
            request, on_event=_event_forwarder(run, phase, agent),
            on_spawn=lambda pid: run.tracer.process_start(
                run.adw_id, "agent", agent.name, pid,
                f"{agent.coding_agent} {agent.name} {agent.model}"),
            on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        active_session_id = result.session_id
        save_session(run, agent, active_session_id)
        run.add_usage(result.usage.total_tokens, result.usage.total_cost)
        spent.merge(result.usage)
        latest = result
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # claim gates — violations flow back into the SAME session as corrections
    for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
        violations = []
        for gate in call.gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="gate_fail" if found else "gate_pass", name=gate.__name__,
                payload={"attempt": gate_attempt, "violations": found,
                         "checks": [c.model_dump() for c in report.checks]}))
            run.console.gate_result(gate.__name__, report)
            violations.extend(found)
        if not violations:
            break
        if gate_attempt > phase.params.retries:
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt, phase.params.retries,
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    try:
        touched = permissions.enforce(run, phase, agent, tree_before)
    except permissions.PermissionBreach as breach:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": run.cfg.defaults.protected_files}))
        raise
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, active_session_id or "",
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost,
                                          "usage": spent.model_dump(),
                                          "harness": agent.coding_agent,
                                          "coding_agent": agent.coding_agent,
                                          "session_id": active_session_id,
                                          "effort": agent.effort,
                                          "thinking": agent.thinking,
                                          "returncode": context.returncode,
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


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


def save_session(run, agent: AgentConfig, session_id: str) -> None:
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


def _event_forwarder(run, phase: Phase, agent: AgentConfig):
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
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
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
