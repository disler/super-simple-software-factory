"""Real, read-only smoke tests — one per harness. NOT run automatically.

These call the actual `pi`, `claude`, `codex`, `kiro-cli`, and `agy` CLIs with a
trivial read-only prompt and cost real tokens, so they are skipped unless
explicitly requested:

    SSSF_SMOKE=1 uv run --with pydantic --with pyyaml --with python-dotenv \\
        --with pytest pytest <skill-dir>/tests/test_smoke_manual.py -s
"""

from __future__ import annotations

import os
import shutil
import uuid

import pytest
from adw_modules import (agent_antigravity, agent_claudecode, agent_codex,
                         agent_kirocli, agent_pi)
from adw_modules.data_types import HarnessRequest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SSSF_SMOKE"),
    reason="manual smoke test only — set SSSF_SMOKE=1 to run (costs real tokens)")

PROMPT = "Reply with exactly one word: pong."


def _request(tmp_path, model: str, **overrides) -> HarnessRequest:
    fields = dict(
        prompt=PROMPT, system_prompt="Reply with exactly one word.",
        model=model, thinking="low", session_id=str(uuid.uuid4()),
        cwd=str(tmp_path), raw_output_path=str(tmp_path / "raw_output.jsonl"),
        state_dir=str(tmp_path / "state"))
    fields.update(overrides)
    return HarnessRequest(**fields)


@pytest.mark.skipif(not shutil.which("pi"), reason="pi CLI not on PATH")
def test_pi_smoke(tmp_path):
    result = agent_pi.PiAdapter().run(_request(tmp_path, "google/gemini-3.6-flash"))
    assert result.text.strip()


@pytest.mark.skipif(not shutil.which("claude"), reason="claude CLI not on PATH")
def test_claude_code_smoke(tmp_path):
    result = agent_claudecode.ClaudeCodeAdapter().run(_request(tmp_path, "haiku"))
    assert result.text.strip()


@pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI not on PATH")
def test_codex_smoke(tmp_path):
    result = agent_codex.CodexAdapter().run(_request(tmp_path, "gpt-5-mini"))
    assert result.text.strip()


# session_id=None on both of the following: a random UUID would be sent as a
# real id to resume (`--resume-id` / `--conversation`) and the CLI would fail
# looking for a session it never created. Only ids a CLI itself minted resume.

@pytest.mark.skipif(not shutil.which("kiro-cli"), reason="kiro-cli not on PATH")
def test_kiro_cli_smoke(tmp_path):
    result = agent_kirocli.KiroCliAdapter().run(
        _request(tmp_path, "claude-haiku-4.5", session_id=None))
    assert result.text.strip()
    assert result.session_id                  # Kiro reported its own id back
    assert result.context_window              # resolved from its model catalog


@pytest.mark.skipif(not shutil.which("agy"), reason="agy CLI not on PATH")
def test_antigravity_smoke(tmp_path):
    # thinking matches the slug's own tier: `agy` rejects --model and --effort
    # together, and validate() rejects the pair when they disagree.
    result = agent_antigravity.AntigravityAdapter().run(
        _request(tmp_path, "gemini-3.6-flash-low", session_id=None))
    assert result.text.strip()
    assert result.session_id                  # the conversation_id it minted
    assert result.usage.total_tokens          # agy reports real token counts
