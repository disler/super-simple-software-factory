from adw_modules.data_types import AgentConfig, EventRecord, PromptEngineering
from adw_modules.tracer import Tracer


def _agent(coding_agent):
    return AgentConfig(
        name="builder",
        coding_agent=coding_agent,
        model="model",
        prompt_engineering=PromptEngineering(system="system.md", user="user.md"),
    )


def test_agent_session_upsert_tracks_harness_switch_without_losing_events(tmp_path):
    tracer = Tracer(tmp_path / "trace.db", tmp_path / "events.jsonl")
    tracer.event(EventRecord(adw_id="run1", type="log", name="before-switch"))
    tracer.agent_session_row("run1", _agent("kiro_cli"), "kiro-session")

    tracer.agent_session_row("run1", _agent("antigravity"), "agy-session")

    row = tracer.conn.execute(
        "SELECT coding_agent, session_id FROM agent_sessions "
        "WHERE adw_id='run1' AND agent='builder'"
    ).fetchone()
    events = tracer.conn.execute(
        "SELECT name FROM events WHERE adw_id='run1' ORDER BY started_at"
    ).fetchall()
    assert row == ("antigravity", "agy-session")
    assert events == [("before-switch",)]
