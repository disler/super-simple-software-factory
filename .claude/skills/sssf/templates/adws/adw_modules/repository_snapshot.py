"""Best-effort Git-visible worktree snapshots for permission monitoring.

The snapshot never copies, restores, or writes Git metadata. Git state is
fingerprinted only, because linked worktrees can share refs and configuration.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal


PathKind = Literal["file", "symlink", "missing", "directory"]


class RepositoryStateError(RuntimeError):
    """A repository cannot be monitored safely.

    Example: ``raise RepositoryStateError("git HEAD is not a commit")``.
    """


class SnapshotCleanupError(RepositoryStateError):
    """Snapshot cleanup retained sensitive backup material for an operator.

    Example: ``raise SnapshotCleanupError(path)`` when removal fails.
    """

    def __init__(self, path: Path, error: OSError):
        super().__init__(f"snapshot cleanup failed; remove retained backup {path!s}: {error}")
        self.path = path


@dataclass(frozen=True)
class FileState:
    """The exact identity and recoverable backup of one ordinary node.

    Example: ``FileState("missing")`` represents a node introduced after capture.
    """

    kind: PathKind
    mode: int = 0
    digest: str = ""
    backup_name: str = field(default="", compare=False)


@dataclass(frozen=True)
class GitIntegrityState:
    """Read-only fingerprint of Git state that must not change during a phase.

    Example: ``state = capture_git_integrity(repo)`` before an adapter starts.
    """

    head_commit: str
    current_ref: str | None
    index: FileState
    refs: tuple[str, ...]


@dataclass(frozen=True)
class RepositorySnapshot:
    """Pre-call ordinary-file backups and immutable monitor scope.

    Example: ``snapshot = capture_repository(root, session_dir)``.
    """

    repo_root: Path
    temp_dir: Path
    worktree_backup_dir: Path
    paths: dict[str, FileState]
    git_integrity: GitIntegrityState
    runtime_prefix: str | None
    runtime_node: FileState | None


def capture_repository(repo_root: Path, session_dir: Path) -> RepositorySnapshot:
    """Capture Git-visible ordinary files and freeze an in-repository runtime.

    An external absolute runtime is outside this safeguard and needs no prefix.
    Example: ``snapshot = capture_repository(root, session)``.
    """
    root = validate_repository(repo_root)
    runtime_prefix, runtime_node = capture_runtime_prefix(repo_root, session_dir)
    temp_dir = Path(tempfile.mkdtemp(prefix="sssf-repository-snapshot-"))
    try:
        backup_dir = temp_dir / "worktree"
        paths = capture_worktree_paths(root, backup_dir)
        integrity = capture_git_integrity(root)
        return RepositorySnapshot(root, temp_dir, backup_dir, paths, integrity,
                                  runtime_prefix, runtime_node)
    except BaseException as error:
        _cleanup_failed_capture(temp_dir, error)
        raise


def validate_repository(repo_root: Path) -> Path:
    """Require ``repo_root`` to be a worktree root with a resolvable HEAD.

    Example: ``root = validate_repository(run.repo_root)``.
    """
    root = repo_root.resolve()
    actual = Path(_git(root, ["rev-parse", "--show-toplevel"]).decode().strip()).resolve()
    inside = _git(root, ["rev-parse", "--is-inside-work-tree"]).decode().strip()
    _git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    if root != actual or inside != "true":
        raise RepositoryStateError(f"repository root {root!s} expected usable worktree root {actual!s}")
    return root


def capture_runtime_prefix(repo_root: Path, session_dir: Path) -> tuple[str | None, FileState | None]:
    """Freeze an in-repository runtime directory after rejecting symlink components.

    Example: ``prefix, node = capture_runtime_prefix(root, run.session_dir)``.
    """
    lexical_root = _lexical_absolute(repo_root)
    target = _runtime_lexical_path(lexical_root, session_dir)
    try:
        relative = target.relative_to(lexical_root)
    except ValueError:
        return None, None
    if not relative.parts:
        raise RepositoryStateError(f"runtime directory {target!s} must not be the repository root")
    _reject_symlink_components(lexical_root, relative)
    node = _node_state(target, directories=True)
    if node.kind != "directory":
        raise RepositoryStateError(f"runtime directory {target!s} expected an existing directory")
    return relative.as_posix(), node


def capture_worktree_paths(repo_root: Path, backup_dir: Path) -> dict[str, FileState]:
    """Back up tracked and non-ignored untracked paths by bytes and node mode.

    Example: ``paths = capture_worktree_paths(repo, backup_dir)``.
    """
    names = _git(repo_root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    return _capture_paths(repo_root, _decode_git_paths(names), backup_dir)


def capture_git_integrity(repo_root: Path) -> GitIntegrityState:
    """Fingerprint HEAD, current ref, current index, and ref map without copying them.

    Example: ``state = capture_git_integrity(repo)`` after a command exits.
    """
    head_commit = _git(repo_root, ["rev-parse", "--verify", "HEAD^{commit}"]).decode().strip()
    current_ref = _symbolic_ref(repo_root)
    index_path = _git_path(repo_root, "--git-path", "index")
    refs = tuple(_git(repo_root, ["for-each-ref", "--format=%(refname)%09%(objectname)"])
                 .decode(errors="surrogateescape").splitlines())
    return GitIntegrityState(head_commit, current_ref, _node_state(index_path), refs)


def worktree_changes(snapshot: RepositorySnapshot) -> tuple[dict[str, FileState], list[str]]:
    """Return current Git-visible path states and exact changes from capture.

    Example: ``after, touched = worktree_changes(snapshot)`` after a process exits.
    """
    after = capture_worktree_paths(snapshot.repo_root, snapshot.temp_dir / "after-worktree")
    names = sorted(set(snapshot.paths) | set(after))
    return after, [name for name in names if snapshot.paths.get(name) != after.get(name)]


def git_integrity_changes(snapshot: RepositorySnapshot) -> list[str]:
    """Return changed Git-state labels without attempting automatic recovery.

    Example: ``changes = git_integrity_changes(snapshot)`` reports a moved ref.
    """
    after = capture_git_integrity(snapshot.repo_root)
    before = snapshot.git_integrity
    labels: list[str] = []
    if before.head_commit != after.head_commit:
        labels.append("HEAD")
    if before.current_ref != after.current_ref:
        labels.append("current ref")
    if before.index != after.index:
        labels.append("index")
    if before.refs != after.refs:
        labels.append("ref map")
    return labels


def runtime_node_changed(snapshot: RepositorySnapshot) -> bool:
    """Report replacement or mode changes to the frozen runtime-directory node.

    Example: ``if runtime_node_changed(snapshot): raise PermissionBreach(...)``.
    """
    if snapshot.runtime_prefix is None or snapshot.runtime_node is None:
        return False
    node = _path_at(snapshot.repo_root, snapshot.runtime_prefix)
    return _node_state(node, directories=True) != snapshot.runtime_node


def restore_worktree_path(snapshot: RepositorySnapshot, path: str) -> None:
    """Restore an ordinary worktree path or remove one introduced after capture.

    Example: ``restore_worktree_path(snapshot, "src/app.py")``.
    """
    _restore_path(snapshot.repo_root, path, snapshot.paths.get(path, FileState("missing")),
                  snapshot.worktree_backup_dir)


def cleanup_snapshot(snapshot: RepositorySnapshot) -> None:
    """Remove the private backup directory or report its retained location.

    Example: ``cleanup_snapshot(snapshot)`` after enforcement.
    """
    _remove_temp_dir(snapshot.temp_dir)


def _cleanup_failed_capture(temp_dir: Path, original: BaseException) -> None:
    try:
        _remove_temp_dir(temp_dir)
    except SnapshotCleanupError as cleanup:
        raise cleanup from original


def _remove_temp_dir(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as error:
        raise SnapshotCleanupError(path, error) from error


def _git(repo_root: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True)
    except OSError as error:
        raise RepositoryStateError(f"git {' '.join(args)!r} could not run in {repo_root!s}: {error}") from error
    if result.returncode == 0:
        return result.stdout
    stderr = result.stderr.decode(errors="replace").strip()
    raise RepositoryStateError(f"git {' '.join(args)!r} failed in {repo_root!s}: {stderr}")


def _symbolic_ref(repo_root: Path) -> str | None:
    result = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=repo_root,
                            capture_output=True)
    if result.returncode == 0:
        return result.stdout.decode(errors="surrogateescape").strip()
    if result.returncode == 1:
        return None
    stderr = result.stderr.decode(errors="replace").strip()
    raise RepositoryStateError(f"git 'symbolic-ref --quiet HEAD' failed in {repo_root!s}: {stderr}")


def _git_path(repo_root: Path, *args: str) -> Path:
    output = _git(repo_root, ["rev-parse", *args]).decode(errors="surrogateescape").strip()
    path = Path(output)
    return path if path.is_absolute() else repo_root / path


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _runtime_lexical_path(root: Path, session_dir: Path) -> Path:
    """Anchor relative runtime paths to the captured repository root.

    Example: ``_runtime_lexical_path(root, Path("adws/session"))``.
    """
    if session_dir.is_absolute():
        return _lexical_absolute(session_dir)
    return root.joinpath(session_dir)


def _reject_symlink_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise RepositoryStateError(f"runtime component {current!s} expected to exist") from error
        if stat.S_ISLNK(mode):
            raise RepositoryStateError(f"runtime component {current!s} must not be a symlink")


def _decode_git_paths(output: bytes) -> list[str]:
    names = [item.decode(errors="surrogateescape") for item in output.split(b"\0") if item]
    for name in names:
        _relative_parts(name)
    return names


def _capture_paths(root: Path, paths: list[str], backup_dir: Path) -> dict[str, FileState]:
    states: dict[str, FileState] = {}
    for index, path in enumerate(paths):
        states[path] = _capture_path(root, path, backup_dir, f"{index:08d}")
    return states


def _capture_path(root: Path, relative: str, backup_dir: Path, backup_name: str) -> FileState:
    target = _path_at(root, relative)
    state = _node_state(target)
    if state.kind == "missing":
        return state
    content = _node_content(target, state.kind)
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / backup_name).write_bytes(content)
    return FileState(state.kind, state.mode, state.digest, backup_name)


def _node_state(path: Path, directories: bool = False) -> FileState:
    try:
        node = path.lstat()
    except FileNotFoundError:
        return FileState("missing")
    mode = stat.S_IMODE(node.st_mode)
    if directories and stat.S_ISDIR(node.st_mode):
        return FileState("directory", mode)
    kind = _node_kind(path, node.st_mode)
    content = _node_content(path, kind)
    return FileState(kind, mode, _digest(content))


def _node_kind(path: Path, mode: int) -> PathKind:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    raise RepositoryStateError(f"path {path!s} expected regular file or symlink, found mode {mode:o}")


def _node_content(path: Path, kind: PathKind) -> bytes:
    if kind == "file":
        return path.read_bytes()
    if kind == "symlink":
        return os.fsencode(os.readlink(path))
    raise RepositoryStateError(f"path {path!s} expected a readable file or symlink, found {kind}")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _relative_parts(relative: str) -> tuple[str, ...]:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
        raise RepositoryStateError(f"repository path {relative!r} expected a relative non-parent path")
    return parts


def _path_at(root: Path, relative: str) -> Path:
    return root.joinpath(*_relative_parts(relative))


def _restore_path(root: Path, relative: str, state: FileState, backup_dir: Path) -> None:
    target = _path_at(root, relative)
    if state.kind == "missing":
        _remove_node(target)
        return
    _ensure_parent(target.parent)
    content = _backup_content(backup_dir, state, relative)
    _remove_node(target)
    _write_node(target, state, content)


def _ensure_parent(parent: Path) -> None:
    if parent.is_dir() and not parent.is_symlink():
        return
    if parent.exists() or parent.is_symlink():
        _remove_node(parent)
    parent.mkdir(parents=True, exist_ok=True)


def _backup_content(backup_dir: Path, state: FileState, relative: str) -> bytes:
    content = (backup_dir / state.backup_name).read_bytes()
    if _digest(content) != state.digest:
        raise RepositoryStateError(f"backup for {relative!r} did not match captured digest {state.digest}")
    return content


def _write_node(target: Path, state: FileState, content: bytes) -> None:
    if state.kind == "file":
        target.write_bytes(content)
        os.chmod(target, state.mode)
        return
    if state.kind == "symlink":
        os.symlink(os.fsdecode(content), target)
        return
    raise RepositoryStateError(f"path {target!s} expected restorable file or symlink, found {state.kind}")


def _remove_node(target: Path) -> None:
    try:
        node = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(node.st_mode):
        shutil.rmtree(target)
        return
    target.unlink()
