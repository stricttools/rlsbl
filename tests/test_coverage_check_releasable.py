"""Tests for changelog checks reading releasable-level JSONL from workspace root.

Covers:
- rlsbl check --tag changelog from workspace root reads releasable JSONL
- Covered commits in releasable JSONL are not reported as uncovered
- Works with 2+ releasables (each checked independently)
- Implicit-mode workspace doesn't crash
- Workspace root with path = "." project still works (regression)
"""

import json
import os
from pathlib import Path

import pytest

from conftest import (
    git_head,
    make_commit,
    make_workspace,
    run_git,
)

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.workspace import (
    Releasable,
    WorkspaceProject,
    get_releasable_changes_dir,
    load_workspace,
    save_workspace,
    write_releasable_version,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _setup_releasable_monorepo(
    repo, *, releasables, projects, initial_version="0.1.0"
):
    """Set up a monorepo with explicit releasables and return a WorkspaceCheckContext.

    Creates git repo, writes workspace.toml with [[releasables]] and
    [[projects]], sets up per-releasable state dirs, commits, and tags.
    """
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")

    # Write workspace.toml with releasables
    save_workspace(str(repo), projects, releasables=releasables)

    # Create per-project directories with minimal project files
    for proj in projects:
        proj_dir = repo / proj["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{proj["name"]}"\nversion = "{initial_version}"\n'
        )
        rlsbl_dir = proj_dir / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"private": False, "targets": ["pypi"]}) + "\n"
        )

    # Set up per-releasable state directories
    for rel in releasables:
        write_releasable_version(str(repo), rel.name, initial_version)
        changes_dir = get_releasable_changes_dir(str(repo), rel.name)
        os.makedirs(changes_dir, exist_ok=True)
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write("")

    # Commit all files
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "scaffold monorepo")

    # Tag each releasable
    for rel in releasables:
        tag = rel.tag_format.format(name=rel.name, version=initial_version)
        run_git(repo, "tag", tag)

    return releasables, projects


def _make_workspace_ctx(repo, releasables, projects=None):
    """Create a WorkspaceCheckContext for the given repo at workspace root."""
    if projects is None:
        projects = load_workspace(str(repo))
    return WorkspaceCheckContext(
        project_root=Path(str(repo)),
        workspace_root=Path(str(repo)),
        config={},
        projects=projects,
        graph=None,
        releasables=releasables,
    )


# ==================================================================
# Test 1: changelog checks from workspace root read releasable JSONL
# ==================================================================


