"""Tests for scaffold skipping redundant per-package config in releasables (Phase 3c-d).

Covers:
1. New package in a releasable: no per-package config.json when identical to releasable
2. New package in a releasable: per-package config.json written when different from releasable
3. Existing identical config.json: removed with informational message
4. Same for publish.json
5. Package NOT in a releasable: config.json written normally (backward compat)
"""

import json
import os
from pathlib import Path

import pytest

from rlsbl.commands.init_cmd import (
    _get_releasable_config_dir,
    _skip_redundant_releasable_configs,
    run_cmd,
)
from rlsbl.context import create_context
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    get_releasable_dir,
)
from conftest import make_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_explicit_workspace(root, releasables, projects):
    """Create a workspace.toml with [[releasables]] and [[projects]] sections."""
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)

    lines = []

    if projects:
        for proj in projects:
            lines.append("[[projects]]")
            lines.append(f'path = "{proj["path"]}"')
            lines.append(f'name = "{proj["name"]}"')
            if "releasable" in proj:
                val = proj["releasable"]
                if isinstance(val, str):
                    lines.append(f'releasable = "{val}"')
                elif val is False:
                    lines.append("releasable = false")
            if proj.get("dev_only"):
                lines.append("dev_only = true")
            if proj.get("library"):
                lines.append("library = true")
            lines.append("")
    else:
        lines.append("projects = []")
        lines.append("")

    for rel in releasables:
        lines.append("[[releasables]]")
        lines.append(f'name = "{rel["name"]}"')
        if "tag_format" in rel:
            lines.append(f'tag_format = "{rel["tag_format"]}"')
        lines.append("")

    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))


def _setup_releasable(root, releasable_name, config=None, publish=None):
    """Create the releasable directory with optional config.json and publish.json."""
    rel_dir = Path(get_releasable_dir(str(root), releasable_name))
    rel_dir.mkdir(parents=True, exist_ok=True)

    if config is not None:
        config_path = rel_dir / "config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")

    if publish is not None:
        publish_path = rel_dir / "publish.json"
        publish_path.write_text(json.dumps(publish, indent=2) + "\n")


