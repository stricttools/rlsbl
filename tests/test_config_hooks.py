"""Tests for config-driven hook execution (Phase 4a-b).

Covers:
1. normalize_hook_entry handles strings and dicts correctly
2. run_config_hooks runs commands in order
3. run_config_hooks respects dir field
4. run_config_hooks merges env field
5. Pre-release hook failure aborts (raises HookError)
6. Post-release hook failure does not abort (logs warning)
7. Config hooks take precedence over script files
8. Script fallback when config has no entries for a hook
9. Both string and structured entries work in the same list
10. Releasable-level hooks inherited through config
"""

import os
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from rlsbl.commands.release.hooks import (
    _get_config_hooks,
    is_hook_customized,
    normalize_hook_entry,
    run_config_hooks,
    run_release_hook,
    run_releasable_hooks,
)
from rlsbl.commands.release.validate import HookError


# ---------------------------------------------------------------------------
# 1. normalize_hook_entry handles strings and dicts correctly
# ---------------------------------------------------------------------------

class TestNormalizeHookEntry:
    """Tests for normalize_hook_entry."""

    def test_string_entry_becomes_cmd_dict(self):
        """A plain string is converted to {"cmd": <string>}."""
        result = normalize_hook_entry("npm test")
        assert result == {"cmd": "npm test"}

    def test_dict_entry_with_cmd_only(self):
        """A dict with just cmd passes through."""
        result = normalize_hook_entry({"cmd": "uv run pytest"})
        assert result == {"cmd": "uv run pytest"}

    def test_dict_entry_with_all_fields(self):
        """A dict with cmd, dir, and env passes through."""
        entry = {"cmd": "npm test", "dir": "./sub", "env": {"NODE_ENV": "test"}}
        result = normalize_hook_entry(entry)
        assert result == {"cmd": "npm test", "dir": "./sub", "env": {"NODE_ENV": "test"}}

    def test_dict_entry_is_copied(self):
        """The returned dict is a copy, not a reference to the original."""
        original = {"cmd": "npm test", "env": {"A": "1"}}
        result = normalize_hook_entry(original)
        result["cmd"] = "changed"
        assert original["cmd"] == "npm test"

    def test_dict_missing_cmd_raises(self):
        """A dict without cmd raises HookError."""
        with pytest.raises(HookError, match="missing required 'cmd' field"):
            normalize_hook_entry({"dir": "./sub"})

    def test_invalid_dir_type_raises(self):
        """A dict with non-string dir raises HookError."""
        with pytest.raises(HookError, match="'dir' must be a string"):
            normalize_hook_entry({"cmd": "test", "dir": 42})

    def test_invalid_env_type_raises(self):
        """A dict with non-dict env raises HookError."""
        with pytest.raises(HookError, match="'env' must be a dict"):
            normalize_hook_entry({"cmd": "test", "env": "bad"})

    def test_invalid_type_raises(self):
        """A non-string, non-dict value raises HookError."""
        with pytest.raises(HookError, match="must be a string or dict"):
            normalize_hook_entry(42)

    def test_empty_string_is_valid(self):
        """An empty string is technically valid (will be an empty bash command)."""
        result = normalize_hook_entry("")
        assert result == {"cmd": ""}

    def test_list_type_raises(self):
        """A list value raises HookError."""
        with pytest.raises(HookError, match="must be a string or dict"):
            normalize_hook_entry(["cmd", "arg"])


# ---------------------------------------------------------------------------
# Helper: mock subprocess for hook execution tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_subprocess():
    """Patch subprocess at the rlsbl.commands.release package level."""
    mock = MagicMock()
    mock.CalledProcessError = subprocess.CalledProcessError
    mock.TimeoutExpired = subprocess.TimeoutExpired
    mock.run = MagicMock(return_value=MagicMock(returncode=0))
    with patch("rlsbl.commands.release.subprocess", mock):
        yield mock


# ---------------------------------------------------------------------------
# 2. run_config_hooks runs commands in order
# ---------------------------------------------------------------------------

