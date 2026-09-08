"""agents.py session lifecycle: reuse only when coding_agent, model, AND
permission class (read_only) all still match, and the REAL session id an
adapter returns — never the placeholder agents.py offered — is what retries
and agent_map.json use.

Every HarnessAdapter here is a scripted stub: no subprocess, no real model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adw_modules import agents, harnesses, permissions, session
from adw_modules.data_types import (AgentCall, AgentConfig, GenericOutput,
                                    HarnessResult, PhaseParams,
                                    PromptEngineering, SSSFConfig,
                                    UsageBreakdown)


class ScriptedAdapter:
    """Plays back one (expected_session_id, HarnessResult) step per call.

    `expected_session_id=None` means "don't check" (used for the first call
    in a phase, whose placeholder id agents.py mints fresh each time).
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen_session_ids: list[str | None] = []
        self.seen_requests = []

    def validate(self, agent):
        return []

    def run(self, request, on_event=None, on_spawn=None, on_exit=None):
        self.seen_session_ids.append(request.session_id)
        self.seen_requests.append(request)
        expected_session_id, result = self.steps.pop(0)
        if expected_session_id is not None:
            assert request.session_id == expected_session_id
        if on_spawn:
            on_spawn(1234)
        if on_exit:
            on_exit(1234)
        return result


