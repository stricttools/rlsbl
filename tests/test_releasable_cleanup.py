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

import subprocess as _subprocess_module

# Original subprocess.run, captured before any test patches the shared
# subprocess module object.
_REAL_SUBPROCESS_RUN = _subprocess_module.run


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
        _make_rlsbl_dir(pkg)
        # Write valid JSON so read_json_config doesn't choke on empty content.
        # Use a unique value so it differs from the releasable config (empty dict),
        # preventing config.json cleanup from triggering.
        (pkg / ".rlsbl" / "config.json").write_text('{"unique": true}\n')
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
            files=["config.json", "hashes.json", "managed-files.json"],
        )
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_subset_of_expected_files(self, tmp_project):
        """A subset of expected files is still clean."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, files=["config.json"])
        result = verify_minimal_rlsbl(str(pkg))
        assert result == []

    def test_hooks_dir_is_expected(self, tmp_project):
        """hooks/ directory is expected: per-package script hooks are a
        live feature (run_releasable_hooks / get_package_hook_path)."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["hooks"])
        result = verify_minimal_rlsbl(str(pkg))
        assert "hooks" not in result

    def test_expected_contents_match(self):
        """EXPECTED_RLSBL_CONTENTS has the documented set."""
        assert EXPECTED_RLSBL_CONTENTS == {
            "config.json",
            "hashes.json",
            "managed-files.json",
            "hooks",
        }

    def test_clean_after_cleanup(self, tmp_project):
        """After cleanup removes all non-minimal state, verify passes."""
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(
            pkg,
            subdirs=["changes", "releases", "hooks", "bases", "lint"],
            files=["config.json", "version"],
        )
        # Simulate cleanup by removing all non-minimal entries
        # (hooks/ stays: per-package script hooks are a live feature)
        import shutil
        for subdir in ("changes", "releases", "bases", "lint"):
            shutil.rmtree(str(pkg / ".rlsbl" / subdir))
        os.remove(str(pkg / ".rlsbl" / "version"))

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
            files=["config.json", "version"],
        )
        result = verify_minimal_rlsbl(str(pkg))
        assert "changes" in result
        assert "version" in result
        assert "hooks" not in result  # live per-package script hooks feature
        assert "config.json" not in result


# ---------------------------------------------------------------------------
# Root-path member exemption + dry-run
# ---------------------------------------------------------------------------


class TestRootMemberExemption:
    """Members whose path resolves to the workspace root are exempt from
    cleanup: their .rlsbl/ and CHANGELOG.md are workspace-level files."""

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_root_member_untouched(self, mock_run, tmp_project):
        _make_rlsbl_dir(tmp_project, subdirs=["changes", "releases"],
                        files=["version"])
        (tmp_project / "CHANGELOG.md").write_text("# combined root changelog\n")
        _write_workspace(tmp_project, """\
[[releasables]]
name = "solo"

[[projects]]
path = "."
name = "solo-pkg"
releasable = "solo"
""")
        removed = cleanup_per_package_release_state(str(tmp_project))
        assert removed == []
        assert (tmp_project / "CHANGELOG.md").is_file()
        assert (tmp_project / ".rlsbl" / "changes").is_dir()
        mock_run.assert_not_called()


class TestCleanupDryRun:

    @patch("rlsbl.releasable_cleanup.subprocess.run")
    def test_dry_run_collects_without_deleting(self, mock_run, tmp_project):
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes", "releases"], files=["version"])
        (pkg / "CHANGELOG.md").write_text("# changelog\n")
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        removed = cleanup_per_package_release_state(str(tmp_project), dry_run=True)
        assert sorted(os.path.basename(p) for p in removed) == [
            "CHANGELOG.md", "changes", "releases", "version",
        ]
        # Nothing deleted, no saferm calls
        assert (pkg / ".rlsbl" / "changes").is_dir()
        assert (pkg / "CHANGELOG.md").is_file()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# releasable-residue check
# ---------------------------------------------------------------------------