class TestRunConfigHooksOrder:
    """Test that run_config_hooks runs commands sequentially."""

    def test_runs_commands_in_order(self, mock_subprocess):
        """Commands are executed in list order."""
        config = {
            "hooks": {
                "pre_checks": ["echo first", "echo second", "echo third"],
            },
        }
        result = run_config_hooks(
            "pre-checks", config, "/project", {"PATH": "/usr/bin"}, 30,
        )
        assert result is True
        assert mock_subprocess.run.call_count == 3
        calls = mock_subprocess.run.call_args_list
        assert calls[0][0][0] == ["bash", "-c", "echo first"]
        assert calls[1][0][0] == ["bash", "-c", "echo second"]
        assert calls[2][0][0] == ["bash", "-c", "echo third"]

    def test_empty_list_returns_true(self, mock_subprocess):
        """An empty list means config owns the hook slot (no commands to run)."""
        config = {"hooks": {"pre_checks": []}}
        result = run_config_hooks(
            "pre-checks", config, "/project", {}, 30,
        )
        assert result is True
        mock_subprocess.run.assert_not_called()

    def test_no_config_returns_false(self, mock_subprocess):
        """Missing hooks section returns False (fall back to script)."""
        config = {}
        result = run_config_hooks(
            "pre-checks", config, "/project", {}, 30,
        )
        assert result is False
        mock_subprocess.run.assert_not_called()

    def test_no_hook_slot_returns_false(self, mock_subprocess):
        """Hooks section exists but missing the specific slot returns False."""
        config = {"hooks": {"post_release": ["echo done"]}}
        result = run_config_hooks(
            "pre-checks", config, "/project", {}, 30,
        )
        assert result is False
        mock_subprocess.run.assert_not_called()


# ---------------------------------------------------------------------------
# 3. run_config_hooks respects dir field
# ---------------------------------------------------------------------------

class TestRunConfigHooksDir:
    """Test that the dir field sets the working directory."""

    def test_dir_relative_to_project(self, mock_subprocess):
        """Dir field is joined with project_dir."""
        config = {
            "hooks": {
                "pre_checks": [{"cmd": "npm test", "dir": "packages/web"}],
            },
        }
        run_config_hooks(
            "pre-checks", config, "/project", {}, 30,
        )
        _, kwargs = mock_subprocess.run.call_args
        assert kwargs["cwd"] == os.path.join("/project", "packages/web")

    def test_no_dir_uses_project_dir(self, mock_subprocess):
        """Without dir field, cwd is the project directory."""
        config = {
            "hooks": {
                "pre_checks": ["npm test"],
            },
        }
        run_config_hooks(
            "pre-checks", config, "/project", {}, 30,
        )
        _, kwargs = mock_subprocess.run.call_args
        assert kwargs["cwd"] == "/project"


# ---------------------------------------------------------------------------
# 4. run_config_hooks merges env field
# ---------------------------------------------------------------------------

class TestRunConfigHooksEnv:
    """Test that the env field merges with the base environment."""

    def test_env_merged_with_base(self, mock_subprocess):
        """Entry env vars are merged on top of base env."""
        config = {
            "hooks": {
                "pre_release": [
                    {"cmd": "pytest", "env": {"COVERAGE": "1", "VERBOSE": "true"}},
                ],
            },
        }
        base_env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        run_config_hooks(
            "pre-release", config, "/project", base_env, 30,
        )
        _, kwargs = mock_subprocess.run.call_args
        passed_env = kwargs["env"]
        # Base env preserved
        assert passed_env["PATH"] == "/usr/bin"
        assert passed_env["HOME"] == "/home/user"
        # Entry env merged
        assert passed_env["COVERAGE"] == "1"
        assert passed_env["VERBOSE"] == "true"

    def test_entry_env_overrides_base(self, mock_subprocess):
        """Entry env vars override conflicting base env vars."""
        config = {
            "hooks": {
                "pre_release": [
                    {"cmd": "pytest", "env": {"NODE_ENV": "test"}},
                ],
            },
        }
        base_env = {"NODE_ENV": "production", "PATH": "/usr/bin"}
        run_config_hooks(
            "pre-release", config, "/project", base_env, 30,
        )
        _, kwargs = mock_subprocess.run.call_args
        assert kwargs["env"]["NODE_ENV"] == "test"

    def test_no_env_passes_base_only(self, mock_subprocess):
        """Without env field, only base env is used."""
        config = {"hooks": {"pre_release": ["pytest"]}}
        base_env = {"PATH": "/usr/bin"}
        run_config_hooks(
            "pre-release", config, "/project", base_env, 30,
        )
        _, kwargs = mock_subprocess.run.call_args
        assert kwargs["env"] == {"PATH": "/usr/bin"}

    def test_base_env_not_mutated(self, mock_subprocess):
        """The base env dict must not be mutated by entry env merging."""
        config = {
            "hooks": {
                "pre_release": [
                    {"cmd": "test", "env": {"EXTRA": "val"}},
                ],
            },
        }
        base_env = {"PATH": "/usr/bin"}
        run_config_hooks(
            "pre-release", config, "/project", base_env, 30,
        )
        assert "EXTRA" not in base_env


