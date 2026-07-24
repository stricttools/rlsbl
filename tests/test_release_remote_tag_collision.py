"""Tests for the pre-mutation remote tag collision check in compute_release_version.

The computed release tag must not already exist on the remote (at any commit)
before the release mutates local state. A remote-only stale tag -- left by an
interrupted or partially-undone release, possibly on another machine -- would
otherwise only surface at push time, after the version bump and release commit.
An inconclusive remote probe (ls-remote failure) is equally fatal: a release
must not start without confirming its target tag is free.

Drives against real git: a working repo plus a file:// bare remote. Each test
patches ``rlsbl.commands.release.remote_tag_commit`` back to the real util
(overriding the autouse offline mock in conftest) so the live ls-remote runs
against the test's own bare remote via the process cwd.
"""

import json
import subprocess

import pytest
from githarness import add_remote, commit_file, git, init_repo
from unittest.mock import MagicMock, patch

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    compute_release_version,
)
from rlsbl.utils import remote_tag_commit as real_remote_tag_commit

RTC = "rlsbl.commands.release.remote_tag_commit"


def _mock_target(version):
    target = MagicMock()
    target.read_version.return_value = version
    target.tag_format.side_effect = lambda v: f"v{v}"
    return target


def _setup_repo_at_v100(tmp_path, monkeypatch):
    """A repo at 1.0.0 with a local v1.0.0 tag and a bare remote in sync."""
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(
        repo, "package.json",
        json.dumps({"name": "pkg", "version": "1.0.0"}, indent=2) + "\n",
        "initial",
    )
    git(repo, "tag", "v1.0.0")  # current tag exists -> bump path (-> v1.0.1)
    add_remote(repo, tmp_path / "remote")  # pushes main + v1.0.0
    monkeypatch.chdir(repo)  # remote_tag_commit(tag) uses process cwd
    return repo


def test_fresh_release_aborts_on_remote_only_stale_tag(tmp_path, monkeypatch):
    repo = _setup_repo_at_v100(tmp_path, monkeypatch)

    # Create v1.0.1 on the bare remote ONLY (not present locally): the exact
    # stale-tag scenario the pre-mutation check must catch.
    git(repo, "tag", "v1.0.1")
    remote_sha = git(repo, "rev-parse", "refs/tags/v1.0.1^{}")
    subprocess.run(
        ["git", "push", "-q", "origin", "v1.0.1"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    git(repo, "tag", "-d", "v1.0.1")  # remove local copy -> remote-only

    head_before = git(repo, "rev-parse", "HEAD")
    target = _mock_target("1.0.0")
    with patch(RTC, side_effect=real_remote_tag_commit):
        with pytest.raises(ReleaseValidationError) as exc:
            compute_release_version(
                target, str(repo), "patch", None, None, lambda _m: None,
                project_dir=str(repo),
            )

    msg = str(exc.value)
    assert "v1.0.1" in msg
    assert "origin" in msg
    assert remote_sha in msg  # remote SHA is named

    # Pre-mutation guarantee: nothing committed, tree still clean.
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "status", "--porcelain") == ""


def test_inconclusive_remote_aborts_pre_mutation(tmp_path, monkeypatch):
    repo = _setup_repo_at_v100(tmp_path, monkeypatch)
    # Point origin at a nonexistent path so ls-remote fails -> INCONCLUSIVE.
    git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    target = _mock_target("1.0.0")
    with patch(RTC, side_effect=real_remote_tag_commit):
        with pytest.raises(ReleaseValidationError) as exc:
            compute_release_version(
                target, str(repo), "patch", None, None, lambda _m: None,
                project_dir=str(repo),
            )
    msg = str(exc.value)
    assert "v1.0.1" in msg
    assert "could not verify" in msg.lower()


def test_absent_remote_tag_proceeds(tmp_path, monkeypatch):
    """No colliding remote tag -> the bump path completes normally."""
    repo = _setup_repo_at_v100(tmp_path, monkeypatch)
    target = _mock_target("1.0.0")
    with patch(RTC, side_effect=real_remote_tag_commit):
        current, new, bump, tag = compute_release_version(
            target, str(repo), "patch", None, None, lambda _m: None,
            project_dir=str(repo),
        )
    assert (current, new, bump, tag) == ("1.0.0", "1.0.1", "patch", "v1.0.1")
