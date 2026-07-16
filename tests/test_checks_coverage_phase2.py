"""Phase 2 coverage tests for check modules.

Targets:
- rlsbl/checks/prepush.py  (lines 34, 58, 65-87, 95, 98, 128-142)
- rlsbl/checks/project.py  (104 uncovered lines across many checks)
- rlsbl/checks/quality.py  (lines 28-56, 79-80, 151-176, 231-232, 271)
- rlsbl/checks/workspace.py (50 uncovered lines across many checks)
"""

import json
import os
import subprocess
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import git_head, make_commit, make_ctx, make_workspace, run_git

from strictcli import CheckResult

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.checks.scope import scope_adapter
from rlsbl.context import ProjectContext


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _run_check(name, ctx):
    """Run a check through the scope adapter, mirroring the real runtime.

    If the check has a scope and the scope adapter returns a CheckResult
    (e.g. skip), that result is returned directly. Otherwise the adapted
    context is passed to the check's impl function.
    """
    cdef = app._check_defs[name]
    check_ctx = ctx
    if cdef.scope:
        adapted = scope_adapter(ctx, cdef.scope)
        if isinstance(adapted, CheckResult):
            return adapted
        check_ctx = adapted
    return cdef.impl(check_ctx)


def _init_repo(repo):
    """Initialize a minimal git repo with one commit."""
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")


def _setup_scaffold(repo, config=None, tag=None):
    """Add .rlsbl/ scaffold to a repo."""
    if config is None:
        config = {"publish_mode": "ci", "targets": []}
    elif "targets" not in config:
        config = {**config, "targets": []}
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    (changes / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(json.dumps(config) + "\n")
    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold")
    if tag:
        run_git(repo, "tag", tag)


def _make_ws_ctx(repo, projects, graph=None, releasables=None, push_stdin=None):
    """Create a WorkspaceCheckContext."""
    ctx = WorkspaceCheckContext(
        project_root=Path(str(repo)),
        workspace_root=Path(str(repo)),
        config={},
        projects=projects,
        graph=graph,
        releasables=releasables or [],
    )
    if push_stdin is not None:
        ctx.push_stdin = push_stdin
    return ctx


# ==================================================================
# checks/prepush.py
# ==================================================================


class TestPrepushNoRefsParsed:
    """Line 34: _parse_stdin_refs returns None."""

    def test_no_refs_from_stdin_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        # Empty push_stdin that produces no refs
        ctx.push_stdin = ""

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "skip"
        assert "no refs" in result.message


class TestPrepushMonorepoPushedCommitsNone:
    """Line 58: _get_pushed_commits returns None in monorepo mode."""

    def test_pushed_commits_none_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        run_git(repo, "tag", "v0.0.0")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        changes = pkg / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci", "targets": ["npm"]}))

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        base_sha = git_head(repo)

        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "change")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_workspace
        projects = load_workspace(str(repo))

        ctx = _make_ws_ctx(
            repo, projects,
            push_stdin=f"refs/heads/main {head_sha} refs/heads/main {base_sha}",
        )

        with patch(
            "rlsbl.prepush_utils._get_pushed_commits",
            return_value=None,
        ):
            result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "skip"
        assert "could not determine pushed commits" in result.message


