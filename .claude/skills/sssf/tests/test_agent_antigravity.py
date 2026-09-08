"""AntigravityAdapter: exercised against a fake `agy` emitting Antigravity CLI
1.1.25's documented headless stream — one `init`, any number of `step_update`,
and exactly one `result`. The payloads below follow the schema published at
antigravity.google/docs/cli/headless. See agent_antigravity.py's module
docstring, including why this adapter's wire format comes from that spec rather
than from a captured live run."""

from __future__ import annotations

import uuid

import pytest
from adw_modules import agent_antigravity
from adw_modules.data_types import AgentConfig, HarnessRequest, PromptEngineering

CONVERSATION = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _agy_wiring(fake_cli, monkeypatch):
    monkeypatch.setattr(agent_antigravity, "AGY_PATH", str(fake_cli))
    # The catalog is process-cached; a stale entry would leak between tests.
    agent_antigravity._model_slugs.cache_clear()
    yield
    agent_antigravity._model_slugs.cache_clear()


def _request(tmp_path, **overrides) -> HarnessRequest:
    fields = dict(
        prompt="do the thing",
        system_prompt="you are a test agent",
        model="gemini-3.8-flash-medium",
        thinking="high",
        session_id=None,
        cwd=str(tmp_path),
        raw_output_path=str(tmp_path / "raw_output.jsonl"),
    )
    fields.update(overrides)
    return HarnessRequest(**fields)


def _agent(**overrides) -> AgentConfig:
    # test-model is what the fake `agy models` reports by default, so the
    # validate() tests below isolate the problem each one is about.
    fields = dict(name="x", coding_agent="antigravity", model="test-model",
                  thinking="high", tools=None,
                  prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    fields.update(overrides)
    return AgentConfig(**fields)


def _step(**payload) -> dict:
    return {"event": "step_update",
            "step_update": {"conversation_id": CONVERSATION, **payload}}


USAGE = {"input_tokens": 10415, "output_tokens": 657, "thinking_tokens": 616,
         "cache_read_tokens": 8113, "total_tokens": 11072}

STREAM_LINES = [
    {"event": "init", "conversation_id": CONVERSATION,
     "init": {"cwd": "/repo", "tools": ["run_command", "write_to_file"],
              "permission_mode": "always-proceed"}},
    _step(step_index=0, state="DONE", step_type="user_input"),
    _step(step_index=4, state="ACTIVE", step_type="tool", tool_name="run_command"),
    _step(step_index=4, state="DONE", step_type="tool", tool_name="run_command",
          duration_seconds=0.07,
          tool_info={"name": "run_command",
                     "parameters": {"CommandLine": "echo hello_headless_demo"},
                     "output": "hello_headless_demo\r\n"}),
    _step(step_index=5, state="ACTIVE", step_type="agent_response", text_delta="FINAL_"),
    _step(step_index=5, state="DONE", step_type="agent_response", text_delta="TEXT",
          duration_seconds=6.28, usage=USAGE),
    {"event": "result",
     "result": {"conversation_id": CONVERSATION, "status": "SUCCESS",
                "response": "FINAL_TEXT", "duration_seconds": 6.88,
                "num_turns": 1, "usage": USAGE}},
]


def test_command_shape_is_headless_streaming_json(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path))
    argv = fake_cli_env.argv()
    assert argv[0] == "-p"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert "--conversation" not in argv          # no real conversation yet
    assert "--continue" not in argv


def test_effort_is_omitted_when_the_model_slug_already_carries_the_tier(
        tmp_path, fake_cli_env):
    """Regression, found against the real CLI: `agy` treats the pair as
    invalid, not as a preference to reconcile —
    `--model gemini-3.6-flash-medium conflicts with --effort=low`. Every slug
    in the observed catalog carries a tier, so in practice --effort never ships."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(
        _request(tmp_path, model="gemini-3.6-flash-medium", thinking="medium"))
    assert "--effort" not in fake_cli_env.argv()


def test_effort_is_sent_for_a_slug_without_a_tier(tmp_path, fake_cli_env):
    """The documented flag still applies to a slug that carries no tier."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(
        _request(tmp_path, model="some-future-model", thinking="high"))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--effort") + 1] == "high"


def test_print_timeout_is_raised_above_the_five_minute_default(tmp_path, fake_cli_env):
    """A builder phase implementing a plan routinely runs past 5m, and the
    default would kill it mid-edit."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--print-timeout") + 1] == agent_antigravity.PRINT_TIMEOUT
    assert agent_antigravity.PRINT_TIMEOUT != "5m"


def test_permissions_are_pre_approved_for_the_whole_turn(tmp_path, fake_cli_env):
    """Headless mode SOFT-DENIES a tool it cannot get approval for: the run
    continues and exits 0. Without this flag an agent reports success on a
    phase whose tools were quietly refused."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path))
    assert "--dangerously-skip-permissions" in fake_cli_env.argv()


