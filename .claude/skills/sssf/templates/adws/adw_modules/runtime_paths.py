"""Runtime filesystem locations resolved once for one ADW session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved runtime paths shared by tracing, session state, and permissions.

    Example: ``paths.session_dir`` is the fixed home for one ``adw_id``.
    """

    repo_root: Path
    data_dir: Path
    session_dir: Path
    events_path: Path


def resolve_runtime_paths(repo_root: Path, configured_dir: str, adw_id: str) -> RuntimePaths:
    """Resolve one configured runtime root against the repository when relative.

    Example: ``resolve_runtime_paths(root, "adws/adw_data", "a1b2c3d4")``.
    """
    configured_path = Path(configured_dir)
    data_dir = configured_path if configured_path.is_absolute() else repo_root / configured_path
    session_dir = data_dir / "sessions" / adw_id
    return RuntimePaths(repo_root, data_dir, session_dir, session_dir / "events.jsonl")
