"""Tests for pre-release hook override of built-in tests and lint."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.release import (
    _compute_content_hash,
    _get_pre_release_template_hashes,
    _is_hook_effectively_empty,
)
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


def _setup_project(tmp_path, hook_body=None):
    """Create a minimal project.  Optionally writes a pre-release hook."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release.\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (tmp_path / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
    )
    if hook_body is not None:
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-release.sh"
        hook.write_text(hook_body)
        hook.chmod(0o755)


# The current scaffold template content (V2, after the comment update).
_CURRENT_TEMPLATE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "# Project-specific pre-release checks.\n"
    "# When this hook is customized (any change from the scaffold template),\n"
    "# built-in tests and lint are skipped -- the hook is expected to handle them.\n"
    "# Add custom validation here, e.g.:\n"
    "#   - Run tests and lint with project-specific flags\n"
    "#   - Check for uncommitted documentation\n"
    "#   - Verify external service connectivity\n"
    "#   - Run integration tests not covered by the test suite\n"
)

# The original scaffold template content (V1, before the comment update).
_V1_TEMPLATE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "# Project-specific pre-release checks.\n"
    "# Built-in checks (tests, lint) run automatically before this hook.\n"
    "# Add custom validation here, e.g.:\n"
    "#   - Check for uncommitted documentation\n"
    "#   - Verify external service connectivity\n"
    "#   - Run integration tests not covered by the test suite\n"
)


# ---------------------------------------------------------------------------
# _is_hook_effectively_empty tests
# ---------------------------------------------------------------------------

class TestIsHookEffectivelyEmpty:
    """Tests for the _is_hook_effectively_empty utility function."""

    def test_missing_hook_file_returns_true(self, tmp_path):
        """A nonexistent hook file is effectively empty."""
        hook_path = str(tmp_path / ".rlsbl" / "hooks" / "pre-release.sh")
        assert _is_hook_effectively_empty(hook_path) is True

    def test_current_template_returns_true(self, tmp_path):
        """A hook with the current scaffold template content is effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_CURRENT_TEMPLATE)
        assert _is_hook_effectively_empty(str(hook)) is True

    def test_v1_template_returns_true(self, tmp_path):
        """A hook with the V1 (old) scaffold template content is effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_V1_TEMPLATE)
        assert _is_hook_effectively_empty(str(hook)) is True

    def test_template_with_trailing_whitespace_returns_true(self, tmp_path):
        """Trailing whitespace differences are ignored (same as hook_hashes pattern)."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_CURRENT_TEMPLATE + "\n\n  \t  \n")
        assert _is_hook_effectively_empty(str(hook)) is True

    def test_customized_hook_returns_false(self, tmp_path):
        """A hook with custom lines appended is NOT effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_CURRENT_TEMPLATE + "uv run pytest\n")
        assert _is_hook_effectively_empty(str(hook)) is False

    def test_completely_different_hook_returns_false(self, tmp_path):
        """A fully custom hook is NOT effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text("#!/bin/bash\nset -e\nmake test\nmake lint\n")
        assert _is_hook_effectively_empty(str(hook)) is False

    def test_empty_file_returns_false(self, tmp_path):
        """An empty file is NOT a template match -- it's customized (someone cleared it)."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text("")
        assert _is_hook_effectively_empty(str(hook)) is False


class TestTemplateHashes:
    """Tests for _get_pre_release_template_hashes and _compute_content_hash."""

    def test_hash_set_contains_current_template(self):
        """The hash set includes the current template file."""
        hashes = _get_pre_release_template_hashes()
        current_hash = _compute_content_hash(_CURRENT_TEMPLATE)
        assert current_hash in hashes

    def test_hash_set_contains_v1_template(self):
        """The hash set includes the V1 template."""
        hashes = _get_pre_release_template_hashes()
        v1_hash = _compute_content_hash(_V1_TEMPLATE)
        assert v1_hash in hashes

    def test_hash_set_has_at_least_two_entries(self):
        """We track at least two template versions."""
        hashes = _get_pre_release_template_hashes()
        assert len(hashes) >= 2

    def test_compute_content_hash_strips_trailing_whitespace(self):
        """Trailing whitespace is stripped before hashing."""
        a = _compute_content_hash("hello\n")
        b = _compute_content_hash("hello\n\n  \t  ")
        assert a == b

    def test_compute_content_hash_different_content(self):
        """Different content produces different hashes."""
        a = _compute_content_hash("hello\n")
        b = _compute_content_hash("world\n")
        assert a != b


# ---------------------------------------------------------------------------
# Release flow integration tests
# ---------------------------------------------------------------------------

class TestBuiltinTestsSkippedWhenHookCustomized:
    """Tests that the release flow skips built-in tests/lint when the
    pre-release hook has been customized."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release._run_builtin_lint", return_value=True)
    @patch("rlsbl.commands.release._run_builtin_tests", return_value=True)
    def test_skips_tests_and_lint_when_hook_customized(
        self,
        mock_tests,
        mock_lint,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """When the pre-release hook is customized, built-in tests and lint
        are skipped."""
        _setup_project(
            tmp_project,
            hook_body=_V1_TEMPLATE + "uv run pytest\n",
        )
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        mock_tests.assert_not_called()
        mock_lint.assert_not_called()

        captured = capsys.readouterr()
        assert "Skipping built-in tests (pre-release hook handles testing)" in captured.out
        assert "Skipping built-in lint (pre-release hook handles linting)" in captured.out

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release._run_builtin_lint", return_value=True)
    @patch("rlsbl.commands.release._run_builtin_tests", return_value=True)
    def test_runs_tests_and_lint_when_hook_is_template(
        self,
        mock_tests,
        mock_lint,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """When the pre-release hook is the unmodified scaffold template,
        built-in tests and lint run normally."""
        _setup_project(tmp_project, hook_body=_V1_TEMPLATE)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        mock_tests.assert_called_once()
        mock_lint.assert_called_once()

        captured = capsys.readouterr()
        assert "Skipping built-in tests" not in captured.out
        assert "Skipping built-in lint" not in captured.out

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release._run_builtin_lint", return_value=True)
    @patch("rlsbl.commands.release._run_builtin_tests", return_value=True)
    def test_runs_tests_and_lint_when_hook_missing(
        self,
        mock_tests,
        mock_lint,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """When no pre-release hook exists, built-in tests and lint run."""
        _setup_project(tmp_project, hook_body=None)  # no hook
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "quiet": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        mock_tests.assert_called_once()
        mock_lint.assert_called_once()

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release._run_builtin_lint", return_value=True)
    @patch("rlsbl.commands.release._run_builtin_tests", return_value=True)
    def test_hook_still_runs_when_customized(
        self,
        mock_tests,
        mock_lint,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
    ):
        """Even when tests/lint are skipped, the pre-release hook itself
        still executes."""
        _setup_project(
            tmp_project,
            hook_body="#!/bin/bash\necho 'custom tests'\n",
        )
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "quiet": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        # Built-in tests/lint skipped
        mock_tests.assert_not_called()
        mock_lint.assert_not_called()

        # The pre-release hook was still called via subprocess.run
        assert mock_sp.run.call_count >= 1
        hook_calls = [
            c for c in mock_sp.run.call_args_list
            if len(c[0][0]) > 1 and "pre-release.sh" in c[0][0][1]
        ]
        assert len(hook_calls) == 1
