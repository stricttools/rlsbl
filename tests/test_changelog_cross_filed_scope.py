"""A cross-filed changelog entry is out of SCOPE, not out of RANGE.

In a workspace, `changelog-range` filters the unreleased commit range to the
commits this releasable's members own, then reports every entry hash outside
the filtered set as "not in the unreleased range". For an entry filed under the
wrong releasable that sentence is false: the commit IS in the range -- it is
between the release this checkout is anchored to and HEAD -- it just belongs to
another releasable's territory. The reader is sent to look for a rebase or a
stale hash, and finds a perfectly current commit.

The two conditions produce different messages now, and only the genuinely
out-of-range one says so.
"""

import json
import os

import pytest

from conftest import git_head, run_git
from test_coverage_check_releasable import (
    _make_workspace_ctx,
    _setup_releasable_monorepo,
)

from rlsbl import app
from rlsbl.workspace import Releasable, get_releasable_changes_dir


RELEASABLES = [Releasable(name="alpha"), Releasable(name="beta")]
PROJECTS = [
    {"path": "libs/core", "name": "core", "releasable": "alpha"},
    {"path": "libs/other", "name": "other", "releasable": "beta"},
]


@pytest.fixture
def two_releasables(tmp_path, monkeypatch):
    """A workspace with releasables `alpha` (libs/core) and `beta` (libs/other)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    _setup_releasable_monorepo(repo, releasables=RELEASABLES, projects=PROJECTS)
    return repo


def _write_alpha_entry(repo, sha, description="cross-filed feature"):
    changes_dir = get_releasable_changes_dir(str(repo), "alpha")
    path = os.path.join(changes_dir, "unreleased.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "commits": [sha],
            "user_facing": True,
            "description": description,
            "type": "feature",
        }, separators=(",", ":")) + "\n")
    run_git(repo, "add", path)
    run_git(repo, "commit", "-q", "-m", "add entry")


def _range_problems(repo):
    ctx = _make_workspace_ctx(repo, RELEASABLES)
    result = app._check_defs["changelog-range"].impl(ctx)
    return result, [p.text for p in result.problems]


class TestCrossFiledEntry:
    """An in-range commit owned by another releasable."""

    def _cross_file(self, repo):
        """Commit only into beta's territory, then file the entry under alpha."""
        (repo / "libs" / "other" / "feat.py").write_text("y = 2\n")
        run_git(repo, "add", "libs/other/feat.py")
        run_git(repo, "commit", "-q", "-m", "feat: a beta change")
        sha = git_head(repo)
        _write_alpha_entry(repo, sha)
        return sha

    def test_it_is_not_reported_as_out_of_range(self, two_releasables):
        sha = self._cross_file(two_releasables)
        result, problems = _range_problems(two_releasables)

        assert result.status != "pass", "the cross-filed entry must be flagged"
        text = " ".join(problems)
        assert sha[:12] in text
        assert "not in unreleased range" not in text, (
            "the commit IS in the unreleased range; only its owner is wrong"
        )

    def test_it_names_the_member_and_releasable_that_own_the_commit(
        self, two_releasables,
    ):
        self._cross_file(two_releasables)
        _result, problems = _range_problems(two_releasables)
        text = " ".join(problems)

        assert "scope" in text
        assert "other" in text, "the owning member is not named"
        assert "beta" in text, "the owning releasable is not named"

    def test_it_prints_a_followable_remedy(self, two_releasables):
        from test_remedy_followability import (
            assert_invocation_is_real,
            invocations_in,
        )

        self._cross_file(two_releasables)
        _result, problems = _range_problems(two_releasables)
        text = " ".join(problems)

        invocations = invocations_in(text)
        assert invocations, text
        for invocation in invocations:
            assert_invocation_is_real(invocation)
        assert any(
            inv.startswith("rlsbl changelog remove") for inv in invocations
        ), invocations
        assert any(
            inv.startswith("rlsbl changelog add") for inv in invocations
        ), invocations

    def test_following_the_remedy_clears_the_finding(
        self, two_releasables, monkeypatch,
    ):
        from unittest import mock

        from test_remedy_followability import invocations_in

        import rlsbl
        import shlex

        self._cross_file(two_releasables)
        # "Remove it here" means from the releasable whose changelog holds the
        # entry; the workspace root resolves to the root member, which is a
        # dev node in this fixture and keeps no changelog at all.
        monkeypatch.chdir(two_releasables / "libs" / "core")
        _result, problems = _range_problems(two_releasables)
        removal = [
            inv for inv in invocations_in(" ".join(problems))
            if inv.startswith("rlsbl changelog remove")
        ]
        assert len(removal) == 1, removal

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            run = rlsbl.app.test(shlex.split(removal[0])[1:])
        assert run.exit_code == 0, run.stderr

        result, problems = _range_problems(two_releasables)
        assert result.status == "pass", problems


