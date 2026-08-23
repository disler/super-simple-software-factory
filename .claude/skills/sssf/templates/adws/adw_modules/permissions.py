"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` saves exact working-tree
content and Git metadata before an agent runs; `enforce()` compares and restores
it afterwards, failing the phase if the agent touched anything outside its allowlist.

Comparing captured states, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from .data_types import AgentConfig, Phase, SSSFConfig
from .repository_snapshot import (FileState, RepositorySnapshot,
                                  RepositoryStateError, capture_repository,
                                  cleanup_snapshot, metadata_changed,
                                  preserve_worktree_path, restore_git_metadata,
                                  restore_preserved_path, restore_worktree_path,
                                  worktree_changes)


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


class PermissionRun(Protocol):
    """Run state required by permission enforcement.

    Example: ``snapshot(run)`` validates and captures this run's checkout.
    """

    cfg: SSSFConfig
    repo_root: Path
    session_dir: Path


def snapshot(run: PermissionRun) -> RepositorySnapshot:
    """Capture the recoverable repository state required before a backend spawn.

    This fails closed: ``run.repo_root`` must be the actual root of a usable
    Git worktree with a valid HEAD commit. Git failures are never empty state.

    Example: ``before = snapshot(run)`` before ``adapter.run(...)``.
    """
    return capture_repository(run.repo_root)


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, quietly widening patterns. `**` is the
    explicit way to cross directories.
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            piece, width = ".*", 2
        elif char == "*":
            piece, width = "[^/]*", 1
        elif char == "?":
            piece, width = "[^/]", 1
        else:
            piece, width = re.escape(char), 1
        out.append(piece)
        i += width
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def orchestrator_runtime_paths(run: PermissionRun) -> list[str]:
    """Return runtime paths exempt from post-call Git change checks.

    The orchestrator creates prompts, streams, and envelopes here. Writable
    harness profiles may also create handoff files. Native read-only profiles
    cannot write this repository-local directory, so their structured result is
    the available handoff channel.
    """
    try:
        relative = run.session_dir.resolve().relative_to(run.repo_root.resolve())
    except ValueError:
        return []
    return [relative.as_posix().rstrip("/") + "/"]


def permitted(path: str, agent: AgentConfig, run: PermissionRun) -> bool:
    """Session runtime first, then the agent's own list, then what is protected."""
    if any(_matches(path, p) for p in orchestrator_runtime_paths(run)):
        return True
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in run.cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _preserve_permitted(snapshot: RepositorySnapshot, paths: list[str],
                        after: dict[str, FileState], agent: AgentConfig,
                        run: PermissionRun) -> dict[str, FileState]:
    """Save permitted post-call files before metadata recovery changes the checkout."""
    saved: dict[str, FileState] = {}
    for path in paths:
        if permitted(path, agent, run):
            saved[path] = preserve_worktree_path(snapshot, path, after.get(path, FileState("missing")))
    return saved

def _restore_unauthorized(snapshot: RepositorySnapshot, breaches: list[str]) -> None:
    """Restore every denied worktree path from its recoverable pre-call copy."""
    for path in breaches:
        restore_worktree_path(snapshot, path)


def _verify_recovery(snapshot: RepositorySnapshot, expected: dict[str, FileState]) -> None:
    """Fail closed when exact path or metadata recovery does not reproduce state."""
    after, _ = worktree_changes(snapshot)
    failures = [path for path, state in expected.items() if after.get(path, FileState("missing")) != state]
    if failures or metadata_changed(snapshot):
        detail = ", ".join(failures) or "Git metadata"
        raise RepositoryStateError(f"repository recovery did not restore exact state: {detail}")


def _breach_message(agent: AgentConfig, run: PermissionRun, breaches: list[str],
                    metadata_was_changed: bool) -> str:
    """Format a complete permission error after successful recovery."""
    scope = ("read-only" if agent.writes == [] else f"limited to {agent.writes}"
             if agent.writes else f"barred from {run.cfg.defaults.protected_files}")
    items = [f"  - {path} — restored from snapshot" for path in breaches]
    if metadata_was_changed:
        items.append("  - Git metadata — restored from snapshot")
    return f"{agent.name} is {scope} but modified {len(items)} protected state item(s):\n" + "\n".join(items)


def _restore_permitted(snapshot: RepositorySnapshot, saved: dict[str, FileState]) -> None:
    """Reapply permitted post-call paths after denied paths and metadata recover."""
    for path, state in saved.items():
        restore_preserved_path(snapshot, path, state)


def _expected_paths(snapshot: RepositorySnapshot, touched: list[str],
                    saved: dict[str, FileState]) -> dict[str, FileState]:
    """Build final state: permitted post-call paths and baseline paths otherwise."""
    return {path: saved.get(path, snapshot.paths.get(path, FileState("missing")))
            for path in touched}


def _post_call_state(snapshot_state: RepositorySnapshot) -> tuple[dict[str, FileState], list[str], bool]:
    """Capture post-call paths, recovering corrupt Git metadata before retrying."""
    try:
        after, touched = worktree_changes(snapshot_state)
        return after, touched, metadata_changed(snapshot_state)
    except RepositoryStateError:
        restore_git_metadata(snapshot_state)
        after, touched = worktree_changes(snapshot_state)
        return after, touched, True


def _enforce_snapshot(run: PermissionRun, agent: AgentConfig,
                      snapshot_state: RepositorySnapshot) -> list[str]:
    """Recover denied content and report every permission boundary violation."""
    after, touched, metadata_was_changed = _post_call_state(snapshot_state)
    breaches = [path for path in touched if not permitted(path, agent, run)]
    saved = _preserve_permitted(snapshot_state, touched, after, agent, run)
    _restore_unauthorized(snapshot_state, breaches)
    if metadata_was_changed:
        restore_git_metadata(snapshot_state)
    _restore_permitted(snapshot_state, saved)
    _verify_recovery(snapshot_state, _expected_paths(snapshot_state, touched, saved))
    if breaches or metadata_was_changed:
        raise PermissionBreach(_breach_message(agent, run, breaches, metadata_was_changed))
    return touched


def enforce(run: PermissionRun, phase: Phase, agent: AgentConfig,
            before: RepositorySnapshot) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding an unauthorized change while
    reporting a failure, so a byte-backed snapshot restores every denied path,
    including pre-existing dirty and untracked content, before the phase dies.

    Example: ``touched = enforce(run, phase, agent, before)``.
    """
    del phase
    try:
        return _enforce_snapshot(run, agent, before)
    except RepositoryStateError as error:
        raise PermissionBreach(f"repository permission recovery failed: {error}") from error
    finally:
        cleanup_snapshot(before)
