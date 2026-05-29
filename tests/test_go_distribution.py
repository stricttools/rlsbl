"""Tests for Go binary distribution features (Homebrew tap, npm wrapper).

Covers: config reading (githubOwner), Homebrew template vars and rendering,
npm wrapper template vars, template_mappings, template rendering,
no-config baseline behavior, and publish-time go install for CLI projects.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.init_cmd import process_template
from rlsbl.context import ProjectContext
from rlsbl.targets import TARGETS
from rlsbl.targets.go import GoTarget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_go_project(tmp_path):
    """Create a minimal Go binary project."""
    (tmp_path / "go.mod").write_text(
        "module github.com/testuser/testproject\n\ngo 1.21\n"
    )
    (tmp_path / "VERSION").write_text("1.0.0")
    (tmp_path / "main.go").write_text("package main\n")


def _template_path(name):
    """Return the absolute path to a Go template file."""
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "rlsbl", "templates", "go", name,
    )


def _shared_template_path(name):
    """Return the absolute path to a shared template file."""
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "rlsbl", "templates", "shared", name,
    )


# ---------------------------------------------------------------------------
# Test class 1: Go config reading
# ---------------------------------------------------------------------------


class TestGoConfigReading:
    """Test that Go target reads rlsbl config and derives githubOwner."""

    def test_github_owner_from_repo_name(self, tmp_path, monkeypatch):
        """githubOwner is extracted from repoName."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert vars_["githubOwner"] == "testuser"

    def test_github_owner_empty_without_slash(self, tmp_path, monkeypatch):
        """githubOwner is empty when repoName has no slash."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "go.mod").write_text("module testproject\n\ngo 1.21\n")
        (tmp_path / "VERSION").write_text("1.0.0")
        (tmp_path / "main.go").write_text("package main\n")
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert vars_["githubOwner"] == ""


# ---------------------------------------------------------------------------
# Test class 2: Homebrew template vars
# ---------------------------------------------------------------------------


class TestHomebrewTemplateVars:
    """Test brewsSection and homebrewEnv generation."""

    def test_brews_section_with_config(self, tmp_path, monkeypatch):
        """brewsSection generated when homebrew.tap configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            '{"homebrew": {"tap": "homebrew-tap"}}'
        )
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert "brews:" in vars_["brewsSection"]
        assert "homebrew-tap" in vars_["brewsSection"]
        assert "HOMEBREW_TAP_TOKEN" in vars_["brewsSection"]

    def test_brews_section_empty_without_config(self, tmp_path, monkeypatch):
        """brewsSection is empty when homebrew not configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert vars_["brewsSection"] == ""

    def test_homebrew_env_with_config(self, tmp_path, monkeypatch):
        """homebrewEnv contains HOMEBREW_TAP_TOKEN when configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            '{"homebrew": {"tap": "homebrew-tap"}}'
        )
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert "HOMEBREW_TAP_TOKEN" in vars_["homebrewEnv"]

    def test_homebrew_env_empty_without_config(self, tmp_path, monkeypatch):
        """homebrewEnv is empty when homebrew not configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert vars_["homebrewEnv"] == ""


# ---------------------------------------------------------------------------
# Test class 3: Homebrew template rendering
# ---------------------------------------------------------------------------


class TestHomebrewTemplateRendering:
    """Test that goreleaser.yml.tpl and publish.yml.tpl render correctly."""

    def test_goreleaser_with_brews(self):
        """goreleaser template renders brews section."""
        template = open(_template_path("goreleaser.yml.tpl")).read()
        vars_ = {
            "goreleaserMain": ".",
            "brewsSection": (
                "\nbrews:\n  - repository:\n      owner: testuser\n"
                "      name: homebrew-tap\n"
            ),
        }
        content, unreplaced = process_template(template, vars_)
        assert "brews:" in content
        assert "{{brewsSection}}" not in content

    def test_goreleaser_without_brews(self):
        """goreleaser template renders cleanly without brews."""
        template = open(_template_path("goreleaser.yml.tpl")).read()
        vars_ = {"goreleaserMain": ".", "brewsSection": ""}
        content, unreplaced = process_template(template, vars_)
        assert "brews:" not in content
        assert "{{brewsSection}}" not in content

    def test_publish_with_homebrew_env(self):
        """publish template renders HOMEBREW_TAP_TOKEN."""
        template = open(_template_path("publish.yml.tpl")).read()
        vars_ = {
            "homebrewEnv": (
                "\n          HOMEBREW_TAP_TOKEN:"
                " ${{ secrets.HOMEBREW_TAP_TOKEN }}"
            ),
            "npmPublishJobs": "",
        }
        content, _ = process_template(template, vars_)
        assert "HOMEBREW_TAP_TOKEN" in content

    def test_publish_without_homebrew_env(self):
        """publish template renders cleanly without HOMEBREW_TAP_TOKEN."""
        template = open(_template_path("publish.yml.tpl")).read()
        vars_ = {"homebrewEnv": "", "npmPublishJobs": ""}
        content, _ = process_template(template, vars_)
        assert "HOMEBREW_TAP_TOKEN" not in content


# ---------------------------------------------------------------------------
# Test class 4: npm wrapper template vars
# ---------------------------------------------------------------------------


class TestNpmWrapperTemplateVars:
    """Test npmScope and npmPublishJobs generation."""

    def test_npm_scope_with_config(self, tmp_path, monkeypatch):
        """npmScope set when npm_wrapper.scope configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            '{"npm_wrapper": {"scope": "@testuser"}}'
        )
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert vars_["npmScope"] == "@testuser"

    def test_npm_scope_empty_without_config(self, tmp_path, monkeypatch):
        """npmScope is empty when npm_wrapper not configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert vars_.get("npmScope", "") == ""

    def test_npm_publish_jobs_with_config(self, tmp_path, monkeypatch):
        """npmPublishJobs generated when npm_wrapper.scope configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            '{"npm_wrapper": {"scope": "@testuser"}}'
        )
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert "npm publish" in vars_.get("npmPublishJobs", "")
        assert "needs:" in vars_.get("npmPublishJobs", "")

    def test_npm_publish_jobs_empty_without_config(self, tmp_path, monkeypatch):
        """npmPublishJobs is empty when npm_wrapper not configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        assert vars_.get("npmPublishJobs", "") == ""


# ---------------------------------------------------------------------------
# Test class 5: npm wrapper template_mappings
# ---------------------------------------------------------------------------


class TestNpmWrapperTemplateMappings:
    """Test that template_mappings() includes npm wrapper files when configured."""

    def test_mappings_include_npm_wrapper_when_configured(
        self, tmp_path, monkeypatch
    ):
        """npm wrapper mappings included in shared_template_mappings when configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            '{"npm_wrapper": {"scope": "@testuser"}}'
        )
        mappings = TARGETS["go"].shared_template_mappings(str(tmp_path))
        targets = [m["target"] for m in mappings]
        assert "npm-wrapper/package.json" in targets
        assert "npm-wrapper/bin/index.js" in targets
        assert "npm-wrapper/linux-x64/package.json" in targets
        assert "npm-wrapper/win32-x64/package.json" in targets

    def test_mappings_exclude_npm_wrapper_when_not_configured(
        self, tmp_path, monkeypatch
    ):
        """npm wrapper mappings excluded from shared_template_mappings when not configured."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        mappings = TARGETS["go"].shared_template_mappings(str(tmp_path))
        targets = [m["target"] for m in mappings]
        assert "npm-wrapper/package.json" not in targets

    def test_mappings_exclude_npm_wrapper_for_libraries(
        self, tmp_path, monkeypatch
    ):
        """npm wrapper mappings excluded for Go libraries."""
        monkeypatch.chdir(tmp_path)
        # Create a library (no package main)
        (tmp_path / "go.mod").write_text(
            "module github.com/user/lib\n\ngo 1.21\n"
        )
        (tmp_path / "VERSION").write_text("1.0.0")
        (tmp_path / "lib.go").write_text("package lib\n")
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            '{"npm_wrapper": {"scope": "@user"}}'
        )
        mappings = TARGETS["go"].shared_template_mappings(str(tmp_path))
        targets = [m["target"] for m in mappings]
        assert "npm-wrapper/package.json" not in targets


# ---------------------------------------------------------------------------
# Test class 6: npm wrapper template rendering
# ---------------------------------------------------------------------------


class TestNpmWrapperTemplateRendering:
    """Test the npm wrapper templates render correctly."""

    def test_wrapper_package_json_renders(self):
        """Wrapper package.json template renders valid JSON."""
        template = open(_shared_template_path("npm-wrapper/package.json.tpl")).read()
        vars_ = {"npmScope": "@testuser", "binCommand": "mycli"}
        content, unreplaced = process_template(template, vars_)
        assert unreplaced == []
        data = json.loads(content)
        assert data["name"] == "@testuser/mycli"
        assert "@testuser/mycli-linux-x64" in data["optionalDependencies"]

    def test_platform_package_json_renders(self):
        """Platform package.json template renders valid JSON."""
        template = open(
            _shared_template_path("npm-wrapper/platform-linux-x64.json.tpl")
        ).read()
        vars_ = {"npmScope": "@testuser", "binCommand": "mycli"}
        content, _ = process_template(template, vars_)
        data = json.loads(content)
        assert data["name"] == "@testuser/mycli-linux-x64"
        assert data["os"] == ["linux"]
        assert data["cpu"] == ["x64"]

    def test_bin_script_renders(self):
        """Bin script template renders with correct platform mappings."""
        template = open(_shared_template_path("npm-wrapper/bin-index.js.tpl")).read()
        vars_ = {"npmScope": "@testuser", "binCommand": "mycli"}
        content, unreplaced = process_template(template, vars_)
        assert unreplaced == []
        assert "@testuser/mycli-linux-x64" in content
        assert "@testuser/mycli-win32-x64" in content
        assert "mycli.exe" in content  # Windows binary name


# ---------------------------------------------------------------------------
# Test class 7: no-config baseline
# ---------------------------------------------------------------------------


class TestNoConfigBaseline:
    """Verify no regressions when features are not configured."""

    def test_baseline_goreleaser_no_brews(self, tmp_path, monkeypatch):
        """Without homebrew config, goreleaser output has no brews section."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        template = open(_template_path("goreleaser.yml.tpl")).read()
        content, _ = process_template(template, vars_)
        assert "brews:" not in content

    def test_baseline_publish_no_homebrew_env(self, tmp_path, monkeypatch):
        """Without config, publish has no HOMEBREW_TAP_TOKEN."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        template = open(_template_path("publish.yml.tpl")).read()
        content, _ = process_template(template, vars_)
        assert "HOMEBREW_TAP_TOKEN" not in content

    def test_baseline_publish_no_npm_jobs(self, tmp_path, monkeypatch):
        """Without config, publish has no npm publish jobs."""
        monkeypatch.chdir(tmp_path)
        _setup_go_project(tmp_path)
        vars_ = TARGETS["go"].template_vars(str(tmp_path), str(tmp_path))
        template = open(_template_path("publish.yml.tpl")).read()
        content, _ = process_template(template, vars_)
        assert "npm publish" not in content


# ---------------------------------------------------------------------------
# Test class 8: publish go install for CLI projects
# ---------------------------------------------------------------------------


class TestGoPublishInstall:
    """Test that publish() runs `go install` for CLI projects and skips it for libraries."""

    def test_publish_installs_cli_project(self, tmp_path):
        """publish() calls `go install .` for a project with package main at root."""
        target = GoTarget()
        (tmp_path / "go.mod").write_text("module github.com/user/mycli\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")

        with patch("rlsbl.targets.go.run") as mock_run, \
             patch("subprocess.run") as mock_subprocess_run, \
             patch("shutil.which", return_value="/usr/bin/go"):
            mock_subprocess_run.return_value = subprocess.CompletedProcess(
                args=["go", "install", "."], returncode=0
            )
            target.publish(str(tmp_path), "1.0.0", ProjectContext(project_root=Path(str(tmp_path)), workspace_root=None, config={}))

            # Verify go install was called with "."
            mock_subprocess_run.assert_called_once_with(
                ["go", "install", "."],
                cwd=str(tmp_path),
                check=True,
            )

    def test_publish_installs_cmd_project(self, tmp_path):
        """publish() calls `go install ./cmd/<name>` for a project with cmd/ layout."""
        target = GoTarget()
        (tmp_path / "go.mod").write_text("module github.com/user/mycli\n\ngo 1.21\n")
        cmd_dir = tmp_path / "cmd" / "mycli"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text("package main\n\nfunc main() {}\n")

        with patch("rlsbl.targets.go.run") as mock_run, \
             patch("subprocess.run") as mock_subprocess_run, \
             patch("shutil.which", return_value="/usr/bin/go"):
            mock_subprocess_run.return_value = subprocess.CompletedProcess(
                args=["go", "install", "./cmd/mycli"], returncode=0
            )
            target.publish(str(tmp_path), "1.0.0", ProjectContext(project_root=Path(str(tmp_path)), workspace_root=None, config={}))

            mock_subprocess_run.assert_called_once_with(
                ["go", "install", "./cmd/mycli"],
                cwd=str(tmp_path),
                check=True,
            )

    def test_publish_skips_install_for_library(self, tmp_path):
        """publish() does NOT call `go install` for a library project."""
        target = GoTarget()
        (tmp_path / "go.mod").write_text("module github.com/user/mylib\n\ngo 1.21\n")
        (tmp_path / "mylib.go").write_text("package mylib\n\nfunc Hello() {}\n")

        with patch("rlsbl.targets.go.run") as mock_run, \
             patch("subprocess.run") as mock_subprocess_run, \
             patch("shutil.which", return_value="/usr/bin/go"):
            target.publish(str(tmp_path), "1.0.0", ProjectContext(project_root=Path(str(tmp_path)), workspace_root=None, config={}))

            # subprocess.run should NOT have been called (go install is skipped)
            mock_subprocess_run.assert_not_called()

    def test_publish_install_failure_is_non_fatal(self, tmp_path, capsys):
        """publish() prints a warning but doesn't raise when go install fails."""
        target = GoTarget()
        (tmp_path / "go.mod").write_text("module github.com/user/mycli\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")

        with patch("rlsbl.targets.go.run") as mock_run, \
             patch("subprocess.run") as mock_subprocess_run, \
             patch("shutil.which", return_value="/usr/bin/go"):
            mock_subprocess_run.side_effect = subprocess.CalledProcessError(
                1, ["go", "install", "."]
            )
            # Should not raise
            target.publish(str(tmp_path), "1.0.0", ProjectContext(project_root=Path(str(tmp_path)), workspace_root=None, config={}))

        captured = capsys.readouterr()
        assert "Warning: go install failed" in captured.out
