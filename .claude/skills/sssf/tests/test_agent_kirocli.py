"""KiroCliAdapter: exercised against a fake `kiro-cli` emitting Kiro CLI
2.21.0's real `--output-format stream-json` ACP stream — runStarted, the
sessionUpdate wrapper around tool_call / tool_call_update / session_info_update
/ agent_message_chunk, runFinished, and runError. Every payload below was
captured from a live v3 run. See agent_kirocli.py's module docstring."""

from __future__ import annotations

import uuid

import pytest
from adw_modules import agent_kirocli
from adw_modules.data_types import AgentConfig, HarnessRequest, PromptEngineering

# v3 mints `sess_<uuid>`; v2 mints a bare uuid. Both are Kiro's, both resumable.
REAL_SESSION = f"sess_{uuid.uuid4()}"
TOOL_CALL_ID = "run_command_tooluse_daQv6F3jSkBvSZ16X6u9gj"


@pytest.fixture(autouse=True)
def _kiro_wiring(fake_cli, monkeypatch):
    monkeypatch.setattr(agent_kirocli, "KIRO_PATH", str(fake_cli))
    # The catalog is process-cached; a stale entry would leak between tests.
    agent_kirocli._model_catalog.cache_clear()
    yield
    agent_kirocli._model_catalog.cache_clear()


def _request(tmp_path, **overrides) -> HarnessRequest:
    fields = dict(
        prompt="do the thing",
        system_prompt="you are a test agent",
        model="test-model",          # what the fake catalog reports, 200k window
        thinking="high",
        session_id=None,
        cwd=str(tmp_path),
        raw_output_path=str(tmp_path / "raw_output.jsonl"),
    )
    fields.update(overrides)
    return HarnessRequest(**fields)


def _agent(**overrides) -> AgentConfig:
    fields = dict(name="x", coding_agent="kiro_cli", model="test-model", tools=None,
                  prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    fields.update(overrides)
    return AgentConfig(**fields)


def _update(update: dict) -> dict:
    return {"type": "sessionUpdate", "data": {"sessionId": REAL_SESSION, "update": update}}


STREAM_LINES = [
    {"type": "runStarted",
     "data": {"payloadSchema": "acp", "acpProtocolVersion": 1, "engine": "v3"}},
    # v3 interleaves plain log lines with the JSON stream; they must be skipped,
    # not crash the reader.
    "[INFO] kas.server.starting {\"version\":\"0.54.8\"}",
    _update({"sessionUpdate": "tool_call", "toolCallId": TOOL_CALL_ID,
             "title": "Run Command", "kind": "execute",
             "rawInput": {"command": "ls -la", "run_in_background": False},
             "_meta": {"kiro": {"toolOrigin": "default"}}}),
    # Output-only update, no status: mid-flight, so it must not emit a record.
    _update({"sessionUpdate": "tool_call_update", "toolCallId": TOOL_CALL_ID,
             "content": [{"type": "content",
                          "content": {"type": "text", "text": "file1\nfile2"}}]}),
    _update({"sessionUpdate": "tool_call_update", "toolCallId": TOOL_CALL_ID,
             "status": "completed", "title": "Run Command",
             "content": [{"type": "content",
                          "content": {"type": "text", "text": "file1\nfile2"}}],
             "rawInput": {"command": "ls -la"},
             "rawOutput": {"items": [{"Json": {"exit_status": "exit status: 0"}}]}}),
    # The real shape captured from 2.21.0: absolute token counts per component,
    # `tools` totalling its own builtin/mcp children, plus the percentage the
    # adapter deliberately ignores and the credits it cannot record.
    _update({"sessionUpdate": "session_info_update",
             "_meta": {"kiro": {
                 "kind": "context_usage",
                 "contextUsage": {"usagePercentage": 0.9},
                 "breakdown": {
                     "contextFiles": {"tokens": 0, "percent": 0, "items": []},
                     "kiroResponses": {"tokens": 0, "percent": 0},
                     "sessionFiles": {"tokens": 0, "percent": 0},
                     "tools": {"tokens": 5076, "percent": 0.5,
                               "builtin": {"tokens": 5076, "percent": 0.5},
                               "mcp": {"tokens": 0, "percent": 0}},
                     "yourPrompts": {"tokens": 4290, "percent": 0.4}},
                 "promptTurnSummaries": [
                     {"unit": "credit", "unitPlural": "credits",
                      "usage": 0.0275470104145937}]}}}),
    _update({"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "FINAL_TEXT"}}),
    {"type": "runFinished",
     "data": {"sessionId": REAL_SESSION, "status": "success",
              "stopReason": "end_turn", "finalText": "FINAL_TEXT",
              "finalTextTruncated": False}},
]


def test_engine_is_pinned_and_model_and_effort_are_passed(tmp_path, fake_cli_env):
    """v2 accepts --model and --effort and then silently ignores both, so the
    engine is not left to the CLI's default — see agent_kirocli.py."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--agent-engine") + 1] == "v3"
    assert argv[argv.index("--model") + 1] == "test-model"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--resume-id" not in argv          # no real Kiro session yet


def test_prompt_is_separated_from_the_flags(tmp_path, fake_cli_env):
    """A prompt starting with a dash is read as an unknown flag without `--`,
    and an agent's prompt is arbitrary text."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path, prompt="--not a flag"))
    argv = fake_cli_env.argv()
    assert argv[-2] == "--"
    assert argv[-1].endswith("--not a flag")


def test_fresh_turn_folds_the_system_prompt_into_the_prompt(tmp_path, fake_cli_env):
    """Kiro CLI has no system-prompt flag, so a fresh turn carries it inline."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert fake_cli_env.argv()[-1] == "you are a test agent\n\ndo the thing"


def test_resumed_turn_does_not_resend_the_system_prompt(tmp_path, fake_cli_env):
    """The resumed session already holds it; re-sending would pay for it again
    on every JSON retry and gate correction."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path, session_id=REAL_SESSION))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--resume-id") + 1] == REAL_SESSION
    assert argv[-1] == "do the thing"


def test_a_bare_uuid_session_is_also_resumable(tmp_path, fake_cli_env):
    """The v2 engine mints ids without the `sess_` prefix."""
    fake_cli_env.set_lines(STREAM_LINES)
    bare = str(uuid.uuid4())
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path, session_id=bare))
    argv = fake_cli_env.argv()
    assert argv[argv.index("--resume-id") + 1] == bare


