"""Tests for pre-push changelog coverage in multi-releasable monorepos.

Verifies that the prepush-changelog-coverage check correctly scopes
changelog coverage to releasable-level changelogs when the workspace
uses explicit [[releasables]] mode.

Scenarios:
1. Monorepo with 2 releasables, each with 2+ member packages
2. Commits touching only one releasable's packages are covered by that
   releasable's unreleased.jsonl
3. Pre-push check passes when all commits have correct releasable-level coverage
4. Pre-push check fails when a commit is missing from its releasable's changelog
"""

import json
import os
from pathlib import Path

import pytest

from conftest import git_head, make_commit, run_git
from rlsbl import app
from rlsbl.changelog.files import append_entry
from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    get_releasable_changes_dir,
    load_releasables,
    load_workspace,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _write_workspace_explicit(root, releasables, projects):
    """Write a workspace.toml with explicit releasable definitions."""
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    lines = []
    for rel in releasables:
        lines.append("[[releasables]]")
        lines.append(f'name = "{rel["name"]}"')
        if "tag_format" in rel:
            lines.append(f'tag_format = "{rel["tag_format"]}"')
        lines.append("")
    for proj in projects:
        lines.append("[[projects]]")
        lines.append(f'path = "{proj["path"]}"')
        lines.append(f'name = "{proj["name"]}"')
        if "releasable" in proj:
            val = proj["releasable"]
            if isinstance(val, bool) and val is False:
                lines.append("releasable = false")
            elif isinstance(val, str):
                lines.append(f'releasable = "{val}"')
        lines.append("")
    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))


def _setup_releasable_changes(root, releasable_name):
    """Create a releasable changes directory with an empty unreleased.jsonl."""
    changes_dir = get_releasable_changes_dir(str(root), releasable_name)
    os.makedirs(changes_dir, exist_ok=True)
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    with open(unreleased, "w", encoding="utf-8") as f:
        pass  # empty file
    return changes_dir


def _make_push_stdin(local_sha, remote_sha):
    """Build a push stdin string in the format git passes to pre-push hooks."""
    return f"refs/heads/main {local_sha} refs/heads/main {remote_sha}"


def _build_ctx(root, push_stdin):
    """Build a WorkspaceCheckContext with releasables loaded."""
    projects = load_workspace(str(root))
    releasables = load_releasables(str(root), projects=projects)
    ctx = WorkspaceCheckContext(
        project_root=Path(root),
        workspace_root=Path(root),
        config={},
        projects=projects,
        graph=None,
        releasables=releasables,
    )
    ctx.push_stdin = push_stdin
    return ctx


# ------------------------------------------------------------------
# Fixture: multi-releasable monorepo
# ------------------------------------------------------------------


@pytest.fixture
def releasable_monorepo(tmp_path, monkeypatch):
    """Create a monorepo with 2 releasables, each with 2 member packages.

    Layout:
        frontend/ (releasable: "frontend")
            fe-app/
            fe-lib/
        backend/ (releasable: "backend")
            be-api/
            be-worker/

    Returns a dict with root, the releasable changes dirs, and base_sha.
    """
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)

    # Init git repo
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "test@test.local")
    run_git(root, "config", "user.name", "Test")

    (root / "README.md").write_text("# monorepo\n")
    run_git(root, "add", "README.md")
    run_git(root, "commit", "-q", "-m", "initial")
    run_git(root, "tag", "v0.0.0")

    # Create workspace with explicit releasables
    _write_workspace_explicit(
        root,
        releasables=[
            {"name": "frontend"},
            {"name": "backend"},
        ],
        projects=[
            {"path": "fe-app", "name": "fe-app", "releasable": "frontend"},
            {"path": "fe-lib", "name": "fe-lib", "releasable": "frontend"},
            {"path": "be-api", "name": "be-api", "releasable": "backend"},
            {"path": "be-worker", "name": "be-worker", "releasable": "backend"},
        ],
    )

    # Create package directories with placeholder files
    for pkg in ("fe-app", "fe-lib", "be-api", "be-worker"):
        (root / pkg).mkdir()
        (root / pkg / "init.py").write_text(f"# {pkg}\n")

    # Create releasable-level changelog directories
    fe_changes = _setup_releasable_changes(root, "frontend")
    be_changes = _setup_releasable_changes(root, "backend")

    # Commit all scaffold files
    run_git(root, "add", ".")
    run_git(root, "commit", "-q", "-m", "scaffold monorepo")

    base_sha = git_head(root)

    return {
        "root": root,
        "fe_changes": fe_changes,
        "be_changes": be_changes,
        "base_sha": base_sha,
    }


# ------------------------------------------------------------------
# Test 1: passes when all commits covered in correct releasable changelogs
# ------------------------------------------------------------------


