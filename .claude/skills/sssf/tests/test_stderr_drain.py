"""Regression: a CLI that fills the stderr pipe must not block stdout
streaming — every adapter reads stdout to completion before ever touching
stderr, and stdout/stderr are independent OS pipes with their own fixed
capacity. A child blocked mid write to a full, undrained stderr pipe stops
producing stdout too, which looks exactly like a hung agent.

Each adapter must drain stderr concurrently (adw_modules.utils.drain_stderr)
so this never happens. Every test here runs the adapter off the main thread
with a hard timeout (see conftest.call_with_timeout): if the fix regresses,
the test fails fast instead of hanging the suite.
"""

from __future__ import annotations

import pytest
from adw_modules import agent_claudecode, agent_codex, agent_pi
from adw_modules.data_types import HarnessRequest
from conftest import call_with_timeout

# Far past any OS pipe buffer (commonly ~64KB on Linux/macOS) — if stderr
# isn't drained concurrently, writing this blocks the fake CLI mid-stream.
STDERR_BYTES = 4_000_000
DEADLOCK_TIMEOUT = 15.0   # generous; a correctly draining adapter returns in well under 1s

PI_LINES = [
    {"type": "tool_execution_start", "toolCallId": "call1", "toolName": "bash",
     "args": {"command": "ls"}},
    {"type": "tool_execution_end", "toolCallId": "call1", "toolName": "bash",
     "args": {"command": "ls"}, "isError": False,
     "result": {"content": [{"type": "text", "text": "ok"}]}},
    {"type": "message_end", "message": {"role": "assistant",
     "content": [{"type": "text", "text": "FINAL_TEXT"}],
     "usage": {"totalTokens": 1}, "stopReason": "stop"}},
]

CLAUDECODE_LINES = [
    {"type": "system", "subtype": "init", "session_id": "11111111-1111-1111-1111-111111111111"},
    {"type": "result", "subtype": "success", "is_error": False,
     "session_id": "11111111-1111-1111-1111-111111111111", "result": "FINAL_TEXT",
     "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0},
]

CODEX_LINES = [
    {"type": "thread.started", "thread_id": "22222222-2222-2222-2222-222222222222"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
     "text": "FINAL_TEXT"}},
]


@pytest.fixture(autouse=True)
def _pi_wiring(monkeypatch, tmp_path):
    """Same wiring test_agent_pi.py needs: point PiAdapter's model catalog and
    context-window lookup at fixtures instead of the real filesystem."""
    models_json = tmp_path / "models.json"
    models_json.write_text('{"providers": {"testprov": {"models": '
                           '[{"id": "test-model", "contextWindow": 128000}]}}}')
    monkeypatch.setattr(agent_pi, "MODELS_JSON", str(models_json))
    agent_pi._pi_catalog.cache_clear()
    yield
    agent_pi._pi_catalog.cache_clear()


def test_pi_drains_stderr_without_deadlock(tmp_path, fake_cli, fake_cli_env, monkeypatch):
    monkeypatch.setattr(agent_pi, "PI_PATH", str(fake_cli))
    fake_cli_env.set_lines(PI_LINES)
    fake_cli_env.set_stderr_bytes(STDERR_BYTES)
    request = HarnessRequest(
        prompt="do the thing", system_prompt="sys", model="testprov/test-model",
        thinking="high", cwd=str(tmp_path), raw_output_path=str(tmp_path / "raw.jsonl"),
        state_dir=str(tmp_path / "state"))

    result = call_with_timeout(lambda: agent_pi.PiAdapter().run(request), DEADLOCK_TIMEOUT)
    assert result.text == "FINAL_TEXT"
    assert result.returncode == 0


def test_claude_code_drains_stderr_without_deadlock(tmp_path, fake_cli, fake_cli_env, monkeypatch):
    monkeypatch.setattr(agent_claudecode, "CLAUDE_PATH", str(fake_cli))
    fake_cli_env.set_lines(CLAUDECODE_LINES)
    fake_cli_env.set_stderr_bytes(STDERR_BYTES)
    request = HarnessRequest(
        prompt="do the thing", system_prompt="sys", model="opus", thinking="high",
        cwd=str(tmp_path), raw_output_path=str(tmp_path / "raw.jsonl"))

    result = call_with_timeout(
        lambda: agent_claudecode.ClaudeCodeAdapter().run(request), DEADLOCK_TIMEOUT
    )
    assert result.text == "FINAL_TEXT"
    assert result.returncode == 0


def test_codex_drains_stderr_without_deadlock(tmp_path, fake_cli, fake_cli_env, monkeypatch):
    monkeypatch.setattr(agent_codex, "CODEX_PATH", str(fake_cli))
    fake_cli_env.set_lines(CODEX_LINES)
    fake_cli_env.set_stderr_bytes(STDERR_BYTES)
    request = HarnessRequest(
        prompt="do the thing", system_prompt="sys", model="gpt-5.6-sol", thinking="high",
        cwd=str(tmp_path), raw_output_path=str(tmp_path / "raw.jsonl"))

    result = call_with_timeout(lambda: agent_codex.CodexAdapter().run(request), DEADLOCK_TIMEOUT)
    assert result.text == "FINAL_TEXT"
    assert result.returncode == 0


def test_pi_stderr_still_surfaces_on_failure(tmp_path, fake_cli, fake_cli_env, monkeypatch):
    """The drain must not just prevent the hang — the collected stderr text
    still has to reach the raised error message. One line (so the blast fires
    at index 0) with no message_end -> no usable text -> pi's failure path."""
    monkeypatch.setattr(agent_pi, "PI_PATH", str(fake_cli))
    fake_cli_env.set_lines([{"type": "tool_execution_start", "toolCallId": "c1",
                             "toolName": "bash", "args": {"command": "x"}}])
    fake_cli_env.set_exit(1)
    fake_cli_env.set_stderr_bytes(STDERR_BYTES)
    request = HarnessRequest(
        prompt="do the thing", system_prompt="sys", model="testprov/test-model",
        thinking="high", cwd=str(tmp_path), raw_output_path=str(tmp_path / "raw.jsonl"),
        state_dir=str(tmp_path / "state"))

    errors: list[RuntimeError] = []

    def run():
        try:
            agent_pi.PiAdapter().run(request)
        except RuntimeError as error:
            errors.append(error)

    call_with_timeout(run, DEADLOCK_TIMEOUT)
    assert len(errors) == 1
    assert "pi exited 1" in str(errors[0])
    assert "E" * 100 in str(errors[0])   # the drained stderr content made it into the message