class TestPrepushExplicitReleasableMode:
    """Lines 65-87: explicit releasable mode monorepo paths."""

    def test_explicit_releasable_covered_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        run_git(repo, "tag", "v0.0.0")

        # Create project with releasable
        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')

        # Create releasable changes dir
        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir(exist_ok=True)
        rel_changes = ws_dir / "releasables" / "my-rel" / "changes"
        rel_changes.mkdir(parents=True)
        (rel_changes / "unreleased.jsonl").write_text("")

        # workspace.toml with [[releasables]]
        (ws_dir / "workspace.toml").write_text(
            '[[releasables]]\nname = "my-rel"\n\n'
            '[[projects]]\npath = "packages/alpha"\nname = "alpha"\n'
            'releasable = "my-rel"\n'
        )

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        run_git(repo, "tag", "alpha@v0.1.0")

        base_sha = git_head(repo)

        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: something")
        feat_sha = git_head(repo)

        # Write changelog entry covering the commit
        entry = json.dumps({
            "commits": [feat_sha],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        (rel_changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add entry")
        head_sha = git_head(repo)

        from rlsbl.workspace import Releasable, load_workspace
        projects = load_workspace(str(repo))
        releasables = [Releasable("my-rel")]

        ctx = _make_ws_ctx(
            repo, projects, releasables=releasables,
            push_stdin=f"refs/heads/main {head_sha} refs/heads/main {base_sha}",
        )

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_explicit_releasable_uncovered_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        run_git(repo, "tag", "v0.0.0")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')

        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir(exist_ok=True)
        rel_changes = ws_dir / "releasables" / "my-rel" / "changes"
        rel_changes.mkdir(parents=True)
        (rel_changes / "unreleased.jsonl").write_text("")

        (ws_dir / "workspace.toml").write_text(
            '[[releasables]]\nname = "my-rel"\n\n'
            '[[projects]]\npath = "packages/alpha"\nname = "alpha"\n'
            'releasable = "my-rel"\n'
        )

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        run_git(repo, "tag", "my-rel@v0.1.0")

        base_sha = git_head(repo)

        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: uncovered")
        head_sha = git_head(repo)

        from rlsbl.workspace import Releasable, load_workspace
        projects = load_workspace(str(repo))
        releasables = [Releasable("my-rel")]

        ctx = _make_ws_ctx(
            repo, projects, releasables=releasables,
            push_stdin=f"refs/heads/main {head_sha} refs/heads/main {base_sha}",
        )

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail"
        assert "my-rel" in result.message


class TestPrepushImplicitMonorepoEdgeCases:
    """Lines 95, 98: no changes dir and no project commits in implicit mode."""

    def test_implicit_no_changes_dir_skipped(self, tmp_path, monkeypatch):
        """Project without .rlsbl/changes/ is skipped in implicit monorepo mode."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        run_git(repo, "tag", "v0.0.0")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        # No .rlsbl/changes/ dir at all

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        base_sha = git_head(repo)

        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "change")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_workspace
        projects = load_workspace(str(repo))

        ctx = _make_ws_ctx(
            repo, projects,
            push_stdin=f"refs/heads/main {head_sha} refs/heads/main {base_sha}",
        )

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        # No changes dir -> project is skipped -> passes
        assert result.status == "pass"

    def test_implicit_no_project_commits_skipped(self, tmp_path, monkeypatch):
        """Project with changes dir but no matching commits -> skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        run_git(repo, "tag", "v0.0.0")

        # Two projects: alpha has changes dir, beta does not
        for name in ["alpha", "beta"]:
            pkg = repo / "packages" / name
            pkg.mkdir(parents=True)
            (pkg / "package.json").write_text(
                json.dumps({"name": name, "version": "0.1.0"}) + "\n"
            )

        changes = repo / "packages" / "alpha" / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (repo / "packages" / "alpha" / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["npm"]})
        )

        make_workspace(repo, [
            {"path": "packages/alpha", "name": "alpha"},
            {"path": "packages/beta", "name": "beta"},
        ])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        base_sha = git_head(repo)

        # Make a commit only in beta (alpha has changes dir but no commits)
        (repo / "packages" / "beta" / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/beta/index.js")
        run_git(repo, "commit", "-q", "-m", "change in beta")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_workspace
        projects = load_workspace(str(repo))

        ctx = _make_ws_ctx(
            repo, projects,
            push_stdin=f"refs/heads/main {head_sha} refs/heads/main {base_sha}",
        )

        # beta has no changes dir -> skipped; alpha has no commits -> skipped
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"


class TestPrepushGitignoreGuardExplicitMode:
    """Lines 128-142: gitignore guard in explicit releasable mode."""

    def test_gitignore_guard_explicit_mode_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')

        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        rel_changes = ws_dir / "releasables" / "my-rel" / "changes"
        rel_changes.mkdir(parents=True)
        (rel_changes / "unreleased.jsonl").write_text("")

        (ws_dir / "workspace.toml").write_text(
            '[[releasables]]\nname = "my-rel"\n\n'
            '[[projects]]\npath = "packages/alpha"\nname = "alpha"\n'
            'releasable = "my-rel"\n'
        )

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        from rlsbl.workspace import Releasable, load_workspace
        projects = load_workspace(str(repo))
        releasables = [Releasable("my-rel")]

        ctx = WorkspaceCheckContext(
            project_root=Path(str(pkg)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
            releasables=releasables,
        )

        result = app._check_defs["prepush-gitignore-guard"].impl(ctx)
        assert result.status == "pass"


class TestPrepushManualWarning:
    """Lines 161-173: prepush-manual-warning with pushed release branches."""

    def test_manual_push_to_release_branch_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo, config={"release_branches": ["main"]})
        head = git_head(repo)
        zero = "0" * 40
        ctx.push_stdin = f"refs/heads/main {head} refs/heads/main {zero}"

        result = app._check_defs["prepush-manual-warning"].impl(ctx)
        assert result.status == "warn"
        assert "main" in result.message

    def test_manual_push_not_release_branch_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo, config={"release_branches": ["main"]})
        head = git_head(repo)
        zero = "0" * 40
        ctx.push_stdin = f"refs/heads/develop {head} refs/heads/develop {zero}"

        result = app._check_defs["prepush-manual-warning"].impl(ctx)
        assert result.status == "pass"


# ==================================================================
# checks/project.py
# ==================================================================


class TestFindConflictedScaffoldFiles:
    """find_conflicted_scaffold_files edge cases."""

    def test_conflicted_file_detected(self, tmp_path, monkeypatch):
        from rlsbl.checks.project import find_conflicted_scaffold_files

        repo = tmp_path
        rlsbl_dir = repo / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            '<<<<<<< HEAD\n"old"\n=======\n"new"\n>>>>>>> branch\n'
        )
        result = find_conflicted_scaffold_files(repo)
        assert len(result) == 1
        assert result[0][0] == os.path.join(".rlsbl", "config.json")
        assert result[0][1] == 1  # first conflict marker on line 1

    def test_no_conflict_markers(self, tmp_path):
        from rlsbl.checks.project import find_conflicted_scaffold_files

        repo = tmp_path
        rlsbl_dir = repo / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"publish_mode": "ci"}\n')
        result = find_conflicted_scaffold_files(repo)
        assert result == []

    def test_malformed_managed_files_registry(self, tmp_path):
        from rlsbl.checks.project import find_conflicted_scaffold_files

        repo = tmp_path
        rlsbl_dir = repo / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "managed-files.json").write_text("not json{{{")
        (rlsbl_dir / "config.json").write_text("{}\n")
        # Should not crash
        result = find_conflicted_scaffold_files(repo)
        assert isinstance(result, list)

    def test_unicode_decode_error_skipped(self, tmp_path):
        from rlsbl.checks.project import find_conflicted_scaffold_files

        repo = tmp_path
        rlsbl_dir = repo / ".rlsbl"
        rlsbl_dir.mkdir()
        # Write a binary file that can't be decoded as UTF-8
        (rlsbl_dir / "binary.bin").write_bytes(b"\x80\x81\x82\x83")
        result = find_conflicted_scaffold_files(repo)
        assert result == []

    def test_workflows_dir_scanned(self, tmp_path):
        from rlsbl.checks.project import find_conflicted_scaffold_files

        repo = tmp_path
        (repo / ".rlsbl").mkdir()
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            '<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> branch\n'
        )
        result = find_conflicted_scaffold_files(repo)
        assert len(result) == 1
        assert "ci.yml" in result[0][0]

    def test_managed_files_registry_with_entries(self, tmp_path):
        from rlsbl.checks.project import find_conflicted_scaffold_files

        repo = tmp_path
        rlsbl_dir = repo / ".rlsbl"
        rlsbl_dir.mkdir()
        # Managed-files.json references a non-existent file (skipped)
        (rlsbl_dir / "managed-files.json").write_text(
            json.dumps({"files": {"missing-file.txt": {}}})
        )
        result = find_conflicted_scaffold_files(repo)
        assert result == []


class TestLockCheck:
    """check_lock: stale lock detection."""

    def test_no_lock_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = app._check_defs["lock"].impl(ctx)
        assert result.status == "pass"

    def test_stale_lock_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with patch("rlsbl.lock.is_stale", return_value=True):
            result = app._check_defs["lock"].impl(ctx)
        assert result.status == "warn"
        assert "stale" in result.message