class TestPrepushReleasableAllCovered:
    """Pre-push check passes when every commit is covered by the correct
    releasable-level changelog."""

    def test_single_releasable_commit_covered(self, releasable_monorepo):
        """A commit in one releasable, covered by that releasable's changelog, passes."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]
        fe_changes = releasable_monorepo["fe_changes"]

        # Make a commit in the frontend releasable
        sha = make_commit(root, "fe-app/feature.py", "feat: new button")

        # Cover it in the frontend changelog
        entry = ChangelogEntry(commits=[sha], user_facing=False)
        append_entry(fe_changes, entry)

        ctx = _build_ctx(root, _make_push_stdin(sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass", f"Expected pass, got {result.status}: {result.message}"

    def test_both_releasables_covered(self, releasable_monorepo):
        """Commits in both releasables, each covered by their respective changelog, passes."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]
        fe_changes = releasable_monorepo["fe_changes"]
        be_changes = releasable_monorepo["be_changes"]

        # Make commits in both releasables
        sha_fe = make_commit(root, "fe-lib/utils.py", "feat: frontend util")
        sha_be = make_commit(root, "be-api/handler.py", "feat: backend handler")

        # Cover each in the correct releasable changelog
        append_entry(fe_changes, ChangelogEntry(commits=[sha_fe], user_facing=False))
        append_entry(be_changes, ChangelogEntry(commits=[sha_be], user_facing=False))

        head_sha = git_head(root)
        ctx = _build_ctx(root, _make_push_stdin(head_sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass", f"Expected pass, got {result.status}: {result.message}"

    def test_multiple_commits_per_releasable_covered(self, releasable_monorepo):
        """Multiple commits across packages within a releasable all covered."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]
        fe_changes = releasable_monorepo["fe_changes"]

        # Two commits in different frontend packages
        sha1 = make_commit(root, "fe-app/page.py", "feat: new page")
        sha2 = make_commit(root, "fe-lib/helper.py", "feat: new helper")

        # Cover both in the frontend changelog
        append_entry(fe_changes, ChangelogEntry(commits=[sha1, sha2], user_facing=False))

        head_sha = git_head(root)
        ctx = _build_ctx(root, _make_push_stdin(head_sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass", f"Expected pass, got {result.status}: {result.message}"


# ------------------------------------------------------------------
# Test 2: fails when commit missing from its releasable's changelog
# ------------------------------------------------------------------


class TestPrepushReleasableMissingCoverage:
    """Pre-push check fails when a commit touching releasable-A's packages
    is missing from releasable-A's changelog."""

    def test_frontend_commit_uncovered_fails(self, releasable_monorepo):
        """A frontend commit with no frontend changelog entry fails."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]

        # Make a commit in the frontend releasable with no changelog entry
        sha = make_commit(root, "fe-app/feature.py", "feat: uncovered feature")

        ctx = _build_ctx(root, _make_push_stdin(sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail", f"Expected fail, got {result.status}: {result.message}"
        assert "frontend" in result.message

    def test_backend_commit_uncovered_fails(self, releasable_monorepo):
        """A backend commit with no backend changelog entry fails."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]

        # Make a commit in the backend releasable with no changelog entry
        sha = make_commit(root, "be-worker/task.py", "feat: uncovered task")

        ctx = _build_ctx(root, _make_push_stdin(sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail", f"Expected fail, got {result.status}: {result.message}"
        assert "backend" in result.message

    def test_one_releasable_covered_other_not(self, releasable_monorepo):
        """Frontend covered but backend uncovered: check fails for backend."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]
        fe_changes = releasable_monorepo["fe_changes"]

        # Make commits in both releasables
        sha_fe = make_commit(root, "fe-app/feature.py", "feat: frontend thing")
        sha_be = make_commit(root, "be-api/endpoint.py", "feat: backend thing")

        # Only cover the frontend commit
        append_entry(fe_changes, ChangelogEntry(commits=[sha_fe], user_facing=False))

        head_sha = git_head(root)
        ctx = _build_ctx(root, _make_push_stdin(head_sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail", f"Expected fail, got {result.status}: {result.message}"
        # The failure message should mention backend, not frontend
        assert "backend" in result.message
        assert "frontend" not in result.message


# ------------------------------------------------------------------
# Test 3: cross-releasable isolation (coverage in wrong releasable)
# ------------------------------------------------------------------


class TestPrepushReleasableIsolation:
    """Coverage in releasable-B's changelog does not satisfy releasable-A's commits."""

    def test_coverage_in_wrong_releasable_fails(self, releasable_monorepo):
        """A frontend commit covered only in the backend changelog still fails."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]
        be_changes = releasable_monorepo["be_changes"]

        # Make a commit in the frontend releasable
        sha = make_commit(root, "fe-lib/component.py", "feat: new component")

        # Cover it in the WRONG releasable's changelog (backend)
        append_entry(be_changes, ChangelogEntry(commits=[sha], user_facing=False))

        ctx = _build_ctx(root, _make_push_stdin(sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail", f"Expected fail, got {result.status}: {result.message}"
        assert "frontend" in result.message


# ------------------------------------------------------------------
# Test 4: commits outside all releasables are not checked
# ------------------------------------------------------------------


class TestPrepushReleasableRootCommits:
    """Commits touching files outside any releasable's packages pass."""

    def test_root_level_commit_passes(self, releasable_monorepo):
        """A commit only touching files outside any project path passes
        because no affected projects means nothing to check."""
        root = releasable_monorepo["root"]
        base_sha = releasable_monorepo["base_sha"]

        # Make a commit outside all project paths
        (root / "docs").mkdir(exist_ok=True)
        sha = make_commit(root, "docs/readme.md", "docs: update readme")

        ctx = _build_ctx(root, _make_push_stdin(sha, base_sha))
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        # No affected projects -> pass
        assert result.status == "pass", f"Expected pass, got {result.status}: {result.message}"
