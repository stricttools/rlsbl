"""Tests for changelog validation scoping to releasable members.

Verifies that when releasing a multi-member releasable, changelog
coverage checks only consider commits touching the releasable's member
projects -- not all commits in the repo.

Covers Phase 1b fix: passing member_projs list to validate_changelog_state
so _filter_commits_for_scope uses filter_commits_for_releasable.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_commit, run_git
from rlsbl.changelog.validate import check_coverage
from rlsbl.workspace import WorkspaceProject, get_releasable_changes_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_releasable_repo(tmp_path, monkeypatch):
    """Create a git repo simulating two releasables with disjoint members.

    Releasable A: members pkg-a1/, pkg-a2/
    Releasable B: members pkg-b1/

    Returns (root, projects_a, projects_b) where projects_a and projects_b
    are lists of WorkspaceProject suitable for passing as monorepo_project.
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

    # Tag as v0.1.0 so there's a baseline for unreleased range
    run_git(tmp_path, "tag", "rel-a@v0.1.0")
    run_git(tmp_path, "tag", "rel-b@v0.1.0")

    projects_a = [
        WorkspaceProject({"name": "a1", "path": "pkg-a1", "releasable": "rel-a"}),
        WorkspaceProject({"name": "a2", "path": "pkg-a2", "releasable": "rel-a"}),
    ]
    projects_b = [
        WorkspaceProject({"name": "b1", "path": "pkg-b1", "releasable": "rel-b"}),
    ]

    return tmp_path, projects_a, projects_b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReleasableChangelogScope:
    """Changelog coverage scoped to releasable members."""

    def test_commit_in_releasable_a_excluded_from_b(self, two_releasable_repo):
        """A commit touching only releasable-A's files is excluded from
        releasable-B's validation (not flagged as uncovered)."""
        root, projects_a, projects_b = two_releasable_repo

        # Commit touching only releasable-A member
        sha_a = make_commit(root, "pkg-a1/code.py", "change in a1")

        # Set up releasable-B's changes dir with NO entries
        changes_dir_b = get_releasable_changes_dir(str(root), "rel-b")
        os.makedirs(changes_dir_b, exist_ok=True)
        Path(os.path.join(changes_dir_b, "unreleased.jsonl")).write_text("")

        # Coverage check for releasable B with its member projects
        # should pass because the commit is outside B's scope
        passed, details = check_coverage(
            [], tag_glob="rel-b@v*", project=projects_b,
        )
        assert passed, f"Expected pass (commit outside B's scope), got: {details}"
        # Verify the commit was explicitly skipped
        skip_msgs = [d for d in details if "outside package directory" in d]
        assert len(skip_msgs) == 1

    def test_commit_in_releasable_b_included_in_b(self, two_releasable_repo):
        """A commit touching releasable-B's files IS included in
        releasable-B's validation (flagged as uncovered if no entry)."""
        root, projects_a, projects_b = two_releasable_repo

        # Commit touching releasable-B member
        sha_b = make_commit(root, "pkg-b1/code.py", "change in b1")

        # Coverage check for releasable B with no entries should fail
        passed, details = check_coverage(
            [], tag_glob="rel-b@v*", project=projects_b,
        )
        assert not passed, "Expected fail (commit in B's scope with no entry)"
        uncovered_msgs = [d for d in details if "not covered" in d]
        assert len(uncovered_msgs) == 1
        assert sha_b[:12] in uncovered_msgs[0]

    def test_commit_touching_both_releasables_included_in_both(self, two_releasable_repo):
        """A commit touching files in BOTH releasables is included in
        both validations."""
        root, projects_a, projects_b = two_releasable_repo

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
            [], tag_glob="rel-a@v*", project=projects_a,
        )
        assert not passed_a, "Expected fail for A (commit touches A's files)"
        uncovered_a = [d for d in details_a if "not covered" in d]
        assert any(sha_both[:12] in d for d in uncovered_a)

        # Coverage check for releasable B with no entries should also fail
        passed_b, details_b = check_coverage(
            [], tag_glob="rel-b@v*", project=projects_b,
        )
        assert not passed_b, "Expected fail for B (commit touches B's files)"
        uncovered_b = [d for d in details_b if "not covered" in d]
        assert any(sha_both[:12] in d for d in uncovered_b)

    def test_root_commit_excluded_from_both_releasables(self, two_releasable_repo):
        """A workspace-root commit (touching no member's files) is
        excluded from both releasables' validations."""
        root, projects_a, projects_b = two_releasable_repo

        # Commit touching only workspace root
        sha_root = make_commit(root, "README.md", "update readme")

        # Coverage check for releasable A should pass (root commit outside scope)
        passed_a, details_a = check_coverage(
            [], tag_glob="rel-a@v*", project=projects_a,
        )
        assert passed_a, f"Expected pass for A, got: {details_a}"

        # Coverage check for releasable B should also pass
        passed_b, details_b = check_coverage(
            [], tag_glob="rel-b@v*", project=projects_b,
        )
        assert passed_b, f"Expected pass for B, got: {details_b}"