class TestVersionConsistency:
    """version-consistency check."""

    def test_releasable_version_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with patch(
            "rlsbl.checks.project._get_releasable_version_for_project",
            return_value="1.2.3",
        ):
            result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "1.2.3" in result.message

    def test_no_targets_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.checks.project._get_releasable_version_for_project", return_value=None),
            patch("rlsbl.targets.detect_targets", return_value=[]),
        ):
            result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "no targets" in result.message

    def test_version_mismatch_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi", "npm"]})
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        (repo / "package.json").write_text('{"name": "test", "version": "0.2.0"}\n')
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "add", "package.json")
        run_git(repo, "commit", "-q", "-m", "add targets")

        ctx = make_ctx(repo)
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "fail"
        assert "mismatch" in result.message

    def test_selfdoc_json_read_error(self, tmp_path, monkeypatch):
        """Lines 188-191: selfdoc.json read error -> None version."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi"]})
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        # Write invalid selfdoc.json
        (repo / "selfdoc.json").write_text("not json{{{")
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "add", "selfdoc.json")
        run_git(repo, "commit", "-q", "-m", "add targets")

        ctx = make_ctx(repo)
        result = app._check_defs["version-consistency"].impl(ctx)
        # selfdoc version = None, pypi version = 0.1.0 -> only one unique version
        assert result.status == "pass"

    def test_no_versions_reported_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])

        mock_target = MagicMock()
        mock_target.read_version.side_effect = Exception("can't read")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.checks.project._get_releasable_version_for_project", return_value=None),
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "no targets reported a version" in result.message


class TestNameConsistency:
    """name-consistency check."""

    def test_no_targets_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[]):
            result = app._check_defs["name-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "no targets" in result.message

    def test_no_names_reported_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_target = MagicMock()
        mock_target.read_name.side_effect = Exception("can't read")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["name-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "no targets reported a name" in result.message

    def test_name_mismatch_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi", "npm"]})
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )
        (repo / "package.json").write_text('{"name": "beta", "version": "0.1.0"}\n')
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "add", "package.json")
        run_git(repo, "commit", "-q", "-m", "add targets")

        ctx = make_ctx(repo)
        result = app._check_defs["name-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "mismatch" in result.message

    def test_consistent_names_pass(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi"]})
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add target")

        ctx = make_ctx(repo)
        result = app._check_defs["name-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "mylib" in result.message

    def test_missing_name_still_passes(self, tmp_path, monkeypatch):
        """One target has a name, another raises -> missing list included."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_pypi = MagicMock()
        mock_pypi.read_name.return_value = "mylib"
        mock_npm = MagicMock()
        mock_npm.read_name.side_effect = Exception("no name")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[
                TargetEntry("pypi", str(repo)),
                TargetEntry("npm", str(repo)),
            ]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_pypi, "npm": mock_npm}),
        ):
            result = app._check_defs["name-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "no name from" in result.message


class TestLicenseConsistency:
    """license-consistency check."""

    def test_no_targets_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[]):
            result = app._check_defs["license-consistency"].impl(ctx)
        assert result.status == "pass"

    def test_single_license_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_target = MagicMock()
        mock_target.read_metadata.return_value = {"license": "MIT"}

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["license-consistency"].impl(ctx)
        assert result.status == "pass"

    def test_license_mismatch_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_pypi = MagicMock()
        mock_pypi.read_metadata.return_value = {"license": "MIT"}
        mock_npm = MagicMock()
        mock_npm.read_metadata.return_value = {"license": "Apache-2.0"}

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[
                TargetEntry("pypi", str(repo)), TargetEntry("npm", str(repo))
            ]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_pypi, "npm": mock_npm}),
        ):
            result = app._check_defs["license-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "mismatch" in result.message

    def test_consistent_licenses_pass(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_pypi = MagicMock()
        mock_pypi.read_metadata.return_value = {"license": "MIT"}
        mock_npm = MagicMock()
        mock_npm.read_metadata.return_value = {"license": "mit"}

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[
                TargetEntry("pypi", str(repo)), TargetEntry("npm", str(repo))
            ]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_pypi, "npm": mock_npm}),
        ):
            result = app._check_defs["license-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "MIT" in result.message

    def test_metadata_exception(self, tmp_path, monkeypatch):
        """read_metadata raises -> license not collected."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_target = MagicMock()
        mock_target.read_metadata.side_effect = Exception("oops")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["license-consistency"].impl(ctx)
        assert result.status == "pass"


class TestDescriptionConsistency:
    """description-consistency check."""

    def test_no_targets_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[]):
            result = app._check_defs["description-consistency"].impl(ctx)
        assert result.status == "pass"

    def test_single_description_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_target = MagicMock()
        mock_target.read_metadata.return_value = {"description": "A library"}

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["description-consistency"].impl(ctx)
        assert result.status == "pass"

    def test_description_mismatch_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_pypi = MagicMock()
        mock_pypi.read_metadata.return_value = {"description": "A library"}
        mock_npm = MagicMock()
        mock_npm.read_metadata.return_value = {"description": "A different library"}

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[
                TargetEntry("pypi", str(repo)), TargetEntry("npm", str(repo))
            ]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_pypi, "npm": mock_npm}),
        ):
            result = app._check_defs["description-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "mismatch" in result.message

    def test_consistent_descriptions_pass(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_pypi = MagicMock()
        mock_pypi.read_metadata.return_value = {"description": "A library"}
        mock_npm = MagicMock()
        mock_npm.read_metadata.return_value = {"description": "A library"}

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[
                TargetEntry("pypi", str(repo)), TargetEntry("npm", str(repo))
            ]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_pypi, "npm": mock_npm}),
        ):
            result = app._check_defs["description-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "A library" in result.message


class TestPrivateHookStale:
    """private-hook-stale check."""

    def test_no_hook_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "pass"
        assert "no post-release hook" in result.message

    def test_clean_hook_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        hooks_dir = repo / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "post-release.sh").write_text("#!/bin/bash\necho done\n")
        run_git(repo, "add", ".rlsbl/hooks/post-release.sh")
        run_git(repo, "commit", "-q", "-m", "add hook")

        ctx = make_ctx(repo)
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "pass"
        assert "no legacy" in result.message

    def test_stale_hook_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        hooks_dir = repo / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "post-release.sh").write_text(
            "#!/bin/bash\n# Post-release hook for private repositories\necho old\n"
        )
        run_git(repo, "add", ".rlsbl/hooks/post-release.sh")
        run_git(repo, "commit", "-q", "-m", "add stale hook")

        ctx = make_ctx(repo)
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "fail"
        assert "legacy" in result.message


class TestConfigSchema:
    """config-schema check."""

    def test_valid_config_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi"]})

        ctx = make_ctx(repo)
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "pass"

    def test_missing_private_key_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"some_key": True, "targets": ["pypi"]})

        ctx = make_ctx(repo)
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "fail"
        assert any("publish_mode" in d for d in result.details)

    def test_empty_targets_config_schema_fails(self, tmp_path, monkeypatch):
        """validate_config_schema catches targets: [] via config-schema check."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": []})

        ctx = make_ctx(repo)
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "fail"
        assert any("targets is an empty list" in d for d in result.details)

    def test_release_mode_config_schema_fails(self, tmp_path, monkeypatch):
        """validate_config_schema catches release.mode via config-schema check."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={
            "publish_mode": "ci", "targets": ["pypi"],
            "release": {"mode": "imperative"},
        })

        ctx = make_ctx(repo)
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "fail"
        assert any("release.mode" in d for d in result.details)


class TestLicenseFile:
    """license-file check."""

    def test_no_license_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "not found" in result.message

    def test_empty_license_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "LICENSE").write_text("")

        ctx = make_ctx(repo)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "empty" in result.message

    def test_template_vars_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "LICENSE").write_text("Copyright {{year}} by {{author.name}}\n")

        ctx = make_ctx(repo)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "template" in result.message

    def test_valid_license_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "LICENSE").write_text("MIT License\nCopyright 2024\n")

        ctx = make_ctx(repo)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "pass"

    def test_license_os_error(self, tmp_path, monkeypatch):
        """Line 412-413: os.path.getsize raises."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "LICENSE").write_text("content")

        ctx = make_ctx(repo)
        with patch("os.path.getsize", side_effect=OSError("denied")):
            result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "cannot read" in result.message


