"""Regression tests for the diff_matches_claims gate (issue #6).

The gate must reconcile the envelope's `changed_files` claim against the real
working-tree change set — not file existence — because the commit phase stages
the whole tree (`git_helper.commit_all`). A claim that did not happen, or a
change nobody claimed, must both fail the gate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from adw_modules import gates
from adw_modules.data_types import BuildOutput


def git(cmd: list[str], cwd: Path) -> None:
    subprocess.run(["git", *cmd], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git(["init", "-q"], tmp_path)
    git(["config", "user.email", "test@example.com"], tmp_path)
    git(["config", "user.name", "test"], tmp_path)
    return tmp_path


def write(path: Path, name: str, content: str) -> None:
    (path / name).write_text(content)


def commit(path: Path) -> None:
    git(["add", "-A"], path)
    git(["commit", "-qm", "base"], path)


def gate(changed_files: list[str]):
    return gates.diff_matches_claims(
        BuildOutput(status="success", changed_files=changed_files), None)


def test_unclaimed_change_fails(repo):
    """The 5,222-file case: an unexpected file rides into the commit."""
    write(repo, "a.txt", "v2")
    write(repo, "sneaky.txt", "new")
    report = gate(["a.txt"])
    assert not report.passed
    assert any("sneaky.txt" in v for v in report.violations)


def test_claimed_but_unchanged_fails(repo):
    """A file that exists but was never touched is not a change."""
    write(repo, "untouched.txt", "v1")
    commit(repo)
    report = gate(["untouched.txt"])
    assert not report.passed


def test_exact_match_passes(repo):
    write(repo, "a.txt", "v2")
    write(repo, "b.txt", "new")
    report = gate(["a.txt", "b.txt"])
    assert report.passed


def test_gitignored_files_are_not_unclaimed(repo):
    """Gitignored noise never reaches the working-tree change set."""
    write(repo, ".gitignore", "*.log")
    commit(repo)
    write(repo, "a.txt", "v2")
    write(repo, "noise.log", "x")
    report = gate(["a.txt"])
    assert report.passed


def test_non_repo_falls_back_to_existence(tmp_path, monkeypatch):
    """Without a repo there is no diff; fall back to the existence check."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x.txt", "hi")
    assert gate(["x.txt"]).passed
    assert not gate(["missing.txt"]).passed