class TestToolOwnedCommit:
    """An in-range commit whose every file is rlsbl's own bookkeeping.

    No member owns a tool-owned path and no releasable claims one outside its
    own state directory, so there is no owning member's directory to file the
    entry from. The remedy the cross-filed case prints -- "add it from the
    owning member's directory" -- names a place that does not exist here, so
    this case gets its own: the entry is simply removed.
    """

    def _tool_owned_commit(self, repo):
        """Commit only into a tool-owned path, then file the entry under alpha."""
        snapshot = repo / ".rlsbl-monorepo" / "snapshot.json"
        snapshot.write_text('{"packages": []}\n')
        run_git(repo, "add", ".rlsbl-monorepo/snapshot.json")
        run_git(repo, "commit", "-q", "-m", "chore: regenerate the snapshot")
        sha = git_head(repo)
        _write_alpha_entry(repo, sha, description="tool-owned change")
        return sha

    def test_it_says_the_files_are_tool_owned_and_need_no_coverage(
        self, two_releasables,
    ):
        sha = self._tool_owned_commit(two_releasables)
        result, problems = _range_problems(two_releasables)

        assert result.status != "pass", "the entry must still be flagged"
        text = " ".join(problems)
        assert sha[:12] in text
        assert "outside this changelog's scope" in text
        assert "tool-owned" in text
        assert "no changelog covers it" in text

    def test_it_does_not_send_the_reader_to_a_directory_that_does_not_exist(
        self, two_releasables,
    ):
        self._tool_owned_commit(two_releasables)
        _result, problems = _range_problems(two_releasables)
        text = " ".join(problems)

        assert "owning member's directory" not in text
        assert "rlsbl changelog add" not in text

    def test_the_only_remedy_is_a_removal_that_clears_the_finding(
        self, two_releasables, monkeypatch,
    ):
        from unittest import mock

        from test_remedy_followability import (
            assert_invocation_is_real,
            invocations_in,
        )

        import rlsbl
        import shlex

        self._tool_owned_commit(two_releasables)
        monkeypatch.chdir(two_releasables / "libs" / "core")
        _result, problems = _range_problems(two_releasables)

        invocations = invocations_in(" ".join(problems))
        assert len(invocations) == 1, invocations
        assert invocations[0].startswith("rlsbl changelog remove"), invocations
        assert_invocation_is_real(invocations[0])

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            run = rlsbl.app.test(shlex.split(invocations[0])[1:])
        assert run.exit_code == 0, run.stderr

        result, problems = _range_problems(two_releasables)
        assert result.status == "pass", problems


class TestGenuinelyOutOfRangeEntry:
    """The message the cross-filed case used to steal stays exactly as it was."""

    def test_a_pre_release_commit_is_still_out_of_range(self, two_releasables):
        repo = two_releasables
        # A commit in alpha's own territory, but BEFORE alpha's release: the
        # scaffold commit the releasable's archive is anchored to.
        (repo / "libs" / "core" / "old.py").write_text("x = 1\n")
        run_git(repo, "add", "libs/core/old.py")
        run_git(repo, "commit", "-q", "-m", "feat: pre-release change")
        sha = git_head(repo)
        # Release alpha AT that commit, so it falls out of the range.
        from githarness import record_release
        from rlsbl.workspace import get_releasable_dir

        record_release(
            repo, "alpha@v0.2.0",
            release_record=os.path.join(
                get_releasable_dir(str(repo), "alpha"), "releases",
            ),
        )
        (repo / "libs" / "core" / "later.py").write_text("z = 3\n")
        run_git(repo, "add", "libs/core/later.py")
        run_git(repo, "commit", "-q", "-m", "feat: later change")

        _write_alpha_entry(repo, sha, description="already released")
        result, problems = _range_problems(repo)

        assert result.status != "pass"
        text = " ".join(problems)
        assert "not in unreleased range" in text
        assert sha[:12] in text or sha in text
