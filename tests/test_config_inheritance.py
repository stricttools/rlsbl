"""Tests for config inheritance: merge_config and releasable-level config loading."""

import json
import os
import subprocess

import pytest

from rlsbl.config import merge_config, read_project_config


class TestMergeConfigShallow:
    """merge_config replaces top-level scalar and list values from overlay."""

    def test_overlay_replaces_scalar(self):
        base = {"private": True, "push_timeout": 120}
        overlay = {"private": False}
        result = merge_config(base, overlay)
        assert result == {"private": False, "push_timeout": 120}

    def test_overlay_replaces_list(self):
        base = {"targets": ["pypi"]}
        overlay = {"targets": ["npm", "go"]}
        result = merge_config(base, overlay)
        assert result == {"targets": ["npm", "go"]}

    def test_overlay_adds_new_keys(self):
        base = {"private": True}
        overlay = {"push_timeout": 300}
        result = merge_config(base, overlay)
        assert result == {"private": True, "push_timeout": 300}


class TestMergeConfigDeep:
    """merge_config deep-merges nested dicts."""

    def test_nested_dict_merged(self):
        base = {"batch_limits": {"max_commits_per_entry": 5}}
        overlay = {"batch_limits": {"max_entries_per_commit": 3}}
        result = merge_config(base, overlay)
        assert result == {
            "batch_limits": {
                "max_commits_per_entry": 5,
                "max_entries_per_commit": 3,
            }
        }

    def test_nested_dict_overlay_replaces_scalar_in_nested(self):
        base = {"batch_limits": {"max_commits_per_entry": 5, "max_entries_per_commit": 2}}
        overlay = {"batch_limits": {"max_commits_per_entry": 10}}
        result = merge_config(base, overlay)
        assert result == {
            "batch_limits": {
                "max_commits_per_entry": 10,
                "max_entries_per_commit": 2,
            }
        }

    def test_non_dict_replaces_dict(self):
        """When overlay has a non-dict where base has a dict, overlay wins."""
        base = {"pipelines": {"pypi": {"type": "pypi"}}}
        overlay = {"pipelines": "none"}
        result = merge_config(base, overlay)
        assert result == {"pipelines": "none"}

    def test_dict_replaces_non_dict(self):
        """When overlay has a dict where base has a non-dict, overlay wins."""
        base = {"pipelines": "none"}
        overlay = {"pipelines": {"pypi": {"type": "pypi"}}}
        result = merge_config(base, overlay)
        assert result == {"pipelines": {"pypi": {"type": "pypi"}}}


class TestMergeConfigMissingKeys:
    """merge_config preserves base keys absent from overlay."""

    def test_base_preserved_when_overlay_empty(self):
        base = {"private": True, "push_timeout": 120, "targets": ["pypi"]}
        overlay = {}
        result = merge_config(base, overlay)
        assert result == base

    def test_base_preserved_for_absent_keys(self):
        base = {"private": True, "push_timeout": 120, "batch_limits": {"max_commits_per_entry": 5}}
        overlay = {"private": False}
        result = merge_config(base, overlay)
        assert result == {
            "private": False,
            "push_timeout": 120,
            "batch_limits": {"max_commits_per_entry": 5},
        }

    def test_no_mutation_of_inputs(self):
        base = {"batch_limits": {"max_commits_per_entry": 5}}
        overlay = {"batch_limits": {"max_entries_per_commit": 3}}
        base_copy = json.loads(json.dumps(base))
        overlay_copy = json.loads(json.dumps(overlay))
        merge_config(base, overlay)
        assert base == base_copy
        assert overlay == overlay_copy


# ---------------------------------------------------------------------------
# Helpers for filesystem-based tests
# ---------------------------------------------------------------------------