class TestWorkspaceRootReadsReleasableJSONL:
    """rlsbl check --tag changelog from workspace root reads releasable JSONL."""

    def test_coverage_check_reads_releasable_jsonl(self, tmp_path, monkeypatch):
        """Coverage check at workspace root finds entries in releasable changes dir."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="alpha")]
        projects = [
            {"path": "libs/core", "name": "core", "releasable": "alpha"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)

        # Make a commit touching the project
        core_dir = repo / "libs" / "core"
        (core_dir / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "libs/core/src.py")
        run_git(repo, "commit", "-q", "-m", "feat: add src")
        head = git_head(repo)

        # Write entry in releasable changes dir (NOT per-package)
        changes_dir = get_releasable_changes_dir(str(repo), "alpha")
        entry = json.dumps({
            "commits": [head],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(entry + "\n")
        run_git(repo, "add", os.path.join(changes_dir, "unreleased.jsonl"))
        run_git(repo, "commit", "-q", "-m", "add changelog entry")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass", f"Expected pass, got {result.status}: {result.message}"

    def test_hashes_check_reads_releasable_jsonl(self, tmp_path, monkeypatch):
        """Hashes check at workspace root reads from releasable changes dir."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="alpha")]
        projects = [
            {"path": "libs/core", "name": "core", "releasable": "alpha"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)

        # Make a commit and add valid entry
        core_dir = repo / "libs" / "core"
        (core_dir / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "libs/core/src.py")
        run_git(repo, "commit", "-q", "-m", "feat: add src")
        head = git_head(repo)

        changes_dir = get_releasable_changes_dir(str(repo), "alpha")
        entry = json.dumps({
            "commits": [head],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(entry + "\n")
        run_git(repo, "add", os.path.join(changes_dir, "unreleased.jsonl"))
        run_git(repo, "commit", "-q", "-m", "add changelog entry")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "pass"


# ==================================================================
# Test 2: Covered commits in releasable JSONL are not uncovered
# ==================================================================


class TestCoveredCommitsNotReportedUncovered:
    """Covered commits in releasable JSONL are not reported as uncovered."""

    def test_covered_commit_passes(self, tmp_path, monkeypatch):
        """A commit covered by a releasable JSONL entry is not flagged."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="alpha")]
        projects = [
            {"path": "libs/core", "name": "core", "releasable": "alpha"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)

        # Make a commit touching the project
        core_dir = repo / "libs" / "core"
        (core_dir / "feat.py").write_text("y = 2\n")
        run_git(repo, "add", "libs/core/feat.py")
        run_git(repo, "commit", "-q", "-m", "feat: covered change")
        covered_sha = git_head(repo)

        # Cover it in releasable JSONL
        changes_dir = get_releasable_changes_dir(str(repo), "alpha")
        entry = json.dumps({
            "commits": [covered_sha],
            "user_facing": True,
            "description": "covered feature",
            "type": "feature",
        })
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(entry + "\n")
        run_git(repo, "add", os.path.join(changes_dir, "unreleased.jsonl"))
        run_git(repo, "commit", "-q", "-m", "add entry")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_uncovered_commit_fails(self, tmp_path, monkeypatch):
        """An uncovered commit in a releasable is flagged from workspace root."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="alpha")]
        projects = [
            {"path": "libs/core", "name": "core", "releasable": "alpha"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)

        # Make a commit but do NOT add a changelog entry
        core_dir = repo / "libs" / "core"
        (core_dir / "uncovered.py").write_text("z = 3\n")
        run_git(repo, "add", "libs/core/uncovered.py")
        run_git(repo, "commit", "-q", "-m", "feat: uncovered change")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "fail"


# ==================================================================
# Test 3: Works with 2+ releasables (each checked independently)
# ==================================================================


class TestMultipleReleasables:
    """Works with 2+ releasables -- each checked independently."""

    def test_two_releasables_both_covered(self, tmp_path, monkeypatch):
        """Two releasables, each with a covered commit, should pass."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="alpha"), Releasable(name="beta")]
        projects = [
            {"path": "libs/alpha-core", "name": "alpha-core", "releasable": "alpha"},
            {"path": "libs/beta-api", "name": "beta-api", "releasable": "beta"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)

        # Commit in alpha
        alpha_dir = repo / "libs" / "alpha-core"
        (alpha_dir / "feat.py").write_text("a = 1\n")
        run_git(repo, "add", "libs/alpha-core/feat.py")
        run_git(repo, "commit", "-q", "-m", "feat: alpha change")
        alpha_sha = git_head(repo)

        # Commit in beta
        beta_dir = repo / "libs" / "beta-api"
        (beta_dir / "feat.py").write_text("b = 2\n")
        run_git(repo, "add", "libs/beta-api/feat.py")
        run_git(repo, "commit", "-q", "-m", "feat: beta change")
        beta_sha = git_head(repo)

        # Cover both
        alpha_changes = get_releasable_changes_dir(str(repo), "alpha")
        with open(os.path.join(alpha_changes, "unreleased.jsonl"), "w") as f:
            f.write(json.dumps({
                "commits": [alpha_sha],
                "user_facing": True,
                "description": "alpha feature",
                "type": "feature",
            }) + "\n")

        beta_changes = get_releasable_changes_dir(str(repo), "beta")
        with open(os.path.join(beta_changes, "unreleased.jsonl"), "w") as f:
            f.write(json.dumps({
                "commits": [beta_sha],
                "user_facing": True,
                "description": "beta feature",
                "type": "feature",
            }) + "\n")

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add entries")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_two_releasables_one_uncovered(self, tmp_path, monkeypatch):
        """Two releasables, one with uncovered commit, should fail."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="alpha"), Releasable(name="beta")]
        projects = [
            {"path": "libs/alpha-core", "name": "alpha-core", "releasable": "alpha"},
            {"path": "libs/beta-api", "name": "beta-api", "releasable": "beta"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)

        # Commit in alpha (covered)
        alpha_dir = repo / "libs" / "alpha-core"
        (alpha_dir / "feat.py").write_text("a = 1\n")
        run_git(repo, "add", "libs/alpha-core/feat.py")
        run_git(repo, "commit", "-q", "-m", "feat: alpha change")
        alpha_sha = git_head(repo)

        alpha_changes = get_releasable_changes_dir(str(repo), "alpha")
        with open(os.path.join(alpha_changes, "unreleased.jsonl"), "w") as f:
            f.write(json.dumps({
                "commits": [alpha_sha],
                "user_facing": True,
                "description": "alpha feature",
                "type": "feature",
            }) + "\n")

        # Commit in beta (NOT covered)
        beta_dir = repo / "libs" / "beta-api"
        (beta_dir / "feat.py").write_text("b = 2\n")
        run_git(repo, "add", "libs/beta-api/feat.py")
        run_git(repo, "commit", "-q", "-m", "feat: beta change")

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add entries")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "fail"


# ==================================================================
# Test 4: Implicit-mode workspace doesn't crash
# ==================================================================


class TestImplicitModeNoCrash:
    """Implicit-mode workspace (no [[releasables]]) doesn't crash."""

    def test_implicit_mode_context_factory_no_crash(self, tmp_path, monkeypatch):
        """_check_context_factory doesn't crash for implicit-mode workspace."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")

        # Create implicit-mode workspace (no [[releasables]])
        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        changes = pkg / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"private": False, "targets": ["npm"]}))

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        run_git(repo, "tag", "alpha@v0.1.0")

        # The factory should not crash even though there's no [[releasables]]
        from rlsbl import _check_context_factory
        ctx = _check_context_factory()
        assert ctx is not None
        # releasables should be an empty list in implicit mode
        assert ctx.releasables == []

    def test_implicit_mode_changelog_coverage_works(self, tmp_path, monkeypatch):
        """Changelog coverage check works in implicit mode at project dir."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        changes = pkg / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"private": False, "targets": ["npm"]}))

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        run_git(repo, "tag", "alpha@v0.1.0")

        # Make a commit and cover it
        (pkg / "src.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/src.js")
        run_git(repo, "commit", "-q", "-m", "feat: new feature")
        head = git_head(repo)

        entry = json.dumps({
            "commits": [head],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", "packages/alpha/.rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "add entry")

        # Check from the package directory (implicit mode)
        projects = load_workspace(str(repo))
        monkeypatch.chdir(pkg)
        ctx = WorkspaceCheckContext(
            project_root=Path(str(pkg)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
            releasables=[],
        )
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass"


# ==================================================================
# Test 5: Workspace root with path = "." project still works
# ==================================================================


class TestWorkspaceRootWithDotProject:
    """Workspace root with path = '.' project still works (regression)."""

    def test_dot_path_project_explicit_mode(self, tmp_path, monkeypatch):
        """Project with path='.' in explicit mode resolves correctly from root."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="main-rel")]
        projects = [
            {"path": ".", "name": "root-pkg", "releasable": "main-rel"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)

        # Make a commit touching a file in the root project
        (repo / "lib.py").write_text("x = 1\n")
        run_git(repo, "add", "lib.py")
        run_git(repo, "commit", "-q", "-m", "feat: root feature")
        head = git_head(repo)

        # Cover it in the releasable's changes dir
        changes_dir = get_releasable_changes_dir(str(repo), "main-rel")
        entry = json.dumps({
            "commits": [head],
            "user_facing": True,
            "description": "root feature",
            "type": "feature",
        })
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(entry + "\n")
        run_git(repo, "add", os.path.join(changes_dir, "unreleased.jsonl"))
        run_git(repo, "commit", "-q", "-m", "add entry")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_dot_path_project_implicit_mode(self, tmp_path, monkeypatch):
        """Project with path='.' in implicit mode resolves correctly from root."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")

        # Create implicit-mode workspace with path = "."
        (repo / "package.json").write_text('{"name": "root-pkg", "version": "0.1.0"}\n')
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (repo / ".rlsbl" / "config.json").write_text(json.dumps({"private": False, "targets": ["npm"]}))

        make_workspace(repo, [{"path": ".", "name": "root-pkg"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        run_git(repo, "tag", "root-pkg@v0.1.0")

        # Make a commit and cover it
        (repo / "lib.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "lib.js")
        run_git(repo, "commit", "-q", "-m", "feat: root feature")
        head = git_head(repo)

        entry = json.dumps({
            "commits": [head],
            "user_facing": True,
            "description": "root feature",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "add entry")

        projects = load_workspace(str(repo))
        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
            releasables=[],
        )
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass"