# ---------------------------------------------------------------------------
# 5. Pre-release hook failure aborts (raises HookError)
# ---------------------------------------------------------------------------

class TestPreReleaseHookFailure:
    """Test that pre-release/pre-checks hooks abort on failure."""

    def test_pre_checks_failure_raises(self, mock_subprocess):
        """Non-zero exit on pre-checks raises HookError."""
        mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            1, ["bash", "-c", "failing-cmd"]
        )
        config = {"hooks": {"pre_checks": ["failing-cmd"]}}
        with pytest.raises(HookError, match="pre-checks hook command failed"):
            run_config_hooks(
                "pre-checks", config, "/project", {}, 30,
            )

    def test_pre_release_failure_raises(self, mock_subprocess):
        """Non-zero exit on pre-release raises HookError."""
        mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            2, ["bash", "-c", "bad-cmd"]
        )
        config = {"hooks": {"pre_release": ["bad-cmd"]}}
        with pytest.raises(HookError, match="pre-release hook command failed"):
            run_config_hooks(
                "pre-release", config, "/project", {}, 30,
            )

    def test_timeout_raises(self, mock_subprocess):
        """Timeout on fatal hook raises HookError."""
        mock_subprocess.run.side_effect = subprocess.TimeoutExpired(
            ["bash", "-c", "slow-cmd"], 30
        )
        config = {"hooks": {"pre_checks": ["slow-cmd"]}}
        with pytest.raises(HookError, match="timed out after 30s"):
            run_config_hooks(
                "pre-checks", config, "/project", {}, 30,
            )

    def test_first_failure_stops_execution(self, mock_subprocess):
        """After the first failure, subsequent commands are not run."""
        mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            1, ["bash", "-c", "cmd1"]
        )
        config = {"hooks": {"pre_checks": ["cmd1", "cmd2", "cmd3"]}}
        with pytest.raises(HookError):
            run_config_hooks(
                "pre-checks", config, "/project", {}, 30,
            )
        assert mock_subprocess.run.call_count == 1


# ---------------------------------------------------------------------------
# 6. Post-release hook failure does not abort (logs warning)
# ---------------------------------------------------------------------------

