"""Tests for the selfblog post generate wiring in the release flow."""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.commands.release import _run_selfblog_post_generate
from rlsbl.commands.release.validate import HookError
from rlsbl.release_file import ReleaseConfig


def _rc(blog=False, description="test release", context=""):
    """Shorthand for creating a ReleaseConfig with blog support."""
    return ReleaseConfig(
        bump="patch",
        include=["pypi"],
        exclude=[],
        description=description,
        context=context,
        blog=blog,
    )


class TestFlagAssembly:
    """Test that CLI flags are assembled correctly for selfblog post generate."""

    def test_all_flags_present(self, tmp_path):
        """All parameters produce the correct CLI flags."""
        # Create selfdoc.json
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))
        # Create blog body file
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        (releases_dir / "unreleased.md").write_text("Blog body content.\n")

        captured_cmd = []

        def mock_subprocess_run(cmd, *args, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=mock_subprocess_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True, description="Added feature X", context="Because reasons"),
                new_version="1.2.3",
                current_version="1.2.2",
                bump_type="patch",
                changelog_entry="## 1.2.3\n\n- Added feature X\n",
                tag="v1.2.3",
            )

        assert "selfblog" in captured_cmd
        assert "post" in captured_cmd
        assert "generate" in captured_cmd
        assert "--from-release" in captured_cmd
        assert "--version" in captured_cmd
        idx = captured_cmd.index("--version")
        assert captured_cmd[idx + 1] == "1.2.3"
        assert "--prev-version" in captured_cmd
        idx = captured_cmd.index("--prev-version")
        assert captured_cmd[idx + 1] == "1.2.2"
        assert "--bump-type" in captured_cmd
        assert "--description" in captured_cmd
        assert "--context" in captured_cmd
        assert "--changelog-file" in captured_cmd
        assert "--body-file" in captured_cmd
        assert "--project-name" in captured_cmd
        idx = captured_cmd.index("--project-name")
        assert captured_cmd[idx + 1] == "myproject"
        assert "--release-url" in captured_cmd
        idx = captured_cmd.index("--release-url")
        assert "github.com/owner/repo/releases/tag/v1.2.3" in captured_cmd[idx + 1]

    def test_optional_flags_omitted_when_empty(self, tmp_path):
        """Optional flags (prev-version, bump-type, context) are omitted when values are empty."""
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))

        captured_cmd = []

        def mock_subprocess_run(cmd, *args, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=mock_subprocess_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True, description="First release"),
                new_version="0.1.0",
                current_version=None,
                bump_type=None,
                changelog_entry="",
                tag="v0.1.0",
            )

        assert "--prev-version" not in captured_cmd
        assert "--bump-type" not in captured_cmd
        assert "--context" not in captured_cmd


class TestBlogFalseSkips:
    """Test that blog=false skips the selfblog post generate call."""

    def test_blog_false_skips(self, tmp_path):
        """When blog=false, no subprocess call is made."""
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))

        with patch("subprocess.run") as mock_run:
            result = _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=False),
                new_version="1.0.0",
                current_version=None,
                bump_type=None,
                changelog_entry="",
                tag="v1.0.0",
            )
            assert result is True
            mock_run.assert_not_called()