class TestPrivatePublishWorkflow:
    """publish-mode-workflow check."""

    def test_not_private_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci"})

        ctx = make_ctx(repo)
        result = app._check_defs["publish-mode-workflow"].impl(ctx)
        assert result.status == "pass"

    def test_missing_private_key_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"some_key": True})

        ctx = make_ctx(repo)
        result = app._check_defs["publish-mode-workflow"].impl(ctx)
        assert result.status == "fail"
        assert "publish_mode" in result.message

    def test_private_no_workflows_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "none"})

        ctx = make_ctx(repo)
        result = app._check_defs["publish-mode-workflow"].impl(ctx)
        assert result.status == "pass"

    def test_private_with_publish_workflow_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "none"})

        wf_dir = repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("on: push\n")
        run_git(repo, "add", ".github")
        run_git(repo, "commit", "-q", "-m", "add workflow")

        ctx = make_ctx(repo)
        result = app._check_defs["publish-mode-workflow"].impl(ctx)
        assert result.status == "fail"
        assert "publish" in result.message

    def test_private_with_release_trigger_workflow_fails(self, tmp_path, monkeypatch):
        """A workflow that contains release: and published triggers."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "none"})

        wf_dir = repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.yml").write_text(
            "on:\n  release:\n    types: [published]\njobs: {}\n"
        )
        run_git(repo, "add", ".github")
        run_git(repo, "commit", "-q", "-m", "add workflow")

        ctx = make_ctx(repo)
        result = app._check_defs["publish-mode-workflow"].impl(ctx)
        assert result.status == "fail"

    def test_private_with_clean_workflow_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "none"})

        wf_dir = repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("on: push\njobs:\n  test: {}\n")
        run_git(repo, "add", ".github")
        run_git(repo, "commit", "-q", "-m", "add workflow")

        ctx = make_ctx(repo)
        result = app._check_defs["publish-mode-workflow"].impl(ctx)
        assert result.status == "pass"


class TestNpmPrivateMismatch:
    """npm-private-mismatch check."""

    def test_no_package_json_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci"})

        ctx = make_ctx(repo)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "skip"

    def test_unreadable_package_json_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci"})
        (repo / "package.json").write_text("not json{{{")

        ctx = make_ctx(repo)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "skip"

    def test_npm_private_config_not_private_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci"})
        (repo / "package.json").write_text('{"name": "test", "version": "0.1.0", "private": true}\n')
        run_git(repo, "add", "package.json")
        run_git(repo, "commit", "-q", "-m", "add pkg")

        ctx = make_ctx(repo)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "fail"
        assert "private" in result.message

    def test_consistent_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci"})
        (repo / "package.json").write_text('{"name": "test", "version": "0.1.0"}\n')
        run_git(repo, "add", "package.json")
        run_git(repo, "commit", "-q", "-m", "add pkg")

        ctx = make_ctx(repo)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "pass"

    def test_missing_private_config_fails(self, tmp_path, monkeypatch):
        """config has no "private" key."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"some_key": True})
        (repo / "package.json").write_text('{"name": "test", "version": "0.1.0"}\n')
        run_git(repo, "add", "package.json")
        run_git(repo, "commit", "-q", "-m", "add pkg")

        ctx = make_ctx(repo)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "fail"
        assert "publish_mode" in result.message


class TestTargetVersionReadable:
    """target-version-readable check."""

    def test_no_targets_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[]):
            result = app._check_defs["target-version-readable"].impl(ctx)
        assert result.status == "skip"

    def test_readable_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi"]})
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add target")

        ctx = make_ctx(repo)
        result = app._check_defs["target-version-readable"].impl(ctx)
        assert result.status == "pass"

    def test_unreadable_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_target = MagicMock()
        mock_target.read_version.side_effect = Exception("corrupt")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["target-version-readable"].impl(ctx)
        assert result.status == "fail"
        assert "cannot read version" in result.message


class TestSelfdocVersionDrift:
    """selfdoc-version-drift check."""

    def test_no_selfdoc_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"

    def test_unreadable_selfdoc_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "selfdoc.json").write_text("not json{{{")

        ctx = make_ctx(repo)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"

    def test_no_version_in_selfdoc_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "selfdoc.json").write_text('{"name": "test"}\n')

        ctx = make_ctx(repo)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"

    def test_no_targets_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "selfdoc.json").write_text('{"version": "0.1.0"}\n')

        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[]):
            result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"

    def test_primary_version_exception_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "selfdoc.json").write_text('{"version": "0.1.0"}\n')

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_target = MagicMock()
        mock_target.read_version.side_effect = Exception("corrupt")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"

    def test_primary_version_none_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)
        (repo / "selfdoc.json").write_text('{"version": "0.1.0"}\n')

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        mock_target = MagicMock()
        mock_target.read_version.return_value = None

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.targets.TARGETS", {"pypi": mock_target}),
        ):
            result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"

    def test_version_matches_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi"]})
        (repo / "selfdoc.json").write_text('{"version": "0.1.0"}\n')
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "selfdoc.json")
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add")

        ctx = make_ctx(repo)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "pass"

    def test_version_drift_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo, config={"publish_mode": "ci", "targets": ["pypi"]})
        (repo / "selfdoc.json").write_text('{"version": "0.2.0"}\n')
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "selfdoc.json")
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add")

        ctx = make_ctx(repo)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "fail"
        assert "0.2.0" in result.message
        assert "0.1.0" in result.message


class TestScaffoldConflictsCheck:
    """scaffold-conflicts check."""

    def test_no_conflicts_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = app._check_defs["scaffold-conflicts"].impl(ctx)
        assert result.status == "pass"

    def test_conflicts_fail(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        (repo / ".rlsbl" / "hooks").mkdir(parents=True, exist_ok=True)
        (repo / ".rlsbl" / "hooks" / "pre-release.sh").write_text(
            "<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> branch\n"
        )

        ctx = make_ctx(repo)
        result = app._check_defs["scaffold-conflicts"].impl(ctx)
        assert result.status == "fail"
        assert "conflict" in result.message.lower()


class TestGetReleasableVersionForProject:
    """_get_releasable_version_for_project edge cases."""

    def test_no_workspace_root_returns_none(self, tmp_path, monkeypatch):
        from rlsbl.checks.project import _get_releasable_version_for_project

        ctx = ProjectContext(
            project_root=Path(str(tmp_path)),
            workspace_root=None,
            config={},
        )
        assert _get_releasable_version_for_project(ctx) is None

    def test_not_explicit_mode_returns_none(self, tmp_path, monkeypatch):
        from rlsbl.checks.project import _get_releasable_version_for_project

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        make_workspace(repo, [{"path": ".", "name": "test"}])

        ctx = ProjectContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
        )
        assert _get_releasable_version_for_project(ctx) is None


# ==================================================================
# checks/quality.py
# ==================================================================