def test_fresh_turn_folds_the_system_prompt_into_the_prompt(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path))
    argv = fake_cli_env.argv()
    assert argv[1] == "you are a test agent\n\ndo the thing"


def test_resumed_turn_uses_the_conversation_id_and_omits_the_system_prompt(
        tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_antigravity.AntigravityAdapter().run(
        _request(tmp_path, session_id=CONVERSATION))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--conversation") + 1] == CONVERSATION
    assert argv[1] == "do the thing"
    assert result.session_id == CONVERSATION


def test_placeholder_session_id_is_not_resumed(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(
        _request(tmp_path, session_id="sssf-abc123-builder-9f2a"))
    assert "--conversation" not in fake_cli_env.argv()


def test_response_conversation_id_and_real_token_usage_are_extracted(
        tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_antigravity.AntigravityAdapter().run(_request(tmp_path))
    assert result.text == "FINAL_TEXT"
    assert result.session_id == CONVERSATION
    assert result.usage.input_tokens == 10415
    assert result.usage.output_tokens == 657
    assert result.usage.cache_read_tokens == 8113
    assert result.usage.reasoning_tokens == 616
    # Reported, not summed: cache reads are a subset of input here.
    assert result.usage.total_tokens == 11072
    # Cumulative conversation usage is not current context occupancy.
    assert result.context_tokens == 0
    # Antigravity bills AI credits and reports no dollars.
    assert result.usage.total_cost == 0.0


def test_resumed_turn_bills_step_usage_not_cumulative_result_usage(
        tmp_path, fake_cli_env):
    """A resumed result repeats all prior usage; only this process's completed
    steps may be added to session spend."""
    first_step = {"input_tokens": 20, "output_tokens": 5,
                  "cache_read_tokens": 12, "thinking_tokens": 2,
                  "total_tokens": 25}
    second_step = {"input_tokens": 30, "output_tokens": 10,
                   "cache_read_tokens": 18, "thinking_tokens": 4,
                   "total_tokens": 40}
    cumulative = {"input_tokens": 220, "output_tokens": 80,
                  "cache_read_tokens": 130, "thinking_tokens": 25,
                  "total_tokens": 300}
    lines = [
        _step(step_index=8, state="DONE", step_type="agent_response",
              text_delta="part one", usage=first_step),
        _step(step_index=9, state="DONE", step_type="agent_response",
              text_delta="part two", usage=second_step),
        {"event": "result",
         "result": {"conversation_id": CONVERSATION, "status": "SUCCESS",
                    "response": "done", "usage": cumulative}},
    ]
    fake_cli_env.set_lines(lines)

    result = agent_antigravity.AntigravityAdapter().run(
        _request(tmp_path, session_id=CONVERSATION))

    assert result.usage.input_tokens == 50
    assert result.usage.output_tokens == 15
    assert result.usage.cache_read_tokens == 30
    assert result.usage.reasoning_tokens == 6
    assert result.usage.total_tokens == 65
    assert result.context_tokens == 0


def test_an_empty_response_falls_back_to_the_streamed_deltas(tmp_path, fake_cli_env):
    lines = STREAM_LINES[:-1] + [
        {"event": "result",
         "result": {"conversation_id": CONVERSATION, "status": "SUCCESS",
                    "response": "", "usage": USAGE}}]
    fake_cli_env.set_lines(lines)
    result = agent_antigravity.AntigravityAdapter().run(_request(tmp_path))
    assert result.text == "FINAL_TEXT"


def test_tool_events_are_forwarded_once_normalized(tmp_path, fake_cli_env):
    """The ACTIVE sighting starts the span; the DONE one carries the result."""
    fake_cli_env.set_lines(STREAM_LINES)
    events = []
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path), on_event=events.append)
    assert len(events) == 1
    record = events[0]
    assert record["tool"] == "run_command"
    assert record["args"]["CommandLine"] == "echo hello_headless_demo"
    assert record["ok"] is True
    assert record["label"] == "run_command: echo hello_headless_demo"
    assert record["result_snippet"].strip() == "hello_headless_demo"
    assert record["duration_ms"] == 70
    assert record["started_at"] and record["ended_at"]


def test_a_failed_tool_carries_its_error_into_the_record(tmp_path, fake_cli_env):
    lines = STREAM_LINES[:3] + [
        _step(step_index=4, state="DONE", step_type="tool", tool_name="run_command",
              tool_info={"name": "run_command", "parameters": {"CommandLine": "false"},
                         "output": "",
                         "error": {"type": "CommandFailed", "message": "exit 1"}}),
        STREAM_LINES[-1]]
    fake_cli_env.set_lines(lines)
    events = []
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path), on_event=events.append)
    assert len(events) == 1
    assert events[0]["ok"] is False
    assert "CommandFailed: exit 1" in events[0]["result_snippet"]


