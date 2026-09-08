"""What an agent may CHANGE, enforced in code after every harness turn.

`tools:` is a capability list, not a sandbox: `bash` can run destructive Git
commands and `write` can reach paths outside the intended work product. Each
harness turn is therefore bracketed by a working-tree snapshot. A turn that
changes an unauthorized path is rolled back to its exact pre-turn contents and
the phase fails without giving the agent another turn.

The snapshot intentionally covers Git tracked files, ordinary untracked files,
and every configured prompt inside the repository. Common ignored trees such
as dependencies and session runtime stay out of the scan, as do the exact
SQLite observability files written by SSSF itself. Post-execution enforcement
cannot protect paths outside the repository; native harness sandboxes remain
defense in depth where available.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .data_types import AgentConfig


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


@dataclass(frozen=True)
class PathState:
    """Restorable state of one repository path."""

    kind: str
    mode: int = 0
    content: bytes = b""


MISSING = PathState("missing")


def _git_paths(args: list[str], cwd: Path) -> list[str]:
    """Return Git path output without ever treating a filename as a line."""
    result = subprocess.run(["git", *args, "-z"], cwd=cwd, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"could not inspect repository paths: {detail}")
    return [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]


def _repo_relative(run, value: str | Path) -> str | None:
    """Normalize an in-repo path lexically; never resolve through symlinks."""
    root = Path(run.repo_root).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] == ".git":
        return None
    return relative.as_posix()


def _configured_prompt_paths(run) -> set[str]:
    paths = set()
    for configured_agent in run.cfg.agents:
        for ref in (configured_agent.prompt_engineering.system,
                    configured_agent.prompt_engineering.user):
            relative = _repo_relative(run, ref)
            if relative is not None:
                paths.add(relative)
    return paths


def prompt_symlink_component(repo_root: str | Path,
                             value: str | Path) -> str | None:
    """Name an in-repo symlink crossed by a prompt path, if there is one."""
    root = Path(repo_root).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None

    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return current.relative_to(root).as_posix()
    return None


def _observability_paths(run) -> set[str]:
    """Framework-owned SQLite files changed by tracing during adapter calls."""
    database = _repo_relative(run, run.cfg.observability.db)
    if database is None:
        return set()
    return {database, f"{database}-wal", f"{database}-shm",
            f"{database}-journal"}


def _path_state(root: Path, relative: str) -> PathState:
    """Read a path without following a symlink in any parent component."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        return PathState("unsafe")

    candidate = root
    for part in parts[:-1]:
        candidate /= part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return MISSING
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return PathState("unsafe-parent", stat.S_IMODE(info.st_mode),
                             os.fsencode(str(candidate.relative_to(root))))

    candidate /= parts[-1]
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return MISSING
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as stream:
            return PathState("file", mode, stream.read())
    if stat.S_ISLNK(info.st_mode):
        return PathState("symlink", mode, os.fsencode(os.readlink(candidate)))
    if stat.S_ISDIR(info.st_mode):
        return PathState("directory", mode)
    return PathState("other", mode)


def snapshot(run) -> dict[str, PathState]:
    """Capture binary contents, type, and mode for relevant repository paths.

    All tracked paths are retained even when currently deleted, so their
    absence is state too. Untracked files honor the repository's ignore rules;
    configured prompt files are added explicitly so moving `data_dir` or adding
    an ignore rule cannot make an active prompt writable without detection.
    """
    root = Path(run.repo_root).resolve()
    paths = set(_git_paths(
        ["ls-files", "--cached", "--others", "--exclude-standard"], root))
    prompts = _configured_prompt_paths(run)
    paths.difference_update(_observability_paths(run))
    paths.update(prompts)
    return {path: _path_state(root, path) for path in sorted(paths)}


