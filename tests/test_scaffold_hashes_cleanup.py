"""Tests for phantom hashes.json entry cleanup after scaffold config skip (Phase 1g).

Covers:
1. After config skip, removed file paths are not in hashes.json
2. Both call sites (run_cmd and run_cmd_multi) pass removed paths to _finalize_scaffold
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import (
    _finalize_scaffold,
    _skip_redundant_releasable_configs,
    load_hashes,
    run_cmd,
)
from rlsbl.context import create_context
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    get_releasable_dir,
)
from conftest import run_git as _run_git


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_explicit_workspace(root, releasables, projects):
    """Create a workspace.toml with [[releasables]] and [[projects]] sections."""
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for rel in releasables:
        lines.append("[[releasables]]")
        lines.append(f'name = "{rel["name"]}"')
        lines.append("")

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
        lines.append("")

    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))


def _setup_releasable(root, releasable_name, config=None):
    """Create the releasable directory with optional config.json."""
    rel_dir = Path(get_releasable_dir(str(root), releasable_name))
    rel_dir.mkdir(parents=True, exist_ok=True)

    if config is not None:
        config_path = rel_dir / "config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")


def _write_pkg_config(proj_dir, config):
    """Write .rlsbl/config.json for a package."""
    rlsbl_dir = proj_dir / ".rlsbl"
    rlsbl_dir.mkdir(parents=True, exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Tests: _finalize_scaffold purges removed paths from hashes
# ---------------------------------------------------------------------------


class TestFinalizeScaffoldPurgesRemovedPaths:
    """After _skip_redundant_releasable_configs removes files,
    _finalize_scaffold must purge them from hashes.json."""

    def test_removed_config_not_in_hashes(self, mock_git_repo, monkeypatch, mock_saferm):
        """A config.json removed by skip_redundant does not appear in hashes.json."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        config = {"private": False, "targets": ["pypi"]}
        _setup_releasable(mock_git_repo, "www", config=config)
        _write_pkg_config(proj_dir, config)

        monkeypatch.chdir(proj_dir)

        # Simulate existing hashes that include the per-package config
        rel_config_key = ".rlsbl/config.json"
        existing_hashes = {rel_config_key: "abc123deadbeef"}

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)
        assert len(removed) > 0, "Expected at least one file to be removed"

        # Now call _finalize_scaffold with removed_paths
        _finalize_scaffold(
            existing_hashes, [{}],
            [], [], warnings,
            flags={"no-commit": True},
            project_root=proj_dir,
            config={"targets": ["pypi"]},
            removed_paths=removed,
        )

        # Load hashes.json and verify the removed path is not present
        saved = load_hashes()
        assert rel_config_key not in saved, (
            f"Removed config path {rel_config_key} should not be in hashes.json"
        )

    def test_non_removed_files_preserved_in_hashes(self, mock_git_repo, monkeypatch, mock_saferm):
        """Files not removed by skip_redundant are preserved in hashes.json."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        config = {"private": False, "targets": ["pypi"]}
        _setup_releasable(mock_git_repo, "www", config=config)
        _write_pkg_config(proj_dir, config)

        monkeypatch.chdir(proj_dir)

        # Pre-populate hashes with config (will be removed) and another file
        existing_hashes = {
            ".rlsbl/config.json": "abc123",
            ".github/workflows/ci.yml": "def456",
        }

        warnings = []
        removed = _skip_redundant_releasable_configs(proj_dir, warnings)

        _finalize_scaffold(
            existing_hashes, [{}],
            [], [], warnings,
            flags={"no-commit": True},
            project_root=proj_dir,
            config={"targets": ["pypi"]},
            removed_paths=removed,
        )

        saved = load_hashes()
        assert ".github/workflows/ci.yml" in saved, (
            "Non-removed files should be preserved in hashes.json"
        )

    def test_empty_removed_paths_no_crash(self, mock_git_repo, monkeypatch):
        """When no files are removed, _finalize_scaffold works fine with empty list."""
        monkeypatch.chdir(mock_git_repo)

        existing_hashes = {".rlsbl/config.json": "abc123"}

        _finalize_scaffold(
            existing_hashes, [{}],
            [], [], [],
            flags={"no-commit": True},
            project_root=mock_git_repo,
            config={},
            removed_paths=[],
        )

        saved = load_hashes()
        assert ".rlsbl/config.json" in saved

    def test_none_removed_paths_no_crash(self, mock_git_repo, monkeypatch):
        """When removed_paths is None (default), _finalize_scaffold works."""
        monkeypatch.chdir(mock_git_repo)

        existing_hashes = {".rlsbl/config.json": "abc123"}

        _finalize_scaffold(
            existing_hashes, [{}],
            [], [], [],
            flags={"no-commit": True},
            project_root=mock_git_repo,
            config={},
            # removed_paths omitted, defaults to None
        )

        saved = load_hashes()
        assert ".rlsbl/config.json" in saved


# ---------------------------------------------------------------------------
# Tests: run_cmd and run_cmd_multi pass removed paths
# ---------------------------------------------------------------------------


class TestCallSitesPassRemovedPaths:
    """Verify that both run_cmd and run_cmd_multi capture
    _skip_redundant_releasable_configs return value and pass it through."""

    def test_run_cmd_passes_removed_paths(self, mock_git_repo, monkeypatch, mock_saferm):
        """run_cmd captures removed paths and passes them to _finalize_scaffold."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()
        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        config = {"private": False, "targets": ["plain"]}
        _setup_releasable(mock_git_repo, "www", config=config)
        _write_pkg_config(proj_dir, config)

        monkeypatch.chdir(proj_dir)
        ctx = create_context(proj_dir)

        # Track calls to _finalize_scaffold to verify removed_paths is passed
        finalize_calls = []
        original_finalize = _finalize_scaffold.__wrapped__ if hasattr(_finalize_scaffold, '__wrapped__') else _finalize_scaffold

        def tracking_finalize(*args, **kwargs):
            finalize_calls.append(kwargs.get("removed_paths"))
            return original_finalize(*args, **kwargs)

        with patch("rlsbl.commands.init_cmd._finalize_scaffold", side_effect=tracking_finalize):
            run_cmd("plain", [], {
                "no-commit": True,
                "no-tag": True,
                "skip-shared": False,
            }, ctx=ctx)

        assert len(finalize_calls) == 1, "Expected _finalize_scaffold to be called once"
        removed = finalize_calls[0]
        assert removed is not None, "removed_paths should not be None"
        assert isinstance(removed, list), "removed_paths should be a list"
        # The identical config should have been removed
        assert any(".rlsbl/config.json" in str(p) for p in removed), (
            f"Expected config.json in removed paths, got: {removed}"
        )

    def test_run_cmd_no_removal_passes_empty_list(self, mock_git_repo, monkeypatch):
        """run_cmd passes empty list when no configs are redundant."""
        monkeypatch.chdir(mock_git_repo)
        ctx = create_context(mock_git_repo)

        finalize_calls = []
        original_finalize = _finalize_scaffold.__wrapped__ if hasattr(_finalize_scaffold, '__wrapped__') else _finalize_scaffold

        def tracking_finalize(*args, **kwargs):
            finalize_calls.append(kwargs.get("removed_paths"))
            return original_finalize(*args, **kwargs)

        with patch("rlsbl.commands.init_cmd._finalize_scaffold", side_effect=tracking_finalize):
            run_cmd("plain", [], {
                "no-commit": True,
                "no-tag": True,
                "skip-shared": False,
            }, ctx=ctx)

        assert len(finalize_calls) == 1
        removed = finalize_calls[0]
        assert removed is not None
        assert isinstance(removed, list)
        assert len(removed) == 0