def test_nonzero_exit_raises_with_the_envelope_error(tmp_path, fake_cli_env):
    fake_cli_env.set_lines([
        {"event": "result",
         "result": {"conversation_id": "", "status": "ERROR", "response": "",
                    "error": "model does-not-exist-model is not recognized"}}])
    fake_cli_env.set_exit(1)
    with pytest.raises(RuntimeError, match="not recognized"):
        agent_antigravity.AntigravityAdapter().run(_request(tmp_path))


def test_a_non_success_status_raises_even_on_a_clean_exit(tmp_path, fake_cli_env):
    """CANCELED / INTERRUPTED / INVALID / WAITING / RUNNING are failed turns,
    and the text they produced must not be treated as an answer."""
    fake_cli_env.set_lines(STREAM_LINES[:-1] + [
        {"event": "result",
         "result": {"conversation_id": CONVERSATION, "status": "INTERRUPTED",
                    "response": "half an answer", "error": "SIGINT"}}])
    with pytest.raises(RuntimeError, match="INTERRUPTED"):
        agent_antigravity.AntigravityAdapter().run(_request(tmp_path))


def test_no_tools_allowlist_flag_is_invented(tmp_path, fake_cli_env):
    """Antigravity's headless mode has no allowlist flag — `tools` must be a
    pure no-op here, never smuggled in as some invented flag."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path, tools=["read", "bash"]))
    argv = fake_cli_env.argv()
    assert "--tools" not in argv
    assert "read" not in argv and "bash" not in argv


def test_read_only_does_not_claim_a_filesystem_sandbox(tmp_path, fake_cli_env):
    """`agy --sandbox` restricts terminal commands; it is not Codex's
    `--sandbox read-only` filesystem mode, so it is not used as one."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_antigravity.AntigravityAdapter().run(_request(tmp_path, read_only=True))
    assert "--sandbox" not in fake_cli_env.argv()


def test_validate_rejects_harness_engineering():
    problems = agent_antigravity.AntigravityAdapter().validate(
        _agent(harness_engineering=["some-extension.ts"]))
    assert problems and "Pi-only" in problems[0]


def test_validate_rejects_an_effective_tools_list():
    problems = agent_antigravity.AntigravityAdapter().validate(
        _agent(tools=["read", "bash"]))
    assert problems and "tools" in problems[0] and "permissions.allow" in problems[0]


def test_validate_accepts_tools_explicitly_set_to_none():
    assert agent_antigravity.AntigravityAdapter().validate(_agent(tools=None)) == []


@pytest.mark.parametrize("effort", ["off", "minimal", "xhigh", "max"])
def test_validate_rejects_efforts_outside_agys_range(effort):
    """`agy --effort` accepts low | medium | high only."""
    problems = agent_antigravity.AntigravityAdapter().validate(_agent(thinking=effort))
    assert problems and "effort level" in problems[0]


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_validate_accepts_agys_own_effort_range(effort):
    assert agent_antigravity.AntigravityAdapter().validate(_agent(thinking=effort)) == []


def test_validate_rejects_a_model_missing_from_the_account_catalog(fake_cli_env):
    """`agy models` fetches per account, so the list is what THIS login may
    use — not a static roster."""
    fake_cli_env.set_agy_models({"gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
                                 "gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)"})
    problems = agent_antigravity.AntigravityAdapter().validate(_agent(model="nope"))
    assert problems and "not in `agy models`" in problems[0]


def test_validate_rejects_a_thinking_that_contradicts_the_model_tier(fake_cli_env):
    """Silently running medium while the roster says low is the same class of
    lie as a harness that ignores --model. Fail at config time instead."""
    fake_cli_env.set_agy_models({"gemini-3.6-flash-medium": "Gemini 3.6 Flash (Medium)"})
    problems = agent_antigravity.AntigravityAdapter().validate(
        _agent(model="gemini-3.6-flash-medium", thinking="low"))
    assert problems and "contradicts model" in problems[0]
    assert "thinking: medium" in problems[0]      # names the fix


def test_validate_accepts_a_thinking_that_matches_the_model_tier(fake_cli_env):
    fake_cli_env.set_agy_models({"gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)"})
    assert agent_antigravity.AntigravityAdapter().validate(
        _agent(model="gemini-3.6-flash-low", thinking="low")) == []


def test_an_unreadable_catalog_does_not_fail_a_valid_model(monkeypatch):
    """Empty means the CLI is missing, unauthenticated, or offline. Reporting
    that as a bad model name sends the operator after the wrong bug."""
    monkeypatch.setattr(agent_antigravity, "AGY_PATH", "agy-that-does-not-exist")
    agent_antigravity._model_slugs.cache_clear()
    assert agent_antigravity.AntigravityAdapter().validate(_agent(model="anything")) == []
