"""Scaffold installs its git hooks where git runs them, or refuses.

``rlsbl scaffold`` installs the pre-push and post-rewrite hooks. It used to
write them into ``<git-dir>/hooks`` unconditionally and report "Installed":
with ``core.hooksPath`` pointing elsewhere git never runs them there, and in a
linked worktree ``<git-dir>`` is the worktree's own directory, which git
never reads hooks from either. Hooks now go where ``git rev-parse --git-path
hooks`` says git looks; a ``core.hooksPath`` naming any other directory is
refused, naming the setting and the file that holds it, before scaffold
writes anything.
"""

import json
import subprocess

import pytest

from rlsbl import app
from rlsbl.commands.init_cmd import (
    _install_or_update_pre_push_hook,
    refuse_unreachable_hooks_dir,
)
from rlsbl.errors import ConfigError
from rlsbl.hook_hashes import CURRENT_PRE_PUSH_HOOK


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _plain_project(repo):
    (repo / ".rlsbl").mkdir(exist_ok=True)
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": ["plain"], "publish_mode": "ci"})
    )
    (repo / "VERSION").write_text("0.1.0\n")


def test_a_hooks_path_elsewhere_is_refused_and_unsetting_it_clears(mock_git_repo):
    _git(mock_git_repo, "config", "core.hooksPath", ".githooks")
    with pytest.raises(ConfigError) as exc:
        refuse_unreachable_hooks_dir()
    message = str(exc.value)
    assert "core.hooksPath" in message and ".githooks" in message
    assert "git config --file .git/config --unset core.hooksPath" in message

    # The fix the refusal names, run as written.
    subprocess.run(
        "git config --file .git/config --unset core.hooksPath",
        shell=True, cwd=str(mock_git_repo), check=True,
    )
    refuse_unreachable_hooks_dir()


def test_scaffold_refuses_before_writing_anything(mock_git_repo, monkeypatch):
    _plain_project(mock_git_repo)
    _git(mock_git_repo, "config", "core.hooksPath", ".githooks")
    monkeypatch.chdir(mock_git_repo)
    result = app.test(["scaffold", "--no-auto-tag"])
    assert result.exit_code != 0
    assert "core.hooksPath" in result.stderr
    assert not (mock_git_repo / ".git" / "hooks" / "pre-push").exists()
    assert not (mock_git_repo / ".githooks").exists()
    assert not (mock_git_repo / ".rlsbl" / "version").exists()


def test_a_hooks_path_naming_the_default_directory_is_accepted(mock_git_repo, capsys):
    """Pointing core.hooksPath at the repository's own hooks directory is
    where git looks anyway: nothing to refuse."""
    hooks = mock_git_repo / ".git" / "hooks"
    _git(mock_git_repo, "config", "core.hooksPath", str(hooks))
    refuse_unreachable_hooks_dir()
    _install_or_update_pre_push_hook()
    assert (hooks / "pre-push").read_text() == CURRENT_PRE_PUSH_HOOK


def test_a_linked_worktree_installs_into_the_common_hooks(mock_git_repo, capsys):
    """git runs a linked worktree's hooks from the common directory."""
    linked = mock_git_repo.parent / "linked"
    _git(mock_git_repo, "worktree", "add", "-q", "--detach", str(linked))
    import os
    here = os.getcwd()
    os.chdir(linked)
    try:
        refuse_unreachable_hooks_dir()
        _install_or_update_pre_push_hook()
    finally:
        os.chdir(here)
    common = mock_git_repo / ".git" / "hooks" / "pre-push"
    assert common.read_text() == CURRENT_PRE_PUSH_HOOK
    worktree_dir = _git(linked, "rev-parse", "--absolute-git-dir")
    assert not (subprocess.run(
        ["test", "-e", f"{worktree_dir}/hooks/pre-push"]).returncode == 0)
    out = capsys.readouterr().out
    assert "Installed pre-push hook" in out
    assert str(common) in out
