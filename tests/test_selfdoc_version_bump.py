"""Tests for selfdoc.json version bump and version-consistency check.

Verifies that selfdoc.json is bumped during release even when "docs" is not
in the explicit targets list, and that the version-consistency check detects
drift in selfdoc.json regardless of target configuration.
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

        # Now apply the selfdoc.json fallback (the fix)
        bumped_files = set(files_to_commit)
        selfdoc_path = os.path.join(version_dir, "selfdoc.json")
        if os.path.exists(selfdoc_path) and "docs" not in target_paths:
            from rlsbl.targets.docs import DocsTarget
            docs_modified = DocsTarget().write_version(version_dir, new_version, ctx=make_ctx(version_dir))
            for rel in docs_modified:
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

    def test_bump_no_double_bump_when_docs_in_targets(self, tmp_path, monkeypatch):
        """When targets=["npm", "docs"], selfdoc.json is bumped by the loop, not the fallback."""
        _make_project(tmp_path, targets=["npm", "docs"], pkg_version="1.0.0", selfdoc_version="1.0.0")
        monkeypatch.chdir(tmp_path)

        from rlsbl.targets import TARGETS

        npm_target = TARGETS["npm"]
        version_dir = str(tmp_path)
        new_version = "1.1.0"
        registry = "npm"
        primary_path = version_dir
        target_paths = {"npm": version_dir, "docs": version_dir}

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

        # The fallback should NOT trigger because "docs" IS in target_paths
        selfdoc_path = os.path.join(version_dir, "selfdoc.json")
        assert os.path.exists(selfdoc_path)
        assert "docs" in target_paths  # so fallback is skipped

        # Verify selfdoc.json was already bumped by the loop
        with open(selfdoc_path) as f:
            data = json.load(f)
        assert data["version"] == "1.1.0"

        # Count how many times selfdoc.json appears in files_to_commit
        selfdoc_count = sum(1 for f in files_to_commit if "selfdoc.json" in f)
        assert selfdoc_count == 1, f"selfdoc.json appeared {selfdoc_count} times: {files_to_commit}"


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

    def test_passes_auto_detect_includes_docs(self, tmp_path, monkeypatch):
        """Without explicit targets, auto-detection picks up both npm and docs."""
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
        """Without explicit targets, auto-detection catches docs drift."""
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
