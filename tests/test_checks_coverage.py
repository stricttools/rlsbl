"""Tests for check handler functions to increase code coverage.

Covers:
- checks/release.py: unpublished-refs, branch-sync
- checks/changelog.py: all 9 changelog check handlers (standalone mode)
- checks/prepush.py: explicit releasable mode paths
- commands/monorepo/batch_release.py: _releasable_release_order, _finalize_batch_file
"""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import git as git_out, record_release

from conftest import (
    git_head,
    make_commit,
    make_ctx,
    make_releasable_state,
    make_workspace,
    run_git,
    workspace_toml,
)

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _setup_changelog_repo(repo, tag="v0.1.0", targets=None):
    """Set up a git repo with .rlsbl/changes/ and a tag.

    Args:
        targets: list of target names to declare in config.json.
            Defaults to [] (explicitly no targets).
    """
    if targets is None:
        targets = []
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")
    if tag:
        # Tagged AND archived: the release record is what records a release, and a
        # version tag over an empty release record is a repository that shipped and was
        # never backfilled -- a hard error, not a baseline.
        record_release(repo, tag)

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": targets}) + "\n"
    )
    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold")


def _write_changelog_md(repo, version, content="Some changes"):
    """Write a CHANGELOG.md with an entry for the given version."""
    (repo / "CHANGELOG.md").write_text(f"# Changelog\n\n## {version}\n\n{content}\n")


# ==================================================================
# checks/release.py
# ==================================================================