class TestReleaseFlowPassesMemberProjs:
    """Verify the release __init__.py code path sets monorepo_project
    to the member project list when releasing a releasable."""

    def test_releasable_mode_passes_member_list(self, tmp_path, monkeypatch):
        """In releasable mode, monorepo_project is set to member_projs list."""
        # This tests the logic in _run_cmd_inner by checking that
        # validate_changelog_state receives a list of WorkspaceProject
        # as monorepo_project.
        from rlsbl.commands.release.validate import validate_changelog_state

        ws_root = str(tmp_path)
        releasable_name = "myrel"

        # Create releasable changes dir
        changes_dir = get_releasable_changes_dir(ws_root, releasable_name)
        os.makedirs(changes_dir, exist_ok=True)
        Path(os.path.join(changes_dir, "unreleased.jsonl")).write_text("")

        member_projs = [
            WorkspaceProject({"name": "p1", "path": "pkg1", "releasable": "myrel"}),
            WorkspaceProject({"name": "p2", "path": "pkg2", "releasable": "myrel"}),
        ]

        # Patch validate_unreleased to capture the project argument
        captured = {}
        with patch("rlsbl.commands.release.validate_unreleased") as mock_validate:
            mock_validate.return_value = {"passed": True, "checks": {}}

            validate_changelog_state(
                "/some/project", MagicMock(), "myrel-mono", "pkg1",
                {}, monorepo_project=member_projs,
                releasable_name="myrel",
                releasable_tag_fmt="{name}@v{version}",
                workspace_root=ws_root,
            )

        # Verify validate_unreleased was called with project=member_projs
        call_kwargs = mock_validate.call_args
        assert call_kwargs[1].get("project") is member_projs
        # Confirm it's a list (triggers the releasable path in _filter_commits_for_scope)
        assert isinstance(call_kwargs[1]["project"], list)
        assert len(call_kwargs[1]["project"]) == 2

    def test_implicit_monorepo_still_uses_single_project(self, tmp_path):
        """Without releasable_name, monorepo_project is a single project
        (the existing behavior is preserved)."""
        from rlsbl.commands.release.validate import validate_changelog_state

        project_dir = str(tmp_path)
        changes_dir = os.path.join(project_dir, ".rlsbl", "changes")
        os.makedirs(changes_dir, exist_ok=True)
        Path(os.path.join(changes_dir, "unreleased.jsonl")).write_text("")

        single_proj = WorkspaceProject({"name": "p1", "path": "pkg1"})

        with patch("rlsbl.commands.release.validate_unreleased") as mock_validate:
            mock_validate.return_value = {"passed": True, "checks": {}}

            validate_changelog_state(
                project_dir, MagicMock(), "mono", "pkg1",
                {}, monorepo_project=single_proj,
            )

        call_kwargs = mock_validate.call_args
        assert call_kwargs[1].get("project") is single_proj
        # Single project, not a list
        assert not isinstance(call_kwargs[1]["project"], list)
