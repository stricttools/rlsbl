"""Tests for release file integration with rlsbl release.

Verifies that:
- run_cmd(ReleaseConfig, flags) works (dry-run mode)
- Missing release file gives correct error in cmd_release
- Include/exclude validation catches mismatches with detected targets
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.release import run_cmd
from rlsbl.release_file import ReleaseConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_npm_project(tmp_path):
    """Create a minimal npm project with changelog and JSONL setup."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release.\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    # Config to declare targets so detect_targets returns consistent results
    config_dir = tmp_path / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    config = {"targets": ["npm"]}
    (config_dir / "config.json").write_text(json.dumps(config) + "\n")


def _setup_multi_target_project(tmp_path, targets):
    """Create a project detected as having multiple targets."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-pkg"\nversion = "1.0.0"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release.\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    config_dir = tmp_path / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    config = {"targets": targets}
    (config_dir / "config.json").write_text(json.dumps(config) + "\n")


# ---------------------------------------------------------------------------
# ReleaseConfig dry-run tests
# ---------------------------------------------------------------------------

class TestRunCmdWithReleaseConfig:
    """run_cmd(ReleaseConfig, flags) works correctly in dry-run mode."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_dry_run_with_release_config(
        self,
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
        """A ReleaseConfig with bump=patch and include=['npm'] runs dry-run successfully."""
        _setup_npm_project(tmp_project)
        # mock_run: fetch, rev-list, tag -l (current), tag -l (bumped),
        # pre/post hook snapshots
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=[],
        )
        run_cmd(config, {"dry-run": True, "quiet": False, "yes": True})

        captured = capsys.readouterr()
        assert "1.0.1" in captured.out
        assert "patch" in captured.out
        assert "Dry run" in captured.out

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_minor_bump_from_config(
        self,
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
        """A ReleaseConfig with bump=minor produces a minor version bump."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        config = ReleaseConfig(
            bump="minor",
            include=["npm"],
            exclude=[],
        )
        run_cmd(config, {"dry-run": True, "quiet": False, "yes": True})

        captured = capsys.readouterr()
        assert "1.1.0" in captured.out
        assert "minor" in captured.out

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_major_bump_from_config(
        self,
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
        """A ReleaseConfig with bump=major produces a major version bump."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        config = ReleaseConfig(
            bump="major",
            include=["npm"],
            exclude=[],
        )
        run_cmd(config, {"dry-run": True, "quiet": False, "yes": True})

        captured = capsys.readouterr()
        assert "2.0.0" in captured.out
        assert "major" in captured.out


# ---------------------------------------------------------------------------
# Exhaustiveness validation tests
# ---------------------------------------------------------------------------

class TestTargetExhaustivenessValidation:
    """Include + exclude must cover all detected targets."""

    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    def test_missing_target_in_release_file(
        self,
        _branch,
        _clean,
        _gh_inst,
        _gh_auth,
        tmp_project,
        capsys,
    ):
        """Error when a detected target is not in include or exclude."""
        _setup_multi_target_project(tmp_project, ["npm", "pypi"])

        # Release config only includes npm, but pypi is also detected
        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=[],
        )
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(config, {"dry-run": True, "quiet": True, "yes": True})

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "detected targets not in release file" in captured.err
        assert "pypi" in captured.err

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_excluded_targets_satisfy_exhaustiveness(
        self,
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
        """No error when all detected targets appear in include + exclude combined."""
        _setup_multi_target_project(tmp_project, ["npm", "pypi"])
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=["pypi"],
        )
        # Should succeed without SystemExit
        run_cmd(config, {"dry-run": True, "quiet": False, "yes": True})

        captured = capsys.readouterr()
        assert "Dry run" in captured.out

    def test_unknown_target_in_release_file(self, tmp_project, capsys):
        """Error when release file references a target unknown to TARGETS."""
        _setup_npm_project(tmp_project)

        config = ReleaseConfig(
            bump="patch",
            include=["npm", "nonexistent_target"],
            exclude=[],
        )
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(config, {"dry-run": True, "quiet": True, "yes": True})

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "unknown target" in captured.err
        assert "nonexistent_target" in captured.err

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_extra_targets_warns_but_continues(
        self,
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
        """Warning when release file lists targets not detected in project."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=["pypi"],  # pypi not detected but listed in exclude
        )
        # Should succeed (warning, not error)
        run_cmd(config, {"dry-run": True, "quiet": False, "yes": True})

        captured = capsys.readouterr()
        assert "not detected in project" in captured.err
        assert "pypi" in captured.err
        # Release still proceeds
        assert "Dry run" in captured.out

    def test_empty_include_errors(self, tmp_project, capsys):
        """Error when include list is empty."""
        _setup_npm_project(tmp_project)

        config = ReleaseConfig(
            bump="patch",
            include=[],
            exclude=["npm"],
        )
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(config, {"dry-run": True, "quiet": True, "yes": True})

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "empty include list" in captured.err


# ---------------------------------------------------------------------------
# CLI entry point tests (cmd_release)
# ---------------------------------------------------------------------------

class TestCmdReleaseMissingFile:
    """cmd_release errors when no release file exists."""

    def test_no_release_file_gives_error(self, tmp_project, capsys):
        """Running release without a release file prints an actionable error."""
        # Create .rlsbl/ so _require_project_root succeeds
        (tmp_project / ".rlsbl").mkdir()

        from rlsbl import cmd_release

        with pytest.raises(SystemExit) as exc_info:
            cmd_release(
                dry_run=False,
                yes=True,
                quiet=True,
                skip_remote_check=False,
                skip_tests=False,
                skip_lint=False,
                skip_docs=False,
                allow_dirty=False,
                no_tag=False,
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No release file found" in captured.err
        assert "rlsbl release-init" in captured.err


class TestCmdReleaseInvalidFile:
    """cmd_release errors when the release file is malformed."""

    def test_invalid_bump_in_release_file(self, tmp_project, capsys):
        """A release file with an invalid bump type prints a validation error."""
        (tmp_project / ".rlsbl").mkdir()
        releases_dir = tmp_project / ".rlsbl" / "releases"
        releases_dir.mkdir()
        (releases_dir / "unreleased.toml").write_text(
            'bump = "huge"\ninclude = ["npm"]\nexclude = []\n'
        )

        from rlsbl import cmd_release

        with pytest.raises(SystemExit) as exc_info:
            cmd_release(
                dry_run=False,
                yes=True,
                quiet=True,
                skip_remote_check=False,
                skip_tests=False,
                skip_lint=False,
                skip_docs=False,
                allow_dirty=False,
                no_tag=False,
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error in release file" in captured.err


# ---------------------------------------------------------------------------
# Legacy signature compatibility tests
# ---------------------------------------------------------------------------

class TestLegacySignatureCompat:
    """run_cmd(registry, args, flags) still works for backward compatibility."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_legacy_run_cmd_still_works(
        self,
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
        """The old run_cmd(registry, args, flags) signature continues to work."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        # Old-style call
        run_cmd("npm", ["patch"], {"dry-run": True, "quiet": False, "yes": True})

        captured = capsys.readouterr()
        assert "1.0.1" in captured.out
        assert "Dry run" in captured.out