class TestPostReleaseHookFailure:
    """Test that post-release hooks log warnings instead of raising."""

    def test_post_release_failure_warns(self, mock_subprocess, capsys):
        """Non-zero exit on post-release logs warning, does not raise."""
        mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            1, ["bash", "-c", "deploy-cmd"]
        )
        config = {"hooks": {"post_release": ["deploy-cmd"]}}
        # Should NOT raise
        result = run_config_hooks(
            "post-release", config, "/project", {}, 30, fatal=False,
        )
        assert result is True
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "deploy-cmd" in captured.err

    def test_post_release_timeout_warns(self, mock_subprocess, capsys):
        """Timeout on post-release logs warning, does not raise."""
        mock_subprocess.run.side_effect = subprocess.TimeoutExpired(
            ["bash", "-c", "slow-deploy"], 60
        )
        config = {"hooks": {"post_release": ["slow-deploy"]}}
        result = run_config_hooks(
            "post-release", config, "/project", {}, 60, fatal=False,
        )
        assert result is True
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "timed out" in captured.err

    def test_post_release_continues_after_failure(self, mock_subprocess, capsys):
        """Post-release runs all commands even if some fail."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.CalledProcessError(
                    1, ["bash", "-c", "cmd1"]
                )
            # Second call succeeds

        mock_subprocess.run.side_effect = side_effect
        config = {"hooks": {"post_release": ["cmd1", "cmd2"]}}
        run_config_hooks(
            "post-release", config, "/project", {}, 30, fatal=False,
        )
        assert mock_subprocess.run.call_count == 2


# ---------------------------------------------------------------------------
# 7. Config hooks take precedence over script files
# ---------------------------------------------------------------------------

class TestConfigPrecedence:
    """Test that config hooks take priority over script files."""

    def test_config_overrides_script(self, mock_subprocess, tmp_path):
        """When config has entries, the script file is ignored."""
        # Create a script file
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / "pre-checks.sh"
        script.write_text("#!/bin/bash\necho script\n")
        script.chmod(0o755)

        config = {"hooks": {"pre_checks": ["echo config"]}}
        run_release_hook(
            "pre-checks",
            str(script),
            str(tmp_path),
            {"PATH": "/usr/bin"},
            30,
            config=config,
        )
        # Should have run the config command, not the script
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", "-c", "echo config"]

    def test_config_empty_list_overrides_script(self, mock_subprocess, tmp_path):
        """An empty config list means config owns the slot (no script fallback)."""
        # Create a script file
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / "pre-checks.sh"
        script.write_text("#!/bin/bash\necho script\n")
        script.chmod(0o755)

        config = {"hooks": {"pre_checks": []}}
        run_release_hook(
            "pre-checks",
            str(script),
            str(tmp_path),
            {},
            30,
            config=config,
        )
        # Config claimed the slot (empty list), so no subprocess call at all
        mock_subprocess.run.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Script fallback when config has no entries for a hook
# ---------------------------------------------------------------------------

class TestScriptFallback:
    """Test that script files are used when config has no entries."""

    def test_fallback_to_script_no_hooks_section(self, mock_subprocess, tmp_path):
        """Config without hooks section falls back to script."""
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / "pre-checks.sh"
        script.write_text("#!/bin/bash\necho script\n")
        script.chmod(0o755)

        config = {"publish_mode": "ci"}
        run_release_hook(
            "pre-checks",
            str(script),
            str(tmp_path),
            {},
            30,
            config=config,
        )
        # Should have run the script file
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", str(script)]

    def test_fallback_to_script_different_slot(self, mock_subprocess, tmp_path):
        """Config has hooks for a different slot, falls back for this one."""
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / "pre-release.sh"
        script.write_text("#!/bin/bash\necho script\n")
        script.chmod(0o755)

        config = {"hooks": {"pre_checks": ["echo config"]}}
        run_release_hook(
            "pre-release",
            str(script),
            str(tmp_path),
            {},
            30,
            config=config,
        )
        # Should have run the script file (pre-release not in config)
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", str(script)]

    def test_no_config_no_script_is_noop(self, mock_subprocess, tmp_path):
        """No config and no script means nothing happens."""
        run_release_hook(
            "pre-checks",
            str(tmp_path / "nonexistent.sh"),
            str(tmp_path),
            {},
            30,
            config={},
        )
        mock_subprocess.run.assert_not_called()

    def test_none_config_uses_script(self, mock_subprocess, tmp_path):
        """config=None (default) always uses script-based path."""
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / "pre-checks.sh"
        script.write_text("#!/bin/bash\necho script\n")
        script.chmod(0o755)

        run_release_hook(
            "pre-checks",
            str(script),
            str(tmp_path),
            {},
            30,
        )
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", str(script)]


# ---------------------------------------------------------------------------
# 9. Both string and structured entries work in the same list
# ---------------------------------------------------------------------------

class TestMixedEntries:
    """Test that string and dict entries can coexist in one hook list."""

    def test_mixed_list(self, mock_subprocess):
        """A list with both strings and dicts runs all entries."""
        config = {
            "hooks": {
                "pre_checks": [
                    "echo simple",
                    {"cmd": "echo structured", "dir": "./sub"},
                    "echo another",
                    {"cmd": "echo envd", "env": {"FOO": "bar"}},
                ],
            },
        }
        run_config_hooks(
            "pre-checks", config, "/project", {"PATH": "/bin"}, 30,
        )
        assert mock_subprocess.run.call_count == 4

        calls = mock_subprocess.run.call_args_list

        # First: string entry
        assert calls[0][0][0] == ["bash", "-c", "echo simple"]
        assert calls[0][1]["cwd"] == "/project"

        # Second: dict with dir
        assert calls[1][0][0] == ["bash", "-c", "echo structured"]
        assert calls[1][1]["cwd"] == os.path.join("/project", "./sub")

        # Third: string entry
        assert calls[2][0][0] == ["bash", "-c", "echo another"]
        assert calls[2][1]["cwd"] == "/project"

        # Fourth: dict with env
        assert calls[3][0][0] == ["bash", "-c", "echo envd"]
        assert calls[3][1]["env"]["FOO"] == "bar"
        assert calls[3][1]["env"]["PATH"] == "/bin"


# ---------------------------------------------------------------------------
# 10. Releasable-level hooks inherited through config
# ---------------------------------------------------------------------------

class TestReleasableConfigInheritance:
    """Test that releasable-level config hooks work with run_releasable_hooks."""

    def test_releasable_config_hooks_run(self, mock_subprocess, tmp_path):
        """Releasable-level config hooks are executed."""
        # No script files exist
        workspace_root = str(tmp_path)
        # Create the releasable hooks dir (empty -- no scripts)
        rel_hooks_dir = (
            tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "hooks"
        )
        rel_hooks_dir.mkdir(parents=True)

        releasable_config = {
            "hooks": {
                "pre_checks": ["echo releasable-check"],
            },
        }
        log = MagicMock()

        run_releasable_hooks(
            "pre-checks",
            workspace_root,
            "myrel",
            [],  # no member packages
            {"PATH": "/bin"},
            30,
            log,
            releasable_config=releasable_config,
        )
        # Config hook should have run
        assert mock_subprocess.run.call_count == 1
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", "-c", "echo releasable-check"]

    def test_package_config_hooks_override_scripts(self, mock_subprocess, tmp_path):
        """Per-package config hooks override per-package script files."""
        workspace_root = str(tmp_path)
        # Create releasable hooks dir (no scripts)
        rel_hooks_dir = (
            tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "hooks"
        )
        rel_hooks_dir.mkdir(parents=True)

        # Create per-package script (should be ignored)
        pkg_dir = tmp_path / "packages" / "mypkg"
        pkg_hooks = pkg_dir / ".rlsbl" / "hooks"
        pkg_hooks.mkdir(parents=True)
        script = pkg_hooks / "pre-checks.sh"
        script.write_text("#!/bin/bash\necho package-script\n")
        script.chmod(0o755)

        package_configs = {
            "mypkg": {"hooks": {"pre_checks": ["echo package-config"]}},
        }
        log = MagicMock()

        run_releasable_hooks(
            "pre-checks",
            workspace_root,
            "myrel",
            [("mypkg", str(pkg_dir))],
            {"PATH": "/bin"},
            30,
            log,
            package_configs=package_configs,
        )
        # Only the config command should run (not the script)
        assert mock_subprocess.run.call_count == 1
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", "-c", "echo package-config"]

    def test_mixed_releasable_and_package_hooks(self, mock_subprocess, tmp_path):
        """Releasable config + package config both run in correct order."""
        workspace_root = str(tmp_path)
        rel_hooks_dir = (
            tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "hooks"
        )
        rel_hooks_dir.mkdir(parents=True)

        pkg_dir = tmp_path / "packages" / "mypkg"
        pkg_dir.mkdir(parents=True)

        releasable_config = {
            "hooks": {"pre_checks": ["echo releasable"]},
        }
        package_configs = {
            "mypkg": {"hooks": {"pre_checks": ["echo package"]}},
        }
        log = MagicMock()

        # pre-checks: releasable first, then per-package
        run_releasable_hooks(
            "pre-checks",
            workspace_root,
            "myrel",
            [("mypkg", str(pkg_dir))],
            {"PATH": "/bin"},
            30,
            log,
            releasable_config=releasable_config,
            package_configs=package_configs,
        )
        assert mock_subprocess.run.call_count == 2
        calls = mock_subprocess.run.call_args_list
        # Releasable first
        assert calls[0][0][0] == ["bash", "-c", "echo releasable"]
        # Package second
        assert calls[1][0][0] == ["bash", "-c", "echo package"]

    def test_pre_release_order_reversed(self, mock_subprocess, tmp_path):
        """pre-release runs per-package first, then releasable."""
        workspace_root = str(tmp_path)
        rel_hooks_dir = (
            tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "hooks"
        )
        rel_hooks_dir.mkdir(parents=True)

        pkg_dir = tmp_path / "packages" / "mypkg"
        pkg_dir.mkdir(parents=True)

        releasable_config = {
            "hooks": {"pre_release": ["echo releasable"]},
        }
        package_configs = {
            "mypkg": {"hooks": {"pre_release": ["echo package"]}},
        }
        log = MagicMock()

        run_releasable_hooks(
            "pre-release",
            workspace_root,
            "myrel",
            [("mypkg", str(pkg_dir))],
            {"PATH": "/bin"},
            30,
            log,
            releasable_config=releasable_config,
            package_configs=package_configs,
        )
        assert mock_subprocess.run.call_count == 2
        calls = mock_subprocess.run.call_args_list
        # Package first for pre-release
        assert calls[0][0][0] == ["bash", "-c", "echo package"]
        # Releasable second
        assert calls[1][0][0] == ["bash", "-c", "echo releasable"]


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

class TestGetConfigHooks:
    """Tests for _get_config_hooks helper."""

    def test_returns_none_for_empty_config(self):
        assert _get_config_hooks("pre-checks", {}) is None

    def test_returns_none_for_non_dict_config(self):
        assert _get_config_hooks("pre-checks", "not-a-dict") is None

    def test_returns_none_for_non_dict_hooks(self):
        assert _get_config_hooks("pre-checks", {"hooks": "bad"}) is None

    def test_returns_none_for_missing_slot(self):
        assert _get_config_hooks("pre-checks", {"hooks": {}}) is None

    def test_returns_list_for_present_slot(self):
        config = {"hooks": {"pre_checks": ["cmd"]}}
        assert _get_config_hooks("pre-checks", config) == ["cmd"]

    def test_returns_empty_list(self):
        config = {"hooks": {"pre_checks": []}}
        assert _get_config_hooks("pre-checks", config) == []

    def test_non_list_slot_raises(self):
        config = {"hooks": {"pre_checks": "not-a-list"}}
        with pytest.raises(HookError, match="must be a list"):
            _get_config_hooks("pre-checks", config)

    def test_hook_name_mapping(self):
        """Hyphenated hook names map to underscore config keys."""
        config = {
            "hooks": {
                "pre_checks": ["a"],
                "pre_release": ["b"],
                "post_release": ["c"],
            },
        }
        assert _get_config_hooks("pre-checks", config) == ["a"]
        assert _get_config_hooks("pre-release", config) == ["b"]
        assert _get_config_hooks("post-release", config) == ["c"]


class TestIsHookCustomized:
    """Tests for is_hook_customized with config-first detection."""

    def test_config_with_entries_is_customized(self, tmp_path):
        """Non-empty config hooks.pre_release -> customized."""
        config = {"hooks": {"pre_release": ["uv run pytest"]}}
        assert is_hook_customized(config, str(tmp_path / "nonexistent.sh")) is True

    def test_config_with_empty_list_is_not_customized(self, tmp_path):
        """Empty config hooks.pre_release -> not customized."""
        config = {"hooks": {"pre_release": []}}
        assert is_hook_customized(config, str(tmp_path / "nonexistent.sh")) is False

    def test_no_config_falls_back_to_script(self, tmp_path):
        """No hooks section -> falls back to script hash check."""
        # No script file exists -> not customized (effectively empty)
        config = {}
        assert is_hook_customized(config, str(tmp_path / "nonexistent.sh")) is False

    def test_no_pre_release_in_config_falls_back(self, tmp_path):
        """Hooks section without pre_release -> falls back to script check."""
        config = {"hooks": {"pre_checks": ["something"]}}
        assert is_hook_customized(config, str(tmp_path / "nonexistent.sh")) is False


class TestRunReleaseHookPostRelease:
    """Test run_release_hook with post-release hook_name uses non-fatal mode."""

    def test_post_release_config_hooks_non_fatal(self, mock_subprocess, capsys):
        """Post-release config hooks use non-fatal mode."""
        mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            1, ["bash", "-c", "deploy"]
        )
        config = {"hooks": {"post_release": ["deploy"]}}
        # Should NOT raise
        run_release_hook(
            "post-release",
            "/nonexistent.sh",
            "/project",
            {},
            30,
            config=config,
        )
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_releasable_post_release_non_fatal(self, mock_subprocess, capsys):
        """Releasable post-release config hooks use non-fatal mode."""
        mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            1, ["bash", "-c", "deploy"]
        )
        config = {"hooks": {"post_release": ["deploy"]}}
        # Should NOT raise
        run_release_hook(
            "releasable post-release",
            "/nonexistent.sh",
            "/project",
            {},
            30,
            config=config,
        )
        captured = capsys.readouterr()
        assert "Warning" in captured.err


# ---------------------------------------------------------------------------
# 11. Config-driven hooks work when no script file exists (call site fix)
# ---------------------------------------------------------------------------

class TestConfigHooksWithoutScriptFile:
    """Verify config-driven hooks run even when no script file exists on disk.

    This covers the Phase 4 audit fix: before the fix, call sites gated on
    ``os.path.exists(script_path)`` which prevented config-driven hooks from
    running when no script file was present.
    """

    def test_pre_checks_config_runs_without_script(self, mock_subprocess, tmp_path):
        """Config-driven pre-checks runs when no pre-checks.sh exists."""
        # No .rlsbl/hooks/ directory at all
        config = {"hooks": {"pre_checks": ["echo config-check"]}}
        nonexistent_script = str(tmp_path / ".rlsbl" / "hooks" / "pre-checks.sh")

        run_release_hook(
            "pre-checks",
            nonexistent_script,
            str(tmp_path),
            {"PATH": "/usr/bin"},
            30,
            config=config,
        )
        # Config hook should have run despite no script file
        assert mock_subprocess.run.call_count == 1
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", "-c", "echo config-check"]

    def test_pre_release_config_runs_without_script(self, mock_subprocess, tmp_path):
        """Config-driven pre-release runs when no pre-release.sh exists."""
        config = {"hooks": {"pre_release": ["echo config-release"]}}
        nonexistent_script = str(tmp_path / ".rlsbl" / "hooks" / "pre-release.sh")

        run_release_hook(
            "pre-release",
            nonexistent_script,
            str(tmp_path),
            {"PATH": "/usr/bin"},
            30,
            config=config,
        )
        assert mock_subprocess.run.call_count == 1
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", "-c", "echo config-release"]

    def test_post_release_config_runs_without_script(self, mock_subprocess, tmp_path):
        """Config-driven post-release runs when no post-release.sh exists."""
        config = {"hooks": {"post_release": ["echo config-deploy"]}}
        nonexistent_script = str(tmp_path / ".rlsbl" / "hooks" / "post-release.sh")

        run_release_hook(
            "post-release",
            nonexistent_script,
            str(tmp_path),
            {"PATH": "/usr/bin"},
            30,
            config=config,
        )
        assert mock_subprocess.run.call_count == 1
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", "-c", "echo config-deploy"]

    def test_no_config_no_script_is_silent_noop(self, mock_subprocess, tmp_path):
        """No config hooks and no script file results in no subprocess calls."""
        config = {}
        nonexistent_script = str(tmp_path / ".rlsbl" / "hooks" / "pre-checks.sh")

        run_release_hook(
            "pre-checks",
            nonexistent_script,
            str(tmp_path),
            {},
            30,
            config=config,
        )
        mock_subprocess.run.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Releasable hooks receive config at both levels
# ---------------------------------------------------------------------------

class TestReleasableHooksReceiveConfig:
    """Verify run_releasable_hooks passes config to both releasable and package levels."""

    def test_releasable_pre_checks_with_config_no_scripts(self, mock_subprocess, tmp_path):
        """Releasable pre-checks runs config hooks when no script files exist."""
        workspace_root = str(tmp_path)
        # Create the releasable directory (but no hook scripts)
        rel_hooks_dir = (
            tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "hooks"
        )
        rel_hooks_dir.mkdir(parents=True)

        pkg_dir = tmp_path / "packages" / "pkg-a"
        pkg_dir.mkdir(parents=True)

        releasable_config = {
            "hooks": {"pre_checks": ["echo rel-check"]},
        }
        package_configs = {
            "pkg-a": {"hooks": {"pre_checks": ["echo pkg-check"]}},
        }
        log = MagicMock()

        run_releasable_hooks(
            "pre-checks",
            workspace_root,
            "myrel",
            [("pkg-a", str(pkg_dir))],
            {"PATH": "/bin"},
            30,
            log,
            releasable_config=releasable_config,
            package_configs=package_configs,
        )
        # Both releasable and package config hooks should have run
        assert mock_subprocess.run.call_count == 2
        calls = mock_subprocess.run.call_args_list
        # pre-checks: releasable first, then per-package
        assert calls[0][0][0] == ["bash", "-c", "echo rel-check"]
        assert calls[1][0][0] == ["bash", "-c", "echo pkg-check"]

    def test_releasable_post_release_with_config(self, mock_subprocess, tmp_path):
        """Releasable post-release runs config hooks (covers execute.py path)."""
        workspace_root = str(tmp_path)
        rel_hooks_dir = (
            tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "hooks"
        )
        rel_hooks_dir.mkdir(parents=True)

        pkg_dir = tmp_path / "packages" / "pkg-a"
        pkg_dir.mkdir(parents=True)

        releasable_config = {
            "hooks": {"post_release": ["echo rel-deploy"]},
        }
        package_configs = {
            "pkg-a": {"hooks": {"post_release": ["echo pkg-deploy"]}},
        }
        log = MagicMock()

        run_releasable_hooks(
            "post-release",
            workspace_root,
            "myrel",
            [("pkg-a", str(pkg_dir))],
            {"PATH": "/bin"},
            30,
            log,
            releasable_config=releasable_config,
            package_configs=package_configs,
        )
        # post-release: releasable first, then per-package
        assert mock_subprocess.run.call_count == 2
        calls = mock_subprocess.run.call_args_list
        assert calls[0][0][0] == ["bash", "-c", "echo rel-deploy"]
        assert calls[1][0][0] == ["bash", "-c", "echo pkg-deploy"]

    def test_package_config_without_releasable_config(self, mock_subprocess, tmp_path):
        """Package-level config hooks run even when releasable has no config."""
        workspace_root = str(tmp_path)
        rel_hooks_dir = (
            tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "hooks"
        )
        rel_hooks_dir.mkdir(parents=True)

        pkg_dir = tmp_path / "packages" / "pkg-a"
        pkg_dir.mkdir(parents=True)

        package_configs = {
            "pkg-a": {"hooks": {"pre_checks": ["echo pkg-only"]}},
        }
        log = MagicMock()

        run_releasable_hooks(
            "pre-checks",
            workspace_root,
            "myrel",
            [("pkg-a", str(pkg_dir))],
            {"PATH": "/bin"},
            30,
            log,
            releasable_config=None,
            package_configs=package_configs,
        )
        # Only package hook should run (no releasable hook)
        assert mock_subprocess.run.call_count == 1
        args, kwargs = mock_subprocess.run.call_args
        assert args[0] == ["bash", "-c", "echo pkg-only"]