def test_placeholder_session_id_is_not_resumed(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    agent_kirocli.KiroCliAdapter().run(
        _request(tmp_path, session_id="sssf-abc123-builder-9f2a"))
    assert "--resume-id" not in fake_cli_env.argv()


def test_final_text_and_real_session_id_are_extracted(tmp_path, fake_cli_env):
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.text == "FINAL_TEXT"
    assert result.session_id == REAL_SESSION


def test_truncated_final_text_falls_back_to_the_streamed_chunks(tmp_path, fake_cli_env):
    """A clipped finalText is a worse answer than the chunks streamed in full."""
    lines = STREAM_LINES[:-1] + [
        {"type": "runFinished",
         "data": {"sessionId": REAL_SESSION, "status": "success",
                  "finalText": "FINAL_", "finalTextTruncated": True}}]
    fake_cli_env.set_lines(lines)
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.text == "FINAL_TEXT"


def test_context_occupancy_is_summed_from_the_real_token_breakdown(
        tmp_path, fake_cli_env):
    """Kiro reports absolute per-component token counts. Only the top level is
    summed: `tools` already totals its builtin/mcp children."""
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.context_window == 200_000      # the ceiling, from the catalog
    assert result.context_tokens == 9366         # 5076 tools + 4290 yourPrompts


def test_the_context_percentage_is_never_used_to_derive_tokens(tmp_path, fake_cli_env):
    """Regression. usagePercentage does not reconcile with the breakdown or the
    catalog — measured on 2.21.0, one run reported 9366 tokens at 0.90% and
    7008 at 10.83%, implying 1.04M and 65k ceilings for a 200k model. The old
    code multiplied it by the window and overstated occupancy ~3x. Here the
    percentage would yield 0.9% of 200k = 1800; the breakdown is authoritative."""
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.context_tokens != 1_800
    assert result.context_tokens == 9366


def test_the_last_occupancy_report_wins(tmp_path, fake_cli_env):
    """context_tokens is "how full is the window now", not a sum over turns."""
    lines = STREAM_LINES[:-1] + [
        _update({"sessionUpdate": "session_info_update",
                 "_meta": {"kiro": {"breakdown": {
                     "yourPrompts": {"tokens": 12_000, "percent": 6.0}}}}}),
        STREAM_LINES[-1]]
    fake_cli_env.set_lines(lines)
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.context_tokens == 12_000


def test_a_stream_without_a_breakdown_reports_unknown_occupancy(tmp_path, fake_cli_env):
    """0 means "not reported". Guessing from the percentage is what this
    replaced, so the absence must stay visible rather than be filled in."""
    lines = [ln for ln in STREAM_LINES
             if not (isinstance(ln, dict) and ln.get("type") == "sessionUpdate"
                     and (ln["data"]["update"].get("sessionUpdate")
                          == "session_info_update"))]
    fake_cli_env.set_lines(lines)
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.context_tokens == 0
    assert result.context_window == 200_000       # the ceiling is still known


def test_tokens_and_cost_stay_zero(tmp_path, fake_cli_env):
    """Kiro CLI bills credits, not tokens or dollars. The context breakdown is
    occupancy, NOT consumption — usage.merge() sums across retries, so feeding
    occupancy into total_tokens would report a 3-retry phase as 3x the context.
    The credits land in usage.credits, whose unit is not dollars; the cost
    fields stay 0 because a credit is not one."""
    fake_cli_env.set_lines(STREAM_LINES)
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.usage.total_tokens == 0
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.total_cost == 0.0
    assert result.usage.credits == pytest.approx(0.0275470104145937)


def test_credits_accumulate_across_turns_rather_than_last_winning(
        tmp_path, fake_cli_env):
    """Occupancy is a snapshot, credits are a bill. Each session_info_update
    carries only its own turn's summary, so a second one adds rather than
    replaces — the opposite of how context_tokens is handled."""
    second_turn = {"type": "sessionUpdate",
                   "data": {"sessionId": REAL_SESSION,
                            "update": {"sessionUpdate": "session_info_update",
                                       "_meta": {"kiro": {
                                           "promptTurnSummaries": [
                                               {"unit": "credit",
                                                "usage": 0.5}]}}}}}
    fake_cli_env.set_lines(STREAM_LINES + [second_turn])
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.usage.credits == pytest.approx(0.5275470104145937)


def test_a_summary_in_another_unit_is_not_counted_as_credits(
        tmp_path, fake_cli_env):
    """The unit is explicit on the wire, so it is checked rather than assumed."""
    other_unit = {"type": "sessionUpdate",
                  "data": {"sessionId": REAL_SESSION,
                           "update": {"sessionUpdate": "session_info_update",
                                      "_meta": {"kiro": {
                                          "promptTurnSummaries": [
                                              {"unit": "token",
                                               "usage": 9999}]}}}}}
    fake_cli_env.set_lines(STREAM_LINES + [other_unit])
    result = agent_kirocli.KiroCliAdapter().run(_request(tmp_path))
    assert result.usage.credits == pytest.approx(0.0275470104145937)


def test_kiros_own_bookkeeping_call_is_not_traced_as_a_tool_use(
        tmp_path, fake_cli_env):
    """`fetch_cloud_config` is Kiro fetching its own config, not the agent
    choosing a tool. Captured verbatim from 2.21.0, including the detail that
    matters: only the OPENING update identifies it (`_meta.kiro.toolId`), while
    its closing `tool_call_update` carries an empty `_meta` and no title. A
    filter applied per-update therefore drops the opener and lets the closer
    through, which the tracker then reports as a call named "tool" — so the
    whole call id has to be suppressed, not the updates that look internal."""
    internal_id = "54c6445c-7cc1-4b90-9f1e-2c3d4e5f6a7b"
    internal = [
        {"type": "sessionUpdate",
         "data": {"sessionId": REAL_SESSION,
                  "update": {"sessionUpdate": "tool_call",
                             "toolCallId": internal_id,
                             "title": "Fetching your cloud config",
                             "status": "pending",
                             "_meta": {"kiro": {"toolId": "fetch_cloud_config"}}}}},
        {"type": "sessionUpdate",
         "data": {"sessionId": REAL_SESSION,
                  "update": {"sessionUpdate": "tool_call_update",
                             "toolCallId": internal_id,
                             "status": "completed",
                             "_meta": {}}}},
    ]
    fake_cli_env.set_lines(internal + STREAM_LINES)
    events = []
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path), on_event=events.append)
    assert [e["tool"] for e in events] == ["run_command"]


