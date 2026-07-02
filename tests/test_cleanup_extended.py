"""Tests for extended cleanup scope in releasable migration (Phase 5c).

Covers:
- hooks/ directory removal during cleanup
- bases/ directory removal during cleanup
- lint/ directory removal during cleanup
- CHANGELOG.md removal during cleanup
- .rlsbl/version file removal during cleanup
- config.json removal when identical to releasable-level config
- config.json preservation when different from releasable-level config
- verify_minimal_rlsbl returns empty list after full cleanup
- verify_minimal_rlsbl flags unexpected files
"""

import json
import os
from unittest.mock import patch

import pytest

from rlsbl.releasable_cleanup import (
    EXPECTED_RLSBL_CONTENTS,
    cleanup_per_package_release_state,
    verify_minimal_rlsbl,
)
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workspace(tmp_path, content):
    """Write raw TOML content to workspace.toml."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


def _make_rlsbl_dir(project_dir, subdirs=None, files=None):
    """Create .rlsbl/ with optional subdirs and files inside the project."""
    rlsbl = project_dir / ".rlsbl"
    rlsbl.mkdir(parents=True, exist_ok=True)
    for sd in (subdirs or []):
        (rlsbl / sd).mkdir(parents=True, exist_ok=True)
    for f in (files or []):
        (rlsbl / f).parent.mkdir(parents=True, exist_ok=True)
        (rlsbl / f).write_text("")


def _write_releasable_config(tmp_path, releasable_name, config_dict):
    """Write a config.json for a releasable's state directory."""
    rel_dir = tmp_path / WORKSPACE_DIR / "releasables" / releasable_name
    rel_dir.mkdir(parents=True, exist_ok=True)
    (rel_dir / "config.json").write_text(json.dumps(config_dict, indent=2) + "\n")


WORKSPACE_WITH_RELEASABLE = """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
"""


# ---------------------------------------------------------------------------
# Test 1: hooks/ directory preserved during cleanup (live feature)
# ---------------------------------------------------------------------------


class TestCleanupPreservesHooks:
    """cleanup_per_package_release_state preserves hooks/ -- per-package
    script hooks are a live feature (run_releasable_hooks)."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_hooks_dir_preserved(self, mock_run, tmp_project):
        """hooks/ directory is NOT removed for a releasable member."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["hooks"])
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "hooks" not in removed_names
        assert (pkg / ".rlsbl" / "hooks").is_dir()
        calls = [c[0][0] for c in mock_run.call_args_list]
        hooks_calls = [c for c in calls if str(pkg / ".rlsbl" / "hooks") in c]
        assert hooks_calls == []


# ---------------------------------------------------------------------------
# Test 2: bases/ directory removed during cleanup
# ---------------------------------------------------------------------------


class TestCleanupRemovesBases:
    """cleanup_per_package_release_state removes bases/ directory."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_bases_dir_removed(self, mock_run, tmp_project):
        """bases/ directory is removed for a releasable member."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["bases"])
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "bases" in removed_names
        calls = [c[0][0] for c in mock_run.call_args_list]
        bases_calls = [c for c in calls if str(pkg / ".rlsbl" / "bases") in c]
        assert len(bases_calls) == 1
        assert "-r" in bases_calls[0]


# ---------------------------------------------------------------------------
# Test 3: lint/ directory removed during cleanup
# ---------------------------------------------------------------------------


class TestCleanupRemovesLint:
    """cleanup_per_package_release_state removes lint/ directory."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_lint_dir_removed(self, mock_run, tmp_project):
        """lint/ directory is removed for a releasable member."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["lint"])
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "lint" in removed_names
        calls = [c[0][0] for c in mock_run.call_args_list]
        lint_calls = [c for c in calls if str(pkg / ".rlsbl" / "lint") in c]
        assert len(lint_calls) == 1
        assert "-r" in lint_calls[0]


# ---------------------------------------------------------------------------
# Test 4: CHANGELOG.md removed during cleanup
# ---------------------------------------------------------------------------


