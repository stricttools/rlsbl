"""Tests for upfront validation in batch release.

Verifies that validate_gh_cli, validate_clean_tree, and validate_branch_and_remote
run before any package release, acting as a gate to fail early.
"""

import json
import os
from unittest.mock import patch, MagicMock, call

import pytest

from rlsbl.commands.monorepo.batch_release import _cmd_batch_release
from rlsbl.commands.release.validate import ReleaseValidationError
from rlsbl.release_file import get_batch_release_file_path
from rlsbl.workspace import save_workspace, WORKSPACE_DIR


def _write_toml(path, content):
    """Write a TOML string to a file, creating directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_npm_project(base_path, subdir, version="0.1.0"):
    """Create a minimal npm project."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg = {"name": subdir, "version": version}
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump(pkg, f)


def _init_workspace(base_path, projects):
    """Initialize a workspace with the given project list."""
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    save_workspace(str(base_path), projects)


def _setup_batch(mock_git_repo):
    """Create a standard two-package workspace with a batch release file."""
    _make_npm_project(mock_git_repo, "alpha")
    _make_npm_project(mock_git_repo, "beta")

    projects = [
        {"path": "alpha", "name": "alpha"},
        {"path": "beta", "name": "beta"},
    ]
    _init_workspace(mock_git_repo, projects)

    batch_path = get_batch_release_file_path(str(mock_git_repo))
    _write_toml(
        batch_path,
        '[packages.alpha]\n'
        'bump = "patch"\ndescription = "test release"\n'
        'include = ["npm"]\n'
        'exclude = []\n'
        '\n'
        '[packages.beta]\n'
        'bump = "minor"\ndescription = "test release"\n'
        'include = ["npm"]\n'
        'exclude = []\n',
    )
    return batch_path


# ---------------------------------------------------------------------------
# Upfront validation gates
# ---------------------------------------------------------------------------


VALIDATE_GH = "rlsbl.commands.monorepo.batch_release.validate_gh_cli"
VALIDATE_TREE = "rlsbl.commands.monorepo.batch_release.validate_clean_tree"
VALIDATE_BRANCH = "rlsbl.commands.monorepo.batch_release.validate_branch_and_remote"
RUN_CMD = "rlsbl.commands.release.run_cmd"
FINALIZE = "rlsbl.commands.monorepo.batch_release._finalize_batch_file"


class TestBatchUpfrontValidation:

    def test_gh_cli_failure_blocks_all_releases(self, mock_git_repo, capsys):
        """When validate_gh_cli raises, no package release runs."""
        _setup_batch(mock_git_repo)

        run_cmd_mock = MagicMock()

        with patch(VALIDATE_GH, side_effect=ReleaseValidationError("gh not installed")):
            with patch(RUN_CMD, run_cmd_mock):
                with pytest.raises(SystemExit) as exc_info:
                    _cmd_batch_release(
                        {"dry-run": False, "yes": True, "quiet": False},
                        project_root=mock_git_repo,
                    )

        assert exc_info.value.code == 1
        run_cmd_mock.assert_not_called()
        captured = capsys.readouterr()
        assert "gh not installed" in captured.err

    def test_clean_tree_failure_blocks_all_releases(self, mock_git_repo, capsys):
        """When validate_clean_tree raises, no package release runs."""
        _setup_batch(mock_git_repo)

        run_cmd_mock = MagicMock()

        with patch(VALIDATE_GH):
            with patch(VALIDATE_TREE, side_effect=ReleaseValidationError("tree is dirty")):
                with patch(RUN_CMD, run_cmd_mock):
                    with pytest.raises(SystemExit) as exc_info:
                        _cmd_batch_release(
                            {"dry-run": False, "yes": True, "quiet": False},
                            project_root=mock_git_repo,
                        )

        assert exc_info.value.code == 1
        run_cmd_mock.assert_not_called()
        captured = capsys.readouterr()
        assert "tree is dirty" in captured.err

    def test_branch_validation_failure_blocks_all_releases(self, mock_git_repo, capsys):
        """When validate_branch_and_remote raises, no package release runs."""
        _setup_batch(mock_git_repo)

        run_cmd_mock = MagicMock()

        with patch(VALIDATE_GH):
            with patch(VALIDATE_TREE, return_value=set()):
                with patch(VALIDATE_BRANCH, side_effect=ReleaseValidationError("behind origin")):
                    with patch(RUN_CMD, run_cmd_mock):
                        with pytest.raises(SystemExit) as exc_info:
                            _cmd_batch_release(
                                {"dry-run": False, "yes": True, "quiet": False},
                                project_root=mock_git_repo,
                            )

        assert exc_info.value.code == 1
        run_cmd_mock.assert_not_called()
        captured = capsys.readouterr()
        assert "behind origin" in captured.err

    def test_all_validations_pass_releases_proceed(self, mock_git_repo):
        """When all upfront validations pass, packages are released normally."""
        _setup_batch(mock_git_repo)

        released = []

        def mock_run_cmd(release_config, flags, **kwargs):
            released.append(os.path.basename(str(kwargs["ctx"].project_root)))

        with patch(VALIDATE_GH):
            with patch(VALIDATE_TREE, return_value=set()):
                with patch(VALIDATE_BRANCH, return_value="main"):
                    with patch(FINALIZE):
                        with patch(RUN_CMD, mock_run_cmd):
                            _cmd_batch_release(
                                {"dry-run": False, "yes": True, "quiet": False},
                                project_root=mock_git_repo,
                            )

        assert "alpha" in released
        assert "beta" in released
        assert len(released) == 2

    def test_validation_order_gh_first(self, mock_git_repo):
        """validate_gh_cli is called before validate_clean_tree and validate_branch_and_remote."""
        _setup_batch(mock_git_repo)

        call_order = []

        def track_gh():
            call_order.append("gh")

        def track_tree(flags):
            call_order.append("tree")
            return set()

        def track_branch(flags):
            call_order.append("branch")
            return "main"

        def mock_run_cmd(release_config, flags, **kwargs):
            pass

        with patch(VALIDATE_GH, side_effect=track_gh):
            with patch(VALIDATE_TREE, side_effect=track_tree):
                with patch(VALIDATE_BRANCH, side_effect=track_branch):
                    with patch(FINALIZE):
                        with patch(RUN_CMD, mock_run_cmd):
                            _cmd_batch_release(
                                {"dry-run": False, "yes": True, "quiet": False},
                                project_root=mock_git_repo,
                            )

        assert call_order == ["gh", "tree", "branch"]

    def test_flags_passed_to_validate_clean_tree(self, mock_git_repo):
        """validate_clean_tree receives the flags dict."""
        _setup_batch(mock_git_repo)

        captured_flags = []

        def capture_tree(flags):
            captured_flags.append(flags)
            return set()

        def mock_run_cmd(release_config, flags, **kwargs):
            pass

        flags = {"dry-run": False, "yes": True, "quiet": False, "allow-dirty": True}

        with patch(VALIDATE_GH):
            with patch(VALIDATE_TREE, side_effect=capture_tree):
                with patch(VALIDATE_BRANCH, return_value="main"):
                    with patch(FINALIZE):
                        with patch(RUN_CMD, mock_run_cmd):
                            _cmd_batch_release(flags, project_root=mock_git_repo)

        assert len(captured_flags) == 1
        assert captured_flags[0] is flags
