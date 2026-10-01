"""A release's private directory for binaries: ``RLSBL_RELEASE_BIN``.

A project whose release must run its own unreleased build of a producer
(selfdoc releasing itself runs the selfdoc it is about to ship) builds it into
``$RLSBL_RELEASE_BIN`` from a hook. That directory is first on PATH for every
step the release runs, so rlsbl's own producer invocations find that build
instead of the one installed for every session on the machine.
"""

import os
from pathlib import Path

import pytest

from githarness import git, init_repo
from rlsbl import release_checkout
from rlsbl.commands.release.hooks import build_hook_env, run_release_hook
from rlsbl.commands.release.validate import _run_selfdoc_gen


@pytest.fixture(autouse=True)
def _mock_saferm():
    """Run selfdoc for real: these tests are about which selfdoc runs, and the
    suite-wide fixture of this name turns every selfdoc call into a no-op."""
    yield


def _fake_selfdoc(directory, marker, word):
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / "selfdoc"
    tool.write_text(f'#!/bin/sh\necho {word} > "{marker}"\n')
    tool.chmod(0o755)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    (repo / "selfdoc.json").write_text("{}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def _enter(repo):
    head = git(repo, "rev-parse", "refs/heads/main")
    return release_checkout.entered(str(repo), branch="main", sha=head, cwd=str(repo))


def test_a_hook_built_producer_runs_instead_of_the_installed_one(tmp_path, monkeypatch):
    marker = tmp_path / "which-selfdoc"
    outer = tmp_path / "outer-bin"
    _fake_selfdoc(outer, marker, "outer")
    monkeypatch.setenv("PATH", f"{outer}{os.pathsep}{os.environ['PATH']}")
    repo = _repo(tmp_path)

    with _enter(repo) as co:
        hook = Path(co.path) / "build.sh"
        hook.write_text(
            'set -eu\ncat > "$RLSBL_RELEASE_BIN/selfdoc" <<EOF\n'
            f'#!/bin/sh\necho inner > "{marker}"\nEOF\n'
            'chmod +x "$RLSBL_RELEASE_BIN/selfdoc"\n'
        )
        run_release_hook(
            "pre-checks", str(hook), co.path,
            build_hook_env(os.environ.copy(), "0.1.1"), 60,
        )
        assert _run_selfdoc_gen({}, co.path, "0.1.1")

    assert marker.read_text().strip() == "inner"


def test_the_directory_is_private_to_the_release_and_restored_after(tmp_path, monkeypatch):
    monkeypatch.delenv("RLSBL_RELEASE_BIN", raising=False)
    path_before = os.environ["PATH"]
    repo = _repo(tmp_path)

    with _enter(repo) as co:
        bin_dir = os.environ["RLSBL_RELEASE_BIN"]
        assert os.path.isabs(bin_dir) and os.path.isdir(bin_dir)
        assert os.environ["PATH"].split(os.pathsep)[0] == bin_dir
        assert os.listdir(bin_dir) == []
        # Neither in the working tree nor in the checkout a tool walks.
        assert not bin_dir.startswith(co.path + os.sep)
        assert git(repo, "status", "--porcelain", "--ignored") == ""
        (Path(bin_dir) / "stale").write_text("left by this release\n")

    assert os.environ["PATH"] == path_before
    assert "RLSBL_RELEASE_BIN" not in os.environ

    # The next release starts with an empty directory of its own.
    with _enter(repo):
        assert os.listdir(os.environ["RLSBL_RELEASE_BIN"]) == []
