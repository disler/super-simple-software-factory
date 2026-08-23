"""Deterministic change capture: what was built, straight from git.

"What changed since main" is not a judgement call — it is two git commands and
a subtraction. So it is code, and an agent is only handed the result. The
capture writes the full diff into `context_handoff/` and returns a ChangeSet;
`as_envelope` adapts that into the one door every agent handoff uses.

The base is resolved, not assumed. Off the base branch the diff covers the
whole branch plus the working tree; on it, the uncommitted tree; and on a clean
tree, the last commit — because "document the work that was just done" still
has an answer right after a chain committed. Whichever it picked rides along in
`BaseRef.reason`, so the trace never leaves you guessing what a diff was
measured against.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import git_helper
from .data_types import BaseRef, ChangeCapture, ChangeSet, ChangesOutput
from .diagnostics import sanitize_text

DIFF_FILENAME = "changes.diff"


def resolve_base(ref: str) -> BaseRef:
    """Pick the commit the work is measured from, and record why that one."""
    if not git_helper.is_repo():
        raise RuntimeError(
            "not a git repository — change capture needs one. Run `git init` in "
            "the repo root before running an ADW that documents a change.")
    if not git_helper.ref_exists(ref):
        raise RuntimeError(
            f"base ref {ref!r} does not exist in this repository — pass --base "
            f"with a ref that does (e.g. --base master, --base HEAD~1).")

    # Built first, then given its reason: BaseRef.label knows how to print a
    # pinned sha, and the reason is the line a human reads in the trace.
    base = BaseRef(ref=ref, commit=git_helper.merge_base(ref, "HEAD"))
    if git_helper.short_sha(base.commit) != git_helper.short_sha("HEAD"):
        base.reason = (f"HEAD is ahead of {base.label} — diffing every commit since, "
                       f"plus the working tree")
    elif git_helper.is_dirty():
        base.reason = f"HEAD is on {base.label} — diffing the uncommitted working tree"
    elif git_helper.ref_exists("HEAD~1"):
        base.commit = git_helper.rev("HEAD~1")
        base.reason = (f"HEAD is on {base.label} with a clean tree — falling back to "
                       f"the last commit")
    else:
        base.reason = f"HEAD is on {base.label} with a clean tree and no parent commit"
    return base


def capture(run, params: ChangeCapture) -> ChangeSet:
    """Diff the working tree against the resolved base and persist the evidence."""
    base = resolve_base(params.base)
    files = git_helper.diff_files(base.commit)
    untracked = git_helper.untracked_files() if params.include_untracked else []
    insertions, deletions = git_helper.diff_counts(base.commit)
    stat = git_helper.diff_stat(base.commit)

    text = git_helper.diff_text(base.commit)
    lines = text.splitlines()
    truncated = len(lines) > params.max_diff_lines
    if truncated:
        text = "\n".join(lines[:params.max_diff_lines])
        text += (f"\n\n[truncated at {params.max_diff_lines} lines of "
                 f"{len(lines)} — run `git diff {base.commit}` for the rest]")

    # Untracked files are absent from `git diff` by construction, so they are
    # named here rather than silently missing from the record. The reader has
    # `read` and can open any of them.
    untracked_block = ("\n".join(f"  {f}" for f in untracked) if untracked
                       else "  (none)")
    diff_path = run.context_handoff_dir / DIFF_FILENAME
    _write_diff_artifact(diff_path,
        f"# changes since {base.label} @ {git_helper.short_sha(base.commit)}\n"
        f"# {base.reason}\n"
        f"# +{insertions} -{deletions} across {len(files)} tracked file(s)\n\n"
        f"## stat\n{stat or '  (no tracked changes)'}\n\n"
        f"## untracked files\n{untracked_block}\n\n"
        f"## diff\n{text}\n")

    return ChangeSet(base=base, files=files, untracked=untracked,
                     insertions=insertions, deletions=deletions, stat=stat,
                     diff_path=str(diff_path), truncated=truncated)


def _write_diff_artifact(path: Path, content: str) -> None:
    """Persist a restricted, redacted diff artifact for later inspection."""
    path.write_text(sanitize_text(content))
    os.chmod(path, 0o600)


def as_envelope(changes: ChangeSet, notes: str = "") -> ChangesOutput:
    """Wrap a captured change so an agent can be handed it directly."""
    total = len(changes.files) + len(changes.untracked)
    return ChangesOutput(
        status="success",
        summary=(f"{total} file(s) changed since {changes.base.label} "
                 f"(+{changes.insertions} -{changes.deletions})"),
        artifacts=[changes.diff_path],
        notes_for_next_agent=notes,
        base=f"{changes.base.label} @ {git_helper.short_sha(changes.base.commit)} "
             f"— {changes.base.reason}",
        changed_files=changes.files + changes.untracked,
        insertions=changes.insertions,
        deletions=changes.deletions,
        stat=changes.stat,
        diff_path=changes.diff_path,
    )
