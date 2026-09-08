"""Shared fixtures for the multi-harness adapter tests.

Run with:
    uv run --with pydantic --with pyyaml --with python-dotenv --with rich --with pytest \\
        pytest <skill-dir>/tests

No test here calls a real model. Every adapter is exercised against
fake_cli.py, a stand-in CLI driven by env vars (see fixtures/fake_cli.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ADWS_DIR = TESTS_DIR.parent / "templates" / "adws"
FAKE_CLI = TESTS_DIR / "fixtures" / "fake_cli.py"
REPO_ROOT = TESTS_DIR.parents[3]

if str(ADWS_DIR) not in sys.path:
    sys.path.insert(0, str(ADWS_DIR))


@pytest.fixture
def fake_cli() -> Path:
    return FAKE_CLI


@pytest.fixture
def fake_cli_env(tmp_path, monkeypatch):
    """Wire FAKE_CLI_* env vars to files under tmp_path and return a small
    controller: set_lines(jsonl_lines), set_exit(code), argv() -> list[str]."""
    argv_file = tmp_path / "argv.json"
    stdin_file = tmp_path / "stdin.txt"
    lines_file = tmp_path / "lines.jsonl"
    monkeypatch.setenv("FAKE_CLI_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLI_STDIN_FILE", str(stdin_file))
    monkeypatch.setenv("FAKE_CLI_LINES_FILE", str(lines_file))
    monkeypatch.setenv("FAKE_CLI_EXIT_CODE", "0")

    class Controller:
        def set_lines(self, lines: list[dict | str]) -> None:
            lines_file.write_text("\n".join(
                line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")

        def set_exit(self, code: int) -> None:
            monkeypatch.setenv("FAKE_CLI_EXIT_CODE", str(code))

        def set_catalog(self, line: str) -> None:
            monkeypatch.setenv("FAKE_PI_CATALOG", line)

        def set_kiro_models(self, payload: dict) -> None:
            """The JSON catalog `kiro-cli chat --list-models --format json` returns."""
            monkeypatch.setenv("FAKE_KIRO_MODELS", json.dumps(payload))

        def set_agy_models(self, rows: dict[str, str]) -> None:
            """The `<slug>\\t<Display Name>` rows `agy models` returns."""
            monkeypatch.setenv(
                "FAKE_AGY_MODELS",
                "\n".join(f"{slug}\t{name}" for slug, name in rows.items()))

        def set_stderr_bytes(self, count: int) -> None:
            """Have the fake CLI blast `count` bytes to stderr in one write,
            right after its first stdout line — see fixtures/fake_cli.py."""
            monkeypatch.setenv("FAKE_CLI_STDERR_BYTES", str(count))

        def argv(self) -> list[str]:
            return json.loads(argv_file.read_text())

        def stdin(self) -> str:
            return stdin_file.read_text()

    return Controller()


def call_with_timeout(fn: Callable[[], Any], timeout: float = 10.0) -> Any:
    """Run `fn` on a daemon thread and fail loudly if it doesn't return in time.

    Used for the stderr-pipe-deadlock regression tests: if the fix regresses,
    the adapter call can hang forever. A plain call would then hang the whole
    suite; joining a daemon thread with a timeout instead lets pytest report a
    clear failure and exit — the stuck thread (and its blocked subprocess) is
    abandoned, but it cannot keep the process alive since it is a daemon.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        pytest.fail(f"did not return within {timeout}s — likely a stderr-pipe deadlock")
    if "error" in box:
        raise box["error"]
    return box["result"]


@pytest.fixture
def sssf_repo(tmp_path, monkeypatch):
    """A throwaway git repo, checked out as cwd — what Run()/permissions.py expect.

    No commit is made (and no identity configured for one). Runtime is ignored
    just as it is in an installed project, so framework-owned SQLite writes are
    not attributed to the adapter under test.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".git/info/exclude").write_text("/adws/adw_data/\n")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def tracked_repo(tmp_path, monkeypatch):
    """Clone the already-committed fixture repository without staging in tests."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(REPO_ROOT), str(repo)], check=True)
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def prompt_files(sssf_repo):
    """Minimal system/user prompt templates an AgentConfig can point at."""
    directory = sssf_repo / "prompt_engineering"
    directory.mkdir(parents=True, exist_ok=True)
    system = directory / "system.md"
    user = directory / "user.md"
    system.write_text("You are a test agent.")
    user.write_text("{{prompt}}")
    return system, user
