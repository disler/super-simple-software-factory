"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from . import git_helper
from .data_types import EnvelopeBase, GateReport

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run,
                        ignore: tuple[str, ...] = ()) -> GateReport:
    """The claimed change set and the actual change set must be the same set.

    Two directions, because either mismatch is a real defect: a claim that did
    not happen (a file listed as changed but never touched), and a change nobody
    claimed (the second is what rides into `git_helper.commit_all()`, which
    stages the whole working tree). Existence alone is not enough — a builder
    that lists an untouched file, or hides an unexpected one, goes green.
    """
    report = GateReport()
    claimed = {f.lstrip("./") for f in getattr(envelope, "changed_files", [])}

    if not git_helper.is_repo():
        # No git repo: there is no diff to reconcile against, so fall back to
        # the existence check — the only mechanical fact available here.
        for f in sorted(claimed):
            p = Path(f)
            report.check(f, p.exists(),
                         f"exists, {_size(p)}" if p.exists()
                         else "claimed changed file does not exist")
        return report

    actual = {f.lstrip("./") for f in git_helper.changed_files()}

    for f in sorted(claimed):
        report.check(f, f in actual,
                     "claimed and changed" if f in actual
                     else "claimed but not in the diff")

    unclaimed = sorted(f for f in actual - claimed
                       if not any(fnmatch(f, pattern) for pattern in ignore))
    for f in unclaimed[:20]:
        report.check(f, False, "changed but not claimed")
    if len(unclaimed) > 20:
        report.check(f"…{len(unclaimed) - 20} more", False,
                     f"{len(unclaimed)} unclaimed changed files in total")
    if not unclaimed:
        report.check("diff ⊆ claims", True,
                     f"no unclaimed changes among {len(actual)} changed file(s)")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
