"""Tests for strictcli schema auto-dump during release."""

import inspect
import json
import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.release import run_cmd, _run_cmd_inner, _run_strictcli_schema_dump
from rlsbl.commands.release.validate import ReleaseValidationError
from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["pypi"],
        exclude=exclude or [],
    )


class TestStrictcliSchemaDumpFunction:
    """Tests for the _run_strictcli_schema_dump helper function."""

    def test_skipped_when_not_strictcli_project(self, tmp_path, capsys):
        """When the project does not use strictcli, nothing happens."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["click"]\n'
        )
        messages = []
        _run_strictcli_schema_dump(
            {}, lambda msg: messages.append(msg),
            project_dir=str(tmp_path),
        )
        assert not messages
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_dry_run_prints_what_would_happen(self, tmp_path, capsys):
        """In dry-run mode, prints what would happen without running the command."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        messages = []
        _run_strictcli_schema_dump(
            {"dry-run": True}, lambda msg: messages.append(msg),
            project_dir=str(tmp_path),
        )
        assert any("Would run: uv run myapp --dump-schema" in m for m in messages)

    def test_dry_run_silent_when_not_strictcli(self, tmp_path):
        """In dry-run mode with no strictcli, nothing is printed."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["click"]\n'
        )
        messages = []
        _run_strictcli_schema_dump(
            {"dry-run": True}, lambda msg: messages.append(msg),
            project_dir=str(tmp_path),
        )
        assert not messages

    def test_command_failure_raises_error(self, tmp_path):
        """When the dump command fails, ReleaseValidationError is raised."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        messages = []
        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.side_effect = subprocess.CalledProcessError(1, "uv")
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            with pytest.raises(ReleaseValidationError, match="schema dump failed"):
                _run_strictcli_schema_dump(
                    {}, lambda msg: messages.append(msg),
                    project_dir=str(tmp_path),
                )

    def test_timeout_raises_error(self, tmp_path):
        """When the dump command times out, ReleaseValidationError is raised."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        messages = []
        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.side_effect = subprocess.TimeoutExpired("uv", 30)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            with pytest.raises(ReleaseValidationError, match="timed out"):
                _run_strictcli_schema_dump(
                    {}, lambda msg: messages.append(msg),
                    project_dir=str(tmp_path),
                )

    def test_runs_uv_with_correct_args(self, tmp_path):
        """When strictcli is detected, runs uv run <entry_point> --dump-schema."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        messages = []
        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            _run_strictcli_schema_dump(
                {}, lambda msg: messages.append(msg),
                project_dir=str(tmp_path),
            )

        mock_sp.run.assert_called_once()
        call_args = mock_sp.run.call_args
        assert call_args[0][0] == ["uv", "run", "myapp", "--dump-schema"]
        assert call_args[1]["cwd"] == str(tmp_path)
        assert call_args[1]["timeout"] == 30

    def test_version_patched_after_successful_dump(self, tmp_path):
        """When version is provided, schema.json version key is updated after dump."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        schema_dir = tmp_path / ".strictcli"
        schema_dir.mkdir()
        schema_path = schema_dir / "schema.json"
        schema_path.write_text(json.dumps({"version": "1.0.0", "commands": []}))

        messages = []
        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            _run_strictcli_schema_dump(
                {}, lambda msg: messages.append(msg),
                project_dir=str(tmp_path),
                version="2.0.0",
            )

        data = json.loads(schema_path.read_text())
        assert data["version"] == "2.0.0"
        assert data["commands"] == []  # other keys preserved

    def test_version_not_patched_when_none(self, tmp_path):
        """When version is None, schema.json is not touched."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        schema_dir = tmp_path / ".strictcli"
        schema_dir.mkdir()
        schema_path = schema_dir / "schema.json"
        schema_path.write_text(json.dumps({"version": "1.0.0", "commands": []}))

        messages = []
        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            _run_strictcli_schema_dump(
                {}, lambda msg: messages.append(msg),
                project_dir=str(tmp_path),
            )

        data = json.loads(schema_path.read_text())
        assert data["version"] == "1.0.0"  # unchanged

    def test_version_patch_errors_on_missing_schema(self, tmp_path):
        """When version is provided but schema.json doesn't exist, error is raised."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        messages = []
        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            with pytest.raises(ReleaseValidationError, match="does not exist"):
                _run_strictcli_schema_dump(
                    {}, lambda msg: messages.append(msg),
                    project_dir=str(tmp_path),
                    version="2.0.0",
                )

    def test_version_patch_errors_on_missing_version_key(self, tmp_path):
        """When schema.json has no version key, error is raised."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n[project.scripts]\nmyapp = "myapp:main"\n'
        )
        schema_dir = tmp_path / ".strictcli"
        schema_dir.mkdir()
        schema_path = schema_dir / "schema.json"
        schema_path.write_text(json.dumps({"commands": []}))

        messages = []
        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            with pytest.raises(ReleaseValidationError, match="no 'version' key"):
                _run_strictcli_schema_dump(
                    {}, lambda msg: messages.append(msg),
                    project_dir=str(tmp_path),
                    version="2.0.0",
                )


class TestStrictcliSchemaOrdering:
    """Tests that the schema dump runs in the correct position in the release pipeline."""

    def test_schema_dump_after_pre_checks_hook(self):
        """Schema dump must run after the pre-checks hook."""
        source = inspect.getsource(_run_cmd_inner)
        pre_checks_pos = source.index("pre_checks_script")
        schema_pos = source.index("_run_strictcli_schema_dump(")

        assert pre_checks_pos < schema_pos, (
            "pre-checks hook must appear before _run_strictcli_schema_dump"
        )

    def test_schema_dump_before_selfdoc_check(self):
        """Schema dump must run before the selfdoc check."""
        source = inspect.getsource(_run_cmd_inner)
        schema_pos = source.index("_run_strictcli_schema_dump(")
        selfdoc_pos = source.index("_run_selfdoc_check(")

        assert schema_pos < selfdoc_pos, (
            "_run_strictcli_schema_dump must appear before _run_selfdoc_check"
        )

    def test_schema_dump_before_preflight(self):
        """Schema dump must run before test/lint preflight checks."""
        source = inspect.getsource(_run_cmd_inner)
        schema_pos = source.index("_run_strictcli_schema_dump(")
        # Find the test/lint preflight (tag_expr="preflight"), not the
        # changelog preflight (tag_expr="preflight-changelog")
        preflight_pos = source.index('tag_expr="preflight"')

        assert schema_pos < preflight_pos, (
            "_run_strictcli_schema_dump must appear before test/lint preflight"
        )
