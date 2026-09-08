"""ClaudeCodeAdapter: exercised against a fake `claude` CLI emitting the
documented --output-format stream-json shape."""

from __future__ import annotations

import uuid

import pytest
from adw_modules import agent_claudecode
from adw_modules.data_types import HarnessRequest

REAL_SESSION = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _claudecode_wiring(fake_cli, monkeypatch):
    monkeypatch.setattr(agent_claudecode, "CLAUDE_PATH", str(fake_cli))


def _request(tmp_path, **overrides) -> HarnessRequest:
    fields = dict(
        prompt="do the thing",
        system_prompt="you are a test agent",
        model="opus",
        thinking="high",
        session_id=None,
        cwd=str(tmp_path),
        raw_output_path=str(tmp_path / "raw_output.jsonl"),
    )
    fields.update(overrides)
    return HarnessRequest(**fields)


STREAM_LINES = [
    {"type": "system", "subtype": "init", "session_id": REAL_SESSION},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls -la"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False,
         "content": "file1\nfile2"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "session_id": REAL_SESSION,
     "result": "FINAL_TEXT",
     "usage": {"input_tokens": 10, "output_tokens": 5,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
     "total_cost_usd": 0.01},
]


def test_initial_command_contains_model_and_effort(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"
    assert "--resume" not in argv     # no real session yet -> fresh start


def test_continuation_resumes_a_real_uuid_session(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path, session_id=REAL_SESSION))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--resume") + 1] == REAL_SESSION
    assert result.session_id == REAL_SESSION


def test_placeholder_session_id_is_not_resumed(tmp_path, fake_cli_env):
    """agents.py may offer its own non-UUID placeholder when there is no real
    prior Claude Code session — the adapter must not try to --resume it."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path, session_id="sssf-abc123-planner-9f2a"))
    argv = fake_cli_env.argv()
    assert "--resume" not in argv


def test_final_response_and_real_session_id_are_extracted(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path))
    assert result.text == "FINAL_TEXT"
    assert result.session_id == REAL_SESSION
    assert result.usage.total_tokens == 15
    assert result.usage.total_cost == pytest.approx(0.01)


def test_tool_events_are_forwarded_normalized(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    events = []
    agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path), on_event=events.append)
    assert len(events) == 1
    record = events[0]
    assert record["tool"] == "Bash"
    assert record["ok"] is True
    assert "file1" in record["result_snippet"]


def test_nonzero_exit_always_raises_even_with_text(tmp_path, fake_cli_env):
    """Unlike Pi, Claude Code has no "useful response" carve-out: a non-zero
    exit is always a failed phase."""
    fake_cli_env.set_lines(STREAM_LINES)
    fake_cli_env.set_exit(1)
    with pytest.raises(RuntimeError):
        agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path))


def test_validate_rejects_harness_engineering():
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="claude_code", model="opus",
                        harness_engineering=["some-extension.ts"],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    problems = agent_claudecode.ClaudeCodeAdapter().validate(agent)
    assert problems and "Pi-only" in problems[0]


def test_validate_accepts_no_harness_engineering():
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="claude_code", model="opus",
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    assert agent_claudecode.ClaudeCodeAdapter().validate(agent) == []


# ── tool mapping ──────────────────────────────────────────────────────────────

def test_tools_are_translated_exposed_and_preapproved(tmp_path, fake_cli_env):
    """Print mode cannot answer permission prompts, so configured tools must
    be both exposed and approved as the same bounded, comma-separated list."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path, tools=["read", "bash", "edit"]))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--tools") + 1] == "Read,Bash,Edit"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Bash,Edit"


def test_prompt_is_sent_through_stdin_so_tools_cannot_swallow_it(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path, tools=["read", "bash"]))
    argv = fake_cli_env.argv()
    assert "do the thing" not in argv
    assert fake_cli_env.stdin() == "do the thing"


def test_validate_rejects_a_pi_tool_with_no_claude_code_mapping():
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="claude_code", model="opus",
                        tools=["read", "ls"],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    problems = agent_claudecode.ClaudeCodeAdapter().validate(agent)
    assert problems and "ls" in problems[0] and "no Claude Code mapping" in problems[0]


def test_validate_accepts_mappable_tools():
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="claude_code", model="opus",
                        tools=["read", "bash", "edit", "write", "grep", "find"],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    assert agent_claudecode.ClaudeCodeAdapter().validate(agent) == []


def test_run_rejects_an_unmapped_tool_defensively(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    with pytest.raises(ValueError, match="ls"):
        agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path, tools=["ls"]))


# ── effort validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_validate_accepts_every_supported_effort(effort):
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="claude_code", model="opus", thinking=effort,
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    assert agent_claudecode.ClaudeCodeAdapter().validate(agent) == []


@pytest.mark.parametrize("effort", ["off", "minimal"])
def test_validate_rejects_pi_only_effort_levels(effort):
    """Claude Code's own --effort only accepts low/medium/high/xhigh/max —
    Pi's off/minimal aren't in that range."""
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(name="x", coding_agent="claude_code", model="opus", thinking=effort,
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    problems = agent_claudecode.ClaudeCodeAdapter().validate(agent)
    assert problems and effort in problems[0]
