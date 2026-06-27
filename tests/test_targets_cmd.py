"""Tests for rlsbl targets command and multi-target release."""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


class TestTargetsCommand:
    """Tests for the `rlsbl targets` command output."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_lists_all_targets(self):
        """Command output includes all registered targets."""
        from rlsbl.commands.targets_cmd import run_cmd

        buf = StringIO()
        with patch("sys.stdout", buf):
            run_cmd(None, [], {}, project_root=".")

        output = buf.getvalue()
        assert "npm" in output
        assert "pypi" in output
        assert "go" in output

    def test_shows_header_row(self):
        """Output starts with a header row containing column names."""
        from rlsbl.commands.targets_cmd import run_cmd

        buf = StringIO()
        with patch("sys.stdout", buf):
            run_cmd(None, [], {}, project_root=".")

        lines = buf.getvalue().splitlines()
        header = lines[0]
        assert "Target" in header
        assert "Detected" in header
        assert "Version file" in header

    def test_detects_npm_in_project_with_package_json(self):
        """In a directory with package.json, npm shows as detected."""
        with open("package.json", "w") as f:
            json.dump({"name": "test", "version": "1.0.0"}, f)

        from rlsbl.commands.targets_cmd import run_cmd

        buf = StringIO()
        with patch("sys.stdout", buf):
            run_cmd(None, [], {}, project_root=".")

        output = buf.getvalue()
        # Find the npm line and verify it says "yes"
        for line in output.splitlines():
            if line.startswith("npm"):
                assert "yes" in line
                assert "package.json" in line
                break
        else:
            pytest.fail("npm line not found in output")

    def test_no_detection_in_empty_dir(self):
        """In an empty directory, all targets show 'no' for detected."""
        from rlsbl.commands.targets_cmd import run_cmd

        buf = StringIO()
        with patch("sys.stdout", buf):
            run_cmd(None, [], {}, project_root=".")

        output = buf.getvalue()
        lines = output.splitlines()[1:]  # skip header
        for line in lines:
            assert "no" in line

class TestMultiTargetRelease:
    """Tests for multi-target release: secondary targets get build/publish called."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        # Create package.json so npm is the primary target
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f, indent=2)
            f.write("\n")
        # Create CHANGELOG.md
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release with improvements.\n")
        # Create .rlsbl/changes/ for JSONL changelog
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write("")
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"private": False, "targets": ["npm", "spec"]}, f)

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.extract_changelog_entry", return_value="- Improvements")
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    @patch("rlsbl.commands.release._run_selfdoc_check", return_value=True)
    @patch("rlsbl.commands.release._run_selfdoc_gen", return_value=True)
    def test_secondary_targets_called_when_detected(
        self, _selfdoc_gen, _selfdoc_check, _changes_dir, _extract, _finalize, _gen_ver_file, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch, _commit_files, mock_run, _push, _remote_exists, monkeypatch
    ):
        """When a secondary target (spec) is detected, its build is called."""
        # Create version.json so spec target is detected
        with open("version.json", "w") as f:
            json.dump({"version": "1.0.0"}, f)

        # Mock run() responses:
        # 1. git fetch origin --quiet (remote-ahead check)
        # 2. git rev-list --count HEAD..origin/main (0 commits behind)
        # 3. tag -l (current tag exists) -> "v1.0.0"
        # 4. tag -l (new tag doesn't exist) -> ""
        # 5. git status --porcelain (pre-hook snapshot) -> ""
        # 6. git status --porcelain (pre-selfdoc snapshot) -> ""
        # 7. git status --porcelain (post-selfdoc snapshot) -> ""
        # 8. git status --porcelain (post-hook snapshot) -> ""
        # 9. git status --porcelain (baseline snapshot) -> ""
        # 10. git rev-parse --show-toplevel (for vpath) -> "/tmp/fake-repo"
        # 11. git status --porcelain (re-check guard) -> ""
        # 12. git rev-parse HEAD (pre_release_sha capture) -> "pre123"
        # commit_files is mocked separately (no git add/commit calls here)
        # 13. git status --porcelain (backfilled .md detection) -> ""
        # 14. git tag -> ""
        # 15. git push origin tag -> ""
        # 16. git rev-parse HEAD (pushed_sha) -> ""
        # 17. gh release create -> "abc123"
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", "", "", "", "", "/tmp/fake-repo", "", "pre123", "", "", "", "", "abc123"]

        # Mock the spec target's build to track calls
        from rlsbl.targets import TARGETS
        build_mock = MagicMock()
        monkeypatch.setattr(TARGETS["spec"], "build", build_mock)

        from rlsbl.commands.release import run_cmd

        with patch("sys.stdout", StringIO()):
            run_cmd(_rc(include=["npm", "spec"]), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}))

        # Verify spec target build was called
        build_mock.assert_called_once_with(".", "1.0.1")

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.extract_changelog_entry", return_value="- Improvements")
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    @patch("rlsbl.commands.release._run_selfdoc_check", return_value=True)
    @patch("rlsbl.commands.release._run_selfdoc_gen", return_value=True)
    def test_secondary_target_failure_is_non_fatal(
        self, _selfdoc_gen, _selfdoc_check, _changes_dir, _extract, _finalize, _gen_ver_file, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch, _commit_files, mock_run, _push, _remote_exists, monkeypatch
    ):
        """If a secondary target's build raises, release still completes."""
        # Create version.json so spec target is detected
        with open("version.json", "w") as f:
            json.dump({"version": "1.0.0"}, f)

        # Same mock sequence as test_secondary_targets_called_when_detected:
        # 1. git fetch  2. git rev-list  3-4. tag -l x2
        # 5. pre-hook snapshot  6. pre-selfdoc snapshot
        # 7. post-selfdoc snapshot  8. post-hook snapshot
        # 9. baseline  10. rev-parse --show-toplevel
        # 11. re-check guard  12. pre_release_sha  13. backfilled .md detection
        # 14. git tag  15. git push origin tag
        # 16. pushed_sha  17. gh release create
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", "", "", "", "", "/tmp/fake-repo", "", "pre123", "", "", "", "", "abc123"]

        from rlsbl.targets import TARGETS
        monkeypatch.setattr(TARGETS["spec"], "build", MagicMock(side_effect=RuntimeError("build failed")))

        from rlsbl.commands.release import run_cmd

        # Should not raise -- secondary failures are non-fatal
        buf = StringIO()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", buf):
            run_cmd(_rc(include=["npm", "spec"]), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}))

        # Verify warnings were emitted
        stderr_output = buf.getvalue()
        assert "spec target build failed" in stderr_output
