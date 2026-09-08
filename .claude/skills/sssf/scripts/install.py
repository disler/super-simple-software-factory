#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""Install the SSSF factory and its selected host integration into the cwd.

Usage:
    uv run <skill>/scripts/install.py [--integration claude|codex|kiro|none] [--force]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(starter agents), adws/adw_sssf_config/sssf.config.yaml, .env.sample,
.gitignore entries, and the selected host skill.
Existing files are skipped unless --force; known safe path migrations are applied.
"""

import argparse
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_ROOT / "templates"

INTEGRATION_PATHS = {
    "claude": Path(".claude/skills/sssf"),
    "codex": Path(".agents/skills/sssf"),
    # Kiro CLI's default agent carries `skill://.kiro/skills/*/SKILL.md`, so a
    # skill one level down from `.kiro/skills/` is discovered with no agent
    # config, exposed as /sssf exactly like Claude Code's.
    "kiro": Path(".kiro/skills/sssf"),
}

COMPANION_SKILLS = ("sssf-grill-me", "sssf-pick-models")

IGNORED_SOURCE_NAMES = {
    "__pycache__",
    "node_modules",
    "dist",
    ".ruff_cache",
    ".DS_Store",
}

LEGACY_VISUALIZER_COMMAND = "cd .claude/skills/sssf/apps/visualizer"
PORTABLE_VISUALIZER_COMMAND = (
    # The justfile is committed but the integration directory is a per-clone
    # choice, so the recipe probes every known host instead of hardcoding the
    # one selected at install time. A new host must be added here too —
    # test_visualizer_recipe_probes_every_integration holds this to
    # INTEGRATION_PATHS.
    'skill_dir=".agents/skills/sssf"; '
    '[ -d "$skill_dir" ] || skill_dir=".claude/skills/sssf"; '
    '[ -d "$skill_dir" ] || skill_dir=".kiro/skills/sssf"; '
    '[ -d "$skill_dir" ] || { echo "SSSF integration not installed" >&2; exit 1; }; '
    'cd "$skill_dir/apps/visualizer"'
)
LEGACY_SSSF_RECIPE = (
    'simple-sdlc *ARGS:\n    uv run adws/adw_simple_sdlc.py --config {{config}} "$@"'
)
SSSF_RECIPE = 'sssf *ARGS:\n    uv run adws/adw_simple_sdlc.py --config {{config}} "$@"'
LEGACY_SSSF_EXAMPLE = (
    '# the full chain, plus review and docs: just simple-sdlc "add a /health endpoint"'
)
SSSF_EXAMPLE = (
    '# the full chain, plus review and docs: just sssf "<prompt or path/to/spec.md>"'
)

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
    # Any untracked dependency tree in the repo reads as an agent write, because
    # permissions.snapshot() lists untracked files. The visualizer carries its
    # own .gitignore for the copy stamped under the integration directory; this
    # entry covers the host repo's own node_modules, which is the same breach
    # vector whenever the repo SSSF is stamped into is a JS project.
    "node_modules/",
]


def stamp(
    src: Path,
    dest: Path,
    force: bool,
    stamped: list[str],
    skipped: list[str],
) -> None:
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name in IGNORED_SOURCE_NAMES:
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
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# sssf runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f"{gitignore} (+{len(missing)} entries)")


def migrate_legacy_justfile(root: Path, stamped: list[str]) -> None:
    """Apply narrowly scoped migrations to original generated recipes."""
    justfile = root / "justfile"
    if not justfile.exists():
        return

    current = justfile.read_text()
    updated = current.replace(
        LEGACY_VISUALIZER_COMMAND,
        PORTABLE_VISUALIZER_COMMAND,
    )
    updated = updated.replace(LEGACY_SSSF_RECIPE, SSSF_RECIPE)
    updated = updated.replace(LEGACY_SSSF_EXAMPLE, SSSF_EXAMPLE)
    if updated == current:
        return

    justfile.write_text(updated)
    stamped.append(f"{justfile} (updated generated recipes)")


def default_integration(root: Path) -> str:
    """Preserve the integration from which an in-repo skill was invoked."""
    for integration, relative_path in INTEGRATION_PATHS.items():
        if SKILL_ROOT == (root / relative_path).resolve():
            return integration
    return "claude"


def install_integration(
    root: Path,
    integration: str,
    force: bool,
    stamped: list[str],
    skipped: list[str],
) -> None:
    if integration == "none":
        return

    destination = (root / INTEGRATION_PATHS[integration]).resolve()
    if SKILL_ROOT != destination:
        stamp(SKILL_ROOT, destination, force, stamped, skipped)

    integration_root = destination.parent
    for skill_name in COMPANION_SKILLS:
        source = SKILL_ROOT.parent / skill_name
        companion_destination = integration_root / skill_name
        if source.resolve() != companion_destination.resolve():
            stamp(source, companion_destination, force, stamped, skipped)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--integration",
        choices=(*INTEGRATION_PATHS, "none"),
        help=(
            "host integration to install; defaults to the current in-repo "
            "integration, or claude when run externally"
        ),
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args()

    root = Path.cwd()
    integration = args.integration or default_integration(root)
    stamped: list[str] = []
    skipped: list[str] = []

    stamp(TEMPLATES / "adws", root / "adws", args.force, stamped, skipped)
    stamp(
        TEMPLATES / "prompt_engineering",
        root / "adws" / "adw_data" / "prompt_engineering",
        args.force,
        stamped,
        skipped,
    )
    stamp(
        TEMPLATES / "harness_engineering",
        root / "adws" / "adw_data" / "harness_engineering",
        args.force,
        stamped,
        skipped,
    )
    stamp(
        TEMPLATES / "sssf.config.yaml",
        root / "adws" / "adw_sssf_config" / "sssf.config.yaml",
        args.force,
        stamped,
        skipped,
    )
    stamp(TEMPLATES / "env.sample", root / ".env.sample", args.force, stamped, skipped)
    # The recipes are part of the operating experience, and several cookbooks
    # plus the run banner tell you to use them, so a stamped repo has to have
    # them. Skipped like any other file if the repo already has a justfile.
    stamp(TEMPLATES / "justfile", root / "justfile", args.force, stamped, skipped)
    migrate_legacy_justfile(root, stamped)
    ensure_gitignore(root, stamped)
    install_integration(root, integration, args.force, stamped, skipped)

    print(f"sssf installed into {root}")
    print(f"  integration: {integration}")
    print(f"  stamped: {len(stamped)} file(s)")
    for s in stamped:
        print(f"    + {s}")
    if skipped:
        print(f"  skipped (already exist, use --force to overwrite): {len(skipped)}")
    print("\nnext steps:")
    print("  1. cp .env.sample .env   # then set the key(s) your roster needs")
    # Any chain ending in a commit phase runs `git add -A`, so whatever this
    # install left untracked would land in the builder's commit under the
    # builder's message. Committing here keeps the factory out of it.
    print("  2. git add -A && git commit -m 'stamp sssf'   # BEFORE the first chain")
    print("  3. just demo             # two cheap read-only runs, end to end")
    print("  4. just sessions         # what just happened")
    print("  5. just obs              # the trace UI, needs bun")
    print("\n  no just? the raw form of step 3 is:")
    print('     uv run adws/adw_prompt.py "say hello" --agent scout')
    return 0


if __name__ == "__main__":
    sys.exit(main())
