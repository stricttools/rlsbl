"""Tests for config-based hook customization detection (Phase 4c-d).

Verifies:
- Config-driven pre_release detection takes precedence over script hashing
- Backward compatibility: no hooks config section falls back to script hash
- Releasable-level config-driven detection
- Config-driven hooks produce files that appear in hook_generated set
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.release.hooks import (
    _is_hook_effectively_empty,
    is_hook_customized,
    is_releasable_hook_customized,
)
from rlsbl.commands.release.validate import parse_porcelain_paths

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

_WORKSPACE_DIR = ".rlsbl-monorepo"


def _make_hook_file(tmp_path, body):
    """Write a pre-release.sh hook and return its path."""
    hooks_dir = tmp_path / ".rlsbl" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-release.sh"
    hook.write_text(body)
    hook.chmod(0o755)
    return str(hook)


def _make_releasable_workspace(tmp_path, releasable_name, members):
    """Create minimal releasable workspace structure."""
    ws_dir = tmp_path / _WORKSPACE_DIR
    rel_dir = ws_dir / "releasables" / releasable_name
    rel_dir.mkdir(parents=True, exist_ok=True)
    # Create workspace.toml
    lines = ['[workspace]\nname = "test-ws"\n\n']
    for m in members:
        lines.append(f'[[projects]]\nname = "{m["name"]}"\npath = "{m["path"]}"\n')
        if "releasable" in m:
            lines.append(f'releasable = "{m["releasable"]}"\n')
        lines.append("\n")
    lines.append(f'[[releasables]]\nname = "{releasable_name}"\n')
    (ws_dir / "workspace.toml").write_text("".join(lines))
    return rel_dir


def _make_releasable_hook(tmp_path, releasable_name, hook_name, body):
    """Write a releasable-level hook script."""
    hook_dir = tmp_path / _WORKSPACE_DIR / "releasables" / releasable_name / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook = hook_dir / hook_name
    hook.write_text(body)
    hook.chmod(0o755)
    return str(hook)


# ---------------------------------------------------------------------------
# Phase 4c: is_hook_customized tests
# ---------------------------------------------------------------------------


class TestIsHookCustomized:
    """Tests for config-based hook customization detection."""

    def test_config_with_pre_release_entries_is_customized(self, tmp_path):
        """Config with hooks.pre_release entries -> customized=True."""
        config = {"hooks": {"pre_release": ["uv run pytest", "uv run ruff check ."]}}
        hook_path = str(tmp_path / "nonexistent" / "pre-release.sh")
        assert is_hook_customized(config, hook_path) is True

    def test_config_with_empty_pre_release_not_customized(self, tmp_path):
        """Config with empty hooks.pre_release list -> customized=False."""
        config = {"hooks": {"pre_release": []}}
        hook_path = str(tmp_path / "nonexistent" / "pre-release.sh")
        assert is_hook_customized(config, hook_path) is False

    def test_no_hooks_section_customized_script_is_customized(self, tmp_path):
        """No hooks config section + customized script -> customized=True (backward compat)."""
        config = {"private": False}
        hook_path = _make_hook_file(tmp_path, _V1_TEMPLATE + "npm test\n")
        assert is_hook_customized(config, hook_path) is True

    def test_no_hooks_section_template_script_not_customized(self, tmp_path):
        """No hooks config section + template script -> customized=False."""
        config = {"private": False}
        hook_path = _make_hook_file(tmp_path, _V1_TEMPLATE)
        assert is_hook_customized(config, hook_path) is False

    def test_no_hooks_section_no_script_not_customized(self, tmp_path):
        """No hooks config section + missing script -> customized=False."""
        config = {"private": False}
        hook_path = str(tmp_path / "nonexistent" / "pre-release.sh")
        assert is_hook_customized(config, hook_path) is False

    def test_hooks_section_without_pre_release_key_falls_back(self, tmp_path):
        """hooks section without pre_release key falls back to script check."""
        config = {"hooks": {"pre_checks": ["echo check"]}}
        # Customized script -> should detect as customized via fallback
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        assert is_hook_customized(config, hook_path) is True

    def test_hooks_section_without_pre_release_key_template_script(self, tmp_path):
        """hooks section without pre_release key + template script -> not customized."""
        config = {"hooks": {"pre_checks": ["echo check"]}}
        hook_path = _make_hook_file(tmp_path, _V1_TEMPLATE)
        assert is_hook_customized(config, hook_path) is False

    def test_config_pre_release_overrides_customized_script(self, tmp_path):
        """Config with empty pre_release overrides a customized script file."""
        config = {"hooks": {"pre_release": []}}
        # Even though the script is customized, config says "empty" -> not customized
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        assert is_hook_customized(config, hook_path) is False

    def test_config_pre_release_overrides_missing_script(self, tmp_path):
        """Config with pre_release entries overrides missing script -> customized."""
        config = {"hooks": {"pre_release": ["npm test"]}}
        hook_path = str(tmp_path / "nonexistent" / "pre-release.sh")
        assert is_hook_customized(config, hook_path) is True

    def test_non_dict_config_falls_back(self, tmp_path):
        """Non-dict config falls back to script check."""
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        assert is_hook_customized(None, hook_path) is True

    def test_non_dict_hooks_section_falls_back(self, tmp_path):
        """Non-dict hooks section falls back to script check."""
        config = {"hooks": "invalid"}
        hook_path = _make_hook_file(tmp_path, "#!/bin/bash\nmake test\n")
        assert is_hook_customized(config, hook_path) is True

    def test_non_list_pre_release_raises(self, tmp_path):
        """Non-list pre_release value raises HookError."""
        from rlsbl.commands.release.validate import HookError
        config = {"hooks": {"pre_release": "not a list"}}
        hook_path = str(tmp_path / "nonexistent" / "pre-release.sh")
        with pytest.raises(HookError, match="must be a list"):
            is_hook_customized(config, hook_path)


# ---------------------------------------------------------------------------
# Phase 4c: is_releasable_hook_customized tests
# ---------------------------------------------------------------------------


class TestIsReleasableHookCustomizedWithConfig:
    """Tests for releasable-level config-driven hook customization detection."""

    def test_releasable_config_with_pre_release_is_customized(self, tmp_path):
        """Releasable config with hooks.pre_release entries -> customized=True."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        config = {"hooks": {"pre_release": ["npm test"]}}
        assert is_releasable_hook_customized(str(tmp_path), "www", config=config) is True

    def test_releasable_config_with_empty_pre_release_not_customized(self, tmp_path):
        """Releasable config with empty hooks.pre_release -> customized=False."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        config = {"hooks": {"pre_release": []}}
        assert is_releasable_hook_customized(str(tmp_path), "www", config=config) is False

    def test_releasable_no_config_falls_back_to_script(self, tmp_path):
        """No config passed -> falls back to script hash check."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        _make_releasable_hook(tmp_path, "www", "pre-release.sh",
                              "#!/bin/bash\nnpm run build\n")
        assert is_releasable_hook_customized(str(tmp_path), "www") is True

    def test_releasable_no_config_template_script_not_customized(self, tmp_path):
        """No config + template script -> not customized."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        _make_releasable_hook(tmp_path, "www", "pre-release.sh", _V1_TEMPLATE)
        assert is_releasable_hook_customized(str(tmp_path), "www") is False

    def test_releasable_config_no_hooks_section_customized_script(self, tmp_path):
        """Config without hooks section + customized script -> customized=True (backward compat)."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        _make_releasable_hook(tmp_path, "www", "pre-release.sh",
                              "#!/bin/bash\nnpm run build\n")
        config = {"private": False}
        assert is_releasable_hook_customized(str(tmp_path), "www", config=config) is True


