"""Cross-project integration tests for rlsbl-selfdoc blog post generation.

Tests the contract between rlsbl's release flow and selfdoc's
``post generate --from-release`` command, focusing on realistic project
structures and flag/file correctness.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.release import _run_selfblog_post_generate
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


def _realistic_selfdoc_config(project_name="myproject", **overrides):
    """Return a realistic selfdoc.json dict matching selfdoc's actual config schema."""
    config = {
        "project_name": project_name,
        "source": "src",
        "base_url": f"https://{project_name}.pages.dev",
        "versions": True,
        "locales": ["en"],
        "root_files": ["README.md"],
    }
    config.update(overrides)
    return config


def _setup_project(tmp_path, selfdoc_config=None, blog_body=None, rlsbl_config=None):
    """Set up a temp directory that looks like a real selfdoc+rlsbl project.

    Creates selfdoc.json, .rlsbl/config.json, and optionally
    .rlsbl/releases/unreleased.md with blog body content.
    """
    if selfdoc_config is not None:
        (tmp_path / "selfdoc.json").write_text(json.dumps(selfdoc_config))

    rlsbl_dir = tmp_path / ".rlsbl"
    rlsbl_dir.mkdir(parents=True, exist_ok=True)

    config = rlsbl_config or {"target": "pypi", "name": "myproject"}
    (rlsbl_dir / "config.json").write_text(json.dumps(config))

    releases_dir = rlsbl_dir / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    if blog_body is not None:
        (releases_dir / "unreleased.md").write_text(blog_body)


def _capture_subprocess(captured_cmd):
    """Return a mock subprocess.run that captures the command list."""

    def mock_run(cmd, *args, **kwargs):
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    return mock_run


class TestRealisticProjectStructure:
    """Test with a directory structure matching a real selfdoc+rlsbl project."""

    def test_full_project_structure(self, tmp_path):
        """A realistic project with selfdoc.json, .rlsbl/config.json, and blog body
        produces the correct selfblog invocation."""
        selfdoc_config = _realistic_selfdoc_config("cooltools")
        blog_body = "## What's new\n\nThis release adds the widget feature.\n"
        _setup_project(tmp_path, selfdoc_config=selfdoc_config, blog_body=blog_body)

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="git@github.com:acme/cooltools.git"),
        ):
            result = _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True, description="Added widget feature"),
                new_version="0.5.0",
                current_version="0.4.1",
                bump_type="minor",
                changelog_entry="## 0.5.0\n\n### Features\n- Added widget feature\n",
                tag="v0.5.0",
            )

        assert result is True
        assert captured_cmd[:4] == ["selfblog", "post", "generate", "--from-release"]


class TestChangelogFileContentVerification:
    """Verify that the temp changelog file actually contains the changelog_entry content."""

    def test_changelog_content_written_to_temp_file(self, tmp_path):
        """The temp file referenced by --changelog-file contains the exact
        changelog_entry string."""
        _setup_project(tmp_path, selfdoc_config=_realistic_selfdoc_config())

        changelog_text = "## 2.0.0\n\n### Breaking\n- Removed legacy API\n\n### Features\n- New REST API\n"
        observed_content = []

        def inspecting_run(cmd, *args, **kwargs):
            if "selfblog" in cmd and "--changelog-file" in cmd:
                idx = cmd.index("--changelog-file") + 1
                filepath = cmd[idx]
                with open(filepath, "r", encoding="utf-8") as f:
                    observed_content.append(f.read())
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=inspecting_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="2.0.0",
                current_version="1.9.0",
                bump_type="major",
                changelog_entry=changelog_text,
                tag="v2.0.0",
            )

        assert len(observed_content) == 1
        assert observed_content[0] == changelog_text

    def test_empty_changelog_entry_writes_empty_file(self, tmp_path):
        """When changelog_entry is empty, the temp file exists but is empty."""
        _setup_project(tmp_path, selfdoc_config=_realistic_selfdoc_config())

        observed_content = []

        def inspecting_run(cmd, *args, **kwargs):
            if "selfblog" in cmd and "--changelog-file" in cmd:
                idx = cmd.index("--changelog-file") + 1
                with open(cmd[idx], "r", encoding="utf-8") as f:
                    observed_content.append(f.read())
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=inspecting_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                changelog_entry="",
                tag="v1.0.0",
            )

        assert len(observed_content) == 1
        assert observed_content[0] == ""

    def test_none_changelog_entry_writes_empty_file(self, tmp_path):
        """When changelog_entry is None, the temp file exists but is empty."""
        _setup_project(tmp_path, selfdoc_config=_realistic_selfdoc_config())

        observed_content = []

        def inspecting_run(cmd, *args, **kwargs):
            if "selfblog" in cmd and "--changelog-file" in cmd:
                idx = cmd.index("--changelog-file") + 1
                with open(cmd[idx], "r", encoding="utf-8") as f:
                    observed_content.append(f.read())
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=inspecting_run),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                changelog_entry=None,
                tag="v1.0.0",
            )

        assert len(observed_content) == 1
        assert observed_content[0] == ""


