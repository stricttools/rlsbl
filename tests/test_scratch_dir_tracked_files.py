"""Scaffold refuses a scratch directory that already holds tracked files.

``experiments/`` and ``screenshots/`` are scratch directories: scaffold writes
a ``.gitignore`` into each that ignores everything, so nothing in them can be
committed by accident. A project that already committed files there (a
``screenshots/`` of README images, say) would have them silently turned into
tracked files under an ignore-everything rule. Scaffold refuses instead,
listing the files and the remedies.
"""

import subprocess

import pytest

from rlsbl.commands.init_cmd import run_cmd
from rlsbl.context import create_context
from rlsbl.errors import ConfigError

FLAGS = {"auto-commit": False, "auto-tag": False, "skip-shared": False}


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit(repo, rel, text="x\n"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"add {rel}")


def _scaffold(repo):
    run_cmd("plain", [], dict(FLAGS), ctx=create_context(repo))


@pytest.fixture
def repo_with_tracked_screenshots(mock_git_repo):
    _commit(mock_git_repo, "screenshots/hero.png")
    _commit(mock_git_repo, "screenshots/sub/detail.png")
    return mock_git_repo


def test_tracked_files_in_a_scratch_directory_are_refused(repo_with_tracked_screenshots):
    repo = repo_with_tracked_screenshots
    with pytest.raises(ConfigError) as info:
        _scaffold(repo)
    message = str(info.value)
    assert "screenshots/hero.png" in message
    assert "screenshots/sub/detail.png" in message
    assert "assets/" in message
    assert "rename the directory" in message
    assert "`mkdir -p assets && git mv screenshots/hero.png assets/hero.png`" in message
    # Refused before anything was written.
    assert not (repo / "screenshots" / ".gitignore").exists()
    assert not (repo / "experiments").exists()


def test_moving_the_files_to_assets_clears_the_refusal(repo_with_tracked_screenshots):
    repo = repo_with_tracked_screenshots
    (repo / "assets").mkdir()
    _git(repo, "mv", "screenshots/hero.png", "assets/hero.png")
    _git(repo, "mv", "screenshots/sub", "assets/sub")
    _git(repo, "commit", "-q", "-m", "move images to assets")
    _scaffold(repo)
    assert (repo / "screenshots" / ".gitignore").is_file()


def test_renaming_the_directory_clears_the_refusal(repo_with_tracked_screenshots):
    repo = repo_with_tracked_screenshots
    _git(repo, "mv", "screenshots", "gallery")
    _git(repo, "commit", "-q", "-m", "rename")
    _scaffold(repo)
    assert (repo / "screenshots" / ".gitignore").is_file()


def test_the_files_scaffold_itself_commits_there_are_not_refused(mock_git_repo):
    _scaffold(mock_git_repo)
    _git(mock_git_repo, "add", "--", "experiments", "screenshots")
    _git(mock_git_repo, "commit", "-q", "-m", "scaffold")
    _scaffold(mock_git_repo)  # the committed scratch .gitignore files are fine
