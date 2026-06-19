"""Tests for per-package .rlsbl/ cleanup after releasable model migration.

Covers:
- cleanup_per_package_release_state removes changes/ and releases/
- Cleanup skips non-releasable projects (releasable = false)
- Cleanup skips workspaces without [[releasables]] section
- verify_minimal_rlsbl identifies unexpected files
- verify_minimal_rlsbl passes for clean state
- saferm integration (mocked)
"""

import os
from unittest.mock import patch, call

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


# ---------------------------------------------------------------------------
# cleanup_per_package_release_state: removes changes/ and releases/
# ---------------------------------------------------------------------------


class TestCleanupRemovesState:
    """cleanup_per_package_release_state removes changes/ and releases/ for releasable members."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_removes_changes_dir(self, mock_run, tmp_project):
        """changes/ directory is removed for a releasable member."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert len(removed) == 1
        assert removed[0] == str(pkg / ".rlsbl" / "changes")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "saferm"
        assert "-r" in args

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_removes_releases_dir(self, mock_run, tmp_project):
        """releases/ directory is removed for a releasable member."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["releases"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert len(removed) == 1
        assert removed[0] == str(pkg / ".rlsbl" / "releases")

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_removes_both_dirs(self, mock_run, tmp_project):
        """Both changes/ and releases/ are removed when present."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes", "releases"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert len(removed) == 2
        paths = set(removed)
        assert str(pkg / ".rlsbl" / "changes") in paths
        assert str(pkg / ".rlsbl" / "releases") in paths
        assert mock_run.call_count == 2

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_skips_when_dirs_absent(self, mock_run, tmp_project):
        """No saferm calls when changes/ and releases/ don't exist."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, files=["config.json"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert removed == []
        mock_run.assert_not_called()

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_multiple_projects_in_releasable(self, mock_run, tmp_project):
        """Cleanup works across multiple projects in the same releasable."""
        for name in ("a", "b", "c"):
            _make_rlsbl_dir(tmp_project / name, subdirs=["changes"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"

[[projects]]
path = "b"
name = "b"
releasable = "core"

[[projects]]
path = "c"
name = "c"
releasable = "core"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert len(removed) == 3
        assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# cleanup_per_package_release_state: skips non-releasable projects
# ---------------------------------------------------------------------------


class TestCleanupSkipsNonReleasable:
    """Cleanup skips projects with releasable = false."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_skips_releasable_false(self, mock_run, tmp_project):
        """Projects with releasable = false are not cleaned."""
        pkg = tmp_project / "tests"
        _make_rlsbl_dir(pkg, subdirs=["changes", "releases"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"

[[projects]]
path = "tests"
name = "tests"
releasable = false
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert removed == []
        mock_run.assert_not_called()

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_skips_dev_node(self, mock_run, tmp_project):
        """Legacy dev_node projects (non-releasable) are not cleaned."""
        pkg = tmp_project / "tests"
        _make_rlsbl_dir(pkg, subdirs=["changes"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"

[[projects]]
path = "tests"
name = "tests"
dev_node = true
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert removed == []
        mock_run.assert_not_called()

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_only_cleans_releasable_members(self, mock_run, tmp_project):
        """Mixed workspace: only releasable members are cleaned."""
        for name in ("core-lib", "tests", "utils"):
            _make_rlsbl_dir(tmp_project / name, subdirs=["changes", "releases"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "core-lib"
name = "core-lib"
releasable = "core"

[[projects]]
path = "tests"
name = "tests"
releasable = false

[[projects]]
path = "utils"
name = "utils"
releasable = "core"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        # core-lib and utils each have changes/ and releases/ = 4 total
        assert len(removed) == 4
        removed_set = set(removed)
        assert str(tmp_project / "tests" / ".rlsbl" / "changes") not in removed_set
        assert str(tmp_project / "tests" / ".rlsbl" / "releases") not in removed_set


# ---------------------------------------------------------------------------
# cleanup_per_package_release_state: skips when no [[releasables]]
# ---------------------------------------------------------------------------


class TestCleanupSkipsNoReleasables:
    """Cleanup does nothing when workspace has no [[releasables]]."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_no_releasables_no_cleanup(self, mock_run, tmp_project):
        """Without [[releasables]], cleanup returns empty list."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes", "releases"])
        _write_workspace(tmp_project, """\
[[projects]]
path = "pkg"
name = "pkg"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert removed == []
        mock_run.assert_not_called()

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_no_releasables_with_dev_node(self, mock_run, tmp_project):
        """Without [[releasables]] and dev_node projects, still does nothing."""
        for name in ("a", "b"):
            _make_rlsbl_dir(tmp_project / name, subdirs=["changes"])
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"

[[projects]]
path = "b"
name = "b"
dev_node = true
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert removed == []
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# cleanup_per_package_release_state: saferm error handling
# ---------------------------------------------------------------------------


class TestCleanupSafermErrors:
    """Error handling for saferm invocation."""

    def test_saferm_not_found_raises(self, tmp_project):
        """RuntimeError when saferm is not on PATH."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        with patch("rlsbl.releasable_cleanup.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="saferm is not installed"):
                cleanup_per_package_release_state(str(tmp_project))

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_saferm_description_includes_project_name(self, mock_run, tmp_project):
        """saferm is called with a descriptive message including the project name."""
        pkg = tmp_project / "mylib"
        _make_rlsbl_dir(pkg, subdirs=["changes"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "mylib"
name = "mylib"
releasable = "core"
""")
        cleanup_per_package_release_state(str(tmp_project))
        args = mock_run.call_args[0][0]
        desc_idx = args.index("--description") + 1
        desc = args[desc_idx]
        assert "mylib" in desc
        assert "changes" in desc


# ---------------------------------------------------------------------------
# cleanup_per_package_release_state: pre-loaded projects and releasables
# ---------------------------------------------------------------------------


class TestCleanupPreloaded:
    """Cleanup accepts pre-loaded projects and releasables."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_preloaded_projects_and_releasables(self, mock_run, tmp_project):
        """Passing pre-loaded data avoids re-reading workspace.toml."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        from rlsbl.workspace import load_workspace, load_releasables
        projects = load_workspace(str(tmp_project))
        releasables = load_releasables(str(tmp_project), projects=projects)

        removed = cleanup_per_package_release_state(
            str(tmp_project), projects=projects, releasables=releasables
        )
        assert len(removed) == 1


# ---------------------------------------------------------------------------
# verify_minimal_rlsbl: identifies unexpected files
# ---------------------------------------------------------------------------


class TestVerifyMinimalIdentifiesUnexpected:
    """verify_minimal_rlsbl detects files/dirs that shouldn't be in .rlsbl/."""

    def test_changes_dir_is_unexpected(self, tmp_project):
        """changes/ directory is flagged as unexpected."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "changes" in result

    def test_releases_dir_is_unexpected(self, tmp_project):
        """releases/ directory is flagged as unexpected."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["releases"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "releases" in result

    def test_both_unexpected(self, tmp_project):
        """Both changes/ and releases/ are flagged."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes", "releases"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "changes" in result
        assert "releases" in result

    def test_unknown_file_is_unexpected(self, tmp_project):
        """An unknown file (e.g., version) is flagged."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, files=["version"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "version" in result

    def test_unknown_dir_is_unexpected(self, tmp_project):
        """An unknown directory (e.g., bases/) is flagged."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["bases"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "bases" in result

    def test_multiple_unexpected(self, tmp_project):
        """Multiple unexpected entries are all reported."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes", "releases", "bases"], files=["version"])
        result = verify_minimal_rlsbl(str(pkg))
        assert len(result) == 4
        assert set(result) == {"changes", "releases", "bases", "version"}

    def test_results_are_sorted(self, tmp_project):
        """Unexpected entries are returned in sorted order."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["releases", "changes", "bases"])
        result = verify_minimal_rlsbl(str(pkg))
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# verify_minimal_rlsbl: passes for clean state
# ---------------------------------------------------------------------------


class TestVerifyMinimalCleanState:
    """verify_minimal_rlsbl returns empty list for expected contents."""

    def test_empty_rlsbl_dir(self, tmp_project):
        """Empty .rlsbl/ is clean."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg)
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_no_rlsbl_dir(self, tmp_project):
        """No .rlsbl/ directory at all is clean."""
        pkg = tmp_project / "pkg"
        pkg.mkdir()
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_all_expected_files(self, tmp_project):
        """All expected files present -- no unexpected entries."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(
            pkg,
            subdirs=["hooks"],
            files=["publish.json", "config.json", "hashes.json", "managed-files.json"],
        )
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_subset_of_expected_files(self, tmp_project):
        """A subset of expected files is still clean."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, files=["publish.json", "config.json"])
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_only_hooks_dir(self, tmp_project):
        """Only hooks/ directory is clean."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["hooks"])
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_expected_contents_match(self):
        """EXPECTED_RLSBL_CONTENTS has the documented set."""
        assert EXPECTED_RLSBL_CONTENTS == {
            "publish.json",
            "config.json",
            "hooks",
            "hashes.json",
            "managed-files.json",
        }

    def test_clean_after_cleanup(self, tmp_project):
        """After cleanup removes changes/ and releases/, verify passes."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(
            pkg,
            subdirs=["changes", "releases", "hooks"],
            files=["config.json", "publish.json"],
        )
        # Simulate cleanup by removing the directories
        import shutil
        shutil.rmtree(str(pkg / ".rlsbl" / "changes"))
        shutil.rmtree(str(pkg / ".rlsbl" / "releases"))

        result = verify_minimal_rlsbl(str(pkg))
        assert result == []


# ---------------------------------------------------------------------------
# verify_minimal_rlsbl: mixed state
# ---------------------------------------------------------------------------


class TestVerifyMinimalMixedState:
    """verify_minimal_rlsbl with a mix of expected and unexpected contents."""

    def test_expected_plus_unexpected(self, tmp_project):
        """Expected files are not reported; only unexpected ones."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(
            pkg,
            subdirs=["hooks", "changes"],
            files=["config.json", "publish.json", "version"],
        )
        result = verify_minimal_rlsbl(str(pkg))
        assert "changes" in result
        assert "version" in result
        assert "hooks" not in result
        assert "config.json" not in result
        assert "publish.json" not in result