def _write_json(path, data):
    """Write a JSON dict to a file, creating parent dirs."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(str(path), "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _make_releasable_dir(root, releasable_name):
    """Create a releasable state directory and return its path."""
    rel_dir = root / ".rlsbl-monorepo" / "releasables" / releasable_name
    rel_dir.mkdir(parents=True, exist_ok=True)
    return rel_dir


# ---------------------------------------------------------------------------
# read_project_config with releasable inheritance
# ---------------------------------------------------------------------------


class TestReadProjectConfigNoReleasable:
    """Without releasable_config_dir, behavior is unchanged."""

    def test_per_package_config_only(self, tmp_path):
        _write_json(tmp_path / ".rlsbl" / "config.json", {"private": False, "push_timeout": 300})
        result = read_project_config(tmp_path)
        assert result == {"private": False, "push_timeout": 300, "coverage_unit": "commit"}

    def test_per_package_absent_returns_empty(self, tmp_path):
        result = read_project_config(tmp_path)
        assert result == {"coverage_unit": "commit"}


class TestReadProjectConfigReleasableInheritance:
    """read_project_config merges releasable config as base when dir is given."""

    def test_per_package_absent_returns_releasable_config(self, tmp_path):
        """Per-package config absent -> releasable config returned as-is."""
        rel_dir = _make_releasable_dir(tmp_path, "alpha")
        _write_json(rel_dir / "config.json", {"private": False, "push_timeout": 120})

        # No per-package config exists
        pkg_dir = tmp_path / "pkgs" / "core"
        pkg_dir.mkdir(parents=True)

        result = read_project_config(pkg_dir, releasable_config_dir=str(rel_dir))
        assert result == {"private": False, "push_timeout": 120, "coverage_unit": "commit"}

    def test_per_package_merges_on_top_of_releasable(self, tmp_path):
        """Per-package config present -> merged on top of releasable."""
        rel_dir = _make_releasable_dir(tmp_path, "alpha")
        _write_json(rel_dir / "config.json", {
            "private": False,
            "push_timeout": 120,
            "batch_limits": {"max_commits_per_entry": 5},
        })

        pkg_dir = tmp_path / "pkgs" / "core"
        _write_json(pkg_dir / ".rlsbl" / "config.json", {
            "push_timeout": 300,
            "batch_limits": {"max_entries_per_commit": 2},
        })

        result = read_project_config(pkg_dir, releasable_config_dir=str(rel_dir))
        assert result == {
            "private": False,
            "push_timeout": 300,
            "coverage_unit": "commit",
            "batch_limits": {
                "max_commits_per_entry": 5,
                "max_entries_per_commit": 2,
            },
        }

    def test_per_package_overrides_releasable(self, tmp_path):
        """Per-package values override releasable values."""
        rel_dir = _make_releasable_dir(tmp_path, "alpha")
        _write_json(rel_dir / "config.json", {"private": False, "push_timeout": 120})

        pkg_dir = tmp_path / "pkgs" / "core"
        _write_json(pkg_dir / ".rlsbl" / "config.json", {"private": True})

        result = read_project_config(pkg_dir, releasable_config_dir=str(rel_dir))
        assert result["private"] is True
        # Inherited from releasable
        assert result["push_timeout"] == 120

    def test_releasable_dir_empty_returns_package_config(self, tmp_path):
        """Empty releasable dir (no config.json) -> per-package only."""
        rel_dir = _make_releasable_dir(tmp_path, "alpha")

        pkg_dir = tmp_path / "pkgs" / "core"
        _write_json(pkg_dir / ".rlsbl" / "config.json", {"private": True})

        result = read_project_config(pkg_dir, releasable_config_dir=str(rel_dir))
        assert result == {"private": True, "coverage_unit": "commit"}


# ---------------------------------------------------------------------------
# create_context with releasable detection
# ---------------------------------------------------------------------------


def _run_git(repo, *args):
    """Run a git command in the given repo directory."""
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


class TestCreateContextReleasable:
    """create_context detects releasable membership and applies inheritance."""

    def _setup_monorepo(self, tmp_path):
        """Create a monorepo with a releasable and two member packages."""
        from pathlib import Path

        _run_git(tmp_path, "init", "-q", "-b", "main")
        _run_git(tmp_path, "config", "user.email", "test@test.local")
        _run_git(tmp_path, "config", "user.name", "Test")

        # workspace.toml with releasable
        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[releasables]]\nname = "alpha"\n\n'
            '[[projects]]\npath = "core"\nname = "core"\nreleasable = "alpha"\n\n'
            '[[projects]]\npath = "cli"\nname = "cli"\nreleasable = "alpha"\n'
        )

        # Releasable-level config
        rel_dir = ws_dir / "releasables" / "alpha"
        rel_dir.mkdir(parents=True)
        _write_json(rel_dir / "config.json", {"private": False, "push_timeout": 120})

        # Per-package directories with minimal config
        core_dir = tmp_path / "core"
        core_dir.mkdir()
        _write_json(core_dir / ".rlsbl" / "config.json", {
            "batch_limits": {"max_commits_per_entry": 3},
        })

        cli_dir = tmp_path / "cli"
        cli_dir.mkdir()
        # cli has no per-package config

        # Initial commit
        (tmp_path / "README.md").write_text("# test\n")
        _run_git(tmp_path, "add", ".")
        _run_git(tmp_path, "commit", "-q", "-m", "initial")

        return tmp_path, core_dir, cli_dir

    def test_core_inherits_releasable_config(self, tmp_path, monkeypatch):
        from pathlib import Path
        from rlsbl.context import create_context

        monorepo, core_dir, _ = self._setup_monorepo(tmp_path)
        monkeypatch.chdir(core_dir)

        ctx = create_context(Path(core_dir), workspace_root=Path(monorepo))
        # private inherited from releasable
        assert ctx.config.get("private") is False
        # push_timeout inherited from releasable
        assert ctx.config.get("push_timeout") == 120
        # batch_limits from per-package
        assert ctx.config.get("batch_limits") == {"max_commits_per_entry": 3}

    def test_cli_inherits_releasable_config_fully(self, tmp_path, monkeypatch):
        from pathlib import Path
        from rlsbl.context import create_context

        monorepo, _, cli_dir = self._setup_monorepo(tmp_path)
        monkeypatch.chdir(cli_dir)

        ctx = create_context(Path(cli_dir), workspace_root=Path(monorepo))
        # cli has no per-package config, so gets pure releasable config
        assert ctx.config.get("private") is False
        assert ctx.config.get("push_timeout") == 120

    def test_no_workspace_root_no_inheritance(self, tmp_path, monkeypatch):
        from pathlib import Path
        from rlsbl.context import create_context

        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / ".rlsbl" / "config.json", {"private": True})

        ctx = create_context(Path(tmp_path))
        assert ctx.config == {"private": True, "coverage_unit": "commit"}


# ---------------------------------------------------------------------------
# _sync_member_package_versions respects inherited private: false
# ---------------------------------------------------------------------------


class TestSyncMemberPackageVersionsInheritance:
    """_sync_member_package_versions uses read_project_config with inheritance."""

    def test_inherited_private_false_enables_sync(self, tmp_path):
        """A package with no private field inherits private: false from releasable
        and should have its version synced."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from rlsbl.commands.release.execute import _sync_member_package_versions
        from rlsbl.context import ProjectContext

        monorepo_root = tmp_path

        # Releasable config with private: false
        rel_dir = _make_releasable_dir(tmp_path, "alpha")
        _write_json(rel_dir / "config.json", {"private": False})

        # Package with no private field in config.json (should inherit false)
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _write_json(pkg_dir / ".rlsbl" / "config.json", {
            "batch_limits": {"max_commits_per_entry": 5},
        })
        # Create a minimal pyproject.toml
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "testpkg"\nversion = "0.1.0"\n'
        )

        ctx = ProjectContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
        )

        files_to_commit = []
        log_calls = []

        # Mock detect_targets and TARGETS to return a target that reports a version write
        mock_entry = MagicMock()
        mock_entry.name = "pypi"
        mock_entry.path = str(pkg_dir)

        mock_target = MagicMock()
        mock_target.write_version.return_value = ["pyproject.toml"]

        with patch("rlsbl.targets.detect_targets", return_value=[mock_entry]), \
             patch("rlsbl.commands.release.TARGETS", {"pypi": mock_target}):
            _sync_member_package_versions(
                member_package_paths=["pkg"],
                monorepo_root=str(monorepo_root),
                new_version="1.0.0",
                files_to_commit=files_to_commit,
                git_root=str(monorepo_root),
                log=log_calls.append,
                ctx=ctx,
                releasable_config_dir=str(rel_dir),
            )

        # write_version was called because private=false was inherited
        mock_target.write_version.assert_called_once()
        assert len(files_to_commit) > 0

    def test_package_private_true_skips_sync(self, tmp_path):
        """A package with explicit private: true should be skipped even if
        releasable says private: false."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from rlsbl.commands.release.execute import _sync_member_package_versions
        from rlsbl.context import ProjectContext

        monorepo_root = tmp_path

        # Releasable config with private: false
        rel_dir = _make_releasable_dir(tmp_path, "alpha")
        _write_json(rel_dir / "config.json", {"private": False})

        # Package overrides with private: true
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _write_json(pkg_dir / ".rlsbl" / "config.json", {"private": True})

        ctx = ProjectContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
        )

        files_to_commit = []
        log_calls = []

        with patch("rlsbl.targets.detect_targets") as mock_detect:
            _sync_member_package_versions(
                member_package_paths=["pkg"],
                monorepo_root=str(monorepo_root),
                new_version="1.0.0",
                files_to_commit=files_to_commit,
                git_root=str(monorepo_root),
                log=log_calls.append,
                ctx=ctx,
                releasable_config_dir=str(rel_dir),
            )

        # detect_targets should not be called -- package is private
        mock_detect.assert_not_called()
        assert len(files_to_commit) == 0