class TestLibraryLint:
    """library-lint check.

    After the scope migration, library-lint relies on the scope adapter
    (``workspace:library``) to ensure the context is a WorkspaceCheckContext
    with ctx.projects pre-filtered to library projects only.
    """

    def test_not_in_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = _run_check("library-lint", ctx)
        assert result.status == "skip"

    def test_workspace_no_libraries_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        pkg = repo / "pkg"
        pkg.mkdir()
        (pkg / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n'
        )

        make_workspace(repo, [{"path": "pkg", "name": "pkg"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "pkg", "path": "pkg"})]
        ctx = _make_ws_ctx(repo, projects)
        # scope:library filters projects to [] since pkg has no library=True
        result = _run_check("library-lint", ctx)
        assert result.status == "pass"
        assert "no library" in result.message

    def test_workspace_load_failure_now_skips(self, tmp_path, monkeypatch):
        """After scope migration, library-lint no longer calls load_workspace.

        A non-workspace context is skipped by the scope adapter, so this
        test verifies that behavior instead of the old load_workspace error path.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = _run_check("library-lint", ctx)
        assert result.status == "skip"

    def test_library_lint_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        from rlsbl.lint.result import LintResult
        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "mylib", "path": "mylib", "library": True}),
        ]
        errors = [LintResult("file.py", 1, "R001", "error", "bad import")]

        # scope:library pre-filters to library projects; provide a WS context
        ctx = _make_ws_ctx(repo, projects)
        with patch("rlsbl.lint.lint_library", return_value=errors):
            result = _run_check("library-lint", ctx)
        assert result.status == "fail"
        assert "1 error" in result.message

    def test_library_lint_warnings_only(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        from rlsbl.lint.result import LintResult
        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "mylib", "path": "mylib", "library": True}),
        ]
        warnings = [LintResult("file.py", 1, "R001", "warning", "minor issue")]

        ctx = _make_ws_ctx(repo, projects)
        with patch("rlsbl.lint.lint_library", return_value=warnings):
            result = _run_check("library-lint", ctx)
        assert result.status == "warn"

    def test_library_lint_clean(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "mylib", "path": "mylib", "library": True}),
        ]

        ctx = _make_ws_ctx(repo, projects)
        with patch("rlsbl.lint.lint_library", return_value=[]):
            result = _run_check("library-lint", ctx)
        assert result.status == "pass"
        assert "clean" in result.message


class TestDeadModules:
    """dead-modules check."""

    def test_not_supported_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("swift", str(repo))]):
            result = app._check_defs["dead-modules"].impl(ctx)
        assert result.status == "skip"

    def test_pypi_dead_modules_found(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.dep_validation.find_dead_modules", return_value=["orphan.py"]),
        ):
            result = app._check_defs["dead-modules"].impl(ctx)
        assert result.status == "warn"
        assert "1 dead" in result.message

    def test_no_dead_modules(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.dep_validation.find_dead_modules", return_value=[]),
        ):
            result = app._check_defs["dead-modules"].impl(ctx)
        assert result.status == "pass"

    def test_workspace_context_excludes_siblings(self, tmp_path, monkeypatch):
        """Lines 79-80: workspace context with project -> sibling exclusion."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.project = projects[0]

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.dep_validation.find_dead_modules", return_value=[]),
        ):
            result = app._check_defs["dead-modules"].impl(ctx)
        assert result.status == "pass"


class TestCircularDeps:
    """circular-deps check."""

    def test_not_supported_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("go", str(repo))]):
            result = app._check_defs["circular-deps"].impl(ctx)
        assert result.status == "skip"

    def test_npm_cycles_fail(self, tmp_path, monkeypatch):
        """Lines 173-176: npm cycles produce fail (not warn)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("npm", str(repo))]),
            patch("rlsbl.dep_validation.find_circular_npm_deps", return_value=[["a", "b"]]),
        ):
            result = app._check_defs["circular-deps"].impl(ctx)
        assert result.status == "fail"
        assert "cycle" in result.message.lower()

    def test_python_cycles_warn(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.dep_validation.find_circular_python_deps", return_value=[["x", "y"]]),
        ):
            result = app._check_defs["circular-deps"].impl(ctx)
        assert result.status == "warn"

    def test_no_cycles_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.dep_validation.find_circular_python_deps", return_value=[]),
        ):
            result = app._check_defs["circular-deps"].impl(ctx)
        assert result.status == "pass"

    def test_workspace_context_excludes_siblings(self, tmp_path, monkeypatch):
        """Lines 151-152: workspace context -> sibling exclusion."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.project = projects[0]

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.dep_validation.find_circular_python_deps", return_value=[]),
        ):
            result = app._check_defs["circular-deps"].impl(ctx)
        assert result.status == "pass"


class TestScaffoldUnreplacedVars:
    """scaffold-unreplaced-vars check."""

    def test_no_template_vars_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "pass"

    def test_template_vars_found_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        wf_dir = repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("name: {{project.name}}\n")
        run_git(repo, "add", ".github")
        run_git(repo, "commit", "-q", "-m", "add workflow")

        ctx = make_ctx(repo)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "fail"
        assert "unreplaced" in result.message

    def test_docker_meta_lines_excluded(self, tmp_path, monkeypatch):
        """Lines 231-232: docker metadata lines are skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        wf_dir = repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "build.yml").write_text(
            "tags: type=semver,pattern={{version}}\n"
        )
        run_git(repo, "add", ".github")
        run_git(repo, "commit", "-q", "-m", "add workflow")

        ctx = make_ctx(repo)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "pass"

    def test_github_actions_syntax_excluded(self, tmp_path, monkeypatch):
        """${{ ... }} should not be flagged."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        wf_dir = repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("env: ${{ secrets.TOKEN }}\n")
        run_git(repo, "add", ".github")
        run_git(repo, "commit", "-q", "-m", "add workflow")

        ctx = make_ctx(repo)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "pass"


class TestTestSuiteCheck:
    """test-suite check."""

    def test_workspace_root_skips(self, tmp_path, monkeypatch):
        """Line 256-257: running at workspace root -> skip."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "a", "path": "a"})]

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        result = app._check_defs["test-suite"].impl(ctx)
        assert result.status == "skip"
        assert "workspace root" in result.message

    def test_no_recognized_target_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        with patch("rlsbl.targets.detect_targets", return_value=[]):
            result = app._check_defs["test-suite"].impl(ctx)
        assert result.status == "skip"

    def test_tests_pass(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.testing.run_project_tests", return_value=True),
        ):
            result = app._check_defs["test-suite"].impl(ctx)
        assert result.status == "pass"

    def test_tests_fail(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", str(repo))]),
            patch("rlsbl.testing.run_project_tests", return_value=False),
        ):
            result = app._check_defs["test-suite"].impl(ctx)
        assert result.status == "fail"


# ==================================================================
# checks/workspace.py
# ==================================================================


class TestWorkspaceCiRouter:
    """workspace-ci-router check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = _run_check("workspace-ci-router", ctx)
        assert result.status == "skip"

    def test_router_missing_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "a", "path": "a"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-ci-router"].impl(ctx)
        assert result.status == "fail"

    def test_router_exists_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci-router.yml").write_text("on: push\n")

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "a", "path": "a"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-ci-router"].impl(ctx)
        assert result.status == "pass"


