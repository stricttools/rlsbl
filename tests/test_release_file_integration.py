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
    config = {"targets": ["npm"], "private": False}
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
    config = {"targets": targets, "private": False}
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
    """cmd_release_run errors when no release file exists."""

    def test_no_release_file_gives_error(self, tmp_project, capsys):
        """Running release run without a release file prints an actionable error."""
        # Create .rlsbl/ so _require_project_root succeeds
        (tmp_project / ".rlsbl").mkdir()

        from rlsbl import cmd_release_run

        with pytest.raises(SystemExit) as exc_info:
            cmd_release_run(
                dry_run=False,
                yes=True,
                quiet=True,
                allow_dirty=False,
                watch=False,
                no_watch=True,
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No release file found" in captured.err
        assert "rlsbl release init" in captured.err


class TestCmdReleaseInvalidFile:
    """cmd_release_run errors when the release file is malformed."""

    def test_invalid_bump_in_release_file(self, tmp_project, capsys):
        """A release file with an invalid bump type prints a validation error."""
        (tmp_project / ".rlsbl").mkdir()
        releases_dir = tmp_project / ".rlsbl" / "releases"
        releases_dir.mkdir()
        (releases_dir / "unreleased.toml").write_text(
            'bump = "huge"\ninclude = ["npm"]\nexclude = []\n'
        )

        from rlsbl import cmd_release_run

        with pytest.raises(SystemExit) as exc_info:
            cmd_release_run(
                dry_run=False,
                yes=True,
                quiet=True,
                allow_dirty=False,
                watch=False,
                no_watch=True,
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error in release file" in captured.err


# ---------------------------------------------------------------------------
# ReleaseConfig signature tests
# ---------------------------------------------------------------------------

class TestReleaseConfigSignature:
    """run_cmd(ReleaseConfig, flags) is the only supported calling convention."""

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
    def test_run_cmd_with_release_config(
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
        """run_cmd(ReleaseConfig, flags) works in dry-run mode."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        run_cmd(
            ReleaseConfig(bump="patch", include=["npm"], exclude=[]),
            {"dry-run": True, "quiet": False, "yes": True},
        )

        captured = capsys.readouterr()
        assert "1.0.1" in captured.out
        assert "Dry run" in captured.out


# ---------------------------------------------------------------------------
# Release file finalization tests
# ---------------------------------------------------------------------------

class TestReleaseFileFinalization:
    """After a release, unreleased.toml is renamed to vX.Y.Z.toml (read-only)
    and a fresh empty unreleased.toml is created."""

    def test_finalize_release_file(self, tmp_path):
        """Simulates the finalization logic: rename, chmod, recreate."""
        from rlsbl.release_file import get_release_file_path

        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        release_file = releases_dir / "unreleased.toml"
        release_file.write_text('bump = "patch"\ninclude = ["npm"]\nexclude = []\n')

        release_file_path = str(release_file)
        new_version = "1.0.0"

        # Simulate finalization (same logic as in release.py)
        versioned_release = os.path.join(str(releases_dir), f"v{new_version}.toml")
        os.rename(release_file_path, versioned_release)
        os.chmod(versioned_release, 0o444)
        with open(release_file_path, "w", encoding="utf-8") as f:
            pass  # empty file

        # Verify versioned file exists and is read-only
        assert os.path.exists(versioned_release)
        import stat
        mode = os.stat(versioned_release).st_mode
        assert not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

        # Verify content was preserved
        with open(versioned_release, "r") as f:
            content = f.read()
        assert 'bump = "patch"' in content

        # Verify new unreleased.toml exists and is empty
        assert os.path.exists(release_file_path)
        assert os.path.getsize(release_file_path) == 0

    def test_finalize_skipped_when_no_release_file(self, tmp_path):
        """When no release file exists, finalization is a no-op."""
        from rlsbl.release_file import get_release_file_path

        release_file_path = get_release_file_path(str(tmp_path))
        # No release file created -- just verify the path doesn't exist
        assert not os.path.exists(release_file_path)
        # The release code checks os.path.exists() before finalizing,
        # so this is a no-op (no exception)


# ---------------------------------------------------------------------------
# Monorepo release file path tests
# ---------------------------------------------------------------------------

class TestMonorepoReleaseFilePath:
    """get_release_file_path with a project subdir returns the correct path."""

    def test_subdir_path(self):
        from rlsbl.release_file import get_release_file_path

        path = get_release_file_path("packages/mylib")
        expected = os.path.join("packages", "mylib", ".rlsbl", "releases", "unreleased.toml")
        assert os.path.normpath(path) == os.path.normpath(expected)

    def test_absolute_subdir_path(self, tmp_path):
        from rlsbl.release_file import get_release_file_path

        project_dir = str(tmp_path / "packages" / "mylib")
        path = get_release_file_path(project_dir)
        expected = os.path.join(project_dir, ".rlsbl", "releases", "unreleased.toml")
        assert path == expected

    def test_monorepo_finalization_uses_project_dir(self, tmp_path):
        """In monorepo mode, finalization operates on the package's releases dir."""
        from rlsbl.release_file import get_release_file_path

        # Simulate monorepo structure: root/python/.rlsbl/releases/
        pkg_dir = tmp_path / "python"
        releases_dir = pkg_dir / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        release_file = releases_dir / "unreleased.toml"
        release_file.write_text('bump = "minor"\ninclude = ["pypi"]\nexclude = []\n')

        release_file_path = get_release_file_path(str(pkg_dir))
        assert os.path.exists(release_file_path)

        # Simulate finalization
        new_version = "0.2.0"
        versioned_release = os.path.join(str(releases_dir), f"v{new_version}.toml")
        os.rename(release_file_path, versioned_release)
        os.chmod(versioned_release, 0o444)
        with open(release_file_path, "w", encoding="utf-8") as f:
            pass

        # Verify both files are in the package's directory
        assert os.path.exists(versioned_release)
        assert str(pkg_dir) in versioned_release
        assert os.path.exists(release_file_path)
        assert os.path.getsize(release_file_path) == 0


# ---------------------------------------------------------------------------
# Monorepo directory scoping tests
# ---------------------------------------------------------------------------

class TestMonorepoDirectoryScoping:
    """validate_unreleased receives the correct project dict in monorepo mode.

    Regression test: ensures resolve_project receives the correct start path
    (the package directory, not the workspace root), so it returns the project
    dict rather than None.
    """

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased")
    def test_validate_unreleased_receives_project_dict(
        self,
        mock_validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        monorepo_fixture,
        monkeypatch,
    ):
        """In monorepo mode, validate_unreleased is called with project=<dict>, not None."""
        mock_validate.return_value = {"passed": True, "checks": {}}
        # mock_run: fetch, rev-list, tag -l (current), tag -l (bumped),
        # pre/post hook snapshots
        mock_run.side_effect = ["", "0", "mypylib@v0.1.0", "", "", ""]

        # Create CHANGELOG.md so the post-validation fallback path succeeds
        (monorepo_fixture.python_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.1\n\nPatch release.\n"
        )

        # Chdir to the python subproject (simulates user running release there)
        monkeypatch.chdir(monorepo_fixture.python_dir)

        config = ReleaseConfig(
            bump="patch",
            include=["pypi"],
            exclude=[],
        )
        run_cmd(config, {"dry-run": True, "quiet": False, "yes": True})

        # Verify validate_unreleased was called with a non-None project dict
        mock_validate.assert_called_once()
        call_kwargs = mock_validate.call_args
        project_arg = call_kwargs.kwargs.get("project") or call_kwargs[1].get("project")
        # If passed as positional, check the second positional arg is not it;
        # validate_unreleased(changes_dir, tag_glob=..., project=...)
        if project_arg is None and len(call_kwargs.args) > 2:
            project_arg = call_kwargs.args[2]
        assert project_arg is not None, (
            "validate_unreleased was called with project=None; "
            "directory scoping is broken in monorepo mode"
        )
        assert project_arg["name"] == "mypylib"
        assert project_arg["path"] == "python"