class TestUnpublishedRefsCheck:
    """The successor of local-tag, remote-tag and github-release.

    Those three each looked at the primary tag of the CURRENT version only.
    This one asks the release record which versions were released and renders every ref
    each of them owns -- primary, companions, recorded aliases -- against the
    local repository and against origin, plus each version's GitHub Release
    against the forge's own listing. Its failure classes: a ref missing
    locally, a ref missing on origin, a ref at the wrong commit, and a
    published tag carrying no GitHub Release.
    """

    def _project(self, tmp_path, monkeypatch, *, version="0.1.0"):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None, targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            f'[project]\nname = "test"\nversion = "{version}"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")
        return repo

    def _run(self, ctx, *, local, remote=None, has_remote=True,
             releases=None, releases_error=None, gh_known=True,
             github_repo="owner/repo"):
        """Run the check with both tag namespaces and the Release listing supplied.

        ``releases`` defaults to "origin's tags all carry a Release", which is
        the healthy world every pre-existing case here describes. Pass an
        explicit sequence to make a Release absent, ``releases_error`` to make
        the listing raise, or ``gh_known=False`` to make gh unavailable.
        """
        from rlsbl.commands.release_reconcile import ReconcileError

        def _list_releases(**kwargs):
            if releases_error is not None:
                raise ReconcileError(releases_error)
            if releases is None:
                return frozenset(remote.commits if remote is not None else {}), gh_known
            return frozenset(releases), gh_known

        patches = [
            patch("rlsbl.utils.local_tag_commits", return_value=local),
            patch("rlsbl.utils.remote_is_configured", return_value=has_remote),
            patch("rlsbl.utils.get_github_repo", return_value=github_repo),
            patch("rlsbl.commands.release_reconcile.list_releases", _list_releases),
        ]
        if remote is not None:
            patches.append(
                patch("rlsbl.utils.remote_tag_commits", return_value=remote)
            )
        for p in patches:
            p.start()
        try:
            return app._check_defs["unpublished-refs"].impl(ctx)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_no_target_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        result = app._check_defs["unpublished-refs"].impl(make_ctx(repo))
        assert result.status == "skip"
        assert "no release target" in result.message

    def test_nothing_released_skips(self, tmp_path, monkeypatch):
        repo = self._project(tmp_path, monkeypatch)
        result = app._check_defs["unpublished-refs"].impl(make_ctx(repo))
        assert result.status == "skip"
        assert "no release" in result.message

    def test_every_ref_present_passes(self, tmp_path, monkeypatch):
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
        )
        assert result.status == "pass", result
        assert "1 released version" in result.message

    def test_a_ref_missing_locally_fails(self, tmp_path, monkeypatch):
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({}),
            remote=TagCommitMap({}),
        )
        assert result.status == "fail"
        assert any("does not exist locally" in p.text for p in result.problems)
        assert any("rlsbl release reconcile" in p.text for p in result.problems)

    def test_a_ref_missing_on_origin_fails(self, tmp_path, monkeypatch):
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({}),
        )
        assert result.status == "fail"
        assert any("not on origin" in p.text for p in result.problems)

    def test_a_ref_at_the_wrong_commit_fails(self, tmp_path, monkeypatch):
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        moved = "b" * 40

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": moved}),
            remote=TagCommitMap({"v0.1.0": moved}),
        )
        assert result.status == "fail"
        assert any("the ref moved" in p.text for p in result.problems)

    def test_an_unreadable_remote_is_an_error_not_a_pass(self, tmp_path, monkeypatch):
        """Fail-closed: an unanswered probe is not an answer."""
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({}, error="connection refused"),
        )
        assert result.status == "fail"
        assert any("connection refused" in p.text for p in result.problems)

    def test_an_unreadable_local_namespace_is_an_error(self, tmp_path, monkeypatch):
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({}, error="not a git repository"),
        )
        assert result.status == "fail"
        assert any("not a git repository" in p.text for p in result.problems)

    def test_no_origin_remote_checks_the_local_half_only(self, tmp_path, monkeypatch):
        """No remote is a different state from an unreachable one."""
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            has_remote=False,
        )
        assert result.status == "pass"
        assert "no origin remote" in result.message

    def test_an_unrecoverable_versions_absent_ref_is_counted_not_errored(
        self, tmp_path, monkeypatch,
    ):
        """No commit to recreate it at means no repair to name.

        Surfaced in the pass message rather than passed over: a check that can
        never go green is a check people stop reading, and an unrecoverable
        archive is a permanent recorded fact, not a fixable state.
        """
        from rlsbl.release_file import write_archived_release_file
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        write_archived_release_file(
            str(repo / ".rlsbl" / "releases"), "0.1.0",
            bump="patch", include=["pypi"], description="an old release",
            candidate_sha=None, tree_hashes=None, unrecoverable=True,
        )

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({}),
            remote=TagCommitMap({}),
        )
        assert result.status == "pass", result
        assert "unrecoverable" in result.message

    def test_a_missing_github_release_fails(self, tmp_path, monkeypatch):
        """A published tag with no Release document is its own failure.

        The tag resolves for a consumer, but the forge shows no notes, and the
        publish workflow finds no released-commit marker to judge. The retired
        `github-release` check asked this of the current version only; here it
        is asked of every archived one.
        """
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
            releases=[],
        )
        assert result.status == "fail"
        assert any("no GitHub Release" in p.text for p in result.problems), (
            result.problems
        )
        assert any("rlsbl release reconcile" in p.text for p in result.problems)

    def test_a_present_github_release_passes(self, tmp_path, monkeypatch):
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
            releases=["v0.1.0"],
        )
        assert result.status == "pass", result
        assert "GitHub Release" in result.message

    def test_a_release_is_not_demanded_for_a_tag_origin_lacks(
        self, tmp_path, monkeypatch,
    ):
        """A Release hangs off a tag on the forge, so an unpushed tag owes none.

        The finding for that version is that its ref never reached origin;
        adding "and it has no Release" would be noise on the same cause.
        """
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({}),
            releases=[],
        )
        assert result.status == "fail"
        assert any("not on origin" in p.text for p in result.problems)
        assert not any("no GitHub Release" in p.text for p in result.problems), (
            result.problems
        )

    def test_an_unrecoverable_versions_absent_release_is_counted_not_errored(
        self, tmp_path, monkeypatch,
    ):
        """No release commit means no released-commit marker to write, so no repair.

        `rlsbl release reconcile` skips an unrecoverable version entirely, so
        demanding a Release for one would be a finding with no remedy.
        """
        from rlsbl.release_file import write_archived_release_file
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        write_archived_release_file(
            str(repo / ".rlsbl" / "releases"), "0.1.0",
            bump="patch", include=["pypi"], description="an old release",
            candidate_sha=None, tree_hashes=None, unrecoverable=True,
        )
        sha = "a" * 40

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
            releases=[],
        )
        assert result.status == "pass", result
        assert "unrecoverable" in result.message
        assert "GitHub Release" in result.message

    def test_an_unreadable_release_listing_is_an_error_not_a_pass(
        self, tmp_path, monkeypatch,
    ):
        """Fail-closed: an unread listing is not an empty one."""
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
            releases_error="HTTP 502 from api.github.com",
        )
        assert result.status == "fail"
        assert any("HTTP 502" in p.text for p in result.problems), result.problems

    def test_an_unavailable_gh_is_an_error_not_a_pass(self, tmp_path, monkeypatch):
        """gh missing or unauthenticated cannot answer, so it must not pass."""
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
            gh_known=False,
        )
        assert result.status == "fail"
        assert any("gh" in p.text for p in result.problems), result.problems

    def test_no_github_repository_checks_the_ref_half_only(
        self, tmp_path, monkeypatch,
    ):
        """A repository the forge does not host has no Release to be missing."""
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
            releases=[],
            github_repo=None,
        )
        assert result.status == "pass", result
        assert "GitHub Release" not in result.message

    def test_the_failure_summary_omits_the_release_half_when_it_was_skipped(
        self, tmp_path, monkeypatch,
    ):
        """A half that was never asked must not be reported as answered.

        With no GitHub repository behind origin there is no Release listing, so
        the summary said "0 tag(s) without a GitHub Release" -- a count of a
        question nobody put. The pass path already draws this distinction
        ("every ref" vs "every ref and GitHub Release"); the failure summary
        mirrors it.
        """
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({}),
            github_repo=None,
        )
        assert result.status == "fail"
        assert "GitHub Release" not in result.message, result.message
        assert "missing on origin" in result.message

    def test_the_failure_summary_keeps_the_release_half_when_it_was_asked(
        self, tmp_path, monkeypatch,
    ):
        """The clause stays when the listing really was read."""
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch)
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
            releases=[],
        )
        assert result.status == "fail"
        assert "1 tag(s) without a GitHub Release" in result.message

    def test_the_release_listing_is_read_once_per_run(
        self, tmp_path, monkeypatch,
    ):
        """ONE listing answers every archived version, and must stay one.

        `gh release list` is a network round trip; moving it inside the
        per-version loop would multiply it by the number of released versions
        and force a recency window, which is exactly what the retired
        `github-release` check did. The call counter pins the single listing.
        """
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch, version="0.3.0")
        record_release(repo, "v0.1.0")
        make_commit(repo, "more.txt", "more work")
        record_release(repo, "v0.2.0")
        make_commit(repo, "even-more.txt", "even more work")
        record_release(repo, "v0.3.0")
        tags = {
            tag: git_out(repo, "rev-parse", f"{tag}^{{}}")
            for tag in ("v0.1.0", "v0.2.0", "v0.3.0")
        }

        calls = []

        def _list_releases(**kwargs):
            calls.append(kwargs)
            return frozenset(tags), True

        patches = [
            patch("rlsbl.utils.local_tag_commits",
                  return_value=TagCommitMap(dict(tags))),
            patch("rlsbl.utils.remote_is_configured", return_value=True),
            patch("rlsbl.utils.remote_tag_commits",
                  return_value=TagCommitMap(dict(tags))),
            patch("rlsbl.utils.get_github_repo", return_value="owner/repo"),
            patch("rlsbl.commands.release_reconcile.list_releases", _list_releases),
        ]
        for p in patches:
            p.start()
        try:
            result = app._check_defs["unpublished-refs"].impl(make_ctx(repo))
        finally:
            for p in reversed(patches):
                p.stop()

        assert result.status == "pass", result
        assert len(calls) == 1, calls

    def _archive_never_released(self, repo, version):
        from rlsbl.release_file import write_archived_release_file

        write_archived_release_file(
            str(repo / ".rlsbl" / "releases"), version,
            bump="patch", include=["pypi"], description=f"phantom {version}",
            candidate_sha=None, tree_hashes=None, never_released=True,
        )

    def test_a_never_released_version_owes_no_refs(self, tmp_path, monkeypatch):
        """A version number no release ever used has no refs to be missing.

        Demanding them would make the check permanently red on a repository
        that recorded a phantom version -- and the remedy it names,
        ``rlsbl release reconcile``, skips such a version entirely, so there
        would be nothing to run either.
        """
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch, version="0.2.0")
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")
        self._archive_never_released(repo, "0.2.0")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha}),
            remote=TagCommitMap({"v0.1.0": sha}),
        )
        assert result.status == "pass", result
        assert "never released" in result.message
        assert "0.2.0" in result.message
        # The phantom is not counted as a released version either.
        assert "1 released version(s)" in result.message

    def test_a_never_released_version_whose_tag_exists_is_still_no_finding(
        self, tmp_path, monkeypatch,
    ):
        """The phantom tag case: the tag that created the phantom is still there.

        It exists locally and on origin, carries no GitHub Release, and none of
        that is a problem -- the version was never released, so no Release is
        owed.
        """
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch, version="0.2.0")
        record_release(repo, "v0.1.0")
        sha = git_out(repo, "rev-parse", "v0.1.0^{}")
        run_git(repo, "tag", "v0.2.0")
        phantom = git_out(repo, "rev-parse", "v0.2.0^{}")
        self._archive_never_released(repo, "0.2.0")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.1.0": sha, "v0.2.0": phantom}),
            remote=TagCommitMap({"v0.1.0": sha, "v0.2.0": phantom}),
            releases=["v0.1.0"],
        )
        assert result.status == "pass", result

    def test_every_archived_version_is_covered_not_just_the_latest(
        self, tmp_path, monkeypatch,
    ):
        """The three retired checks saw only the current version."""
        from rlsbl.utils import TagCommitMap

        repo = self._project(tmp_path, monkeypatch, version="0.2.0")
        record_release(repo, "v0.1.0")
        make_commit(repo, "more.txt", "more work")
        record_release(repo, "v0.2.0")
        latest = git_out(repo, "rev-parse", "v0.2.0^{}")

        result = self._run(
            make_ctx(repo),
            local=TagCommitMap({"v0.2.0": latest}),
            remote=TagCommitMap({"v0.2.0": latest}),
        )
        assert result.status == "fail"
        assert any("v0.1.0" in p.text for p in result.problems), result.problems