class TestWorkspaceCiSynced:
    """workspace-ci-synced check: lines 46-61."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("workspace-ci-synced", ctx)
        assert result.status == "skip"

    @staticmethod
    def _write_router(repo, job_keys):
        """Write a minimal ci-router.yml with a detect job plus *job_keys*."""
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        lines = ["name: CI Router", "on: push", "jobs:", "  detect:", "    runs-on: ubuntu-latest"]
        for key in job_keys:
            lines.append(f"  {key}:")
            lines.append("    runs-on: ubuntu-latest")
        (wf / "ci-router.yml").write_text("\n".join(lines) + "\n")

    def test_missing_router_fails(self, tmp_path, monkeypatch):
        """No ci-router.yml at all -> fail."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-ci-synced"].impl(ctx)
        assert result.status == "fail"
        assert "ci-router.yml not found" in result.message

    def test_project_jobs_removed_from_router_fails(self, tmp_path, monkeypatch):
        """A router missing a project's inlined jobs -> fail naming the project."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Router has beta's jobs but not alpha's.
        self._write_router(repo, ["beta-ci-build"])

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
            WorkspaceProject({"name": "beta", "path": "beta"}),
        ]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-ci-synced"].impl(ctx)
        assert result.status == "fail"
        assert "alpha" in result.message
        assert "beta" not in result.message.replace("beta-ci", "")

    def test_freshly_synced_router_passes(self, tmp_path, monkeypatch):
        """A router with each project's inlined jobs -> pass."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        self._write_router(repo, ["alpha-ci-build", "alpha-ci-test", "beta-ci-build"])

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
            WorkspaceProject({"name": "beta", "path": "beta"}),
        ]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-ci-synced"].impl(ctx)
        assert result.status == "pass"

    def test_plain_target_skipped_no_ci_required(self, tmp_path, monkeypatch):
        """A project whose only detected target is 'plain' is skipped
        because plain has no ci_templates capability."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Router exists (with an unrelated detect job) so the check reaches the
        # per-project loop; the plain project must be skipped, not flagged.
        self._write_router(repo, [])

        proj_dir = repo / "myplain"
        proj_dir.mkdir()

        from rlsbl.targets import TargetEntry
        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "myplain", "path": "myplain"}),
        ]
        ctx = _make_ws_ctx(repo, projects)

        # Mock detect_targets to return plain for this project
        with patch(
            "rlsbl.targets.detect_targets",
            return_value=[TargetEntry("plain", str(proj_dir))],
        ):
            result = app._check_defs["workspace-ci-synced"].impl(ctx)

        assert result.status == "pass"
        assert "1 skipped" in result.message

    def test_dev_node_member_skipped_via_scope(self, tmp_path, monkeypatch):
        """A dev_node member is filtered out by the workspace:non_dev_node scope,
        so its absence from the router does not fail the check."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Router has alpha's jobs but NOT the dev_node member's.
        self._write_router(repo, ["alpha-ci-build"])

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
            WorkspaceProject({"name": "devtool", "path": "devtool", "dev_node": True}),
        ]
        ctx = _make_ws_ctx(repo, projects)

        # Run through the scope adapter (workspace:non_dev_node) as the real
        # runtime does -- the dev_node member must be filtered before the check.
        result = _run_check("workspace-ci-synced", ctx)
        assert result.status == "pass", f"{result.status}: {result.message}"