def changed_paths(before: dict[str, PathState],
                  after: dict[str, PathState]) -> list[str]:
    """Every path whose contents, type, mode, or existence changed."""
    return sorted({path for path in set(before) | set(after)
                   if before.get(path, MISSING) != after.get(path, MISSING)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator."""
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(run) -> list[str]:
    """Only this run's session directory is exempt from repo write limits."""
    relative = _repo_relative(run, run.session_dir)
    return [relative.rstrip("/") + "/"] if relative is not None else []


def _runtime_prefix(run) -> str | None:
    relative = _repo_relative(run, run.cfg.defaults.data_dir)
    return relative.rstrip("/") + "/" if relative is not None else None


def _write_patterns(run, agent: AgentConfig) -> list[str]:
    patterns = []
    for pattern in agent.writes or []:
        if "*" in pattern or "?" in pattern or pattern.endswith("/"):
            patterns.append(pattern.removeprefix("./"))
            continue
        relative = _repo_relative(run, pattern)
        if relative is not None:
            patterns.append(relative)
    return patterns


def permitted(path: str, agent: AgentConfig, run) -> bool:
    """Apply prompt protection before runtime and general write grants."""
    write_patterns = _write_patterns(run, agent)
    if path in _configured_prompt_paths(run):
        # Broad Markdown globs in the default documenter roster must not unlock
        # the instructions that define what that agent is allowed to do.
        return path in {pattern for pattern in write_patterns
                        if "*" not in pattern and "?" not in pattern
                        and not pattern.endswith("/")}
    if any(_matches(path, pattern) for pattern in always_writable(run)):
        return True
    runtime_prefix = _runtime_prefix(run)
    if runtime_prefix is not None and path.startswith(runtime_prefix):
        return False
    if any(_matches(path, pattern) for pattern in write_patterns):
        return True
    if any(_matches(path, pattern) for pattern in run.cfg.defaults.protected_files):
        return False
    return agent.writes is None


def _safe_parent(root: Path, relative: str) -> tuple[Path | None, str | None]:
    parent = root
    for part in Path(relative).parts[:-1]:
        parent /= part
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return None, f"parent is missing: {parent.relative_to(root)}"
        if stat.S_ISLNK(info.st_mode):
            return None, f"parent is a symlink: {parent.relative_to(root)}"
        if not stat.S_ISDIR(info.st_mode):
            return None, f"parent is not a directory: {parent.relative_to(root)}"
    return parent, None


def _restore(run, path: str, state: PathState) -> str:
    """Restore one exact path, refusing ambiguous recursive cleanup."""
    root = Path(run.repo_root).resolve()
    parent, problem = _safe_parent(root, path)
    if parent is None:
        return f"could not restore ({problem})"
    target = root / path
    try:
        current = target.lstat()
    except FileNotFoundError:
        current = None

    if state.kind == "missing":
        if current is None:
            return "already absent"
        if stat.S_ISDIR(current.st_mode):
            return "could not delete (path is a directory; refusing recursive deletion)"
        try:
            target.unlink()
            return "deleted"
        except OSError as error:
            return f"could not delete ({error})"

    if state.kind not in {"file", "symlink"}:
        return f"could not restore unsupported baseline type {state.kind!r}"
    if current is not None and stat.S_ISDIR(current.st_mode):
        return "could not restore (path is a directory; refusing recursive deletion)"

    temporary: str | None = None
    try:
        if state.kind == "file":
            descriptor, temporary = tempfile.mkstemp(prefix=".sssf-restore-", dir=parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(state.content)
            os.chmod(temporary, state.mode)
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".sssf-restore-", dir=parent)
            os.close(descriptor)
            Path(temporary).unlink()
            os.symlink(os.fsdecode(state.content), temporary)
            if hasattr(os, "lchmod"):
                os.lchmod(temporary, state.mode)
        os.replace(temporary, target)
        return "restored"
    except OSError as error:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
        return f"could not restore ({error})"


def enforce(run, phase, agent: AgentConfig,
            before: dict[str, PathState]) -> list[str]:
    """Undo unauthorized changes since `before`, then fail the phase."""
    after = snapshot(run)
    touched = changed_paths(before, after)
    breaches = [path for path in touched if not permitted(path, agent, run)]
    if not breaches:
        return touched

    outcomes = {}
    collision_prefixes = []
    for path in breaches:
        collision = next((prefix for prefix in collision_prefixes
                          if path.startswith(prefix + "/")), None)
        if collision is not None:
            outcomes[path] = f"left untouched (ancestor collision at {collision})"
            continue
        outcome = _restore(run, path, before.get(path, MISSING))
        outcomes[path] = outcome
        if "refusing recursive deletion" in outcome:
            collision_prefixes.append(path)
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files} and active prompts")
    detail = "\n".join(f"  - {path} — {outcome}"
                       for path, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")