# ---------------------------------------------------------------------------
# Phase 4d: hook_generated dirty snapshot with config-driven hooks
# ---------------------------------------------------------------------------


class TestHookGeneratedDirtySnapshot:
    """Tests verifying the dirty-snapshot mechanism works with config-driven hooks.

    The dirty snapshot in __init__.py captures pre_hook_dirty (before hooks)
    and post_hook_dirty (after hooks), then computes hook_generated as the
    difference. This works regardless of whether hooks are scripts or
    config-driven commands since both produce filesystem changes captured
    by git status --porcelain.
    """

    def test_parse_porcelain_new_file(self):
        """A new untracked file appears in parse_porcelain_paths output."""
        output = "?? generated.txt\n"
        result = parse_porcelain_paths(output)
        assert "generated.txt" in result

    def test_parse_porcelain_modified_file(self):
        """A modified tracked file appears in parse_porcelain_paths output."""
        output = " M src/config.py\n"
        result = parse_porcelain_paths(output)
        assert "src/config.py" in result

    def test_hook_generated_set_creation(self):
        """The hook_generated set is post_hook_dirty - pre_hook_dirty."""
        pre_hook_dirty = {"existing.txt"}
        post_hook_dirty = {"existing.txt", "new_file.txt", "modified.py"}
        hook_generated = post_hook_dirty - pre_hook_dirty
        assert hook_generated == {"new_file.txt", "modified.py"}

    def test_hook_generated_empty_when_no_changes(self):
        """No filesystem changes -> empty hook_generated."""
        pre_hook_dirty = {"existing.txt"}
        post_hook_dirty = {"existing.txt"}
        hook_generated = post_hook_dirty - pre_hook_dirty
        assert hook_generated == set()

    def test_config_driven_command_creates_file_appears_in_generated(
        self, tmp_path, monkeypatch
    ):
        """A config-driven hook command that creates a file should be
        captured by the dirty snapshot mechanism.

        This test simulates the full flow: run_config_hooks creates a file,
        git status --porcelain shows it, parse_porcelain_paths picks it up,
        and the set difference produces the correct hook_generated set.
        """
        import subprocess as real_subprocess

        # Set up a minimal git repo
        monkeypatch.chdir(tmp_path)
        real_subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(tmp_path), check=True,
        )
        real_subprocess.run(
            ["git", "config", "user.email", "test@test.local"],
            cwd=str(tmp_path), check=True,
        )
        real_subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True,
        )
        (tmp_path / "README.md").write_text("# test\n")
        real_subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(tmp_path), check=True,
        )
        real_subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=str(tmp_path), check=True,
        )

        # Snapshot before hook
        pre_output = real_subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path), capture_output=True, text=True, check=True,
        ).stdout
        pre_hook_dirty = parse_porcelain_paths(pre_output) if pre_output else set()

        # Simulate config-driven hook: create a file (as the hook command would)
        (tmp_path / "generated_by_hook.txt").write_text("hook output\n")

        # Snapshot after hook
        post_output = real_subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path), capture_output=True, text=True, check=True,
        ).stdout
        post_hook_dirty = parse_porcelain_paths(post_output) if post_output else set()

        hook_generated = post_hook_dirty - pre_hook_dirty
        assert "generated_by_hook.txt" in hook_generated

    def test_config_driven_command_modifies_file_appears_in_generated(
        self, tmp_path, monkeypatch
    ):
        """A config-driven hook command that modifies a tracked file should
        appear in the hook_generated set.
        """
        import subprocess as real_subprocess

        monkeypatch.chdir(tmp_path)
        real_subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(tmp_path), check=True,
        )
        real_subprocess.run(
            ["git", "config", "user.email", "test@test.local"],
            cwd=str(tmp_path), check=True,
        )
        real_subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True,
        )
        tracked_file = tmp_path / "config.json"
        tracked_file.write_text('{"version": "1.0.0"}\n')
        real_subprocess.run(
            ["git", "add", "config.json"],
            cwd=str(tmp_path), check=True,
        )
        real_subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=str(tmp_path), check=True,
        )

        # Snapshot before hook
        pre_output = real_subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path), capture_output=True, text=True, check=True,
        ).stdout
        pre_hook_dirty = parse_porcelain_paths(pre_output) if pre_output else set()

        # Simulate config-driven hook: modify the tracked file
        tracked_file.write_text('{"version": "1.0.1"}\n')

        # Snapshot after hook
        post_output = real_subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path), capture_output=True, text=True, check=True,
        ).stdout
        post_hook_dirty = parse_porcelain_paths(post_output) if post_output else set()

        hook_generated = post_hook_dirty - pre_hook_dirty
        assert "config.json" in hook_generated