class TestProjectNameResolution:
    """Test project_name resolution from selfdoc.json with fallback chain."""

    def test_project_name_from_project_name_key(self, tmp_path):
        """project_name key in selfdoc.json takes priority."""
        config = _realistic_selfdoc_config("from-project-name")
        config["name"] = "from-name"
        _setup_project(tmp_path, selfdoc_config=config)

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                tag="v1.0.0",
            )

        idx = captured_cmd.index("--project-name")
        assert captured_cmd[idx + 1] == "from-project-name"

    def test_project_name_fallback_to_name_key(self, tmp_path):
        """When project_name is absent, falls back to name key."""
        config = {"name": "from-name-key", "source": "src"}
        _setup_project(tmp_path, selfdoc_config=config)

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                tag="v1.0.0",
            )

        idx = captured_cmd.index("--project-name")
        assert captured_cmd[idx + 1] == "from-name-key"

    def test_project_name_fallback_to_directory_basename(self, tmp_path):
        """When neither project_name nor name exists, falls back to directory name."""
        config = {"source": "src"}
        _setup_project(tmp_path, selfdoc_config=config)

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                tag="v1.0.0",
            )

        idx = captured_cmd.index("--project-name")
        assert captured_cmd[idx + 1] == os.path.basename(str(tmp_path))

    def test_project_name_fallback_on_malformed_json(self, tmp_path):
        """When selfdoc.json contains invalid JSON, falls back to directory name."""
        _setup_project(tmp_path)
        (tmp_path / "selfdoc.json").write_text("{invalid json!!")

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                tag="v1.0.0",
            )

        idx = captured_cmd.index("--project-name")
        assert captured_cmd[idx + 1] == os.path.basename(str(tmp_path))


class TestBodyFilePath:
    """Verify the --body-file flag points to the actual unreleased.md path."""

    def test_body_file_included_when_present(self, tmp_path):
        """When .rlsbl/releases/unreleased.md exists, --body-file points to it."""
        body_content = "## Release highlights\n\nMajor improvements to performance.\n"
        _setup_project(
            tmp_path,
            selfdoc_config=_realistic_selfdoc_config(),
            blog_body=body_content,
        )

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="3.0.0",
                tag="v3.0.0",
            )

        expected_body_path = os.path.join(str(tmp_path), ".rlsbl", "releases", "unreleased.md")
        idx = captured_cmd.index("--body-file")
        assert captured_cmd[idx + 1] == expected_body_path
        assert os.path.isfile(expected_body_path)

    def test_body_file_omitted_when_absent(self, tmp_path):
        """When .rlsbl/releases/unreleased.md does not exist, --body-file is omitted."""
        _setup_project(tmp_path, selfdoc_config=_realistic_selfdoc_config())

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="git@github.com:owner/repo.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="1.0.0",
                tag="v1.0.0",
            )

        assert "--body-file" not in captured_cmd


class TestReleaseURLWithHTTPS:
    """Test release URL generation with HTTPS-style git remote URLs."""

    def test_https_remote_produces_correct_release_url(self, tmp_path):
        """HTTPS remote URL (https://github.com/owner/repo.git) produces
        the correct release URL."""
        _setup_project(tmp_path, selfdoc_config=_realistic_selfdoc_config())

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="https://github.com/acme/toolkit.git"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="0.8.0",
                tag="v0.8.0",
            )

        idx = captured_cmd.index("--release-url")
        assert captured_cmd[idx + 1] == "https://github.com/acme/toolkit/releases/tag/v0.8.0"

    def test_https_remote_without_dot_git_suffix(self, tmp_path):
        """HTTPS remote URL without .git suffix produces the correct release URL."""
        _setup_project(tmp_path, selfdoc_config=_realistic_selfdoc_config())

        captured_cmd = []

        with (
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("subprocess.run", side_effect=_capture_subprocess(captured_cmd)),
            patch("rlsbl.commands.release.run", return_value="https://github.com/acme/toolkit"),
        ):
            _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=_rc(blog=True),
                new_version="0.8.0",
                tag="v0.8.0",
            )

        idx = captured_cmd.index("--release-url")
        assert captured_cmd[idx + 1] == "https://github.com/acme/toolkit/releases/tag/v0.8.0"


class TestNoneReleaseConfig:
    """Verify early return when release_config is None."""

    def test_none_release_config_returns_true(self, tmp_path):
        """When release_config is None, returns True without any subprocess call."""
        _setup_project(tmp_path, selfdoc_config=_realistic_selfdoc_config())

        with patch("subprocess.run") as mock_run:
            result = _run_selfblog_post_generate(
                {},
                project_dir=str(tmp_path),
                release_config=None,
                new_version="1.0.0",
                tag="v1.0.0",
            )

        assert result is True
        mock_run.assert_not_called()

    def test_none_release_config_does_not_check_selfdoc_json(self, tmp_path):
        """When release_config is None, selfdoc.json is not even checked."""
        # No selfdoc.json -- but that should not matter because we bail
        # before reaching the selfdoc.json check.
        result = _run_selfblog_post_generate(
            {},
            project_dir=str(tmp_path),
            release_config=None,
            new_version="1.0.0",
            tag="v1.0.0",
        )

        assert result is True