class TestBranchSyncCheck:
    """Tests for the branch-sync check."""

    def test_no_remote_tracking_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        # No remote => rev-list will fail
        result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "skip"
        assert "no remote tracking" in result.message

    def test_in_sync_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="0\t0"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "pass"

    def test_ahead_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="0\t3"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "warn"
        assert "3 commit(s) ahead" in result.message

    def test_behind_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="2\t0"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "fail"
        assert "2 commit(s) behind" in result.message

    def test_diverged_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="2\t3"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "fail"
        assert "behind" in result.message and "ahead" in result.message

    def test_unexpected_output_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="garbage"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "fail"
        assert "unexpected" in result.message


# ==================================================================
# checks/changelog.py
# ==================================================================


@pytest.fixture
def changelog_repo(tmp_path, monkeypatch):
    """A git repo with changelog setup, a tag, and a commit with an entry."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    # Include pyproject.toml in initial commit (before the tag)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n'
    )
    run_git(repo, "add", "README.md")
    run_git(repo, "add", "pyproject.toml")
    run_git(repo, "commit", "-q", "-m", "initial")

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci"}) + "\n"
    )
    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold")

    # Tag after scaffold
    record_release(repo, "v0.1.0")

    # Make a real commit that we can add to the changelog
    (repo / "src.py").write_text("x = 1\n")
    run_git(repo, "add", "src.py")
    run_git(repo, "commit", "-q", "-m", "add src")
    head = git_head(repo)

    # Write an entry covering that commit
    entry = json.dumps({
        "commits": [head],
        "user_facing": True,
        "description": "new feature",
        "type": "feature",
    })
    (changes / "unreleased.jsonl").write_text(entry + "\n")
    run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    run_git(repo, "commit", "-q", "-m", "add changelog entry")

    return repo


class TestChangelogEntryCheck:
    """Tests for the changelog-entry check."""

    def test_no_version_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "skip"

    def test_no_changelog_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "fail"
        assert "not found" in result.message

    def test_entry_exists_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        _write_changelog_md(repo, "0.1.0")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add files")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "pass"

    def test_entry_missing_for_version_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        _write_changelog_md(repo, "0.0.1")  # wrong version
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add files")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "warn"
        assert "no entry" in result.message


class TestChangelogHashesCheck:
    """Tests for the changelog-hashes check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "skip"

    def test_valid_hashes_pass(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "pass"

    def test_invalid_hashes_fail(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        entry = json.dumps({
            "commits": ["deadbeefdeadbeef"],
            "user_facing": True,
            "description": "bad",
            "type": "fix",
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "bad entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "fail"


class TestChangelogRangeCheck:
    """Tests for the changelog-range check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-range"].impl(ctx)
        assert result.status == "skip"

    def test_in_range_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-range"].impl(ctx)
        assert result.status == "pass"

    def test_out_of_range_fails(self, tmp_path, monkeypatch):
        """A hash that resolves but is before the tag should fail."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        old_sha = git_head(repo)  # commit BEFORE the tag

        # Tag after the initial commit
        record_release(repo, "v0.1.0")

        # Create changelog infra
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )

        # Make an entry pointing to the OLD commit (before the tag)
        entry = json.dumps({
            "commits": [old_sha],
            "user_facing": True,
            "description": "old commit",
            "type": "fix",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "add entry for old commit")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-range"].impl(ctx)
        assert result.status == "fail"


class TestChangelogCoverageCheck:
    """Tests for the changelog-coverage check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "skip"

    def test_all_covered_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_uncovered_commit_fails(self, changelog_repo):
        # Add a new commit not covered by any entry
        (changelog_repo / "uncovered.py").write_text("y = 2\n")
        run_git(changelog_repo, "add", "uncovered.py")
        run_git(changelog_repo, "commit", "-q", "-m", "uncovered change")

        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "fail"


class TestChangelogOrphansCheck:
    """Tests for the changelog-orphans check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-orphans"].impl(ctx)
        assert result.status == "skip"

    def test_no_orphans_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-orphans"].impl(ctx)
        assert result.status == "pass"

    def test_all_bad_hashes_orphan_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        entry = json.dumps({
            "commits": ["aaaa" * 10],
            "user_facing": True,
            "description": "orphan",
            "type": "fix",
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "orphan entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-orphans"].impl(ctx)
        assert result.status == "fail"


class TestChangelogSchemaCheck:
    """Tests for the changelog-schema check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "skip"

    def test_valid_schema_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "pass"

    def test_bad_schema_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        # user_facing: true but no description and no type
        entry = json.dumps({
            "commits": ["abc1234"],
            "user_facing": True,
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "bad schema entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "fail"


class TestChangelogUserFacingCheck:
    """Tests for the changelog-user-facing check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-user-facing"].impl(ctx)
        assert result.status == "skip"

    def test_has_user_facing_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-user-facing"].impl(ctx)
        assert result.status == "pass"

    def test_no_user_facing_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")
        head = git_head(repo)

        entry = json.dumps({
            "commits": [head],
            "user_facing": False,
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "non-uf entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-user-facing"].impl(ctx)
        assert result.status == "warn"


class TestChangelogBatchCommitsCheck:
    """Tests for the changelog-batch-commits check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-commits"].impl(ctx)
        assert result.status == "skip"

    def test_within_limit_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-batch-commits"].impl(ctx)
        assert result.status == "pass"

    def test_exceeds_limit_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        # Create many commits and reference them all in one entry
        shas = []
        for i in range(10):
            (repo / f"f{i}.py").write_text(f"x{i}\n")
            run_git(repo, "add", f"f{i}.py")
            run_git(repo, "commit", "-q", "-m", f"change {i}")
            shas.append(git_head(repo))

        entry = json.dumps({
            "commits": shas,
            "user_facing": True,
            "description": "big batch",
            "type": "feature",
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "batch entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-commits"].impl(ctx)
        assert result.status == "fail"


class TestChangelogBatchEntriesCheck:
    """Tests for the changelog-batch-entries check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-entries"].impl(ctx)
        assert result.status == "skip"

    def test_within_limit_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-batch-entries"].impl(ctx)
        assert result.status == "pass"

    def test_exceeds_limit_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        # Make one commit
        (repo / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "src.py")
        run_git(repo, "commit", "-q", "-m", "change")
        sha = git_head(repo)

        # Reference the same commit in many entries (exceed max_entries_per_commit)
        entries = []
        for i in range(10):
            entries.append(json.dumps({
                "commits": [sha],
                "user_facing": True,
                "description": f"entry {i}",
                "type": "feature",
            }))
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(
            "\n".join(entries) + "\n"
        )
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "many entries")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-entries"].impl(ctx)
        assert result.status == "fail"


# ==================================================================
# commands/monorepo/batch_release.py: _releasable_release_order
# ==================================================================


class TestReleasableReleaseOrder:
    """Tests for _releasable_release_order."""

    def test_orders_by_member_topological_position(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo.batch_release import _releasable_release_order
        from rlsbl.workspace import Releasable, WorkspaceProject

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "init")

        # Create projects: b depends on a
        (repo / "a").mkdir()
        (repo / "a" / "pyproject.toml").write_text(
            '[project]\nname = "a"\nversion = "0.1.0"\n'
        )
        (repo / "b").mkdir()
        (repo / "b" / "pyproject.toml").write_text(
            '[project]\nname = "b"\nversion = "0.1.0"\n'
            '[project.optional-dependencies]\n'
        )

        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "rel-a"}),
            WorkspaceProject({"name": "b", "path": "b", "releasable": "rel-b", "deps": ["a"]}),
        ]

        # Write workspace.toml
        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            workspace_toml('[[releasables]]\nname = "rel-a"\n\n'
            '[[releasables]]\nname = "rel-b"\n\n'
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = "rel-a"\n\n'
            '[[projects]]\npath = "b"\nname = "b"\nreleasable = "rel-b"\ndeps = ["a"]\n')
        )
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "setup")

        from rlsbl.workspace_graph import WorkspaceGraph

        releasables = [
            Releasable("rel-a"),
            Releasable("rel-b"),
        ]
        graph = WorkspaceGraph(str(repo), projects)

        result = _releasable_release_order(
            {"rel-a", "rel-b"}, releasables, projects, graph,
        )
        # rel-a should come before rel-b since b depends on a
        assert result.index("rel-a") < result.index("rel-b")

    def test_single_releasable(self):
        """A single releasable returns a list with just that name."""
        from rlsbl.commands.monorepo.batch_release import _releasable_release_order
        from rlsbl.workspace import Releasable, WorkspaceProject

        # Create a mock graph with a simple topological_order
        class MockGraph:
            def topological_order(self):
                return ["a"]

        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "rel-a"}),
        ]
        releasables = [Releasable("rel-a")]

        result = _releasable_release_order(
            {"rel-a"}, releasables, projects, MockGraph(),
        )
        assert result == ["rel-a"]