class TestMissingSelfdoc:
    """Test graceful handling when selfblog is not available."""

    def test_no_selfdoc_json_skips(self, tmp_path):
        """When selfdoc.json doesn't exist, skip gracefully."""
        with patch("subprocess.run") as mock_run:
            result = _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                current_version=None,
                bump_type=None,
                changelog_entry="",
                tag="v1.0.0",
            )
            assert result is True
            mock_run.assert_not_called()

    def test_selfblog_not_installed_skips(self, tmp_path, capsys):
        """When selfblog is not installed, skip with a note."""
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))

        with (
            patch("rlsbl.commands.release.require_tool", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            result = _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                current_version=None,
                bump_type=None,
                changelog_entry="",
                tag="v1.0.0",
            )
            assert result is True
            mock_run.assert_not_called()

        captured = capsys.readouterr()
        assert "selfblog is not installed" in captured.out


class TestTempFileCleanup:
    """Test that the changelog temp file is cleaned up."""

    def test_temp_file_cleaned_on_success(self, tmp_path):
        """Temp changelog file is removed after successful run."""
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))

        temp_files_created = []

        from rlsbl import _effects_direct

        original_temp_file = _effects_direct.temp_file

        def tracking_temp(content, **kwargs):
            path = original_temp_file(content, **kwargs)
            temp_files_created.append(path)
            return path

        def mock_subprocess_run(cmd, *args, **kwargs):
            # Verify temp file exists during subprocess call
            if "selfblog" in cmd:
                changelog_idx = cmd.index("--changelog-file") + 1
                assert os.path.exists(cmd[changelog_idx]), "temp file should exist during subprocess call"
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=mock_subprocess_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
            patch("rlsbl._effects_direct.temp_file", side_effect=tracking_temp),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                current_version=None,
                bump_type=None,
                changelog_entry="test changelog",
                tag="v1.0.0",
            )

        assert len(temp_files_created) >= 1
        for tf in temp_files_created:
            assert not os.path.exists(tf), f"temp file {tf} should be cleaned up"

    def test_temp_file_cleaned_on_failure(self, tmp_path):
        """Temp changelog file is removed even when selfblog fails."""
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))

        temp_files_created = []
        from rlsbl import _effects_direct

        original_temp_file = _effects_direct.temp_file

        def tracking_temp(content, **kwargs):
            path = original_temp_file(content, **kwargs)
            temp_files_created.append(path)
            return path

        def failing_subprocess_run(cmd, *args, **kwargs):
            if "selfblog" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=failing_subprocess_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
            patch("rlsbl._effects_direct.temp_file", side_effect=tracking_temp),
            pytest.raises(HookError),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                current_version=None,
                bump_type=None,
                changelog_entry="test changelog",
                tag="v1.0.0",
            )

        assert len(temp_files_created) >= 1
        for tf in temp_files_created:
            assert not os.path.exists(tf), f"temp file {tf} should be cleaned up even on failure"


class TestDryRun:
    """Test dry-run behavior."""

    def test_dry_run_records_the_real_argv(self, tmp_path):
        """A preview records the selfblog invocation instead of describing it.

        The hand-rolled ``Would run: selfblog post generate ...`` line that
        used to stand in here named only the version; the recorded call
        carries the whole argv (bump type, description, body file, release
        URL) and no subprocess is ever forked.

        ``require_tool`` is patched so the test does not depend on selfblog
        being installed on the machine running the suite: without it the runner
        prints a "not installed" note and returns before recording anything.
        """
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))

        fake_effects = MagicMock()
        fake_effects.temp_file.return_value = str(tmp_path / "cl.md")
        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfblog"),
            patch("rlsbl.commands.release.publish.effects", fake_effects),
        ):
            result = _run_selfblog_post_generate(
                {"dry-run": True},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                current_version=None,
                bump_type=None,
                changelog_entry="",
                tag="v1.0.0",
            )

        assert result is True
        argv = fake_effects.run.call_args[0][0]
        assert argv[:4] == ["selfblog", "post", "generate", "--from-release"]
        assert "1.0.0" in argv


class TestSubprocessFailure:
    """Test that selfblog failure aborts the release."""

    def test_selfblog_failure_raises_hook_error(self, tmp_path):
        """When selfblog post generate fails, HookError is raised."""
        selfdoc_json = tmp_path / "selfdoc.json"
        selfdoc_json.write_text(json.dumps({"project_name": "myproject"}))

        def failing_subprocess_run(cmd, *args, **kwargs):
            if "selfblog" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=failing_subprocess_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
            pytest.raises(HookError, match="selfblog post generate failed"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                current_version=None,
                bump_type=None,
                changelog_entry="test",
                tag="v1.0.0",
            )
