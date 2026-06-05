"""Tests for selfdoc.json version bump and version-consistency check.

Verifies that selfdoc.json is bumped during release via the inline
_bump_selfdoc_version function, and that the version-consistency check
detects drift in selfdoc.json regardless of target configuration.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from strictcli import CheckResult

from rlsbl import app
from conftest import make_ctx
from rlsbl.context import ProjectContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(root, *, targets=None, pkg_version="1.0.0", selfdoc_version="1.0.0"):
    """Create a minimal project with package.json + selfdoc.json + .rlsbl config.

    Parameters
    ----------
    root : Path
        Project root directory.
    targets : list or None
        If not None, written to .rlsbl/config.json as the "targets" list.
    pkg_version : str
        Version to put in package.json.
    selfdoc_version : str
        Version to put in selfdoc.json.
    """
    # package.json (npm target)
    (root / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": pkg_version})
    )
    # selfdoc.json
    (root / "selfdoc.json").write_text(
        json.dumps({"version": selfdoc_version, "language": "python"}, indent=2)
    )
    # .rlsbl/config.json
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    config = {"private": False}
    if targets is not None:
        config["targets"] = targets
    (rlsbl_dir / "config.json").write_text(json.dumps(config))


# ---------------------------------------------------------------------------
# Bug 1: Release version bump includes selfdoc.json
# ---------------------------------------------------------------------------


class TestReleaseBumpSelfdocJson:
    """Verify _run_release_mutating bumps selfdoc.json."""

    def test_bump_includes_selfdoc_when_docs_not_in_targets(self, tmp_path, monkeypatch):
        """When targets=["npm"] and selfdoc.json exists, selfdoc.json is bumped."""
        _make_project(tmp_path, targets=["npm"], pkg_version="1.0.0", selfdoc_version="1.0.0")
        monkeypatch.chdir(tmp_path)

        from rlsbl.targets import TARGETS

        # Use the real npm target to write version
        npm_target = TARGETS["npm"]

        # Simulate what _run_release_mutating does for the version bump
        version_dir = str(tmp_path)
        new_version = "1.1.0"
        registry = "npm"
        primary_path = version_dir
        target_paths = {"npm": version_dir}

        # --- replicate the bump logic from release.py ---
        files_to_commit = []
        modified = npm_target.write_version(primary_path, new_version, ctx=make_ctx(primary_path))
        for rel in modified:
            files_to_commit.append(os.path.normpath(os.path.join(primary_path, rel)))

        for t_name, t_path in target_paths.items():
            if t_name == registry:
                continue
            other_reg = TARGETS.get(t_name)
            if other_reg and other_reg.check_project_exists(t_path):
                other_modified = other_reg.write_version(t_path, new_version, ctx=make_ctx(t_path))
                for rel in other_modified:
                    files_to_commit.append(os.path.normpath(os.path.join(t_path, rel)))

        # Now apply the selfdoc.json bump (inline, no DocsTarget)
        from rlsbl.commands.release import _bump_selfdoc_version
        bumped_files = set(files_to_commit)
        selfdoc_modified = _bump_selfdoc_version(version_dir, new_version)
        for rel in selfdoc_modified:
            fpath = os.path.normpath(os.path.join(version_dir, rel))
            if fpath not in bumped_files:
                files_to_commit.append(fpath)

        # Verify selfdoc.json was bumped
        with open(os.path.join(version_dir, "selfdoc.json")) as f:
            data = json.load(f)
        assert data["version"] == "1.1.0"

        # Verify selfdoc.json is in files_to_commit
        selfdoc_committed = any("selfdoc.json" in f for f in files_to_commit)
        assert selfdoc_committed, f"selfdoc.json not in files_to_commit: {files_to_commit}"

    def test_bump_selfdoc_no_file_returns_empty(self, tmp_path):
        """When selfdoc.json does not exist, _bump_selfdoc_version returns []."""
        from rlsbl.commands.release import _bump_selfdoc_version
        result = _bump_selfdoc_version(str(tmp_path), "1.0.0")
        assert result == []

    def test_bump_selfdoc_updates_versions_array(self, tmp_path):
        """_bump_selfdoc_version also updates the last entry in the versions array."""
        (tmp_path / "selfdoc.json").write_text(json.dumps({
            "version": "1.0.0",
            "versions": [{"version": "1.0.0", "indexed": True}],
        }, indent=2))
        from rlsbl.commands.release import _bump_selfdoc_version
        result = _bump_selfdoc_version(str(tmp_path), "1.1.0")
        assert result == ["selfdoc.json"]
        with open(tmp_path / "selfdoc.json") as f:
            data = json.load(f)
        assert data["version"] == "1.1.0"
        assert data["versions"][-1]["version"] == "1.1.0"


# ---------------------------------------------------------------------------
# Bug 2: Version-consistency check includes selfdoc.json
# ---------------------------------------------------------------------------


class TestVersionConsistencySelfdoc:
    """Verify the version-consistency check sees selfdoc.json."""

    def test_detects_selfdoc_drift_when_docs_not_in_targets(self, tmp_path, monkeypatch):
        """selfdoc.json at 0.5.0 vs package.json at 1.0.0 with targets=["npm"] -> fail."""
        _make_project(
            tmp_path,
            targets=["npm"],
            pkg_version="1.0.0",
            selfdoc_version="0.5.0",
        )
        monkeypatch.chdir(tmp_path)

        ctx = ProjectContext(project_root=tmp_path, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "fail"
        assert "mismatch" in result.message

    def test_passes_when_selfdoc_in_sync(self, tmp_path, monkeypatch):
        """selfdoc.json at 1.0.0 matches package.json at 1.0.0 with targets=["npm"] -> pass."""
        _make_project(
            tmp_path,
            targets=["npm"],
            pkg_version="1.0.0",
            selfdoc_version="1.0.0",
        )
        monkeypatch.chdir(tmp_path)

        ctx = ProjectContext(project_root=tmp_path, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "1.0.0" in result.message

    def test_passes_auto_detect_includes_selfdoc(self, tmp_path, monkeypatch):
        """Without explicit targets, auto-detection picks up npm and selfdoc.json is checked inline."""
        _make_project(
            tmp_path,
            targets=None,  # auto-detect
            pkg_version="2.0.0",
            selfdoc_version="2.0.0",
        )
        monkeypatch.chdir(tmp_path)

        ctx = ProjectContext(project_root=tmp_path, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "2.0.0" in result.message

    def test_drift_detected_auto_detect(self, tmp_path, monkeypatch):
        """Without explicit targets, auto-detection catches selfdoc.json drift."""
        _make_project(
            tmp_path,
            targets=None,  # auto-detect
            pkg_version="2.0.0",
            selfdoc_version="1.9.0",
        )
        monkeypatch.chdir(tmp_path)

        ctx = ProjectContext(project_root=tmp_path, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "fail"
        assert "mismatch" in result.message
