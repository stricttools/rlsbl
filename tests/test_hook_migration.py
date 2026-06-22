"""Tests for backward compatibility bridge: hook migration warning (Phase 4e).

Verifies:
1. Warning emitted when customized script exists without config
2. No warning when config has entries
3. No warning when script matches template (not customized)
4. No warning when no script exists
"""

import os

import pytest

from rlsbl.commands.release.hooks import warn_if_hook_needs_migration


# The V1 scaffold template content (matches a known template hash).
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


def _make_hook_file(tmp_path, body):
    """Write a pre-release.sh hook and return its path."""
    hooks_dir = tmp_path / ".rlsbl" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-release.sh"
    hook.write_text(body)
    hook.chmod(0o755)
    return str(hook)


class TestWarnIfHookNeedsMigration:
    """Tests for warn_if_hook_needs_migration."""

    def test_warning_emitted_when_customized_script_without_config(
        self, tmp_path, capsys
    ):
        """A customized script with no hooks config emits a migration warning."""
        config = {"private": False}
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        result = warn_if_hook_needs_migration(config, hook_path)
        assert result is True
        captured = capsys.readouterr()
        assert "Customized hook script found at" in captured.err
        assert "Migrate to config-driven hooks" in captured.err
        assert '"hooks": {"pre_release": ["<your commands here>"]}' in captured.err

    def test_no_warning_when_config_has_pre_release_entries(
        self, tmp_path, capsys
    ):
        """Config with hooks.pre_release entries -> no warning."""
        config = {"hooks": {"pre_release": ["uv run pytest"]}}
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        result = warn_if_hook_needs_migration(config, hook_path)
        assert result is False
        captured = capsys.readouterr()
        assert "Migrate to config-driven hooks" not in captured.err

    def test_no_warning_when_config_has_empty_pre_release(
        self, tmp_path, capsys
    ):
        """Config with empty hooks.pre_release list -> no warning (migration done)."""
        config = {"hooks": {"pre_release": []}}
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        result = warn_if_hook_needs_migration(config, hook_path)
        assert result is False
        captured = capsys.readouterr()
        assert "Migrate to config-driven hooks" not in captured.err

    def test_no_warning_when_script_matches_template(
        self, tmp_path, capsys
    ):
        """An unmodified scaffold template script -> no warning."""
        config = {"private": False}
        hook_path = _make_hook_file(tmp_path, _V1_TEMPLATE)
        result = warn_if_hook_needs_migration(config, hook_path)
        assert result is False
        captured = capsys.readouterr()
        assert "Migrate to config-driven hooks" not in captured.err

    def test_no_warning_when_no_script_exists(self, tmp_path, capsys):
        """No script file at all -> no warning."""
        config = {"private": False}
        hook_path = str(tmp_path / "nonexistent" / "pre-release.sh")
        result = warn_if_hook_needs_migration(config, hook_path)
        assert result is False
        captured = capsys.readouterr()
        assert "Migrate to config-driven hooks" not in captured.err

    def test_no_warning_with_none_config(self, tmp_path, capsys):
        """None config with no customized script -> no warning."""
        hook_path = str(tmp_path / "nonexistent" / "pre-release.sh")
        result = warn_if_hook_needs_migration(None, hook_path)
        assert result is False
        captured = capsys.readouterr()
        assert "Migrate to config-driven hooks" not in captured.err

    def test_warning_with_none_config_and_customized_script(
        self, tmp_path, capsys
    ):
        """None config with customized script -> warning emitted."""
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nnpm test\n")
        result = warn_if_hook_needs_migration(None, hook_path)
        assert result is True
        captured = capsys.readouterr()
        assert "Customized hook script found at" in captured.err

    def test_no_warning_with_hooks_section_without_pre_release(
        self, tmp_path, capsys
    ):
        """Config with hooks section but no pre_release key + customized script -> warning.

        The hooks section exists (for pre_checks), but since pre_release key
        is missing, this is a migration in progress. Warning is emitted.
        """
        config = {"hooks": {"pre_checks": ["echo check"]}}
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        result = warn_if_hook_needs_migration(config, hook_path)
        assert result is True
        captured = capsys.readouterr()
        assert "Migrate to config-driven hooks" in captured.err