class TestCleanupRemovesChangelog:
    """cleanup_per_package_release_state removes CHANGELOG.md."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_changelog_removed(self, mock_run, tmp_project):
        """CHANGELOG.md is removed for a releasable member."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        (pkg / "CHANGELOG.md").write_text("# Changelog\n")
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "CHANGELOG.md" in removed_names
        # saferm is called WITHOUT -r for a file
        calls = [c[0][0] for c in mock_run.call_args_list]
        changelog_calls = [c for c in calls if str(pkg / "CHANGELOG.md") in c]
        assert len(changelog_calls) == 1
        assert "-r" not in changelog_calls[0]

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_changelog_not_removed_when_absent(self, mock_run, tmp_project):
        """CHANGELOG.md is not removed when it does not exist."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "CHANGELOG.md" not in removed_names


# ---------------------------------------------------------------------------
# Test 5: .rlsbl/version file removed during cleanup
# ---------------------------------------------------------------------------


class TestCleanupRemovesVersion:
    """cleanup_per_package_release_state removes .rlsbl/version file."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_version_file_removed(self, mock_run, tmp_project):
        """.rlsbl/version file is removed for a releasable member."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, files=["version"])
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "version" in removed_names
        # saferm is called WITHOUT -r for a file
        calls = [c[0][0] for c in mock_run.call_args_list]
        version_calls = [c for c in calls if str(pkg / ".rlsbl" / "version") in c]
        assert len(version_calls) == 1
        assert "-r" not in version_calls[0]

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_version_file_not_removed_when_absent(self, mock_run, tmp_project):
        """.rlsbl/version is not removed when it does not exist."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "version" not in removed_names


# ---------------------------------------------------------------------------
# Test 6: config.json removed when identical to releasable config
# ---------------------------------------------------------------------------


class TestCleanupConfigIdentical:
    """cleanup_per_package_release_state removes config.json when matching releasable."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_config_removed_when_identical(self, mock_run, tmp_project):
        """config.json is removed when identical to releasable-level config."""
        pkg = tmp_project / "pkg"
        shared_config = {"private": False, "batch_limits": {"max_commits_per_entry": 5}}
        _make_rlsbl_dir(pkg)
        (pkg / ".rlsbl" / "config.json").write_text(
            json.dumps(shared_config, indent=2) + "\n"
        )
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", shared_config)

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "config.json" in removed_names

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_config_removed_when_both_empty(self, mock_run, tmp_project):
        """config.json is removed when both pkg and releasable configs are empty dicts."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        (pkg / ".rlsbl" / "config.json").write_text("{}\n")
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "config.json" in removed_names


# ---------------------------------------------------------------------------
# Test 7: config.json kept when different from releasable config
# ---------------------------------------------------------------------------


class TestCleanupConfigDifferent:
    """cleanup_per_package_release_state keeps config.json when it differs."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_config_kept_when_different(self, mock_run, tmp_project):
        """config.json is kept when it differs from releasable-level config."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        pkg_config = {"private": False, "custom_setting": "override"}
        rel_config = {"private": False}
        (pkg / ".rlsbl" / "config.json").write_text(
            json.dumps(pkg_config, indent=2) + "\n"
        )
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", rel_config)

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "config.json" not in removed_names

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_config_kept_when_value_differs(self, mock_run, tmp_project):
        """config.json is kept when a value differs from releasable config."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        pkg_config = {"private": True}
        rel_config = {"private": False}
        (pkg / ".rlsbl" / "config.json").write_text(
            json.dumps(pkg_config, indent=2) + "\n"
        )
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", rel_config)

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "config.json" not in removed_names

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_config_kept_when_releasable_has_no_config(self, mock_run, tmp_project):
        """config.json with content is kept when releasable has no config (empty dict)."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        pkg_config = {"private": False}
        (pkg / ".rlsbl" / "config.json").write_text(
            json.dumps(pkg_config, indent=2) + "\n"
        )
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        # Releasable config.json does not exist -- read_json_config returns {}
        # (we do NOT write a releasable config, so it returns {})

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]
        assert "config.json" not in removed_names


# ---------------------------------------------------------------------------
# Test 8: verify_minimal_rlsbl returns empty list after full cleanup
# ---------------------------------------------------------------------------