class TestReleasableResidueCheck:
    """The releasable-residue workspace check wraps verify_minimal_rlsbl as
    a hard error, with the hooks/ and root-path-member exemptions."""

    def _ctx(self, root):
        from pathlib import Path

        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_releasables, load_workspace

        projects = load_workspace(str(root))
        releasables = load_releasables(str(root), projects=projects)
        return WorkspaceCheckContext(
            project_root=Path(str(root)),
            workspace_root=Path(str(root)),
            config={},
            projects=projects,
            graph=None,
            releasables=releasables,
        )

    def _impl(self):
        from rlsbl import app
        return app._check_defs["releasable-residue"].impl

    def test_registered_as_hard_error(self):
        from rlsbl import app
        check_def = app._check_defs["releasable-residue"]
        assert check_def.impl is not None
        assert "workspace" in check_def.tags
        assert check_def.severity == "error"

    def test_fails_on_residue(self, tmp_project):
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes"], files=["version"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        result = self._impl()(self._ctx(tmp_project))
        assert result.status == "fail"
        assert any("changes" in d for d in result.details)
        assert any("version" in d for d in result.details)

    def test_passes_on_minimal_state_with_hooks(self, tmp_project):
        pkg = tmp_project / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["hooks"],
                        files=["config.json", "hashes.json"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        result = self._impl()(self._ctx(tmp_project))
        assert result.status == "pass", result.message

    def test_root_member_exempt(self, tmp_project):
        _make_rlsbl_dir(tmp_project, subdirs=["changes", "releases"],
                        files=["version"])
        _write_workspace(tmp_project, """\
[[releasables]]
name = "solo"

[[projects]]
path = "."
name = "solo-pkg"
releasable = "solo"
""")
        result = self._impl()(self._ctx(tmp_project))
        assert result.status == "pass", result.message

    def test_skips_without_releasables(self, tmp_project):
        _write_workspace(tmp_project, """\
[[projects]]
path = "pkg"
name = "pkg"
""")
        from pathlib import Path

        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace

        ctx = WorkspaceCheckContext(
            project_root=Path(str(tmp_project)),
            workspace_root=Path(str(tmp_project)),
            config={},
            projects=load_workspace(str(tmp_project)),
            graph=None,
            releasables=[],
        )
        result = self._impl()(ctx)
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# rlsbl monorepo cleanup command
# ---------------------------------------------------------------------------


class TestCleanupCommand:
    """run_cleanup_command removes residue via saferm and commits the
    deletions so trees stay clean."""

    @staticmethod
    def _mock_saferm_delete(cmd, *args, **kwargs):
        import shutil
        import subprocess as real_subprocess

        if isinstance(cmd, list) and cmd and cmd[0] == "saferm":
            target = cmd[-1]
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                os.unlink(target)
            return real_subprocess.CompletedProcess(args=cmd, returncode=0)
        # subprocess is a shared module object, so patching
        # rlsbl.releasable_cleanup.subprocess.run patches it globally --
        # delegate to the original captured at module import time.
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    def _setup_git_workspace(self, root):
        import subprocess as sp

        def git(*args):
            sp.run(["git", *args], cwd=str(root), check=True,
                   capture_output=True, text=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t.local")
        git("config", "user.name", "T")

        pkg = root / "pkg"
        _make_rlsbl_dir(pkg, subdirs=["changes", "hooks"],
                        files=["version", "config.json"])
        (pkg / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        (pkg / ".rlsbl" / "hooks" / "pre-release.sh").write_text("#!/bin/sh\n")
        (pkg / ".rlsbl" / "config.json").write_text('{"private": true}\n')
        (pkg / "CHANGELOG.md").write_text("# Changelog\n")
        _write_workspace(root, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        rel_dir = root / WORKSPACE_DIR / "releasables" / "core"
        rel_dir.mkdir(parents=True, exist_ok=True)
        (rel_dir / "config.json").write_text('{"private": true}\n')
        git("add", "-A")
        git("commit", "-q", "-m", "initial")
        return pkg

    def test_cleanup_removes_and_commits(self, tmp_project, monkeypatch):
        from unittest.mock import patch as _patch

        from rlsbl.releasable_cleanup import run_cleanup_command

        pkg = self._setup_git_workspace(tmp_project)
        monkeypatch.chdir(tmp_project)

        with _patch(
            "rlsbl.releasable_cleanup.subprocess.run",
            side_effect=self._mock_saferm_delete,
        ):
            removed = run_cleanup_command(str(tmp_project), yes=True)

        # Residue removed (changes/, version, CHANGELOG.md, config.json --
        # identical to releasable config); hooks/ preserved
        assert not (pkg / ".rlsbl" / "changes").exists()
        assert not (pkg / ".rlsbl" / "version").exists()
        assert not (pkg / "CHANGELOG.md").exists()
        assert not (pkg / ".rlsbl" / "config.json").exists()
        assert (pkg / ".rlsbl" / "hooks" / "pre-release.sh").is_file()
        assert len(removed) == 4

        # Deletions committed: clean tree + commit message
        import subprocess as sp
        status = sp.run(
            ["git", "status", "--porcelain"], cwd=str(tmp_project),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert status == "", f"cleanup must commit its deletions, got: {status}"
        head_msg = sp.run(
            ["git", "log", "-1", "--format=%s"], cwd=str(tmp_project),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert "residue" in head_msg

    def test_cleanup_dry_run_removes_nothing(self, tmp_project, monkeypatch):
        from unittest.mock import patch as _patch

        from rlsbl.releasable_cleanup import run_cleanup_command

        pkg = self._setup_git_workspace(tmp_project)
        monkeypatch.chdir(tmp_project)

        with _patch(
            "rlsbl.releasable_cleanup.subprocess.run",
            side_effect=self._mock_saferm_delete,
        ):
            would = run_cleanup_command(str(tmp_project), dry_run=True)

        assert len(would) == 4
        assert (pkg / ".rlsbl" / "changes").is_dir()
        assert (pkg / "CHANGELOG.md").is_file()

    def test_cleanup_command_registered(self):
        from rlsbl import app
        # The monorepo group must expose the cleanup command
        result = app.test(["monorepo", "--help"])
        assert "cleanup" in result.stdout
