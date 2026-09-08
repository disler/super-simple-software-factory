"""CodexAdapter: exercised against a fake `codex` CLI emitting Codex 0.153.0's
real `exec --json` "thread event" schema — thread.started/turn.started/
turn.completed, item.started/item.completed with item types agent_message and
command_execution. See agent_codex.py's module docstring."""

from __future__ import annotations

import uuid

import pytest
from adw_modules import agent_codex
from adw_modules.data_types import HarnessRequest

REAL_THREAD = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _codex_wiring(fake_cli, monkeypatch):
    monkeypatch.setattr(agent_codex, "CODEX_PATH", str(fake_cli))


def _request(tmp_path, **overrides) -> HarnessRequest:
    fields = dict(
        prompt="do the thing",
        system_prompt="you are a test agent",
        model="gpt-5.6-sol",
        thinking="high",
        session_id=None,
        cwd=str(tmp_path),
        raw_output_path=str(tmp_path / "raw_output.jsonl"),
    )
    fields.update(overrides)
    return HarnessRequest(**fields)


STREAM_LINES = [
    {"type": "thread.started", "thread_id": REAL_THREAD},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_0", "type": "command_execution",
     "command": "ls -la", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
     "command": "ls -la", "aggregated_output": "file1\nfile2", "exit_code": 0,
     "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
     "text": "FINAL_TEXT"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0,
     "output_tokens": 5, "reasoning_output_tokens": 0}},
]


def test_initial_command_contains_model_and_effort(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_codex.CodexAdapter().run(_request(tmp_path))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert "model_reasoning_effort=high" in argv
    assert "resume" not in argv          # no real thread yet -> fresh start


def test_continuation_resumes_a_real_uuid_thread(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_codex.CodexAdapter().run(_request(tmp_path, session_id=REAL_THREAD))
    argv = fake_cli_env.argv()
    assert argv[argv.index("resume") + 1] == REAL_THREAD
    assert result.session_id == REAL_THREAD


def test_placeholder_session_id_is_not_resumed(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_codex.CodexAdapter().run(_request(tmp_path, session_id="sssf-abc123-builder-9f2a"))
    argv = fake_cli_env.argv()
    assert "resume" not in argv


def test_final_response_and_real_thread_id_are_extracted(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_codex.CodexAdapter().run(_request(tmp_path))
    assert result.text == "FINAL_TEXT"
    assert result.session_id == REAL_THREAD
    assert result.usage.total_tokens == 15
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5


def test_tool_events_are_forwarded_normalized(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    events = []
    agent_codex.CodexAdapter().run(_request(tmp_path), on_event=events.append)
    assert len(events) == 1
    record = events[0]
    assert record["tool"] == "command_execution"
    assert record["ok"] is True
    assert "file1" in record["result_snippet"]


def test_nonzero_exit_always_raises_even_with_text(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    fake_cli_env.set_exit(1)
    with pytest.raises(RuntimeError):
        agent_codex.CodexAdapter().run(_request(tmp_path))


def test_validate_rejects_harness_engineering():
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="codex", model="gpt-5.6-sol",
                        harness_engineering=["some-extension.ts"],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    problems = agent_codex.CodexAdapter().validate(agent)
    assert problems and "Pi-only" in problems[0]


def test_read_only_uses_native_sandbox_on_a_fresh_turn(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_codex.CodexAdapter().run(_request(tmp_path, read_only=True))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_read_only_omitted_when_not_declared(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_codex.CodexAdapter().run(_request(tmp_path, read_only=False))
    argv = fake_cli_env.argv()
    assert "--sandbox" not in argv


def test_read_only_omitted_on_resume(tmp_path, fake_cli_env):
    """codex exec resume has no --sandbox flag (confirmed via --help) — asserting
    it here on a resumed turn would be passing an option the real CLI rejects."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_codex.CodexAdapter().run(_request(tmp_path, session_id=REAL_THREAD, read_only=True))
    argv = fake_cli_env.argv()
    assert "--sandbox" not in argv


def test_no_tools_allowlist_flag_is_invented_for_codex(tmp_path, fake_cli_env):
    """Codex's CLI has no --tools-equivalent flag — request.tools must be a
    pure no-op here, never smuggled in as some invented flag."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_codex.CodexAdapter().run(_request(tmp_path, tools=["read", "bash"]))
    argv = fake_cli_env.argv()
    assert "--tools" not in argv
    assert "read" not in argv
    assert "bash" not in argv


def test_validate_rejects_an_effective_tools_list():
    """Codex has no allowlist flag to honor `tools` with — silently ignoring
    it would let the agent use every Codex tool regardless of what the config
    asked for, so validate() must fail objectively instead."""
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="codex", model="gpt-5.6-sol",
                        tools=["read", "bash"],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    problems = agent_codex.CodexAdapter().validate(agent)
    assert problems and "tools" in problems[0] and "read" in problems[0]


def test_validate_accepts_tools_explicitly_set_to_none():
    """tools: null is how a codex agent opts out of an inherited defaults.tools."""
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="codex", model="gpt-5.6-sol", tools=None,
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    assert agent_codex.CodexAdapter().validate(agent) == []
