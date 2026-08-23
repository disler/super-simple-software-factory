#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""/install — stamp the SSSF factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--force]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(4 starter agents), adws/adw_sssf_config/sssf.config.yaml, .env.sample,
.gitignore entries.
Existing files are skipped unless --force.
"""

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@dataclass(frozen=True)
class CliStatus:
    """Describe whether a command-line harness is available.

    Example: ``detect_cli("pi").available`` is ``True`` when Pi responds.
    """

    name: str
    executable: str
    available: bool
    version: str = ""


CLI_COMMANDS: dict[str, str] = {
    "pi": os.environ.get("PI_PATH", "pi"),
    "claude_code": os.environ.get("CLAUDE_PATH", "claude"),
    "codex": os.environ.get("CODEX_PATH", "codex"),
}

GITIGNORE_ENTRIES = [
    "adws/adw_data/sessions/",
    "adws/adw_data/sssf.db*",
    ".env",
    # The ADWs are Python, so importing adw_modules writes bytecode next to it.
    # Chains that end in a commit phase call `git add -A`, so without this a
    # stamped repo commits its own .pyc files — 15 of them showed up in the
    # first repo that was ever installed into from scratch.
    "__pycache__/",
    "*.pyc",
]


def detect_cli(command: str) -> CliStatus:
    """Run the non-authenticating version probe for one configured CLI.

    Example: ``detect_cli("codex")`` runs only ``codex --version``.
    """
    name = next(
        (key for key, executable in CLI_COMMANDS.items() if executable == command), command,
    )
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return CliStatus(name=name, executable=command, available=False)
    version_lines = (result.stdout or result.stderr).strip().splitlines()
    return CliStatus(name=name, executable=command, available=result.returncode == 0,
                     version=version_lines[0] if result.returncode == 0 and version_lines else "")


def stamp(src: Path, dest: Path, force: bool, stamped: list[str], skipped: list[str]) -> None:
    """Copy one template path while preserving existing destination files.

    Example: ``stamp(source, target, False, stamped, skipped)`` leaves a target intact.
    """
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name == "__pycache__":
                continue
            stamp(child, dest / child.name, force, stamped, skipped)
        return
    if dest.exists() and not force:
        skipped.append(str(dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    stamped.append(str(dest))


def ensure_gitignore(root: Path, stamped: list[str]) -> None:
    """Append only missing runtime paths to the target repository ignore file.

    Example: ``ensure_gitignore(Path.cwd(), stamped)`` records a changed file once.
    """
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# sssf runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f"{gitignore} (+{len(missing)} entries)")


def print_cli_diagnostics() -> None:
    """Print one availability line for each configured harness executable.

    Example: ``print_cli_diagnostics()`` warns when ``pi`` is absent.
    """
    print("\nCLI diagnostics:")
    for command in CLI_COMMANDS.values():
        status = detect_cli(command)
        if status.available:
            version = f"; {status.version}" if status.version else ""
            print(f"  {status.name}: available ({status.executable}{version})")
            continue
        print(f"  WARNING: {status.name}: unavailable ({status.executable} --version)")


def print_install_report(root: Path, stamped: list[str], skipped: list[str]) -> None:
    """Print the files changed and preserved by one installation.

    Example: ``print_install_report(root, [], [])`` reports a zero-file install.
    """
    print(f"sssf installed into {root}")
    print(f"  stamped: {len(stamped)} file(s)")
    for target in stamped:
        print(f"    + {target}")
    if skipped:
        print(f"  skipped (already exist, use --force to overwrite): {len(skipped)}")


def print_next_steps() -> None:
    """Show the next commands after the factory is stamped.

    Example: ``print_next_steps()`` prints the demo command for a new repository.
    """
    print("\nnext steps:")
    print("  1. cp .env.sample .env   # then set the key(s) your roster needs")
    print("  2. just demo             # two cheap read-only runs, end to end")
    print("  3. just sessions         # what just happened")
    print("  4. just obs              # the trace UI, needs bun")
    print("\n  no just? the raw form of step 2 is:")
    print("     uv run adws/adw_prompt.py \"say hello\" --agent scout")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args()

    root = Path.cwd()
    stamped, skipped = [], []

    stamp(TEMPLATES / "adws", root / "adws", args.force, stamped, skipped)
    stamp(TEMPLATES / "prompt_engineering",
          root / "adws" / "adw_data" / "prompt_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "harness_engineering",
          root / "adws" / "adw_data" / "harness_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "sssf.config.yaml",
          root / "adws" / "adw_sssf_config" / "sssf.config.yaml",
          args.force, stamped, skipped)
    stamp(TEMPLATES / "env.sample", root / ".env.sample", args.force, stamped, skipped)
    # The recipes are part of the operating experience, and several cookbooks
    # plus the run banner tell you to use them, so a stamped repo has to have
    # them. Skipped like any other file if the repo already has a justfile.
    stamp(TEMPLATES / "justfile", root / "justfile", args.force, stamped, skipped)
    ensure_gitignore(root, stamped)
    print_cli_diagnostics()

    print_install_report(root, stamped, skipped)
    print_next_steps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
