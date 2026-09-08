from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from adw_modules import permissions
from adw_modules.data_types import (AgentConfig, PromptEngineering,
                                    SSSFConfig)


def _agent(name="tester", writes=None, system="prompts/system.md",
           user="prompts/user.md"):
    return AgentConfig(
        name=name,
        prompt_engineering=PromptEngineering(system=system, user=user),
        writes=writes,
    )


def _run(repo: Path, agent: AgentConfig, adw_id="run1", data_dir="runtime"):
    cfg = SSSFConfig(agents=[agent])
    cfg.defaults.data_dir = data_dir
    session_dir = repo / data_dir / "sessions" / adw_id
    return SimpleNamespace(repo_root=repo, session_dir=session_dir,
                           adw_id=adw_id, cfg=cfg)


def test_tracked_dirty_same_size_change_restores_pre_turn_bytes_and_mode(tracked_repo):
    path = tracked_repo / "README.md"
    path.write_bytes(b"operator-change\n")
    path.chmod(0o640)
    agent = _agent(writes=[])
    run = _run(tracked_repo, agent)
    before = permissions.snapshot(run)

    path.write_bytes(b"agent---changes\n")
    path.chmod(0o755)

    with pytest.raises(permissions.PermissionBreach, match="README.md.*restored"):
        permissions.enforce(run, None, agent, before)
    assert path.read_bytes() == b"operator-change\n"
    assert path.stat().st_mode & 0o777 == 0o640


def test_preexisting_untracked_change_is_restored_with_nul_safe_git_names(tracked_repo):
    path = tracked_repo / "notes\nfile.txt"
    path.write_bytes(b"before\x00bytes")
    agent = _agent(writes=[])
    run = _run(tracked_repo, agent)
    before = permissions.snapshot(run)

    path.write_bytes(b"after!\x00bytes")

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)
    assert path.read_bytes() == b"before\x00bytes"


def test_new_forbidden_file_and_symlink_are_removed_without_touching_target(tracked_repo, tmp_path):
    agent = _agent(writes=[])
    run = _run(tracked_repo, agent)
    before = permissions.snapshot(run)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    forbidden = tracked_repo / "forbidden.txt"
    link = tracked_repo / "outside-link"
    forbidden.write_text("remove")
    link.symlink_to(outside)

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)

    assert not forbidden.exists()
    assert not link.is_symlink()
    assert outside.read_text() == "keep"


def test_restore_refuses_to_recursively_delete_a_directory_collision(tracked_repo):
    path = tracked_repo / "operator-note"
    path.write_text("before")
    agent = _agent(writes=[])
    run = _run(tracked_repo, agent)
    before = permissions.snapshot(run)
    path.unlink()
    path.mkdir()
    child = path / "keep.txt"
    child.write_text("keep")

    with pytest.raises(permissions.PermissionBreach,
                       match="refusing recursive deletion"):
        permissions.enforce(run, None, agent, before)

    assert child.read_text() == "keep"


def test_all_active_prompts_override_unrestricted_and_markdown_glob_writes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    prompts = {
        name: (f"custom/prompts/{name}/system.md", f"custom/prompts/{name}/user.md")
        for name in ("builder", "scout", "documenter")
    }
    agents = [
        _agent("builder", None, *prompts["builder"]),
        _agent("scout", [], *prompts["scout"]),
        _agent("documenter", ["**/*.md", "*.md"], *prompts["documenter"]),
    ]
    cfg = SSSFConfig(agents=agents)
    cfg.defaults.data_dir = "custom/runtime"
    run = SimpleNamespace(
        repo_root=repo,
        session_dir=repo / "custom/runtime/sessions/current",
        adw_id="current",
        cfg=cfg,
    )

    for configured_agent in agents:
        system, user = prompts[configured_agent.name]
        assert not permissions.permitted(system, configured_agent, run)
        assert not permissions.permitted(user, configured_agent, run)

    agents[-1].writes.append(prompts["documenter"][0])
    assert permissions.permitted(prompts["documenter"][0], agents[-1], run)

    handoff = "custom/runtime/sessions/current/context_handoff/report.md"
    sibling = "custom/runtime/sessions/other/context_handoff/report.md"
    for configured_agent in agents:
        assert permissions.permitted(handoff, configured_agent, run)
        assert not permissions.permitted(sibling, configured_agent, run)


def test_active_prompt_is_snapshotted_even_when_gitignored(sssf_repo):
    prompt = sssf_repo / "ignored/system.md"
    prompt.parent.mkdir()
    prompt.write_text("before")
    with (sssf_repo / ".git/info/exclude").open("a") as stream:
        stream.write("/ignored/\n")
    agent = _agent(writes=None, system="ignored/system.md")
    run = _run(sssf_repo, agent)
    before = permissions.snapshot(run)

    prompt.write_text("after!")

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)
    assert prompt.read_text() == "before"


def test_prompt_changed_from_regular_file_to_symlink_is_restored(sssf_repo):
    prompt = sssf_repo / "prompts/system.md"
    prompt.parent.mkdir()
    prompt.write_text("instructions")
    target = sssf_repo / "active.md"
    target.write_text("tampered instructions")
    agent = _agent(writes=None)
    run = _run(sssf_repo, agent)
    before = permissions.snapshot(run)

    prompt.unlink()
    prompt.symlink_to(target)

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)
    assert not prompt.is_symlink()
    assert prompt.read_text() == "instructions"


@pytest.mark.skipif(not hasattr(os, "lchmod"), reason="OS has no symlink chmod")
def test_preexisting_symlink_target_and_mode_are_restored(tracked_repo):
    first = tracked_repo / "first-target"
    second = tracked_repo / "second-target"
    first.write_text("first")
    second.write_text("second")
    link = tracked_repo / "operator-link"
    link.symlink_to(first.name)
    os.lchmod(link, 0o700)
    agent = _agent(writes=[])
    run = _run(tracked_repo, agent)
    before = permissions.snapshot(run)

    link.unlink()
    link.symlink_to(second.name)
    os.lchmod(link, 0o755)

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, agent, before)
    assert os.readlink(link) == first.name
    assert link.lstat().st_mode & 0o777 == 0o700
