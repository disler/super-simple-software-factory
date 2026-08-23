"""Best-effort ordinary-file rollback for agent orchestration policy.

``writes`` is not a filesystem sandbox. Claude Code and Codex rely on their
native sandbox; Pi needs external isolation for untrusted workloads. This
module detects and restores Git-visible ordinary worktree paths only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from .data_types import AgentConfig, Phase, SSSFConfig
from .repository_snapshot import (RepositorySnapshot, RepositoryStateError,
                                  SnapshotCleanupError, capture_repository,
                                  cleanup_snapshot, git_integrity_changes,
                                  restore_worktree_path, runtime_node_changed,
                                  worktree_changes)


class PermissionBreach(RuntimeError):
    """An agent modified protected state or Git metadata during its phase."""


class PermissionRun(Protocol):
    """Run state required by permission monitoring.

    Example: ``snapshot(run)`` freezes this run's repository and runtime scope.
    """

    cfg: SSSFConfig
    repo_root: Path
    session_dir: Path


def snapshot(run: PermissionRun) -> RepositorySnapshot:
    """Fail closed unless a valid Git worktree and HEAD can be captured.

    Example: ``before = snapshot(run)`` before a backend spawn.
    """
    return capture_repository(run.repo_root, run.session_dir)


def _glob(pattern: str) -> re.Pattern:
    """Translate a path glob where only ``**`` crosses a separator.

    Example: ``_glob("docs/**/*.md").fullmatch("docs/a/readme.md")``.
    """
    pieces: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**", index):
            piece, width = ".*", 2
        elif char == "*":
            piece, width = "[^/]*", 1
        elif char == "?":
            piece, width = "[^/]", 1
        else:
            piece, width = re.escape(char), 1
        pieces.append(piece)
        index += width
    return re.compile("".join(pieces))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def permitted(path: str, agent: AgentConfig, run: PermissionRun,
              snapshot_state: RepositorySnapshot) -> bool:
    """Apply frozen runtime, explicit writes, and protected-file policy.

    Example: ``permitted("docs/guide.md", agent, run, before)``.
    """
    if snapshot_state.runtime_prefix and path.startswith(snapshot_state.runtime_prefix + "/"):
        return True
    if any(_matches(path, pattern) for pattern in agent.writes or []):
        return True
    if any(_matches(path, pattern) for pattern in run.cfg.defaults.protected_files):
        return False
    return agent.writes is None


def _restore_unauthorized(snapshot_state: RepositorySnapshot, paths: list[str]) -> None:
    """Restore only denied ordinary paths from their private byte backups.

    Example: ``_restore_unauthorized(before, ["src/app.py"])``.
    """
    for path in paths:
        restore_worktree_path(snapshot_state, path)


def _verify_restored(snapshot_state: RepositorySnapshot, paths: list[str]) -> None:
    """Require restored paths to return exactly to their captured file states.

    Example: ``_verify_restored(before, breaches)`` after ordinary rollback.
    """
    after, _ = worktree_changes(snapshot_state)
    failures = [path for path in paths
                if after.get(path) != snapshot_state.paths.get(path)]
    if failures:
        raise RepositoryStateError(f"ordinary path recovery failed for: {', '.join(failures)}")


def _scope_description(agent: AgentConfig, run: PermissionRun) -> str:
    if agent.writes == []:
        return "read-only"
    if agent.writes:
        return f"limited to {agent.writes}"
    return f"barred from {run.cfg.defaults.protected_files}"


def _breach_message(agent: AgentConfig, run: PermissionRun, paths: list[str],
                    metadata: list[str], runtime_changed: bool) -> str:
    """Explain restored ordinary paths and non-restored Git-state changes.

    Example: ``_breach_message(agent, run, paths, metadata, False)``.
    """
    items = [f"  - {path} — restored from private snapshot" for path in paths]
    if runtime_changed:
        items.append("  - frozen runtime directory node — not auto-restored")
    if metadata:
        items.append("  - Git metadata (" + ", ".join(metadata) + ") — not auto-restored")
    suffix = (" Git metadata was not auto-restored and requires operator inspection."
              if metadata else "")
    return (f"{agent.name} is {_scope_description(agent, run)} but modified "
            f"{len(items)} protected state item(s):\n" + "\n".join(items) + suffix)


def _enforce_snapshot(run: PermissionRun, agent: AgentConfig,
                      snapshot_state: RepositorySnapshot) -> list[str]:
    """Detect and roll back denied ordinary paths without changing Git metadata.

    Example: ``touched = _enforce_snapshot(run, agent, before)``.
    """
    metadata = git_integrity_changes(snapshot_state)
    _, touched = worktree_changes(snapshot_state)
    breaches = [path for path in touched if not permitted(path, agent, run, snapshot_state)]
    _restore_unauthorized(snapshot_state, breaches)
    _verify_restored(snapshot_state, breaches)
    runtime_changed = runtime_node_changed(snapshot_state)
    if breaches or metadata or runtime_changed:
        raise PermissionBreach(_breach_message(agent, run, breaches, metadata, runtime_changed))
    return touched


def _cleanup_after_enforcement(snapshot_state: RepositorySnapshot,
                               primary: BaseException | None) -> None:
    """Raise retained-backup cleanup errors without hiding a primary breach.

    Example: ``_cleanup_after_enforcement(before, breach)`` preserves ``breach``.
    """
    try:
        cleanup_snapshot(snapshot_state)
    except SnapshotCleanupError as cleanup:
        if primary is not None:
            raise primary from cleanup
        raise PermissionBreach(str(cleanup)) from cleanup


def enforce(run: PermissionRun, phase: Phase, agent: AgentConfig,
            before: RepositorySnapshot) -> list[str]:
    """Enforce orchestration policy after a call and always clean private backups.

    Git command failures fail the phase. Git metadata is reported, never repaired.
    Example: ``touched = enforce(run, phase, agent, before)``.
    """
    del phase
    return _enforce_with_cleanup(run, agent, before)


def _enforce_with_cleanup(run: PermissionRun, agent: AgentConfig,
                          before: RepositorySnapshot) -> list[str]:
    """Keep cleanup observable while preserving the primary enforcement error.

    Example: ``_enforce_with_cleanup(run, agent, before)`` enforces one call.
    """
    primary: BaseException | None = None
    try:
        return _enforce_snapshot(run, agent, before)
    except PermissionBreach as error:
        primary = error
        raise
    except RepositoryStateError as error:
        breach = PermissionBreach(
            f"repository integrity monitor failed: {error}. Git metadata was not "
            "auto-restored and requires operator inspection.")
        primary = breach
        raise breach from error
    except BaseException as error:
        primary = error
        raise
    finally:
        _cleanup_after_enforcement(before, primary)
