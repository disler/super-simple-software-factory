import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.py"


def load_installer():
    """The installer as a module. It is a script, not a package, and guards
    main() behind __name__, so importing it only defines its constants."""
    path = REPO_ROOT / ".claude/skills/sssf/scripts/install.py"
    spec = importlib.util.spec_from_file_location("sssf_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("integration", "skill_path"),
    [
        ("claude", Path(".claude/skills/sssf")),
        ("codex", Path(".agents/skills/sssf")),
        ("kiro", Path(".kiro/skills/sssf")),
    ],
)
def test_installs_selected_integration(tmp_path, integration, skill_path):
    result = subprocess.run(
        [sys.executable, str(INSTALLER), "--integration", integration],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"integration: {integration}" in result.stdout
    assert (tmp_path / skill_path / "SKILL.md").is_file()
    # Driven off the installer's own tuple: a companion skill that is added
    # there but not stamped beside the factory would otherwise ship invisible.
    for companion in load_installer().COMPANION_SKILLS:
        source = REPO_ROOT / ".claude/skills" / companion / "SKILL.md"
        installed = tmp_path / skill_path.parent / companion / "SKILL.md"
        assert installed.read_text() == source.read_text(), companion
    assert not (tmp_path / skill_path / "apps/visualizer/node_modules").exists()
    assert not (tmp_path / skill_path / "apps/visualizer/dist").exists()
    assert (tmp_path / "adws/adw_modules/harnesses.py").is_file()
    assert (tmp_path / "adws/adw_sssf_config/sssf.config.yaml").is_file()
    installed_justfile = (tmp_path / "justfile").read_text()
    assert "sssf *ARGS:" in installed_justfile
    assert "simple-sdlc *ARGS:" not in installed_justfile


@pytest.mark.parametrize(
    ("integration", "skill_path"),
    [
        ("claude", Path(".claude/skills/sssf")),
        ("codex", Path(".agents/skills/sssf")),
        ("kiro", Path(".kiro/skills/sssf")),
    ],
)
def test_visualizer_artifacts_stay_out_of_the_permission_snapshot(
    tmp_path, integration, skill_path
):
    """`just obs` runs `bun install` inside the stamped visualizer, and
    permissions.snapshot() fingerprints untracked files with exactly the
    `git ls-files` call below. Unignored, those ~5.7k paths appear after a
    phase's `before` snapshot and enforce() attributes them to the running
    agent: a planner limited to `specs/` failed on 5671 breaching paths and the
    rollback unlinked every dependency file. Asserting on the real git output
    rather than on GITIGNORE_ENTRIES is deliberate — a correct constant with an
    unanchored or misplaced pattern still leaves the breach in place."""
    subprocess.run(
        [sys.executable, str(INSTALLER), "--integration", integration],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    visualizer = tmp_path / skill_path / "apps/visualizer"
    for artifact in ("node_modules/vite/package.json", "dist/assets/index.js"):
        path = visualizer / artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    # The host repo's own dependencies are the same breach vector whenever SSSF
    # is stamped into a JS project, and no per-app .gitignore covers them.
    host_dependency = tmp_path / "node_modules/left-pad/index.js"
    host_dependency.parent.mkdir(parents=True, exist_ok=True)
    host_dependency.write_text("module.exports = 0;")

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    assert [p for p in untracked if "node_modules" in p or "/dist/" in p] == []


def test_every_companion_skill_on_disk_is_registered_for_install():
    """The installer stamps `sssf` plus a fixed tuple, so a companion skill
    added to the source tree and not to that tuple exists in the repo and ships
    to nobody. Driving the install assertions off COMPANION_SKILLS cannot catch
    that direction — this compares the tuple against what is actually there."""
    installer = load_installer()
    on_disk = {
        path.parent.name
        for path in (REPO_ROOT / ".claude/skills").glob("*/SKILL.md")
        if path.parent.name != "sssf"
    }
    assert on_disk == set(installer.COMPANION_SKILLS)


def test_none_installs_factory_without_host_integration(tmp_path):
    result = subprocess.run(
        [sys.executable, str(INSTALLER), "--integration", "none"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "integration: none" in result.stdout
    assert (tmp_path / "adws/adw_modules/harnesses.py").is_file()
    for host_dir in (".claude", ".agents", ".kiro"):
        assert not (tmp_path / host_dir).exists()


def test_visualizer_recipe_probes_every_integration():
    """The obs recipe is committed, but which host stamped a given clone is
    not, so it probes for each. The probe chain lives in two places — the
    template a fresh install stamps, and the constant an existing justfile is
    migrated to — and a new INTEGRATION_PATHS entry missing from either yields
    a justfile that cannot find the app."""
    installer = load_installer()
    template = (REPO_ROOT / ".claude/skills/sssf/templates/justfile").read_text()
    for path in installer.INTEGRATION_PATHS.values():
        assert f'skill_dir="{path}"' in installer.PORTABLE_VISUALIZER_COMMAND
        assert f'skill_dir="{path}"' in template


def test_migrates_legacy_visualizer_path_without_replacing_justfile(tmp_path):
    justfile = tmp_path / "justfile"
    justfile.write_text(
        "custom-recipe:\n"
        "    echo keep-me\n"
        "obs:\n"
        "    cd .claude/skills/sssf/apps/visualizer && bun install\n"
    )

    subprocess.run(
        [sys.executable, str(INSTALLER), "--integration", "codex"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    installed = justfile.read_text()
    assert "echo keep-me" in installed
    assert 'skill_dir=".agents/skills/sssf"' in installed
    assert "cd .claude/skills/sssf/apps/visualizer" not in installed


def test_migrates_legacy_sssf_recipe_without_replacing_justfile(tmp_path):
    justfile = tmp_path / "justfile"
    justfile.write_text(
        "custom-recipe:\n"
        "    echo keep-me\n"
        "# the full chain, plus review and docs: "
        'just simple-sdlc "add a /health endpoint"\n'
        "simple-sdlc *ARGS:\n"
        '    uv run adws/adw_simple_sdlc.py --config {{config}} "$@"\n'
    )

    subprocess.run(
        [sys.executable, str(INSTALLER), "--integration", "none"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    installed = justfile.read_text()
    assert "echo keep-me" in installed
    assert 'just sssf "<prompt or path/to/spec.md>"' in installed
    assert "sssf *ARGS:" in installed
    assert "simple-sdlc" not in installed