class TestWorkspaceTargets:
    """workspace-targets check: lines 69-83."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "skip"

    def test_no_targets_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        pkg = repo / "alpha"
        pkg.mkdir()

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-targets"].impl(ctx)
        assert result.status == "fail"
        assert "alpha" in result.message

    def test_all_have_targets_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        pkg = repo / "alpha"
        pkg.mkdir()
        (pkg / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-targets"].impl(ctx)
        assert result.status == "pass"


class TestWorkspaceUnregistered:
    """workspace-unregistered check: lines 88-155."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("workspace-unregistered", ctx)
        assert result.status == "skip"

    def test_unregistered_project_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Create a registered project
        alpha = repo / "alpha"
        alpha.mkdir()
        (alpha / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )

        # Create an unregistered project
        beta = repo / "beta"
        beta.mkdir()
        (beta / "pyproject.toml").write_text(
            '[project]\nname = "beta"\nversion = "0.1.0"\n'
        )

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add projects")

        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "fail"
        assert any("beta" in d for d in result.details)

    def test_all_registered_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        alpha = repo / "alpha"
        alpha.mkdir()
        (alpha / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add projects")

        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "pass"

    def test_private_npm_workspace_skipped(self, tmp_path, monkeypatch):
        """Lines 131-135: private npm workspace roots are skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Private npm workspace root (not a real project)
        pkgs = repo / "packages"
        pkgs.mkdir()
        (pkgs / "package.json").write_text('{"private": true, "workspaces": ["*"]}\n')

        from rlsbl.workspace import WorkspaceProject
        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]

        alpha = repo / "alpha"
        alpha.mkdir()
        (alpha / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add")

        ctx = _make_ws_ctx(repo, projects)
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        # "packages" dir should be skipped because its package.json has private:true
        assert result.status == "pass"

    def test_rlsbl_config_detected(self, tmp_path, monkeypatch):
        """Lines 124-126: directory with .rlsbl/config.json is detected."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # An unregistered directory with .rlsbl/config.json
        gamma = repo / "gamma"
        gamma.mkdir()
        rlsbl = gamma / ".rlsbl"
        rlsbl.mkdir()
        (rlsbl / "config.json").write_text('{"publish_mode": "ci", "targets": ["npm"]}\n')

        from rlsbl.workspace import WorkspaceProject
        projects = []  # No projects registered

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add")

        ctx = _make_ws_ctx(repo, projects)
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "fail"
        assert any("gamma" in d for d in result.details)


class TestWorkspaceStaleEntries:
    """workspace-stale-entries check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("workspace-stale-entries", ctx)
        assert result.status == "skip"

    def test_stale_entry_fails(self, tmp_path, monkeypatch):
        """Line 178: stale entry (dir doesn't exist)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "ghost", "path": "nonexistent"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "fail"
        assert "stale" in result.message

    def test_no_stale_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        alpha = repo / "alpha"
        alpha.mkdir()
        (alpha / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "pass"


class TestDevOnlyBoundary:
    """dev-only-boundary check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("dev-only-boundary", ctx)
        assert result.status == "skip"

    def test_no_dev_only_passes(self, tmp_path, monkeypatch):
        """Line 192: no dev-only projects -> pass."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["dev-only-boundary"].impl(ctx)
        assert result.status == "pass"
        assert "no dev-only" in result.message

    def test_boundary_violation_fails(self, tmp_path, monkeypatch):
        """Lines 213-214, 220: non-dev-only depends on dev-only."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "devtool", "path": "devtool", "dev_only": True}),
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
        ]

        mock_graph = MagicMock()
        mock_graph.transitive_rdeps.return_value = {"alpha"}

        ctx = _make_ws_ctx(repo, projects, graph=mock_graph)

        result = app._check_defs["dev-only-boundary"].impl(ctx)
        assert result.status == "fail"
        assert "boundary" in result.message


class TestSubtreeRemoteReachable:
    """subtree-remote-reachable check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("subtree-remote-reachable", ctx)
        assert result.status == "skip"

    def test_no_subtree_remotes_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["subtree-remote-reachable"].impl(ctx)
        assert result.status == "skip"

    def test_unreachable_remote_fails(self, tmp_path, monkeypatch):
        """Line 261: unreachable subtree remote."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({
            "name": "alpha", "path": "alpha",
            "subtree_remote": "https://nonexistent.invalid/repo.git",
        })]
        ctx = _make_ws_ctx(repo, projects)

        with patch("rlsbl.utils.run", side_effect=subprocess.CalledProcessError(1, "git")):
            result = app._check_defs["subtree-remote-reachable"].impl(ctx)
        assert result.status == "fail"
        assert "unreachable" in result.message

    def test_reachable_remote_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({
            "name": "alpha", "path": "alpha",
            "subtree_remote": "https://github.com/test/repo.git",
        })]
        ctx = _make_ws_ctx(repo, projects)

        with patch("rlsbl.utils.run", return_value=""):
            result = app._check_defs["subtree-remote-reachable"].impl(ctx)
        assert result.status == "pass"


class TestWorkspaceUnbuildable:
    """workspace-unbuildable check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("workspace-unbuildable", ctx)
        assert result.status == "skip"

    def test_no_pypi_targets_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        pkg = repo / "alpha"
        pkg.mkdir()
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "skip"


class TestLayersViolations:
    """layers-violations check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("layers-violations", ctx)
        assert result.status == "skip"

    def test_no_layer_config_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)

        with patch("rlsbl.layers.load_layer_config", return_value=None):
            result = app._check_defs["layers-violations"].impl(ctx)
        assert result.status == "skip"


class TestDeadWorkspacePackages:
    """dead-workspace-packages check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("dead-workspace-packages", ctx)
        assert result.status == "skip"


class TestDepsUnused:
    """deps-unused check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("deps-unused", ctx)
        assert result.status == "skip"


class TestDepsUndeclared:
    """deps-undeclared check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("deps-undeclared", ctx)
        assert result.status == "skip"


class TestDepsRuntimeTestOnly:
    """deps-runtime-test-only check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("deps-runtime-test-only", ctx)
        assert result.status == "skip"


class TestDepsDevInLib:
    """deps-dev-in-lib check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("deps-dev-in-lib", ctx)
        assert result.status == "skip"


class TestDepsStale:
    """deps-stale check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("deps-stale", ctx)
        assert result.status == "skip"


class TestTestSuiteWorkspace:
    """test-suite-workspace check."""

    def test_not_workspace_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        ctx = make_ctx(repo)
        result = _run_check("test-suite-workspace", ctx)
        assert result.status == "skip"

    def test_no_push_context_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = _make_ws_ctx(repo, projects)
        # push_stdin defaults to None

        result = app._check_defs["test-suite-workspace"].impl(ctx)
        assert result.status == "skip"
        assert "not in push context" in result.message


# ==================================================================
# Additional coverage: remaining uncovered lines
# ==================================================================


class TestLibraryLintFindWorkspaceException:
    """After scope migration, library-lint no longer calls find_workspace_root.

    The scope adapter handles workspace detection. This test verifies
    non-workspace contexts are properly skipped.
    """

    def test_find_workspace_root_exception_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        ctx = make_ctx(repo)
        result = _run_check("library-lint", ctx)
        assert result.status == "skip"


class TestCircularDepsGo:
    """circular-deps check for dart target (quality.py lines 173-176)."""

    def test_dart_cycles_warn(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("dart", str(repo))]),
            patch("rlsbl.dep_validation.find_circular_dart_deps", return_value=[["x", "y"]]),
        ):
            result = app._check_defs["circular-deps"].impl(ctx)
        assert result.status == "warn"
        assert "cycle" in result.message.lower()


class TestDeadModulesMultiTarget:
    """dead-modules with multiple target types (go, npm, dart)."""

    def test_go_dead_packages(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("go", str(repo))]),
            patch("rlsbl.dep_validation.find_dead_go_packages", return_value=["internal/old"]),
        ):
            result = app._check_defs["dead-modules"].impl(ctx)
        assert result.status == "warn"

    def test_npm_dead_modules(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("npm", str(repo))]),
            patch("rlsbl.dep_validation.find_dead_npm_modules", return_value=["lib/unused.js"]),
        ):
            result = app._check_defs["dead-modules"].impl(ctx)
        assert result.status == "warn"

    def test_dart_dead_modules(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)
        _setup_scaffold(repo)

        TargetEntry = namedtuple("TargetEntry", ["name", "path"])
        ctx = make_ctx(repo)
        with (
            patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("dart", str(repo))]),
            patch("rlsbl.dep_validation.find_dead_dart_modules", return_value=["lib/dead.dart"]),
        ):
            result = app._check_defs["dead-modules"].impl(ctx)
        assert result.status == "warn"


class TestDevOnlyBoundaryEdgeCases:
    """dev-only-boundary edge cases."""

    def test_transitive_rdeps_key_error(self, tmp_path, monkeypatch):
        """Lines 213-214: transitive_rdeps raises KeyError -> skip that scope."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "devtool", "path": "devtool", "dev_only": True}),
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
        ]

        mock_graph = MagicMock()
        mock_graph.transitive_rdeps.side_effect = KeyError("not found")

        ctx = _make_ws_ctx(repo, projects, graph=mock_graph)

        result = app._check_defs["dev-only-boundary"].impl(ctx)
        # KeyError caught -> no dependents -> pass
        assert result.status == "pass"
        assert "clean" in result.message

    def test_dependent_not_in_projects(self, tmp_path, monkeypatch):
        """Line 220: dependent name not found in projects_by_name."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "devtool", "path": "devtool", "dev_only": True}),
        ]

        mock_graph = MagicMock()
        # Returns a project name that doesn't exist in our project list
        mock_graph.transitive_rdeps.return_value = {"ghost_project"}

        ctx = _make_ws_ctx(repo, projects, graph=mock_graph)

        result = app._check_defs["dev-only-boundary"].impl(ctx)
        # ghost_project not in projects_by_name -> continue -> pass
        assert result.status == "pass"


class TestWorkspaceUnregisteredGitignore:
    """workspace-unregistered gitignore handling (lines 105-107, 119)."""

    def test_gitignored_dir_skipped(self, tmp_path, monkeypatch):
        """Line 119: gitignored directory is skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Create a gitignored directory with a manifest
        ignored = repo / "vendor"
        ignored.mkdir()
        (ignored / "pyproject.toml").write_text(
            '[project]\nname = "vendor"\nversion = "0.1.0"\n'
        )

        (repo / ".gitignore").write_text("vendor/\n")
        run_git(repo, "add", ".gitignore")
        run_git(repo, "commit", "-q", "-m", "add gitignore")

        from rlsbl.workspace import WorkspaceProject

        projects = []
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-unregistered"].impl(ctx)
        # vendor is gitignored -> should not be reported as unregistered
        assert result.status == "pass"


class TestWorkspaceStaleEntriesManifestless:
    """workspace-stale-entries: directory exists but has no manifest (line 178)."""

    def test_dir_exists_no_manifest_is_stale(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Directory exists but has no manifest files
        empty_proj = repo / "empty"
        empty_proj.mkdir()

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "empty", "path": "empty"})]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "fail"
        assert "stale" in result.message