class TestVerifyMinimalAfterFullCleanup:
    """verify_minimal_rlsbl returns empty list when only minimal files remain."""

    def test_empty_after_full_cleanup(self, tmp_project):
        """After all non-minimal state is removed, verify returns empty list."""
        pkg = tmp_project / "pkg"
        # Start with full state
        _make_rlsbl_dir(
            pkg,
            subdirs=["changes", "releases", "hooks", "bases", "lint"],
            files=[
                "config.json", "hashes.json",
                "managed-files.json", "version",
            ],
        )
        # Simulate full cleanup: remove everything except the minimal set
        import shutil
        for subdir in ("changes", "releases", "hooks", "bases", "lint"):
            shutil.rmtree(str(pkg / ".rlsbl" / subdir))
        os.remove(str(pkg / ".rlsbl" / "version"))

        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_minimal_set_only(self, tmp_project):
        """Only hashes.json, managed-files.json is clean."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(
            pkg,
            files=["hashes.json", "managed-files.json"],
        )
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_minimal_set_with_config_override(self, tmp_project):
        """Minimal set plus config.json (for overrides) is clean."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(
            pkg,
            files=["hashes.json", "managed-files.json", "config.json"],
        )
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []


# ---------------------------------------------------------------------------
# Test 9: verify_minimal_rlsbl flags unexpected files
# ---------------------------------------------------------------------------


class TestVerifyMinimalFlagsUnexpected:
    """verify_minimal_rlsbl identifies all non-minimal state as unexpected."""

    def test_hooks_is_expected(self, tmp_project):
        """hooks/ directory is NOT flagged: per-package script hooks are a
        live feature."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["hooks"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "hooks" not in result

    def test_bases_is_unexpected(self, tmp_project):
        """bases/ directory is flagged as unexpected."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["bases"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "bases" in result

    def test_lint_is_unexpected(self, tmp_project):
        """lint/ directory is flagged as unexpected."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["lint"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "lint" in result

    def test_version_is_unexpected(self, tmp_project):
        """version file is flagged as unexpected."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, files=["version"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "version" in result

    def test_changes_is_unexpected(self, tmp_project):
        """changes/ directory is still flagged as unexpected."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "changes" in result

    def test_releases_is_unexpected(self, tmp_project):
        """releases/ directory is still flagged as unexpected."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["releases"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "releases" in result

    def test_all_non_minimal_flagged(self, tmp_project):
        """All non-minimal state is flagged in a single call."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(
            pkg,
            subdirs=["changes", "releases", "hooks", "bases", "lint"],
            files=["version"],
        )
        result = verify_minimal_rlsbl(str(pkg))
        assert set(result) == {"changes", "releases", "bases", "lint", "version"}

    def test_expected_contents_is_minimal(self):
        """EXPECTED_RLSBL_CONTENTS contains only the minimal set."""
        assert EXPECTED_RLSBL_CONTENTS == {
            "config.json",
            "hashes.json",
            "managed-files.json",
            "hooks",
        }


# ---------------------------------------------------------------------------
# Integration: all new cleanup targets together
# ---------------------------------------------------------------------------


class TestCleanupAllNewTargets:
    """Verify that all new cleanup targets are removed in a single pass."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_full_cleanup_removes_all(self, mock_run, tmp_project):
        """All cleanup targets are removed for a releasable member in one call."""
        pkg = tmp_project / "pkg"
        shared_config = {"private": False}
        _make_rlsbl_dir(
            pkg,
            subdirs=["changes", "releases", "hooks", "bases", "lint"],
            files=["version"],
        )
        (pkg / ".rlsbl" / "config.json").write_text(
            json.dumps(shared_config, indent=2) + "\n"
        )
        (pkg / "CHANGELOG.md").write_text("# Changelog\n")
        _write_workspace(tmp_project, WORKSPACE_WITH_RELEASABLE)
        _write_releasable_config(tmp_project, "core", shared_config)

        removed = cleanup_per_package_release_state(str(tmp_project))
        removed_names = [os.path.basename(p) for p in removed]

        # Directories (hooks/ is preserved: live per-package hooks feature)
        assert "changes" in removed_names
        assert "releases" in removed_names
        assert "hooks" not in removed_names
        assert "bases" in removed_names
        assert "lint" in removed_names
        # Files
        assert "version" in removed_names
        assert "CHANGELOG.md" in removed_names
        assert "config.json" in removed_names

        assert len(removed) == 7  # 4 dirs (hooks/ preserved) + 3 files

