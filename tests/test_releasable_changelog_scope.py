"""Tests for changelog validation scoping to releasable members.

Verifies that when releasing a multi-member releasable, changelog
coverage checks only consider commits touching the releasable's member
projects -- not all commits in the repo.

Covers the releasable-scoped coverage check: validate_changelog_state is
handed the releasable's members, and the ownership scope built from them
decides which commits the check considers.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import archive_release, git_head, release_record_dir, make_commit, run_git
from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.changelog.validate import check_coverage, check_in_range, check_no_orphans
from rlsbl.ownership import OwnershipScope, releasable_state_dir
from rlsbl.workspace import (
    WorkspaceProject,
    get_releasable_changes_dir,
)


def _releases_dir(root, releasable_name):
    """The release record for a releasable: the sibling of its changes directory."""
    return release_record_dir(
        None,
        releasable_dir=os.path.dirname(
            get_releasable_changes_dir(str(root), releasable_name)
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_releasable_repo(tmp_path, monkeypatch):
    """Create a git repo simulating two releasables with disjoint members.

    Releasable A: members pkg-a1/, pkg-a2/
    Releasable B: members pkg-b1/

    Returns (root, scope_a, scope_b) -- the ownership scopes for the two
    releasables. Each carries the WHOLE workspace member list (both
    releasables' members plus the root member) alongside the names in scope,
    because a file's owner is decided against every member, plus the
    releasable's own state directory, which the releasable claims itself.
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    # Create project directories
    for d in ["pkg-a1", "pkg-a2", "pkg-b1"]:
        (tmp_path / d).mkdir()

    # Initial commit
    readme = tmp_path / "README.md"
    readme.write_text("# monorepo\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    # Tag as v0.1.0 and archive it for each releasable: the unreleased range
    # is bounded by the releasable's own RELEASE RECORD entry, not by the tag.
    run_git(tmp_path, "tag", "rel-a@v0.1.0")
    run_git(tmp_path, "tag", "rel-b@v0.1.0")
    baseline = git_head(tmp_path)
    for rel in ("rel-a", "rel-b"):
        archive_release(_releases_dir(tmp_path, rel), "0.1.0", baseline)

    projects_a = [
        WorkspaceProject({"name": "a1", "path": "pkg-a1", "releasable": "rel-a"}),
        WorkspaceProject({"name": "a2", "path": "pkg-a2", "releasable": "rel-a"}),
    ]
    projects_b = [
        WorkspaceProject({"name": "b1", "path": "pkg-b1", "releasable": "rel-b"}),
    ]
    root_member = WorkspaceProject({"name": "root", "path": ".", "dev_only": True})
    all_members = [root_member, *projects_a, *projects_b]

    return (
        tmp_path,
        OwnershipScope.for_releasable(all_members, projects_a, "rel-a"),
        OwnershipScope.for_releasable(all_members, projects_b, "rel-b"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReleasableChangelogScope:
    """Changelog coverage scoped to releasable members."""

    def test_commit_in_releasable_a_excluded_from_b(self, two_releasable_repo):
        """A commit touching only releasable-A's files is excluded from
        releasable-B's validation (not flagged as uncovered)."""
        root, scope_a, scope_b = two_releasable_repo

        # Commit touching only releasable-A member
        sha_a = make_commit(root, "pkg-a1/code.py", "change in a1")

        # Set up releasable-B's changes dir with NO entries
        changes_dir_b = get_releasable_changes_dir(str(root), "rel-b")
        os.makedirs(changes_dir_b, exist_ok=True)
        Path(os.path.join(changes_dir_b, "unreleased.jsonl")).write_text("")

        # Coverage check for releasable B with its member projects
        # should pass because the commit is outside B's scope
        passed, details = check_coverage(
            [], _releases_dir(root, "rel-b"),
            tag_glob="rel-b@v*", scope=scope_b,
        )
        assert passed, f"Expected pass (commit outside B's scope), got: {details}"
        # Verify the commit was explicitly skipped
        skip_msgs = [d for d in details if "outside package directory" in d]
        assert len(skip_msgs) == 1

    def test_commit_in_releasable_b_included_in_b(self, two_releasable_repo):
        """A commit touching releasable-B's files IS included in
        releasable-B's validation (flagged as uncovered if no entry)."""
        root, scope_a, scope_b = two_releasable_repo

        # Commit touching releasable-B member
        sha_b = make_commit(root, "pkg-b1/code.py", "change in b1")

        # Coverage check for releasable B with no entries should fail
        passed, details = check_coverage(
            [], _releases_dir(root, "rel-b"),
            tag_glob="rel-b@v*", scope=scope_b,
        )
        assert not passed, "Expected fail (commit in B's scope with no entry)"
        uncovered_msgs = [d for d in details if "not covered" in d]
        assert len(uncovered_msgs) == 1
        assert sha_b[:12] in uncovered_msgs[0]

    def test_commit_touching_both_releasables_included_in_both(self, two_releasable_repo):
        """A commit touching files in BOTH releasables is included in
        both validations."""
        root, scope_a, scope_b = two_releasable_repo

        # Create a commit touching files in both releasables
        (root / "pkg-a1" / "shared.py").write_text("shared a1\n")
        (root / "pkg-b1" / "shared.py").write_text("shared b1\n")
        run_git(root, "add", "pkg-a1/shared.py")
        run_git(root, "add", "pkg-b1/shared.py")
        run_git(root, "commit", "-q", "-m", "cross-releasable change")
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, check=True,
        )
        sha_both = result.stdout.strip()

        # Coverage check for releasable A with no entries should fail
        # (the commit touches pkg-a1 which is in A)
        passed_a, details_a = check_coverage(
            [], _releases_dir(root, "rel-a"),
            tag_glob="rel-a@v*", scope=scope_a,
        )
        assert not passed_a, "Expected fail for A (commit touches A's files)"
        uncovered_a = [d for d in details_a if "not covered" in d]
        assert any(sha_both[:12] in d for d in uncovered_a)

        # Coverage check for releasable B with no entries should also fail
        passed_b, details_b = check_coverage(
            [], _releases_dir(root, "rel-b"),
            tag_glob="rel-b@v*", scope=scope_b,
        )
        assert not passed_b, "Expected fail for B (commit touches B's files)"
        uncovered_b = [d for d in details_b if "not covered" in d]
        assert any(sha_both[:12] in d for d in uncovered_b)

    def test_root_commit_excluded_from_both_releasables(self, two_releasable_repo):
        """A workspace-root commit (touching no member's files) is
        excluded from both releasables' validations."""
        root, scope_a, scope_b = two_releasable_repo

        # Commit touching only workspace root
        sha_root = make_commit(root, "README.md", "update readme")

        # Coverage check for releasable A should pass (root commit outside scope)
        passed_a, details_a = check_coverage(
            [], _releases_dir(root, "rel-a"),
            tag_glob="rel-a@v*", scope=scope_a,
        )
        assert passed_a, f"Expected pass for A, got: {details_a}"

        # Coverage check for releasable B should also pass
        passed_b, details_b = check_coverage(
            [], _releases_dir(root, "rel-b"),
            tag_glob="rel-b@v*", scope=scope_b,
        )
        assert passed_b, f"Expected pass for B, got: {details_b}"


class TestReleasableOwnsItsStateDirectory:
    """A releasable's own state directory is inside its changelog scope.

    Archiving a release file and finalizing a changelog touch
    ``.rlsbl-monorepo/releasables/<name>/`` and nothing else. No member's
    declared path claims that directory, so such a commit used to fall
    outside every releasable's scope -- and an entry naming it was reported
    out of range and then orphaned.
    """

    def _state_commit(self, root, releasable_name):
        """Commit a file under *releasable_name*'s state directory only.

        The version file stands in for the whole directory: attribution is by
        path, so the release archive and the finalized changelog that a real
        release writes beside it answer identically -- and this one is not
        parsed by the release record the checks read.
        """
        rel_path = f"{releasable_state_dir(releasable_name)}/version"
        (root / os.path.dirname(rel_path)).mkdir(parents=True, exist_ok=True)
        return make_commit(root, rel_path, f"bump {releasable_name}")

    def test_state_dir_commit_is_in_its_own_releasables_range(self, two_releasable_repo):
        root, scope_a, scope_b = two_releasable_repo
        sha = self._state_commit(root, "rel-b")

        entries = [ChangelogEntry(commits=[sha], user_facing=False)]
        passed, details = check_in_range(
            entries, _releases_dir(root, "rel-b"),
            tag_glob="rel-b@v*", scope=scope_b,
        )
        assert passed, f"Expected the state-dir commit in B's range, got: {details}"

    def test_state_dir_commit_entry_is_not_orphaned(self, two_releasable_repo):
        root, scope_a, scope_b = two_releasable_repo
        sha = self._state_commit(root, "rel-b")

        entries = [ChangelogEntry(commits=[sha], user_facing=False)]
        passed, details = check_no_orphans(
            entries, _releases_dir(root, "rel-b"),
            tag_glob="rel-b@v*", scope=scope_b,
        )
        assert passed, f"Expected no orphan for the state-dir commit, got: {details}"

    def test_another_releasables_state_dir_stays_out_of_scope(self, two_releasable_repo):
        """Only the releasable whose directory it is claims the commit."""
        root, scope_a, scope_b = two_releasable_repo
        sha = self._state_commit(root, "rel-b")

        entries = [ChangelogEntry(commits=[sha], user_facing=False)]
        passed, details = check_in_range(
            entries, _releases_dir(root, "rel-a"),
            tag_glob="rel-a@v*", scope=scope_a,
        )
        assert not passed, "Expected B's state-dir commit outside A's range"
        assert any("not in unreleased range" in d for d in details)

    def test_state_dir_commit_needs_no_coverage(self, two_releasable_repo):
        """In scope for attribution, still exempt from coverage.

        The tool-owned exempt set already says a commit touching only release
        machinery needs no changelog entry. Claiming the directory answers a
        different question -- WHICH releasable it belongs to -- so coverage
        stays silent while an entry naming it stays valid.
        """
        root, scope_a, scope_b = two_releasable_repo
        self._state_commit(root, "rel-b")

        passed, details = check_coverage(
            [], _releases_dir(root, "rel-b"),
            tag_glob="rel-b@v*", scope=scope_b,
        )
        assert passed, f"Expected pass (changelog-only commit), got: {details}"


class TestReleaseFlowPassesMemberProjs:
    """Verify validate_changelog_state resolves the correct changes_dir.

    After the preflight-changelog migration, validate_changelog_state is a
    thin wrapper around resolve_changes_dir.  Changelog validation is now
    handled by the ``preflight-changelog`` check tag in the release flow.
    """

    def test_releasable_mode_returns_releasable_changes_dir(self, tmp_path):
        """In releasable mode, validate_changelog_state returns the
        releasable-level changes directory."""
        from rlsbl.commands.release.validate import validate_changelog_state

        ws_root = str(tmp_path)
        releasable_name = "myrel"

        # Create releasable changes dir
        expected_dir = get_releasable_changes_dir(ws_root, releasable_name)
        os.makedirs(expected_dir, exist_ok=True)
        Path(os.path.join(expected_dir, "unreleased.jsonl")).write_text("")

        result = validate_changelog_state(
            "/some/project", MagicMock(), "myrel-mono", "pkg1",
            {}, monorepo_project=None,
            releasable_name="myrel",
            releasable_tag_fmt="{name}@v{version}",
            workspace_root=ws_root,
        )

        assert result == expected_dir

    def test_implicit_monorepo_returns_project_changes_dir(self, tmp_path):
        """Without releasable_name, validate_changelog_state returns the
        per-project .rlsbl/changes/ directory."""
        from rlsbl.commands.release.validate import validate_changelog_state

        project_dir = str(tmp_path)
        changes_dir = os.path.join(project_dir, ".rlsbl", "changes")
        os.makedirs(changes_dir, exist_ok=True)
        Path(os.path.join(changes_dir, "unreleased.jsonl")).write_text("")

        result = validate_changelog_state(
            project_dir, MagicMock(), "mono", "pkg1",
            {}, monorepo_project=None,
        )

        assert result == changes_dir