def test_tool_events_are_forwarded_once_normalized(tmp_path, fake_cli_env):
    """Three raw updates describe one call — one row, emitted when the status
    lands, named by the tool id Kiro embedded in the call id."""
    fake_cli_env.set_lines(STREAM_LINES)
    events = []
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path), on_event=events.append)
    assert len(events) == 1
    record = events[0]
    assert record["tool"] == "run_command"
    assert record["args"]["command"] == "ls -la"
    assert record["ok"] is True
    assert record["label"] == "run_command: ls -la"
    assert "file1" in record["result_snippet"]
    assert record["started_at"] and record["ended_at"]


def test_a_rejected_tool_call_is_recorded_as_failed(tmp_path, fake_cli_env):
    """What an untrusted tool looks like on the wire — the reason SSSF runs
    Kiro with --trust-all-tools and leaves the boundary to permissions.py."""
    lines = STREAM_LINES[:3] + [
        _update({"sessionUpdate": "tool_call_update", "toolCallId": TOOL_CALL_ID,
                 "status": "failed", "title": "Run Command",
                 "rawOutput": {"message": "The user rejected this tool call."}}),
        STREAM_LINES[-1]]
    fake_cli_env.set_lines(lines)
    events = []
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path), on_event=events.append)
    assert len(events) == 1
    assert events[0]["ok"] is False
    assert "rejected" in events[0]["result_snippet"]