class TestWorkspaceDepsChecksWithGraph:
    """Workspace dependency checks with a mock graph."""

    def _make_deps_ctx(self, repo, projects, graph):
        ctx = _make_ws_ctx(repo, projects, graph=graph)
        return ctx

    def test_deps_unused_with_errors(self, tmp_path, monkeypatch):
        """deps-unused check with actual unused deps."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]

        Dep = namedtuple("Dep", ["name", "scope", "dep_type", "constraint"])
        mock_graph = MagicMock()
        # Return deps with different scopes to test precedence (line 354)
        mock_graph.dependencies.return_value = [
            Dep("beta", "runtime", "versioned", ">=0.1.0"),
            Dep("beta", "dev", "versioned", ">=0.1.0"),  # duplicate, runtime takes precedence
        ]

        alpha_dir = repo / "alpha"
        alpha_dir.mkdir()

        ctx = self._make_deps_ctx(repo, projects, mock_graph)

        with (
            patch("rlsbl.dep_validation.load_dep_overrides", return_value={}),
            patch("rlsbl.checks._common._build_dep_import_cache", return_value={
                "alpha": (set(), set(), set()),
            }),
            patch("rlsbl.dep_validation.check_unused_deps", return_value=[
                "alpha: 'beta' declared but not imported",
            ]),
        ):
            result = app._check_defs["deps-unused"].impl(ctx)
        assert result.status == "fail"
        assert "unused" in result.message

    def test_deps_undeclared_with_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]

        Dep = namedtuple("Dep", ["name", "scope", "dep_type", "constraint"])
        mock_graph = MagicMock()
        mock_graph.dependencies.return_value = []

        alpha_dir = repo / "alpha"
        alpha_dir.mkdir()

        ctx = self._make_deps_ctx(repo, projects, mock_graph)

        with (
            patch("rlsbl.checks._common._build_dep_import_cache", return_value={
                "alpha": (set(), set(), set()),
            }),
            patch("rlsbl.dep_validation.check_undeclared_deps", return_value=[
                "alpha: imports 'gamma' which is not declared",
            ]),
        ):
            result = app._check_defs["deps-undeclared"].impl(ctx)
        assert result.status == "fail"

    def test_deps_runtime_test_only_flagged(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]

        Dep = namedtuple("Dep", ["name", "scope", "dep_type", "constraint"])
        mock_graph = MagicMock()
        mock_graph.dependencies.return_value = [
            Dep("beta", "runtime", "versioned", ">=0.1.0"),
        ]

        ctx = self._make_deps_ctx(repo, projects, mock_graph)

        with (
            patch("rlsbl.checks._common._build_dep_import_cache", return_value={
                "alpha": (set(), {"beta"}, set()),
            }),
            patch("rlsbl.dep_validation.check_runtime_test_only", return_value=["beta"]),
        ):
            result = app._check_defs["deps-runtime-test-only"].impl(ctx)
        assert result.status == "warn"
        assert "runtime dep" in result.message

    def test_deps_dev_in_lib_flagged(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]

        Dep = namedtuple("Dep", ["name", "scope", "dep_type", "constraint"])
        mock_graph = MagicMock()
        mock_graph.dependencies.return_value = [
            Dep("beta", "dev", "versioned", ">=0.1.0"),
        ]

        ctx = self._make_deps_ctx(repo, projects, mock_graph)

        with (
            patch("rlsbl.checks._common._build_dep_import_cache", return_value={
                "alpha": ({"beta"}, set(), set()),
            }),
            patch("rlsbl.dep_validation.check_dev_in_lib", return_value=["beta"]),
        ):
            result = app._check_defs["deps-dev-in-lib"].impl(ctx)
        assert result.status == "fail"
        assert "dev dep" in result.message

    def test_deps_stale_with_outdated(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        alpha_dir = repo / "alpha"
        alpha_dir.mkdir()
        (alpha_dir / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )
        beta_dir = repo / "beta"
        beta_dir.mkdir()
        (beta_dir / "pyproject.toml").write_text(
            '[project]\nname = "beta"\nversion = "0.2.0"\n'
        )

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
            WorkspaceProject({"name": "beta", "path": "beta"}),
        ]

        Dep = namedtuple("Dep", ["name", "scope", "dep_type", "constraint"])
        mock_graph = MagicMock()
        mock_graph.dependencies.side_effect = lambda name: (
            [Dep("beta", "runtime", "versioned", "<0.2.0")]
            if name == "alpha" else []
        )

        ctx = self._make_deps_ctx(repo, projects, mock_graph)

        with patch(
            "rlsbl.constraints._evaluate_constraint",
            return_value="outdated",
        ):
            result = app._check_defs["deps-stale"].impl(ctx)
        assert result.status == "fail"
        assert "stale" in result.message

    def test_deps_stale_all_current(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        alpha_dir = repo / "alpha"
        alpha_dir.mkdir()
        (alpha_dir / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
        ]

        mock_graph = MagicMock()
        mock_graph.dependencies.return_value = []

        ctx = self._make_deps_ctx(repo, projects, mock_graph)

        result = app._check_defs["deps-stale"].impl(ctx)
        assert result.status == "pass"


class TestWorkspaceLayersViolationsFail:
    """layers-violations check with violations."""

    def test_violations_fail(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        mock_graph = MagicMock()

        ctx = _make_ws_ctx(repo, projects, graph=mock_graph)

        mock_config = MagicMock()
        with (
            patch("rlsbl.layers.load_layer_config", return_value=mock_config),
            patch("rlsbl.layers.check_layer_violations", return_value=[
                "alpha -> beta violates layer boundary",
            ]),
        ):
            result = app._check_defs["layers-violations"].impl(ctx)
        assert result.status == "fail"
        assert "violation" in result.message

    def test_no_violations_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        mock_graph = MagicMock()

        ctx = _make_ws_ctx(repo, projects, graph=mock_graph)

        mock_config = MagicMock()
        with (
            patch("rlsbl.layers.load_layer_config", return_value=mock_config),
            patch("rlsbl.layers.check_layer_violations", return_value=[]),
        ):
            result = app._check_defs["layers-violations"].impl(ctx)
        assert result.status == "pass"


class TestDeadWorkspacePackagesFound:
    """dead-workspace-packages check with dead packages."""

    def test_dead_packages_warned(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha", "library": True}),
        ]

        ctx = _make_ws_ctx(repo, projects)

        dead_result = MagicMock()
        dead_result.message = "alpha: library not imported by any sibling"

        with (
            patch("rlsbl.checks._common._build_dep_import_cache", return_value={}),
            patch("rlsbl.dep_validation.find_dead_workspace_packages", return_value=[dead_result]),
        ):
            result = app._check_defs["dead-workspace-packages"].impl(ctx)
        assert result.status == "warn"

    def test_no_dead_packages(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        from rlsbl.workspace import WorkspaceProject

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]

        ctx = _make_ws_ctx(repo, projects)

        with (
            patch("rlsbl.checks._common._build_dep_import_cache", return_value={}),
            patch("rlsbl.dep_validation.find_dead_workspace_packages", return_value=[]),
        ):
            result = app._check_defs["dead-workspace-packages"].impl(ctx)
        assert result.status == "pass"


class TestWorkspaceCiSyncedEdge:
    """workspace-ci-synced additional: partial inline in the router."""

    def test_partial_missing_router_jobs(self, tmp_path, monkeypatch):
        """One project inlined in the router, one missing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        TestWorkspaceCiSynced._write_router(repo, ["alpha-ci-build"])
        # beta's jobs are absent from the router

        from rlsbl.workspace import WorkspaceProject

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
            WorkspaceProject({"name": "beta", "path": "beta"}),
        ]
        ctx = _make_ws_ctx(repo, projects)

        result = app._check_defs["workspace-ci-synced"].impl(ctx)
        assert result.status == "fail"
        assert "beta" in result.message
