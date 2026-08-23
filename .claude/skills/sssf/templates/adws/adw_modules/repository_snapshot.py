"""Recoverable working-tree and Git metadata snapshots for permission checks.

The snapshot intentionally excludes ``objects/`` and ``logs/``. A temporary
commit can leave unreachable objects and reflog history, but restoring every
ref, HEAD, index, and other metadata makes neither affect the checkout state.
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
    """A repository cannot be snapshotted or restored safely.

    Example: ``raise RepositoryStateError("git HEAD is not a commit")``.
    """


@dataclass(frozen=True)
class FileState:
    """The exact content identity and recoverable backup of one filesystem node.

    Example: ``FileState("missing")`` represents a path introduced after capture.
    """

    kind: PathKind
    mode: int = 0
    digest: str = ""
    backup_name: str = field(default="", compare=False)


@dataclass(frozen=True)
class GitMetadataState:
    """Recoverable metadata for one Git directory.

    Example: ``state = capture_git_metadata(repo, backup_dir)``.
    """

    root: Path
    paths: dict[str, FileState]


@dataclass(frozen=True)
class RepositorySnapshot:
    """Pre-call state used to restore unauthorised repository mutations.

    Example: ``snapshot = capture_repository(Path("."))``.
    """

    repo_root: Path
    temp_dir: Path
    worktree_backup_dir: Path
    paths: dict[str, FileState]
    metadata: tuple[GitMetadataState, ...]


def capture_repository(repo_root: Path) -> RepositorySnapshot:
    """Capture all non-ignored paths and mutable Git state outside ``repo_root``.

    Example: ``snapshot = capture_repository(run.repo_root)`` before a backend spawn.
    """
    root, git_dirs = validate_repository(repo_root)
    temp_dir = Path(tempfile.mkdtemp(prefix="sssf-repository-snapshot-"))
    try:
        worktree_backup_dir = temp_dir / "worktree"
        paths = capture_worktree_paths(root, worktree_backup_dir)
        metadata = tuple(capture_git_metadata(directory, temp_dir / f"git-{index}")
                         for index, directory in enumerate(git_dirs))
        return RepositorySnapshot(root, temp_dir, worktree_backup_dir, paths, metadata)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def validate_repository(repo_root: Path) -> tuple[Path, tuple[Path, ...]]:
    """Require ``repo_root`` to be a worktree root with a resolvable HEAD commit.

    Example: ``root, git_dirs = validate_repository(run.repo_root)``.
    """
    root = repo_root.resolve()
    actual = Path(_git(root, ["rev-parse", "--show-toplevel"]).decode().strip()).resolve()
    inside = _git(root, ["rev-parse", "--is-inside-work-tree"]).decode().strip()
    _git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    if root != actual or inside != "true":
        raise RepositoryStateError(f"repository root {root!s} expected usable worktree root {actual!s}")
    git_dir = _git_path(root, "--git-dir")
    common_dir = _git_path(root, "--git-common-dir")
    return root, tuple(dict.fromkeys((git_dir, common_dir)))


def capture_worktree_paths(repo_root: Path, backup_dir: Path) -> dict[str, FileState]:
    """Capture every tracked or non-ignored untracked path by byte content.

    Example: ``paths = capture_worktree_paths(repo, backup_dir)``.
    """
    names = _git(repo_root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    paths = _decode_git_paths(names)
    return _capture_paths(repo_root, paths, backup_dir)


def capture_git_metadata(git_dir: Path, backup_dir: Path) -> GitMetadataState:
    """Capture mutable Git metadata while excluding non-state object and log stores.

    Example: ``metadata = capture_git_metadata(git_dir, backup_dir)``.
    """
    if not git_dir.is_dir():
        raise RepositoryStateError(f"Git directory {git_dir!s} expected an existing directory")
    names = _metadata_paths(git_dir)
    return GitMetadataState(git_dir, _capture_paths(git_dir, names, backup_dir, directories=True))


def worktree_changes(snapshot: RepositorySnapshot) -> tuple[dict[str, FileState], list[str]]:
    """Return current states and paths whose exact state changed since capture.

    Example: ``after, touched = worktree_changes(snapshot)`` after a backend exits.
    """
    after = capture_worktree_paths(snapshot.repo_root, snapshot.temp_dir / "after-worktree")
    touched = sorted(set(snapshot.paths) | set(after))
    return after, [path for path in touched if snapshot.paths.get(path) != after.get(path)]


def metadata_changed(snapshot: RepositorySnapshot) -> bool:
    """Report whether any mutable Git metadata differs from its initial state.

    Example: ``if metadata_changed(snapshot): restore_git_metadata(snapshot)``.
    """
    for index, expected in enumerate(snapshot.metadata):
        current = capture_git_metadata(expected.root, snapshot.temp_dir / f"after-git-{index}")
        if current.paths != expected.paths:
            return True
    return False


def restore_worktree_path(snapshot: RepositorySnapshot, path: str) -> None:
    """Restore one path to its exact pre-call content, mode, and link state.

    Example: ``restore_worktree_path(snapshot, "src/app.py")``.
    """
    expected = snapshot.paths.get(path, FileState("missing"))
    _restore_path(snapshot.repo_root, path, expected, snapshot.worktree_backup_dir)


def preserve_worktree_path(snapshot: RepositorySnapshot, path: str,
                           state: FileState) -> FileState:
    """Copy a permitted post-call path so metadata recovery cannot discard it.

    Example: ``saved = preserve_worktree_path(snapshot, path, after[path])``.
    """
    return _capture_path(snapshot.repo_root, path, snapshot.temp_dir / "permitted", state.backup_name)


def restore_preserved_path(snapshot: RepositorySnapshot, path: str,
                           state: FileState) -> None:
    """Reapply a permitted post-call path after Git metadata has been restored.

    Example: ``restore_preserved_path(snapshot, path, saved)``.
    """
    _restore_path(snapshot.repo_root, path, state, snapshot.temp_dir / "permitted")


def restore_git_metadata(snapshot: RepositorySnapshot) -> None:
    """Restore all captured Git metadata without checkout or reset commands.

    Example: ``restore_git_metadata(snapshot)`` after a detected ref/index mutation.
    """
    for index, expected in enumerate(snapshot.metadata):
        _restore_metadata_tree(expected, snapshot.temp_dir / f"git-{index}")
    if metadata_changed(snapshot):
        raise RepositoryStateError("Git metadata restoration did not reproduce the captured state")


def cleanup_snapshot(snapshot: RepositorySnapshot) -> None:
    """Remove temporary snapshot contents after enforcement completes.

    Example: ``cleanup_snapshot(snapshot)`` in an enforcement ``finally`` block.
    """
    shutil.rmtree(snapshot.temp_dir, ignore_errors=True)


def _git(repo_root: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True)
    except OSError as error:
        raise RepositoryStateError(f"git {' '.join(args)!r} could not run in {repo_root!s}: {error}") from error
    if result.returncode == 0:
        return result.stdout
    stderr = result.stderr.decode(errors="replace").strip()
    raise RepositoryStateError(f"git {' '.join(args)!r} failed in {repo_root!s}: {stderr}")


def _git_path(repo_root: Path, option: str) -> Path:
    output = _git(repo_root, ["rev-parse", option]).decode(errors="surrogateescape").strip()
    path = Path(output)
    return path if path.is_absolute() else (repo_root / path).resolve()


def _decode_git_paths(output: bytes) -> list[str]:
    names = [name.decode(errors="surrogateescape") for name in output.split(b"\0") if name]
    for name in names:
        _relative_parts(name)
    return names


def _metadata_paths(git_dir: Path) -> list[str]:
    paths: list[str] = []
    for current, directories, files in os.walk(git_dir, followlinks=False):
        relative = Path(current).relative_to(git_dir).as_posix()
        _exclude_metadata_directories(relative, directories)
        paths.extend(_relative_name(relative, name) for name in directories + files)
    return sorted(paths)


def _exclude_metadata_directories(relative: str, directories: list[str]) -> None:
    if relative not in (".", ""):
        return
    directories[:] = [name for name in directories if name not in {"objects", "logs"}]


def _relative_name(parent: str, name: str) -> str:
    """Build one portable relative path from an os.walk parent and entry name."""
    relative = name if parent in (".", "") else f"{parent}/{name}"
    return relative


def _capture_paths(root: Path, paths: list[str], backup_dir: Path,
                   directories: bool = False) -> dict[str, FileState]:
    states: dict[str, FileState] = {}
    for index, path in enumerate(paths):
        states[path] = _capture_path(root, path, backup_dir, f"{index:08d}", directories)
    return states


def _capture_path(root: Path, relative: str, backup_dir: Path, backup_name: str,
                  directories: bool = False) -> FileState:
    target = _path_at(root, relative)
    try:
        node = target.lstat()
    except FileNotFoundError:
        return FileState("missing")
    if directories and stat.S_ISDIR(node.st_mode):
        return FileState("directory", stat.S_IMODE(node.st_mode))
    content, kind = _node_content(target, node.st_mode)
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / backup_name).write_bytes(content)
    return FileState(kind, stat.S_IMODE(node.st_mode), _digest(content), backup_name)


def _node_content(path: Path, mode: int) -> tuple[bytes, PathKind]:
    if stat.S_ISREG(mode):
        return path.read_bytes(), "file"
    if stat.S_ISLNK(mode):
        return os.fsencode(os.readlink(path)), "symlink"
    raise RepositoryStateError(f"path {path!s} expected regular file or symlink, found mode {mode:o}")


def _digest(content: bytes) -> str:
    """Return the stable content digest used for exact snapshot comparison."""
    digest = hashlib.sha256(content).hexdigest()
    return digest


def _relative_parts(relative: str) -> tuple[str, ...]:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
        raise RepositoryStateError(f"repository path {relative!r} expected a relative non-parent path")
    return parts


def _path_at(root: Path, relative: str) -> Path:
    """Resolve a previously validated repository-relative path below ``root``."""
    path = root.joinpath(*_relative_parts(relative))
    return path


def _restore_metadata_tree(expected: GitMetadataState, backup_dir: Path) -> None:
    current_names = set(_metadata_paths(expected.root))
    expected_names = set(expected.paths)
    _remove_extra_metadata(expected.root, current_names - expected_names)
    _restore_metadata_nodes(expected.root, expected.paths, backup_dir)


def _remove_extra_metadata(root: Path, paths: set[str]) -> None:
    """Remove metadata nodes introduced after capture from deepest to shallowest."""
    for relative in sorted(paths, key=lambda value: value.count("/"), reverse=True):
        _remove_node(_path_at(root, relative))


def _restore_metadata_nodes(root: Path, states: dict[str, FileState], backup_dir: Path) -> None:
    directories = sorted((path for path, state in states.items() if state.kind == "directory"),
                         key=lambda value: value.count("/"))
    for relative in directories:
        _restore_directory(_path_at(root, relative), states[relative].mode)
    for relative, state in states.items():
        if state.kind != "directory":
            _restore_path(root, relative, state, backup_dir)


def _restore_directory(target: Path, mode: int) -> None:
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        _remove_node(target)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, mode)


def _restore_path(root: Path, relative: str, state: FileState, backup_dir: Path) -> None:
    target = _path_at(root, relative)
    if state.kind == "missing":
        _remove_node(target)
        return
    _ensure_parent(target.parent)
    if state.kind == "directory":
        _restore_directory(target, state.mode)
        return
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
    os.symlink(os.fsdecode(content), target)


def _remove_node(target: Path) -> None:
    try:
        node = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(node.st_mode):
        shutil.rmtree(target)
        return
    target.unlink()