def test_every_turn_trusts_tools_and_invents_no_allowlist(tmp_path, fake_cli_env):
    """--trust-tools takes v3's own tool ids and DENIES an unknown name, so no
    mapping from `tools` is smuggled in."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path, tools=["read", "bash"]))
    argv = fake_cli_env.argv()
    assert "--trust-all-tools" in argv
    assert "--trust-tools" not in argv
    assert not any(arg.startswith("--trust-tools") for arg in argv)
    assert "read" not in argv and "bash" not in argv


def test_read_only_does_not_select_a_native_sandbox(tmp_path, fake_cli_env):
    """Kiro CLI has no read-only sandbox flag; claiming one would be a
    guarantee the CLI does not give."""
    fake_cli_env.set_lines(STREAM_LINES)
    agent_kirocli.KiroCliAdapter().run(_request(tmp_path, read_only=True))
    assert "--sandbox" not in fake_cli_env.argv()


def test_nonzero_exit_raises_with_the_run_error_message(tmp_path, fake_cli_env):
    fake_cli_env.set_lines([
        {"type": "runError",
         "data": {"sessionId": None, "stage": "engine",
                  "message": "--output-format stream-json is not supported"}}])
    fake_cli_env.set_exit(1)
    with pytest.raises(RuntimeError, match="not supported"):
        agent_kirocli.KiroCliAdapter().run(_request(tmp_path))


def test_validate_rejects_harness_engineering():
    problems = agent_kirocli.KiroCliAdapter().validate(
        _agent(harness_engineering=["some-extension.ts"]))
    assert problems and "Pi-only" in problems[0]


def test_validate_rejects_an_effective_tools_list():
    problems = agent_kirocli.KiroCliAdapter().validate(_agent(tools=["read", "bash"]))
    assert problems and "tools" in problems[0] and "DENIES" in problems[0]


def test_validate_accepts_tools_explicitly_set_to_none(fake_cli_env):
    assert agent_kirocli.KiroCliAdapter().validate(_agent(tools=None)) == []


@pytest.mark.parametrize("effort", ["off", "minimal"])
def test_validate_rejects_efforts_kiro_silently_ignores(effort, fake_cli_env):
    """`--effort off` is accepted by the CLI and then ignored, so validate() is
    the only thing standing between the config and a silently wrong setting."""
    problems = agent_kirocli.KiroCliAdapter().validate(_agent(thinking=effort))
    assert problems and "effort level" in problems[0]


def test_validate_rejects_a_model_missing_from_the_catalog(fake_cli_env):
    fake_cli_env.set_kiro_models(
        {"models": [{"model_id": "claude-sonnet-4.6", "context_window_tokens": 1_000_000}],
         "default_model": "claude-sonnet-4.6"})
    problems = agent_kirocli.KiroCliAdapter().validate(_agent(model="nope"))
    assert problems and "not in `kiro-cli chat --list-models`" in problems[0]


def test_an_unreadable_catalog_does_not_fail_a_valid_model(fake_cli_env, monkeypatch):
    """An empty catalog means the CLI is missing or unauthenticated. Reporting
    that as a bad model name would send the operator after the wrong bug."""
    monkeypatch.setattr(agent_kirocli, "KIRO_PATH", "kiro-cli-that-does-not-exist")
    agent_kirocli._model_catalog.cache_clear()
    assert agent_kirocli.KiroCliAdapter().validate(_agent(model="anything")) == []