def _write_pkg_config(proj_dir, config):
    """Write .rlsbl/config.json for a package."""
    rlsbl_dir = proj_dir / ".rlsbl"
    rlsbl_dir.mkdir(parents=True, exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")


def _write_pkg_publish(proj_dir, publish):
    """Write .rlsbl/publish.json for a package."""
    rlsbl_dir = proj_dir / ".rlsbl"
    rlsbl_dir.mkdir(parents=True, exist_ok=True)
    (rlsbl_dir / "publish.json").write_text(json.dumps(publish, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Tests for _get_releasable_config_dir
# ---------------------------------------------------------------------------


class TestGetReleasableConfigDir:
    """Verify _get_releasable_config_dir resolution."""

    def test_returns_dir_for_releasable_member(self, mock_git_repo):
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        result = _get_releasable_config_dir(proj_dir)
        assert result is not None
        assert result.endswith("releasables/www")

    def test_returns_none_for_non_member(self, mock_git_repo):
        proj_dir = mock_git_repo / "infra"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "infra", "name": "infra", "releasable": False},
        ])

        result = _get_releasable_config_dir(proj_dir)
        assert result is None

    def test_returns_none_outside_monorepo(self, tmp_path):
        result = _get_releasable_config_dir(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# Phase 3c: config.json skip for releasable members
# ---------------------------------------------------------------------------


class TestConfigJsonSkip:
    """Tests for skipping redundant per-package config.json in releasables."""

    def test_identical_config_removed(self, mock_git_repo):
        """Per-package config.json is removed when identical to releasable config."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        config = {"private": False, "targets": ["pypi"]}
        _setup_releasable(mock_git_repo, "www", config=config)
        _write_pkg_config(proj_dir, config)

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        pkg_config_path = proj_dir / ".rlsbl" / "config.json"
        assert not pkg_config_path.exists(), (
            "Per-package config.json should be removed when identical to releasable"
        )
        assert any("config.json" in str(p) for p in removed)

    def test_different_config_kept(self, mock_git_repo):
        """Per-package config.json is kept when different from releasable config."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        releasable_config = {"private": False, "targets": ["pypi"]}
        pkg_config = {"private": False, "targets": ["pypi"], "batch_limits": {"max_commits_per_entry": 10}}
        _setup_releasable(mock_git_repo, "www", config=releasable_config)
        _write_pkg_config(proj_dir, pkg_config)

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        pkg_config_path = proj_dir / ".rlsbl" / "config.json"
        assert pkg_config_path.exists(), (
            "Per-package config.json should be kept when different from releasable"
        )
        assert len(removed) == 0

    def test_scaffold_skips_config_for_releasable_member(self, mock_git_repo, monkeypatch):
        """Full scaffold of a releasable member produces no per-package config.json
        when the releasable config already has the same fields."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        # Scaffold writes: targets=["plain"], private (auto-detected, likely False)
        # Pre-create the releasable config with these same values
        _setup_releasable(mock_git_repo, "www", config={
            "targets": ["plain"],
            "private": False,
        })

        monkeypatch.chdir(proj_dir)
        ctx = create_context(proj_dir)

        run_cmd("plain", [], {
            "no-commit": True,
            "no-tag": True,
            "skip-shared": False,
        }, ctx=ctx)

        pkg_config_path = proj_dir / ".rlsbl" / "config.json"
        assert not pkg_config_path.exists(), (
            "Per-package config.json should not exist when identical to releasable config"
        )

    def test_no_releasable_config_file_keeps_pkg_config(self, mock_git_repo):
        """When releasable has no config.json, per-package config is kept."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        # Only set up the releasable dir without config.json
        rel_dir = Path(get_releasable_dir(str(mock_git_repo), "www"))
        rel_dir.mkdir(parents=True, exist_ok=True)

        pkg_config = {"private": False}
        _write_pkg_config(proj_dir, pkg_config)

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        pkg_config_path = proj_dir / ".rlsbl" / "config.json"
        assert pkg_config_path.exists(), (
            "Per-package config.json should be kept when releasable has no config.json"
        )
        assert len(removed) == 0


# ---------------------------------------------------------------------------
# Phase 3d: publish.json skip for releasable members
# ---------------------------------------------------------------------------


class TestPublishJsonSkip:
    """Tests for skipping redundant per-package publish.json in releasables."""

    def test_identical_publish_removed(self, mock_git_repo):
        """Per-package publish.json is removed when identical to releasable publish.json."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        publish = {"pipelines": {"pypi": {"type": "pypi", "local": False}}}
        _setup_releasable(mock_git_repo, "www", publish=publish)
        _write_pkg_publish(proj_dir, publish)

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        pkg_publish_path = proj_dir / ".rlsbl" / "publish.json"
        assert not pkg_publish_path.exists(), (
            "Per-package publish.json should be removed when identical to releasable"
        )
        assert any("publish.json" in str(p) for p in removed)

    def test_different_publish_kept(self, mock_git_repo):
        """Per-package publish.json is kept when different from releasable publish.json."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        releasable_publish = {"pipelines": {"pypi": {"type": "pypi", "local": False}}}
        pkg_publish = {"pipelines": {"npm": {"type": "npm", "local": False}}}
        _setup_releasable(mock_git_repo, "www", publish=releasable_publish)
        _write_pkg_publish(proj_dir, pkg_publish)

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        pkg_publish_path = proj_dir / ".rlsbl" / "publish.json"
        assert pkg_publish_path.exists(), (
            "Per-package publish.json should be kept when different from releasable"
        )
        assert len(removed) == 0

    def test_both_config_and_publish_removed_when_identical(self, mock_git_repo):
        """Both config.json and publish.json are removed when both match releasable."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        config = {"batch_limits": {"max_commits_per_entry": 5}}
        publish = {"targets": ["pypi"], "private": False}
        _setup_releasable(mock_git_repo, "www", config=config, publish=publish)
        _write_pkg_config(proj_dir, config)
        _write_pkg_publish(proj_dir, publish)

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        assert not (proj_dir / ".rlsbl" / "config.json").exists()
        assert not (proj_dir / ".rlsbl" / "publish.json").exists()
        assert len(removed) == 2


# ---------------------------------------------------------------------------
# Backward compatibility: non-releasable packages
# ---------------------------------------------------------------------------


class TestNonReleasableConfigPreserved:
    """Packages not in a releasable keep their config.json normally."""

    def test_non_monorepo_config_preserved(self, mock_git_repo):
        """Standalone project (no monorepo) keeps its config.json."""
        pkg_config = {"private": False, "targets": ["pypi"]}
        _write_pkg_config(mock_git_repo, pkg_config)

        warnings = []
        removed = _skip_redundant_releasable_configs(mock_git_repo, warnings)

        assert (mock_git_repo / ".rlsbl" / "config.json").exists()
        assert len(removed) == 0

    def test_non_explicit_mode_config_preserved(self, mock_git_repo):
        """Package in a non-explicit monorepo keeps its config.json."""
        proj_dir = mock_git_repo / "lib"
        proj_dir.mkdir()
        # make_workspace creates workspace.toml without [[releasables]]
        make_workspace(mock_git_repo, [
            {"path": "lib", "name": "lib"},
        ])

        pkg_config = {"private": False}
        _write_pkg_config(proj_dir, pkg_config)

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        assert (proj_dir / ".rlsbl" / "config.json").exists()
        assert len(removed) == 0

    def test_scaffold_non_releasable_writes_config(self, mock_git_repo, monkeypatch):
        """Scaffold for a non-monorepo project writes config.json normally."""
        monkeypatch.chdir(mock_git_repo)
        ctx = create_context(mock_git_repo)

        run_cmd("plain", [], {
            "no-commit": True,
            "no-tag": True,
            "skip-shared": False,
        }, ctx=ctx)

        config_path = mock_git_repo / ".rlsbl" / "config.json"
        assert config_path.exists(), (
            "Config.json should be created for non-releasable packages"
        )
        config = json.loads(config_path.read_text())
        assert "targets" in config
        assert "plain" in config["targets"]


# ---------------------------------------------------------------------------
# Phase 1e: saferm invocation for file removal
# ---------------------------------------------------------------------------


class TestSafermInvocation:
    """Verify that scaffold uses saferm instead of os.unlink for file deletion."""

    def test_skip_redundant_uses_saferm(self, mock_git_repo, monkeypatch):
        """_skip_redundant_releasable_configs calls saferm to remove identical configs."""
        from unittest.mock import patch, MagicMock
        import subprocess as real_subprocess

        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        config = {"private": False, "targets": ["pypi"]}
        _setup_releasable(mock_git_repo, "www", config=config)
        _write_pkg_config(proj_dir, config)

        saferm_calls = []
        original_run = real_subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "saferm":
                saferm_calls.append(cmd)
                # Actually delete the file so the rest of the logic works
                target_file = cmd[-1]
                if os.path.exists(target_file):
                    os.unlink(target_file)
                return real_subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        with patch("rlsbl.commands.init_cmd.subprocess.run", side_effect=tracking_run):
            warnings = []
            removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        assert len(saferm_calls) == 1
        call = saferm_calls[0]
        assert call[0] == "saferm"
        assert call[1] == "delete"
        assert "--description" in call
        assert str(proj_dir / ".rlsbl" / "config.json") == call[-1]
        assert any("config.json" in str(p) for p in removed)

    def test_orphan_cleanup_uses_saferm(self, mock_git_repo, monkeypatch):
        """_finalize_scaffold uses saferm to remove orphaned files and their bases."""
        from unittest.mock import patch
        import subprocess as real_subprocess
        from rlsbl.commands.init_cmd import (
            _finalize_scaffold,
            BASES_DIR,
            save_managed_files,
            save_hashes,
            file_hash,
        )

        # Set up a minimal scaffold environment
        monkeypatch.chdir(mock_git_repo)
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(parents=True, exist_ok=True)

        # Create an orphan file and its merge base (use relative paths as
        # _finalize_scaffold operates in the cwd context)
        orphan_rel = os.path.join("old_workflow.yml")
        orphan_abs = mock_git_repo / "old_workflow.yml"
        orphan_abs.write_text("old content\n")
        orphan_hash = file_hash(orphan_rel)

        bases_dir = mock_git_repo / BASES_DIR
        bases_dir.mkdir(parents=True, exist_ok=True)
        base_file = bases_dir / orphan_rel
        base_file.parent.mkdir(parents=True, exist_ok=True)
        base_file.write_text("base content\n")

        # Save the orphan in managed-files so _finalize_scaffold sees it as an orphan
        save_managed_files({orphan_rel: orphan_hash})
        save_hashes({orphan_rel: orphan_hash})

        saferm_calls = []
        original_run = real_subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "saferm":
                saferm_calls.append(list(cmd))
                # Actually delete the file so the rest of the logic works
                target_file = cmd[-1]
                if os.path.exists(target_file):
                    os.unlink(target_file)
                return real_subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        with patch("rlsbl.commands.init_cmd.subprocess.run", side_effect=tracking_run):
            config = {"targets": ["plain"], "private": False}
            created = []
            _finalize_scaffold(
                existing_hashes={orphan_rel: orphan_hash},
                all_hash_dicts=[{}],
                created=created,
                skipped=[],
                warnings=[],
                flags={"no-commit": True, "no-tag": True, "skip-shared": False},
                project_root=str(mock_git_repo),
                config=config,
            )

        # Should have 2 saferm calls: one for orphan file, one for base file
        assert len(saferm_calls) == 2

        # First call: orphan file
        assert saferm_calls[0][0] == "saferm"
        assert saferm_calls[0][1] == "delete"
        assert "--description" in saferm_calls[0]
        assert orphan_rel == saferm_calls[0][-1]

        # Second call: base file
        assert saferm_calls[1][0] == "saferm"
        assert saferm_calls[1][1] == "delete"
        assert "--description" in saferm_calls[1]

        # Verify the orphan shows up in created list
        orphan_entries = [e for e in created if e[1] == "removed (orphan)"]
        assert len(orphan_entries) == 1
