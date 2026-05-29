"""Tests for `rlsbl scaffold --dry-run`: plan-only mode that writes nothing."""

import json
import os
import subprocess
from io import StringIO
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import run_cmd, run_cmd_multi
from rlsbl.context import ProjectContext


def _ctx(root="."):
    """Create a minimal ProjectContext for scaffold tests."""
    from pathlib import Path
    return ProjectContext(project_root=Path(root), workspace_root=None, config={})


@pytest.fixture
def npm_project(mock_git_repo):
    """Set up a minimal npm project."""
    pkg = {"name": "dryrunpkg", "version": "0.1.0"}
    (mock_git_repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    return mock_git_repo


@pytest.fixture
def dual_registry_project(mock_git_repo):
    """Set up a project with both package.json and pyproject.toml."""
    pkg = {"name": "my-dual-dry", "version": "0.2.0"}
    (mock_git_repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    (mock_git_repo / "pyproject.toml").write_text(
        "[project]\nname = \"my-dual-dry\"\nversion = \"0.2.0\"\n"
    )
    return mock_git_repo


def _git_porcelain(repo):
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout


class TestDryRunFreshProject:
    """Dry-run on a fresh project must show planned creates but write nothing."""

    def test_dry_run_writes_no_files(self, npm_project, capsys):
        """No template-target files exist after dry-run."""
        run_cmd("npm", [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        # Confirm no scaffold-generated files exist
        assert not os.path.exists(".github/workflows/ci.yml")
        assert not os.path.exists(".github/workflows/publish.yml")
        assert not os.path.exists(".rlsbl/hooks/pre-release.sh")
        assert not os.path.exists("LICENSE")
        assert not os.path.exists("CHANGELOG.md")
        assert not os.path.exists(".rlsbl/version")
        assert not os.path.exists(".rlsbl/hashes.json")

    def test_dry_run_prints_planned_files(self, npm_project, capsys):
        """Output contains the planned file status table and a DRY RUN banner."""
        run_cmd("npm", [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "Files:" in out
        # Some core scaffold targets should appear
        assert ".github/workflows/ci.yml" in out
        assert "LICENSE" in out

    def test_dry_run_does_not_create_rlsbl_config(self, npm_project):
        """--dry-run must NOT create or write .rlsbl/config.json."""
        run_cmd("npm", [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        assert not os.path.exists(".rlsbl/config.json")

    def test_dry_run_does_not_install_pre_push_hook(self, npm_project):
        """--dry-run must NOT install the .git/hooks/pre-push hook."""
        hook_path = npm_project / ".git" / "hooks" / "pre-push"
        assert not hook_path.exists()  # baseline

        run_cmd("npm", [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        assert not hook_path.exists(), "Dry run must not install hooks"

    def test_dry_run_does_not_commit(self, npm_project):
        """--dry-run must NOT auto-commit. Working tree may be dirty but only
        with files that pre-existed (e.g. package.json) -- it must not commit
        anything scaffolded.
        """
        # baseline: log all commit hashes
        before = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(npm_project),
            capture_output=True, text=True, check=True,
        ).stdout

        run_cmd("npm", [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        after = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(npm_project),
            capture_output=True, text=True, check=True,
        ).stdout

        assert before == after, f"Dry run created commits: before={before!r} after={after!r}"


class TestDryRunExistingProject:
    """Dry-run on an existing scaffolded project must show updates without writing."""

    def test_dry_run_on_existing_project(self, npm_project, capsys):
        """Run real scaffold, modify a file, then verify dry-run shows update plan
        but does not change the file.
        """
        # First, run a real scaffold (no dry-run) to populate
        run_cmd("npm", [], {"no-tag": True, "no-commit": True}, ctx=_ctx())

        ci_path = npm_project / ".github" / "workflows" / "ci.yml"
        assert ci_path.exists()
        original_content = ci_path.read_text()

        # Verify dry-run on a clean scaffolded project doesn't rewrite anything.
        # scaffold always uses the merge path.
        capsys.readouterr()  # drain previous output
        run_cmd("npm", [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        # File content must be untouched
        assert ci_path.read_text() == original_content
        # Verify dry-run banner appeared
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_dry_run_does_not_modify_existing_config(self, npm_project):
        """If .rlsbl/config.json exists, --dry-run must not modify it."""
        # Run real scaffold first to populate config
        run_cmd("npm", [], {"no-tag": True, "no-commit": True}, ctx=_ctx())

        config_path = npm_project / ".rlsbl" / "config.json"
        assert config_path.exists()
        original_mtime = config_path.stat().st_mtime
        original_content = config_path.read_text()

        # Wait a tick so mtime can differ if write happens
        import time
        time.sleep(0.01)

        run_cmd("npm", [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        assert config_path.read_text() == original_content


class TestDryRunMulti:
    """Dry-run on a dual-registry project."""

    def test_dry_run_multi_writes_no_publish(self, dual_registry_project, capsys):
        """--dry-run on a dual-registry project plans publish.yml without writing it."""
        run_cmd_multi(["npm", "pypi"], [], {"dry-run": True, "no-tag": True}, ctx=_ctx())

        publish_path = dual_registry_project / ".github" / "workflows" / "publish.yml"
        assert not publish_path.exists()

        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "publish.yml" in out