# ==================================================================
# commands/monorepo/batch_release.py: _finalize_batch_file
# ==================================================================


class TestFinalizeBatchFile:
    """Tests for _finalize_batch_file."""

    def test_renames_and_locks(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo.batch_release import _finalize_batch_file

        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()
        batch_path = str(releases_dir / "unreleased.toml")

        with open(batch_path, "w") as f:
            f.write("[releasables.test]\nbump = 'patch'\n")

        log_messages = []

        with patch("rlsbl.commands.monorepo.batch_release.commit_files"):
            _finalize_batch_file(batch_path, log_messages.append)

        # Original path should no longer exist (renamed, not recreated)
        assert not os.path.exists(batch_path)

        # A timestamped batch-*.toml should exist
        toml_files = [f for f in os.listdir(str(releases_dir)) if f.startswith("batch-")]
        assert len(toml_files) == 1

        # Timestamped file should be read-only (444)
        ts_path = str(releases_dir / toml_files[0])
        mode = os.stat(ts_path).st_mode
        assert mode & stat.S_IWUSR == 0  # not writable

        assert any("Finalized" in m for m in log_messages)


# ==================================================================
# checks/prepush.py: monorepo without explicit releasables path
# ==================================================================


class TestPrepushChangelogCoverageMonorepoMembers:
    """prepush-changelog-coverage over a workspace's members."""

    def test_monorepo_uncovered_commit_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        record_release(repo, "v0.0.0")

        # Create a sub-project with changelog
        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        (pkg / ".rlsbl").mkdir(parents=True)
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        # The changelog a member's commits are covered by belongs to its
        # releasable, not to the member package.
        changes = make_releasable_state(repo, "alpha") / "changes"
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        record_release(repo, "alpha@v0.1.0")

        base_sha = git_head(repo)

        # Make an uncovered commit
        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: uncovered")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_releasables, load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
            releasables=load_releasables(str(repo), projects),
        )
        ctx.push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail"
        assert changes.is_dir()

    def test_monorepo_covered_commit_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        record_release(repo, "v0.0.0")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        (pkg / ".rlsbl").mkdir(parents=True)
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        changes = make_releasable_state(repo, "alpha") / "changes"
        rel_jsonl = changes / "unreleased.jsonl"
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        record_release(repo, "alpha@v0.1.0")

        base_sha = git_head(repo)

        # Make a commit and cover it
        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: covered")
        head_sha = git_head(repo)

        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        rel_jsonl.write_text(entry + "\n")
        run_git(repo, "add", str(rel_jsonl.relative_to(repo)))
        run_git(repo, "commit", "-q", "-m", "add entry")

        from rlsbl.workspace import load_releasables, load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
            releasables=load_releasables(str(repo), projects),
        )
        ctx.push_stdin = f"refs/heads/main {git_head(repo)} refs/heads/main {base_sha}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_monorepo_non_releasable_skipped(self, tmp_path, monkeypatch):
        """Non-releasable (dev_node) projects are skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        record_release(repo, "v0.0.0")

        # dev_node project -- not releasable
        pkg = repo / "packages" / "devtool"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "devtool", "version": "0.1.0"}\n')

        make_workspace(
            repo,
            [{"path": "packages/devtool", "name": "devtool", "dev_node": True}],
        )
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        base_sha = git_head(repo)

        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/devtool/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: devtool change")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        # dev_node is non-releasable -> skipped -> pass (all affected covered)
        assert result.status == "pass"

    def test_single_project_no_changes_dir_skips(self, tmp_path, monkeypatch):
        """Single-project mode with no .rlsbl/changes/ skips."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        base = git_head(repo)

        (repo / "g.txt").write_text("y\n")
        run_git(repo, "add", "g.txt")
        run_git(repo, "commit", "-q", "-m", "change")
        head = git_head(repo)

        # No .rlsbl/changes/ dir
        ctx = make_ctx(repo)
        ctx.push_stdin = f"refs/heads/main {head} refs/heads/main {base}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "skip"
        assert "not set up" in result.message

    def test_single_project_covered_passes(self, tmp_path, monkeypatch):
        """Single-project mode with covered commits passes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        record_release(repo, "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (repo / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))
        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        base = git_head(repo)

        (repo / "g.txt").write_text("y\n")
        run_git(repo, "add", "g.txt")
        run_git(repo, "commit", "-q", "-m", "change")
        head = git_head(repo)

        entry = json.dumps({
            "commits": [head],
            "user_facing": True,
            "description": "feat",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "add entry")

        ctx = make_ctx(repo)
        ctx.push_stdin = f"refs/heads/main {git_head(repo)} refs/heads/main {base}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_single_project_uncovered_fails(self, tmp_path, monkeypatch):
        """Single-project mode with uncovered commits fails."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        record_release(repo, "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (repo / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))
        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        base = git_head(repo)

        (repo / "g.txt").write_text("y\n")
        run_git(repo, "add", "g.txt")
        run_git(repo, "commit", "-q", "-m", "uncovered change")
        head = git_head(repo)

        ctx = make_ctx(repo)
        ctx.push_stdin = f"refs/heads/main {head} refs/heads/main {base}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail"

    def test_manual_warning_no_push_context_skips(self, tmp_path, monkeypatch):
        """prepush-manual-warning skips when push_stdin is None."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")

        ctx = make_ctx(repo)
        assert ctx.push_stdin is None
        result = app._check_defs["prepush-manual-warning"].impl(ctx)
        assert result.status == "skip"
        assert "not in push context" in result.message

    def test_monorepo_no_pushed_commits_skips(self, tmp_path, monkeypatch):
        """Monorepo mode skips when pushed commits can't be determined."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        # Use zero-sha as remote ref to simulate a "can't determine" path
        zero = "0" * 40
        head = git_head(repo)
        ctx.push_stdin = f"refs/heads/main {head} refs/heads/main {zero}"

        # Patch get_push_changed_files to return None
        with patch("rlsbl.git_util.get_push_changed_files", return_value=None):
            result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "skip"

    def _root_releasable_repo(self, repo, monkeypatch):
        """A workspace whose root member belongs to a releasable with a changelog.

        Without one, a root-owned commit has nothing it could fail to be
        covered by, and asserting a pass on it proves nothing about
        attribution -- which is what this slot used to do.
        """
        from rlsbl.workspace import (
            Releasable,
            WorkspaceProject,
            get_releasable_changes_dir,
            save_workspace,
            write_releasable_version,
        )

        repo.mkdir(exist_ok=True)
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")

        (repo / "package.json").write_text('{"name": "root-pkg", "version": "0.1.0"}\n')
        (repo / ".rlsbl").mkdir(exist_ok=True)
        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["npm"]})
        )

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')

        releasables = [Releasable(name="app", tag_format="v{version}")]
        save_workspace(
            str(repo),
            [
                WorkspaceProject({"path": ".", "name": "root", "releasable": "app"}),
                WorkspaceProject(
                    {"path": "packages/alpha", "name": "alpha", "releasable": False}
                ),
            ],
            releasables=releasables,
        )
        write_releasable_version(str(repo), "app", "0.1.0")
        changes_dir = get_releasable_changes_dir(str(repo), "app")
        os.makedirs(changes_dir, exist_ok=True)
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write("")

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        record_release(repo, "v0.1.0")
        return releasables, changes_dir

    def _prepush_ctx(self, repo, releasables, base_sha, head_sha):
        from rlsbl.workspace import load_workspace

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=load_workspace(str(repo)),
            graph=None,
            releasables=releasables,
        )
        ctx.push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"
        return ctx

    def test_an_uncovered_root_level_commit_blocks_the_push(self, tmp_path, monkeypatch):
        """The negative half: a root-owned commit with no entry is not covered.

        A commit outside every declared member belongs to the root member,
        which owns the residual. It used to affect no project at all, so the
        check reported a vacuous pass.
        """
        repo = tmp_path / "repo"
        releasables, _changes_dir = self._root_releasable_repo(repo, monkeypatch)
        base_sha = git_head(repo)

        (repo / "root-file.txt").write_text("root change\n")
        run_git(repo, "add", "root-file.txt")
        run_git(repo, "commit", "-q", "-m", "root change")
        head_sha = git_head(repo)

        ctx = self._prepush_ctx(repo, releasables, base_sha, head_sha)
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail", result

    def test_a_covered_root_level_commit_passes(self, tmp_path, monkeypatch):
        """The positive half, on the same commit the negative half rejects."""
        repo = tmp_path / "repo"
        releasables, changes_dir = self._root_releasable_repo(repo, monkeypatch)
        base_sha = git_head(repo)

        (repo / "root-file.txt").write_text("root change\n")
        run_git(repo, "add", "root-file.txt")
        run_git(repo, "commit", "-q", "-m", "root change")
        covered_sha = git_head(repo)

        entry = json.dumps({
            "commits": [covered_sha],
            "user_facing": True,
            "description": "root change",
            "type": "feature",
        })
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(entry + "\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "changelog: root change")
        head_sha = git_head(repo)

        ctx = self._prepush_ctx(repo, releasables, base_sha, head_sha)
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass", result
