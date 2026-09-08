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

from . import harnesses, permissions, prompts
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, HarnessRequest, HarnessResult,
                         Phase, SSSFConfig, UsageBreakdown)
from .utils import new_id

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON


class GateFailure(RuntimeError):
    pass


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "thinking", "color", "tools", "writes"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
    return SSSFConfig(**raw)


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def _prompt_symlink_problems(agent: AgentConfig, repo_root: Path) -> list[str]:
    problems = []
    for label, ref in (("system", agent.prompt_engineering.system),
                       ("user", agent.prompt_engineering.user)):
        component = permissions.prompt_symlink_component(repo_root, ref)
        if component is not None:
            problems.append(
                f"agent {agent.name!r}: {label} prompt must use a direct path; "
                f"symlink component: {component}")
    return problems


def _roster_prompt_symlink_problems(cfg: SSSFConfig, repo_root: Path) -> list[str]:
    return [problem for configured_agent in cfg.agents
            for problem in _prompt_symlink_problems(configured_agent, repo_root)]


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent.

    Adapter-specific checks (model resolution, harness_engineering support,
    ...) live in each adapter's own `validate()` — this function never asks
    what coding_agent an agent uses beyond looking its adapter up.
    """
    # Every configured prompt is an active protected path, even when this ADW
    # does not require its agent. Scan only path safety roster-wide; model and
    # existence validation below remains scoped to `required`.
    problems = _roster_prompt_symlink_problems(cfg, Path.cwd())
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        try:
            adapter = harnesses.resolve(agent.coding_agent)
        except ValueError as e:
            problems.append(f"agent {name!r}: {e}")
            continue
        problems.extend(f"agent {name!r}: {p}" for p in adapter.validate(agent))
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> pi run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    prompt_problems = _roster_prompt_symlink_problems(run.cfg, Path(run.repo_root))
    if prompt_problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(prompt_problems))
    adapter = harnesses.resolve(agent.coding_agent)   # resolved once; the rest of this
                                                       # function only calls the common contract
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

    session_id = _agent_session_id(run, agent)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: HarnessResult | None = None
    spent = UsageBreakdown()

    # This immutable phase baseline is only for the final paths_touched event.
    # Enforcement gets a fresh baseline immediately before each harness turn,
    # so a breach is rolled back before parsing or any retry can run.
    phase_before = permissions.snapshot(run)

    def enforce_turn(before) -> None:
        try:
            permissions.enforce(run, phase, agent, before)
        except permissions.PermissionBreach as breach:
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="error", name="permission_breach",
                payload={"agent": agent.name, "error": str(breach),
                         "writes": agent.writes,
                         "protected_files": run.cfg.defaults.protected_files}))
            raise

    def send(prompt_text: str) -> HarnessResult:
        nonlocal latest, session_id
        request = HarnessRequest(
            prompt=prompt_text,
            system_prompt=system_text,
            model=agent.model,
            thinking=agent.thinking,
            session_id=session_id,
            # absolute: these are read by the coding-agent subprocess, which runs in repo_root.
            # "pi_sessions" is a historical name, kept as-is on purpose: it is where pi's own
            # `--session-dir` has always pointed, and renaming it would orphan every session
            # already on disk for an upgrading install. Only pi reads this directory; other
            # adapters receive the same path and are free to ignore it.
            state_dir=str((agent_dir / "pi_sessions").resolve()),
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            tools=agent.tools,
            harness_engineering=agent.harness_engineering,
            cwd=str(run.repo_root),
            read_only=(agent.writes == []),
        )
        turn_before = permissions.snapshot(run)
        try:
            result = adapter.run(
                request,
                on_event=_event_forwarder(run, phase, agent.name),
                on_spawn=lambda pid: run.tracer.process_start(
                    run.adw_id, "agent", agent.name, pid,
                    f"{agent.coding_agent} {agent.name} {agent.model}"),
                on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        except BaseException as adapter_error:
            try:
                enforce_turn(turn_before)
            except permissions.PermissionBreach as breach:
                raise breach from adapter_error
            raise
        enforce_turn(turn_before)
        # Adopt the REAL id the adapter used or was assigned — never the
        # placeholder agents.py offered — so retries within this phase, and
        # the entry saved to agent_map.json, continue the actual session.
        if result.session_id:
            session_id = result.session_id
        run.add_usage(result.usage.total_tokens, result.usage.total_cost,
                      result.usage.credits)
        spent.merge(result.usage)
        latest = result
        return result

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

    touched = permissions.changed_paths(phase_before, permissions.snapshot(run))
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model,
                                    "coding_agent": agent.coding_agent,
                                    "read_only": agent.writes == []})
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
                                          "credits": spent.credits,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost,
                               spent.credits)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session_id(run, agent: AgentConfig) -> str:
    """Reuse a mapped session only when coding_agent, model, AND permission
    class (read_only) all still match — a session built by a different
    harness, model, or write/read-only class is not a safe context window to
    rejoin, no matter how similar the id looks.

    read_only matters beyond bookkeeping: some adapters (Codex) can only pick
    a native read-only sandbox on a FRESH turn — `codex exec resume` has no
    `--sandbox` flag — so a session that started writable and would now be
    read-only must never be resumed, or it silently keeps running with the
    write access it started with. Requiring an exact match forces a new
    session in that direction instead (agents.py stays adapter-agnostic:
    every coding_agent is held to the same rule, whether or not it happens to
    use read_only for anything).

    `entry.get("read_only")` on an agent_map.json entry written before this
    field existed returns None, which never equals a real True/False — so a
    pre-upgrade session always starts fresh rather than being trusted under
    an unknown permission history.
    """
    entry = run.agent_map.get(agent.name)
    read_only = agent.writes == []
    if (entry and entry.get("model") == agent.model
            and entry.get("coding_agent") == agent.coding_agent
            and entry.get("read_only") == read_only):
        return entry["session_id"]           # rejoin the existing context window
    return f"sssf-{run.adw_id}-{agent.name}-{new_id(4)}"


def _event_forwarder(run, phase: Phase, agent_name: str):
    """One tool_call event per real tool call, with its exact args and result.

    The adapter has already folded its own raw wire format into this
    normalized record (see harnesses.HarnessAdapter) — this function is the
    same for every coding_agent.
    """
    def forward(record: dict) -> None:
        # The call's span rides the columns; duration_ms stays in the payload as
        # the adapter's own authoritative number.
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="tool_call", name=record.pop("label", record.get("tool", "tool")),
                                     started_at=record.pop("started_at", None),
                                     ended_at=record.pop("ended_at", None),
                                     payload={**record, "agent": agent_name}))
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
