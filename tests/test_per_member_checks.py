"""Tests for Phase 9.3: checks for multi-artifact releasables.

Covers:
- version-consistency enforces published members' manifests == releasable version
- dead-workspace-packages exempts published releasable members
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from rlsbl.workspace import (
    Releasable,
    save_workspace,
    write_releasable_version,
    get_releasable_dir,
    get_releasable_changes_dir,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)


def _write_workspace(tmp_path, content):
    """Write raw TOML content to workspace.toml."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


# ---------------------------------------------------------------------------
# version-consistency checks
# ---------------------------------------------------------------------------


class TestVersionConsistencyPublishedMembers:
    """version-consistency enforces manifest == releasable version."""

    @staticmethod
    def _get_check_fn():
        from conftest import capture_all_checks
        return capture_all_checks()["version-consistency"]

    def test_published_member_mismatch_fails(self, tmp_path):
        """Non-private member with mismatched manifest version fails."""
        _write_workspace(tmp_path, """\
[[releasables]]
name = "myrel"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "myrel"
""")
        write_releasable_version(str(tmp_path), "myrel", "2.0.0")

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n'
        )
        # Non-private config
        rlsbl_dir = pkg_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=pkg_dir,
            workspace_root=tmp_path,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        assert result.status == "fail"
        assert "mismatch" in result.message
        assert "2.0.0" in result.message

    def test_published_member_matching_passes(self, tmp_path):
        """Non-private member with matching manifest version passes."""
        _write_workspace(tmp_path, """\
[[releasables]]
name = "myrel"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "myrel"
""")
        write_releasable_version(str(tmp_path), "myrel", "2.0.0")

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "2.0.0"\n'
        )
        rlsbl_dir = pkg_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=pkg_dir,
            workspace_root=tmp_path,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        assert result.status == "pass"
        assert "manifests match" in result.message

    def test_private_member_skips_manifest_check(self, tmp_path):
        """Private member skips manifest check, uses version file only."""
        _write_workspace(tmp_path, """\
[[releasables]]
name = "myrel"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "myrel"
""")
        write_releasable_version(str(tmp_path), "myrel", "2.0.0")

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        # Manifest has different version -- should not fail for private
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n'
        )
        rlsbl_dir = pkg_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"publish_mode": "none", "targets": ["pypi"]}) + "\n"
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=pkg_dir,
            workspace_root=tmp_path,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        assert result.status == "pass"
        assert "publish-suppressed member" in result.message


# ---------------------------------------------------------------------------
# dead-workspace-packages exemption
# ---------------------------------------------------------------------------


class TestDeadWorkspacePackagesExemption:
    """Published releasable members are exempt from dead-package warning."""

    def test_published_member_exempted(self):
        """A published member with no importers is not flagged as dead."""
        from rlsbl.dep_validation import find_dead_workspace_packages

        projects = [
            {"name": "core", "library": True},
            {"name": "app", "library": False},
        ]

        # app imports nothing from core
        import_cache = {
            "core": (set(), set(), set()),
            "app": (set(), set(), set()),
        }

        # Without exemption, core would be dead
        dead = find_dead_workspace_packages(projects, import_cache)
        assert len(dead) == 1
        assert dead[0].name == "core"

        # With exemption, core is not dead
        dead = find_dead_workspace_packages(
            projects, import_cache,
            published_members={"core"},
        )
        assert len(dead) == 0

    def test_non_published_member_still_flagged(self):
        """A non-published library with no importers is still flagged."""
        from rlsbl.dep_validation import find_dead_workspace_packages

        projects = [
            {"name": "core", "library": True},
            {"name": "utils", "library": True},
            {"name": "app", "library": False},
        ]

        import_cache = {
            "core": (set(), set(), set()),
            "utils": (set(), set(), set()),
            "app": (set(), set(), set()),
        }

        # Exempt only core
        dead = find_dead_workspace_packages(
            projects, import_cache,
            published_members={"core"},
        )
        assert len(dead) == 1
        assert dead[0].name == "utils"