class ActingAdapter:
    """Runs one in-process action per turn, allowing filesystem regressions."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.seen_requests = []

    def validate(self, agent):
        return []

    def run(self, request, on_event=None, on_spawn=None, on_exit=None):
        self.seen_requests.append(request)
        return self.actions.pop(0)()


class EventingAdapter:
    def __init__(self):
        self.seen_requests = []

    def validate(self, agent):
        return []

    def run(self, request, on_event=None, on_spawn=None, on_exit=None):
        self.seen_requests.append(request)
        on_event({"tool": "read", "ok": True})
        return HarnessResult(text='{"status": "success"}', returncode=0,
                             session_id="event-session")


def _cfg(prompt_files, model="m1", coding_agent="pi"):
    system, user = prompt_files
    return SSSFConfig(agents=[AgentConfig(
        name="tester", coding_agent=coding_agent, model=model,
        prompt_engineering=PromptEngineering(system=str(system), user=str(user)))])


def test_retry_continues_with_the_real_returned_session_id(sssf_repo, prompt_files, monkeypatch):
    """The first send gets back a session_id the harness itself assigned,
    different from SSSF's placeholder. The malformed-JSON retry that follows
    must carry THAT id, not the placeholder — and it's what lands in
    agent_map.json."""
    cfg = _cfg(prompt_files)
    real_id = "harness-assigned-real-id"
    stub = ScriptedAdapter([
        (None, HarnessResult(text="not json", returncode=0, session_id=real_id)),
        (real_id, HarnessResult(text='{"status": "success"}', returncode=0, session_id=real_id)),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)

    run = session.ensure(cfg, adw_id="run1")
    with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                               description="exercise the JSON-retry path")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert stub.seen_session_ids[0].startswith("sssf-")   # the offered placeholder
    assert stub.seen_session_ids[1] == real_id             # adopted before the retry
    assert run.agent_map["tester"]["session_id"] == real_id
    assert run.agent_map["tester"]["coding_agent"] == "pi"


def test_permission_breach_preempts_invalid_json_retry(
        sssf_repo, prompt_files, monkeypatch):
    cfg = _cfg(prompt_files)
    system_prompt = prompt_files[0]
    original = system_prompt.read_text()

    def breach_then_invalid_json():
        system_prompt.write_text("tampered")
        return HarnessResult(text="not json", returncode=0, session_id="real")

    stub = ActingAdapter([breach_then_invalid_json])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)
    run = session.ensure(cfg, adw_id="breach-before-parse")

    with pytest.raises(permissions.PermissionBreach):
        with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                                   description="breach must preempt parsing")) as ph:
            ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert len(stub.seen_requests) == 1
    assert system_prompt.read_text() == original
    assert run.tracer.conn.execute(
        "SELECT count(*) FROM events WHERE name='permission_breach'"
    ).fetchone() == (1,)


def test_adapter_exception_still_enforces_and_is_chained_from_breach(
        sssf_repo, prompt_files, monkeypatch):
    cfg = _cfg(prompt_files)
    cfg.agents[0].writes = []
    forbidden = sssf_repo / "forbidden.txt"

    def breach_then_raise():
        forbidden.write_text("tampered")
        raise RuntimeError("adapter failed")

    stub = ActingAdapter([breach_then_raise])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)
    run = session.ensure(cfg, adw_id="breach-with-adapter-error")

    with pytest.raises(permissions.PermissionBreach) as caught:
        with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                                   description="enforce adapter exceptions")) as ph:
            ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "adapter failed"
    assert not forbidden.exists()


def test_other_roster_agent_prompt_symlink_is_rejected_before_render_or_spawn(
        sssf_repo, prompt_files, monkeypatch):
    active = sssf_repo / "review-effective.md"
    active.write_text("effective instructions")
    reviewer_system = sssf_repo / "review-system.md"
    reviewer_system.symlink_to(active)
    reviewer_user = sssf_repo / "review-user.md"
    reviewer_user.write_text("review")
    cfg = _cfg(prompt_files)
    cfg.agents.append(AgentConfig(
        name="reviewer",
        prompt_engineering=PromptEngineering(
            system=str(reviewer_system), user=str(reviewer_user)),
    ))
    stub = ActingAdapter([])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)

    with pytest.raises(SystemExit, match="reviewer.*prompt must use a direct path"):
        agents.validate(cfg, ["tester"])

    run = session.ensure(cfg, adw_id="prompt-symlink")

    with pytest.raises(SystemExit, match="reviewer.*prompt must use a direct path"):
        with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                                   description="reject any indirect roster prompt")) as ph:
            ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert stub.seen_requests == []


def test_custom_observability_database_is_not_attributed_to_read_only_agent(
        sssf_repo, prompt_files, monkeypatch):
    cfg = _cfg(prompt_files)
    cfg.agents[0].writes = []
    cfg.defaults.data_dir = "custom_runtime"
    cfg.observability.db = "custom_runtime/trace.db"
    stub = EventingAdapter()
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)
    run = session.ensure(cfg, adw_id="custom-db")

    with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                               description="trace events in a custom runtime")) as ph:
        envelope = ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert envelope.status == "success"
    assert run.tracer.conn.execute(
        "SELECT count(*) FROM events WHERE type='tool_call' AND name='read'"
    ).fetchone() == (1,)
    assert not permissions.permitted("custom_runtime/trace.db", cfg.agents[0], run)


def test_credits_reach_the_trace_without_becoming_dollars(
        sssf_repo, prompt_files, monkeypatch):
    """A harness that bills credits instead of tokens (Kiro CLI) must still
    show a cost in the trace. The credits accumulate on the session and stay
    out of total_cost, whose unit is dollars — reporting them there would make
    a 0.44-credit run look like a 44-cent one."""
    import sqlite3

    cfg = _cfg(prompt_files)
    usage = UsageBreakdown(credits=0.22)
    stub = ScriptedAdapter([
        (None, HarnessResult(text='{"status": "success"}', returncode=0,
                             session_id="real-1", usage=usage)),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)

    run = session.ensure(cfg, adw_id="credits1")
    with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                               description="exercise credit accounting")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert run.credits == pytest.approx(0.22)
    assert run.cost == 0.0
    conn = sqlite3.connect(cfg.observability.db)
    row = conn.execute("select total_credits, total_cost, total_tokens from"
                       " sessions where adw_id='credits1'").fetchone()
    conn.close()
    assert row == (pytest.approx(0.22), 0.0, 0)


def test_session_reused_only_when_coding_agent_and_model_both_match(sssf_repo, prompt_files, monkeypatch):
    cfg = _cfg(prompt_files, model="m1", coding_agent="pi")
    real_id = "session-from-first-phase"
    stub = ScriptedAdapter([
        (None, HarnessResult(text='{"status": "success"}', returncode=0, session_id=real_id)),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)

    run = session.ensure(cfg, adw_id="run2")
    with run.phase(PhaseParams(name="first", kind="agent", owner="tester",
                               description="first call, mints a session")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))
    assert run.agent_map["tester"]["session_id"] == real_id

    # Same coding_agent + same model -> the next phase must rejoin it as-is.
    stub.steps.append((real_id, HarnessResult(text='{"status": "success"}', returncode=0,
                                              session_id=real_id)))
    with run.phase(PhaseParams(name="second", kind="agent", owner="tester",
                               description="same agent again, must rejoin the session")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi again", gates=[]))
    assert stub.seen_session_ids[-1] == real_id

    # Changing the model invalidates the mapped session -> a fresh placeholder,
    # never the id a DIFFERENT model's context window was built under.
    cfg.agents[0].model = "m2"
    stub.steps.append((None, HarnessResult(text='{"status": "success"}', returncode=0,
                                           session_id="new-session-for-m2")))
    with run.phase(PhaseParams(name="third", kind="agent", owner="tester",
                               description="model changed, must not reuse the old session")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi once more", gates=[]))
    assert stub.seen_session_ids[-1] != real_id
    assert stub.seen_session_ids[-1].startswith("sssf-")


def test_session_not_reused_across_different_coding_agents(sssf_repo, prompt_files, monkeypatch):
    """Same model string, different coding_agent: still not the same session."""
    cfg = _cfg(prompt_files, model="m1", coding_agent="pi")
    real_id = "pi-session"
    pi_stub = ScriptedAdapter([
        (None, HarnessResult(text='{"status": "success"}', returncode=0, session_id=real_id)),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", pi_stub)

    run = session.ensure(cfg, adw_id="run3")
    with run.phase(PhaseParams(name="first", kind="agent", owner="tester",
                               description="pi mints a session")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))
    assert run.agent_map["tester"]["coding_agent"] == "pi"

    claudecode_stub = ScriptedAdapter([
        (None, HarnessResult(
            text='{"status": "success"}', returncode=0, session_id="claudecode-session"
        )),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "claude_code", claudecode_stub)
    cfg.agents[0].coding_agent = "claude_code"
    with run.phase(PhaseParams(name="second", kind="agent", owner="tester",
                               description="switched to claude_code, must not reuse pi's session")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi again", gates=[]))
    assert claudecode_stub.seen_session_ids[-1] != real_id
    assert run.agent_map["tester"]["coding_agent"] == "claude_code"


def test_validate_fails_objectively_when_harness_engineering_set_for_claude_code(sssf_repo, prompt_files):
    system, user = prompt_files
    cfg = SSSFConfig(agents=[AgentConfig(
        name="planner", coding_agent="claude_code", model="opus",
        harness_engineering=["ext.ts"],
        prompt_engineering=PromptEngineering(system=str(system), user=str(user)))])
    with pytest.raises(SystemExit, match="Pi-only"):
        agents.validate(cfg, ["planner"])


def test_pi_state_dir_stays_pi_sessions_for_backward_compatibility(sssf_repo, prompt_files, monkeypatch):
    """pi's on-disk --session-dir has always been named "pi_sessions"; an
    existing install upgrading to the multi-harness adapters must keep
    resolving to that same directory, or every session already on disk for
    that agent is orphaned."""
    cfg = _cfg(prompt_files, model="m1", coding_agent="pi")
    stub = ScriptedAdapter([
        (None, HarnessResult(text='{"status": "success"}', returncode=0, session_id="s1")),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)

    run = session.ensure(cfg, adw_id="run4")
    with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                               description="pi's state_dir must stay pi_sessions")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert len(stub.seen_requests) == 1
    state_dir = stub.seen_requests[0].state_dir
    assert state_dir.endswith("pi_sessions"), state_dir
    assert "harness_state" not in state_dir


def test_read_only_flag_follows_agent_writes(sssf_repo, prompt_files, monkeypatch):
    """HarnessRequest.read_only must mirror agent.writes == [] exactly — it is
    the one extra signal Codex uses to pick its native read-only sandbox."""
    system, user = prompt_files
    cfg = SSSFConfig(agents=[
        AgentConfig(name="unrestricted", coding_agent="pi", model="m1", writes=None,
                    prompt_engineering=PromptEngineering(system=str(system), user=str(user))),
        AgentConfig(name="scoped", coding_agent="pi", model="m1", writes=["specs/"],
                    prompt_engineering=PromptEngineering(system=str(system), user=str(user))),
        AgentConfig(name="readonly", coding_agent="pi", model="m1", writes=[],
                    prompt_engineering=PromptEngineering(system=str(system), user=str(user))),
    ])
    stub = ScriptedAdapter([
        (None, HarnessResult(text='{"status": "success"}', returncode=0, session_id="s1")),
        (None, HarnessResult(text='{"status": "success"}', returncode=0, session_id="s2")),
        (None, HarnessResult(text='{"status": "success"}', returncode=0, session_id="s3")),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)

    run = session.ensure(cfg, adw_id="run5")
    for name in ("unrestricted", "scoped", "readonly"):
        with run.phase(PhaseParams(name=name, kind="agent", owner=name,
                                   description=f"exercise read_only for {name}")) as ph:
            ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert [r.read_only for r in stub.seen_requests] == [False, False, True]


def test_writes_downgraded_to_read_only_forces_a_fresh_session_not_an_unsafe_resume(
        sssf_repo, prompt_files, monkeypatch):
    """The dangerous direction the HIGH finding is about: a session started
    WRITABLE must never be resumed once config says the agent should now be
    read-only. Codex's `exec resume` cannot re-apply `--sandbox read-only`, so
    the only safe move is a fresh session — which DOES get the sandbox,
    because it is not a resume. Session identity (coding_agent + model +
    read_only) enforces this generically, with no CLI-specific branch in
    agents.py: every coding_agent is held to the same rule."""
    cfg = _cfg(prompt_files, model="m1", coding_agent="codex")
    writable_session = "writable-session"
    stub = ScriptedAdapter([
        (None, HarnessResult(text='{"status": "success"}', returncode=0,
                             session_id=writable_session)),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "codex", stub)

    run = session.ensure(cfg, adw_id="run7")
    with run.phase(PhaseParams(name="first", kind="agent", owner="tester",
                               description="agent starts writable, mints a session")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))
    assert stub.seen_requests[0].read_only is False
    assert run.agent_map["tester"]["read_only"] is False
    assert run.agent_map["tester"]["session_id"] == writable_session

    # Config now says: this agent must be read-only.
    cfg.agents[0].writes = []
    stub.steps.append((None, HarnessResult(text='{"status": "success"}', returncode=0,
                                           session_id="new-readonly-session")))
    with run.phase(PhaseParams(name="second", kind="agent", owner="tester",
                               description="writes downgraded to [] - must not resume the writable session")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi again", gates=[]))

    second_request = stub.seen_requests[-1]
    assert second_request.session_id != writable_session   # NOT resumed
    assert second_request.session_id.startswith("sssf-")   # a fresh placeholder was offered
    assert second_request.read_only is True                # so CodexAdapter applies --sandbox read-only
    assert run.agent_map["tester"]["read_only"] is True
    assert run.agent_map["tester"]["session_id"] == "new-readonly-session"


def test_old_agent_map_entries_without_read_only_are_never_reused(sssf_repo, prompt_files, monkeypatch):
    """agent_map.json entries written before the read_only field existed must
    not be silently trusted as compatible in either direction — they start
    fresh once, rather than resuming under an unknown permission history."""
    cfg = _cfg(prompt_files, model="m1", coding_agent="pi")
    data_dir = Path("adws/adw_data")
    session_dir = data_dir / "sessions" / "run_old_map"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "agent_map.json").write_text(json.dumps({
        "tester": {"session_id": "pre-upgrade-session", "model": "m1", "coding_agent": "pi"}
    }))

    stub = ScriptedAdapter([
        (None, HarnessResult(text='{"status": "success"}', returncode=0, session_id="fresh-session")),
    ])
    monkeypatch.setitem(harnesses.ADAPTERS, "pi", stub)

    run = session.ensure(cfg, adw_id="run_old_map")
    with run.phase(PhaseParams(name="t", kind="agent", owner="tester",
                               description="an old map entry must not be trusted post-upgrade")) as ph:
        ph.call(AgentCall(output_type=GenericOutput, prompt="hi", gates=[]))

    assert stub.seen_session_ids[0] != "pre-upgrade-session"
    assert stub.seen_session_ids[0].startswith("sssf-")
    assert run.agent_map["tester"]["read_only"] is False   # the map entry is healed going forward
    assert run.agent_map["tester"]["session_id"] == "fresh-session"
