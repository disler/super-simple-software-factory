"""PiAdapter: characterizes today's Pi behavior through the common
HarnessRequest/HarnessResult contract, against a fake `pi` CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from adw_modules import agent_pi
from adw_modules.data_types import HarnessRequest


@pytest.fixture(autouse=True)
def _pi_wiring(fake_cli, monkeypatch, tmp_path):
    """Point the adapter at the fake CLI and a throwaway models.json, and
    clear pi's own catalog cache — it is a module-level lru_cache keyed on no
    args, so a stale catalog from an earlier test would otherwise leak in."""
    monkeypatch.setattr(agent_pi, "PI_PATH", str(fake_cli))
    models_json = tmp_path / "models.json"
    models_json.write_text('{"providers": {"testprov": {"models": '
                           '[{"id": "test-model", "contextWindow": 128000}]}}}')
    monkeypatch.setattr(agent_pi, "MODELS_JSON", str(models_json))
    agent_pi._pi_catalog.cache_clear()
    yield
    agent_pi._pi_catalog.cache_clear()


def _request(tmp_path, **overrides) -> HarnessRequest:
    fields = dict(
        prompt="do the thing",
        system_prompt="you are a test agent",
        model="testprov/test-model",
        thinking="high",
        session_id=None,
        cwd=str(tmp_path),
        raw_output_path=str(tmp_path / "raw_output.jsonl"),
        state_dir=str(tmp_path / "state"),
    )
    fields.update(overrides)
    return HarnessRequest(**fields)


TOOL_CALL_LINES = [
    {"type": "tool_execution_start", "toolCallId": "call1", "toolName": "bash",
     "args": {"command": "ls -la"}},
    {"type": "tool_execution_end", "toolCallId": "call1", "toolName": "bash",
     "args": {"command": "ls -la"}, "isError": False,
     "result": {"content": [{"type": "text", "text": "file1\nfile2"}]}},
    {"type": "message_end", "message": {"role": "assistant",
     "content": [{"type": "text", "text": "FINAL_TEXT"}],
     "usage": {"input": 10, "output": 5, "totalTokens": 15}, "stopReason": "stop"}},
]


def test_initial_command_contains_model_and_effort(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(TOOL_CALL_LINES)
    agent_pi.PiAdapter().run(_request(tmp_path))
    argv = fake_cli_env.argv()
    assert "--provider" in argv and argv[argv.index("--provider") + 1] == "testprov"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "test-model"
    assert "--thinking" in argv and argv[argv.index("--thinking") + 1] == "high"


def test_no_session_id_mints_one_and_returns_it(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(TOOL_CALL_LINES)
    result = agent_pi.PiAdapter().run(_request(tmp_path, session_id=None))
    argv = fake_cli_env.argv()
    minted = argv[argv.index("--session-id") + 1]
    assert minted                      # something was generated
    assert result.session_id == minted   # and it is what got echoed back


def test_continuation_uses_the_given_session_id(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(TOOL_CALL_LINES)
    result = agent_pi.PiAdapter().run(_request(tmp_path, session_id="existing-session-42"))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--session-id") + 1] == "existing-session-42"
    assert result.session_id == "existing-session-42"


def test_final_response_is_extracted(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(TOOL_CALL_LINES)
    result = agent_pi.PiAdapter().run(_request(tmp_path))
    assert result.text == "FINAL_TEXT"
    assert result.usage.total_tokens == 15


def test_tool_events_are_forwarded_normalized(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(TOOL_CALL_LINES)
    events = []
    agent_pi.PiAdapter().run(_request(tmp_path), on_event=events.append)
    assert len(events) == 1
    record = events[0]
    assert record["tool"] == "bash"
    assert record["ok"] is True
    assert record["label"] == "bash: ls -la"
    assert "file1" in record["result_snippet"]


def test_nonzero_exit_with_useful_text_does_not_raise(tmp_path, fake_cli_env):
    """Pi's existing rule: a non-zero exit is only an error when NO usable
    response came back — preserved verbatim through the adapter."""
    fake_cli_env.set_lines(TOOL_CALL_LINES)
    fake_cli_env.set_exit(1)
    result = agent_pi.PiAdapter().run(_request(tmp_path))
    assert result.returncode == 1
    assert result.text == "FINAL_TEXT"


def test_nonzero_exit_with_no_text_raises(tmp_path, fake_cli_env):
    fake_cli_env.set_lines([])   # no message_end -> no text produced
    fake_cli_env.set_exit(1)
    with pytest.raises(RuntimeError):
        agent_pi.PiAdapter().run(_request(tmp_path))


def test_raw_output_jsonl_is_preserved(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(TOOL_CALL_LINES)
    request = _request(tmp_path)
    agent_pi.PiAdapter().run(request)
    raw = Path(request.raw_output_path)
    lines = raw.read_text().strip().splitlines()
    assert len(lines) == len(TOOL_CALL_LINES)


def test_validate_rejects_unknown_model(tmp_path):
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="pi", model="testprov/does-not-exist",
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    problems = agent_pi.PiAdapter().validate(agent)
    assert problems and "not found" in problems[0]


def test_validate_accepts_harness_engineering():
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="pi", model="testprov/test-model",
                        harness_engineering=["ext.ts"],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    assert agent_pi.PiAdapter().validate(agent) == []
