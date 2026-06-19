"""Targeted coverage tests for commands modules with <85% coverage.

Covers uncovered paths in:
- rlsbl.commands.undo (66% -> 85%+)
- rlsbl.commands.status (79% -> 85%+)
- rlsbl.commands.unreleased (84% -> 85%+)
- rlsbl.commands.release/__init__.py (76% -> 85%+)
- rlsbl.commands.release/execute.py (82% -> 85%+)
- rlsbl.commands.release_scrub (79% -> 85%+)
- rlsbl.commands.changelog_cmd (78% -> 85%+)
- rlsbl.commands.monorepo.batch_release (57% -> 85%+)
"""

import json
import os
import re
import stat
import subprocess
import time
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from rlsbl.context import ProjectContext

# Convenience to build a minimal context
def _ctx(root=".", config=None, workspace_root=None):
    if isinstance(root, str):
        root = Path(root)
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    return ProjectContext(
        project_root=root,
        workspace_root=workspace_root,
        config=config or {},
    )


# ============================================================================
# rlsbl.commands.undo -- uncovered lines: 25-32, 37-38, 40-41, 64-68, 72-74,
#   79-80, 97-104, 118-120, 128-130, 136-138, 145-147, 180-182, 198-199,
#   202-204, 216-218, 232-234, 240-244, 255-257, 262
# ============================================================================

MOD_UNDO = "rlsbl.commands.undo"


class TestUndoPrintSummary:
    """_print_summary is called only when at least one step FAILed."""

    def test_summary_printed_on_failure(self):
        from rlsbl.commands.undo import _print_summary

        results = [
            ("Delete GitHub Release", "OK", "-"),
            ("Delete remote tag", "FAILED", "git push origin :v1.0.0"),
            ("Delete local tag", "OK", "-"),
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            _print_summary(results)
        output = out.getvalue()
        assert "Step" in output
        assert "FAILED" in output
        assert "git push origin :v1.0.0" in output


class TestUndoGhNotInstalled:
    """Covers lines 37-38: gh CLI not installed."""

    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=False)
    def test_exits_if_gh_not_installed(self, _gh_inst):
        from rlsbl.commands.undo import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert exc_info.value.code == 1


class TestUndoGhNotAuthed:
    """Covers lines 40-41: gh CLI not authenticated."""

    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=False)
    def test_exits_if_gh_not_authed(self, _auth, _inst):
        from rlsbl.commands.undo import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert exc_info.value.code == 1


class TestUndoDirtyTree:
    """Covers line 43-45: working tree not clean."""

    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=False)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    def test_exits_if_dirty(self, _inst, _auth, _clean):
        from rlsbl.commands.undo import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert exc_info.value.code == 1


class TestUndoNoTagsFound:
    """Covers lines 87-89: no tags found."""

    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run", side_effect=Exception("no tags"))
    def test_exits_if_no_tags(self, _run, _inst, _auth, _clean, _ws):
        from rlsbl.commands.undo import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert exc_info.value.code == 1


class TestUndoInteractiveAbort:
    """Covers lines 97-104: user says 'n' or EOF at prompt."""

    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run", return_value="v1.0.0")
    def test_aborted_on_no(self, _run, _inst, _auth, _clean, _ws):
        from rlsbl.commands.undo import run_cmd

        with patch("builtins.input", return_value="n"):
            with patch("sys.stdout", new_callable=StringIO):
                with pytest.raises(SystemExit) as exc_info:
                    run_cmd("npm", [], {}, ctx=_ctx())
        assert exc_info.value.code == 0

    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run", return_value="v1.0.0")
    def test_aborted_on_eof(self, _run, _inst, _auth, _clean, _ws):
        from rlsbl.commands.undo import run_cmd

        with patch("builtins.input", side_effect=EOFError):
            with patch("sys.stdout", new_callable=StringIO):
                with pytest.raises(SystemExit) as exc_info:
                    run_cmd("npm", [], {}, ctx=_ctx())
        assert exc_info.value.code == 1


class TestUndoGhDeleteFails:
    """Covers lines 118-120: gh release delete fails."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed")
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_gh_delete_failure_shows_summary(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",                               # git describe
            "",                                      # gh release view -> OK
            Exception("delete failed"),              # gh release delete -> FAILS
            "",                                      # git push origin :v1.0.0
            "",                                      # git tag -d v1.0.0
            "v1.0.0",                               # git log -1 --format=%s
            "",                                      # git revert
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            with patch("sys.stderr", new_callable=StringIO):
                run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        # Summary table should be printed (has FAILED step)
        assert "FAILED" in out.getvalue()


class TestUndoRemoteTagDeleteFails:
    """Covers lines 128-130: git push origin :tag fails."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed")
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_remote_tag_delete_failure(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",                               # gh release view
            "",                               # gh release delete
            Exception("push failed"),         # git push origin :v1.0.0 -> FAILS
            "",                               # git tag -d
            "v1.0.0",                        # git log
            "",                               # git revert
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            with patch("sys.stderr", new_callable=StringIO):
                run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert "FAILED" in out.getvalue()


class TestUndoLocalTagDeleteFails:
    """Covers lines 136-138: git tag -d fails."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed")
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_local_tag_delete_failure(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",                              # gh release view
            "",                              # gh release delete
            "",                              # git push origin :v1.0.0
            Exception("tag -d failed"),      # git tag -d -> FAILS
            "v1.0.0",                       # git log
            "",                              # git revert
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            with patch("sys.stderr", new_callable=StringIO):
                run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert "FAILED" in out.getvalue()


class TestUndoRevertException:
    """Covers lines 202-204: git revert throws exception."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_revert_exception(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",                                # gh release view
            "",                                # gh release delete
            "",                                # git push origin :v1.0.0
            "",                                # git tag -d
            "v1.0.0",                         # git log
            Exception("revert failed"),        # git revert -> FAILS
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            with patch("sys.stderr", new_callable=StringIO):
                run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert "FAILED" in out.getvalue()


class TestUndoChangelogRestoreFails:
    """Covers lines 216-218: changelog restoration exception."""

    @patch(f"{MOD_UNDO}.generate_changelog", side_effect=Exception("gen failed"))
    @patch(f"{MOD_UNDO}.unfinalize_version", return_value=["unreleased.jsonl"])
    @patch(f"{MOD_UNDO}.get_changes_dir", return_value="/fake/.rlsbl/changes")
    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed")
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_changelog_restore_failure_in_summary(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",                                           # gh release view
            "",                                           # gh release delete
            "",                                           # git push origin :v1.0.0
            "",                                           # git tag -d
            "chore: finalize changelog for 1.0.0",       # git log (finalize commit)
            "",                                           # git revert (finalize)
            "v1.0.0",                                    # git log (version-bump)
            "",                                           # git revert (version-bump)
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            with patch("sys.stderr", new_callable=StringIO):
                run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        output = out.getvalue()
        assert "FAILED" in output
        assert "Restore changelog" in output


class TestUndoReleaseFileRestoreFails:
    """Covers lines 232-234: release file restore exception."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", side_effect=Exception("bad"))
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed")
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_release_file_restore_failure(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",         # gh release view
            "",         # gh release delete
            "",         # git push origin :v1.0.0
            "",         # git tag -d
            "v1.0.0",   # git log
            "",         # git revert
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            with patch("sys.stderr", new_callable=StringIO):
                run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert "FAILED" in out.getvalue()


class TestUndoPushDeclined:
    """Covers lines 240-244: user declines push after revert."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_push_declined_by_user(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",         # gh release view
            "",         # gh release delete
            "",         # git push origin :v1.0.0
            "",         # git tag -d
            "v1.0.0",   # git log
            "",         # git revert
        ]
        # First input is the undo confirmation ("y"), second is the push prompt ("n")
        with patch("builtins.input", side_effect=["y", "n"]):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd("npm", [], {}, ctx=_ctx())
        # No push_if_needed called -- verified by no push call in mock_run

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_push_eof_at_prompt(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",         # gh release view
            "",         # gh release delete
            "",         # git push origin :v1.0.0
            "",         # git tag -d
            "v1.0.0",   # git log
            "",         # git revert
        ]
        # First input is undo confirmation ("y"), second is push prompt (EOF)
        with patch("builtins.input", side_effect=["y", EOFError]):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd("npm", [], {}, ctx=_ctx())


class TestUndoPushFails:
    """Covers lines 255-257: push_if_needed fails."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed", side_effect=Exception("push boom"))
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_push_fails_shows_failure(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",         # gh release view
            "",         # gh release delete
            "",         # git push origin :v1.0.0
            "",         # git tag -d
            "v1.0.0",   # git log
            "",         # git revert
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            with patch("sys.stderr", new_callable=StringIO):
                run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert "FAILED" in out.getvalue()
        assert "Push" in out.getvalue()


class TestUndoReleaseFileFinalize:
    """Covers lines 179-182: release-file finalize commit at HEAD is peeled."""

    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=["unreleased.toml"])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed")
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_release_file_finalize_at_head(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",                                         # git describe
            "",                                                # gh release view
            "",                                                # gh release delete
            "",                                                # git push origin :v1.0.0
            "",                                                # git tag -d
            "chore: finalize release file for 1.0.0",         # git log (release-file finalize)
            "",                                                # git revert (release-file finalize)
            "v1.0.0",                                         # git log (version-bump)
            "",                                                # git revert (version-bump)
            "",                                                # git add (release file restore)
            "",                                                # git commit (release file restore)
        ]
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        assert mock_run.call_count == 11


class TestUndoFinalizeOnlyReverted:
    """Covers lines 196-199: finalize commits reverted but HEAD is not the version-bump."""

    @patch(f"{MOD_UNDO}.generate_changelog")
    @patch(f"{MOD_UNDO}.unfinalize_version", return_value=["unreleased.jsonl"])
    @patch(f"{MOD_UNDO}.get_changes_dir", return_value="/fake/.rlsbl/changes")
    @patch(f"{MOD_UNDO}.unfinalize_release_file", return_value=[])
    @patch(f"{MOD_UNDO}.find_workspace_root", return_value=None)
    @patch(f"{MOD_UNDO}.push_if_needed")
    @patch(f"{MOD_UNDO}.get_current_branch", return_value="main")
    @patch(f"{MOD_UNDO}.is_clean_tree", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_auth", return_value=True)
    @patch(f"{MOD_UNDO}.check_gh_installed", return_value=True)
    @patch(f"{MOD_UNDO}.run")
    def test_finalize_reverted_but_no_version_bump(self, mock_run, *_):
        from rlsbl.commands.undo import run_cmd

        mock_run.side_effect = [
            "v1.0.0",
            "",                                           # gh release view
            "",                                           # gh release delete
            "",                                           # git push origin :v1.0.0
            "",                                           # git tag -d
            "chore: finalize changelog for 1.0.0",       # git log (finalize)
            "",                                           # git revert (finalize)
            "some other commit",                          # git log (not version-bump)
            # changelog restoration: git add, git commit
            "",                                           # git add
            "",                                           # git commit
        ]
        with patch("sys.stdout", new_callable=StringIO) as out:
            run_cmd("npm", [], {"yes": True}, ctx=_ctx())
        # All steps OK, so simple success message (no summary table)
        assert "Undo complete" in out.getvalue()


# ============================================================================
# rlsbl.commands.status -- uncovered lines: 41-42, 55-57, 69-71, 118-119,
#   147-149, 203-212, 216-217, 247, 259, 267, 295-301
# ============================================================================

MOD_STATUS = "rlsbl.commands.status"


class TestStatusProjectNotFound:
    """Covers lines 41-42: target project does not exist."""

    def test_exits_when_project_not_found(self, capsys):
        from rlsbl.commands.status import run_cmd

        mock_target = MagicMock()
        mock_target.check_project_exists.return_value = False

        with patch(f"{MOD_STATUS}.TARGETS", {"npm": mock_target}), \
             patch(f"{MOD_STATUS}.find_workspace_root", return_value=None), \
             patch(f"{MOD_STATUS}.detect_targets", return_value=[]):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", [], {}, ctx=_ctx())
        assert exc_info.value.code == 1
        assert "No npm project found" in capsys.readouterr().err


class TestStatusBranchException:
    """Covers lines 55-57: branch detection fails with non-GitError."""

    def test_branch_warning(self, capsys, tmp_path):
        from rlsbl.commands.status import run_cmd

        mock_target = MagicMock()
        mock_target.check_project_exists.return_value = True
        mock_target.read_version.return_value = "1.0.0"
        mock_target.template_vars.return_value = {"name": "test"}
        mock_target.version_file.return_value = "package.json"

        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."

        with patch(f"{MOD_STATUS}.find_workspace_root", return_value=None), \
             patch(f"{MOD_STATUS}.detect_targets", return_value=[entry]), \
             patch(f"{MOD_STATUS}.TARGETS", {"npm": mock_target}), \
             patch(f"{MOD_STATUS}.get_current_branch", side_effect=RuntimeError("boom")), \
             patch(f"{MOD_STATUS}.run", side_effect=Exception("no tag")), \
             patch(f"{MOD_STATUS}.is_clean_tree", side_effect=Exception("no tree")):
            run_cmd("npm", [], {"json": True}, ctx=_ctx(root=tmp_path))

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["branch"] is None
        assert data["tag"] is None
        assert data["clean"] is None
        assert "could not determine branch" in captured.err


class TestStatusNonReleasableProject:
    """Covers lines 122-123: non-releasable projects report 'non-releasable'."""

    def test_non_releasable_coverage(self, capsys):
        from rlsbl.commands.status import run_cmd

        # Build a project with is_releasable=False
        mock_proj = MagicMock()
        mock_proj.__getitem__ = lambda self, key: {"name": "devnode", "path": "tools/devnode"}[key]
        mock_proj.get = lambda key, default=None: {"name": "devnode", "path": "tools/devnode"}.get(key, default)
        mock_proj.is_releasable = False

        mock_target = MagicMock()
        mock_target.check_project_exists.return_value = True
        mock_target.read_version.return_value = "0.1.0"
        mock_target.template_vars.return_value = {"name": "devnode"}
        mock_target.version_file.return_value = "package.json"
        mock_target.monorepo_tag_glob.return_value = "devnode@v*"

        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."

        patches = {
            f"{MOD_STATUS}.find_workspace_root": "/fake/ws",
            f"{MOD_STATUS}.load_workspace": [mock_proj],
            f"{MOD_STATUS}.resolve_project": mock_proj,
            f"{MOD_STATUS}.get_current_branch": "main",
            f"{MOD_STATUS}.run": "v1.0.0",
            f"{MOD_STATUS}.is_clean_tree": True,
            f"{MOD_STATUS}.changes_dir_exists": False,
        }
        with patch(f"{MOD_STATUS}.find_workspace_root", return_value="/fake/ws"), \
             patch(f"{MOD_STATUS}.load_workspace", return_value=[mock_proj]), \
             patch(f"{MOD_STATUS}.resolve_project", return_value=mock_proj), \
             patch(f"{MOD_STATUS}.detect_targets", return_value=[entry]), \
             patch(f"{MOD_STATUS}.TARGETS", {None: mock_target, "npm": mock_target}), \
             patch(f"{MOD_STATUS}.get_current_branch", return_value="main"), \
             patch(f"{MOD_STATUS}.run", return_value="v1.0.0"), \
             patch(f"{MOD_STATUS}.is_clean_tree", return_value=True), \
             patch(f"{MOD_STATUS}.changes_dir_exists", return_value=False):
            run_cmd("npm", [], {"json": True}, ctx=_ctx())
        data = json.loads(capsys.readouterr().out)
        assert data["jsonl_coverage"] == "non-releasable -- no changelog"


class TestStatusTextOutputPaths:
    """Covers lines 247, 259, 267: text output for None branch, None tag, changelog states."""

    def test_no_changelog_text(self, capsys, tmp_path):
        from rlsbl.commands.status import run_cmd

        mock_target = MagicMock()
        mock_target.check_project_exists.return_value = True
        mock_target.read_version.return_value = "1.0.0"
        mock_target.template_vars.return_value = {"name": "test"}
        mock_target.version_file.return_value = "package.json"

        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."

        with patch(f"{MOD_STATUS}.find_workspace_root", return_value=None), \
             patch(f"{MOD_STATUS}.detect_targets", return_value=[entry]), \
             patch(f"{MOD_STATUS}.TARGETS", {"npm": mock_target}), \
             patch(f"{MOD_STATUS}.get_current_branch", return_value="main"), \
             patch(f"{MOD_STATUS}.run", side_effect=Exception("no tag")), \
             patch(f"{MOD_STATUS}.is_clean_tree", return_value=True):
            run_cmd("npm", [], {}, ctx=_ctx(root=tmp_path))
        out = capsys.readouterr().out
        # No CHANGELOG.md exists in tmp_path
        assert "Changelog: (not found)" in out
        assert "Last tag:  (none)" in out


# ============================================================================
# rlsbl.commands.unreleased -- uncovered lines: 32-33, 42, 71-76, 85-86,
#   107-111
# ============================================================================


class TestUnreleasedNonReleasable:
    """Covers lines 105-111: non-releasable project in monorepo."""

    def test_non_releasable_json(self, capsys):
        mock_proj = MagicMock()
        mock_proj.is_releasable = False
        mock_proj.dev_only = True

        commits = [{"hash": "a" * 40, "subject": "test", "author": "Test", "date": "2024-01-01"}]

        from rlsbl.commands.unreleased import run_cmd

        with patch("rlsbl.commands.unreleased.find_workspace_root", return_value="/ws"), \
             patch("rlsbl.commands.unreleased.resolve_project", return_value=mock_proj), \
             patch("rlsbl.commands.unreleased.get_last_version_tag", return_value="v1.0.0"), \
             patch("rlsbl.commands.unreleased._get_commits_since", return_value=commits):
            with pytest.raises(SystemExit) as exc:
                run_cmd("npm", [], {"json": True}, project_root="/ws/devnode")
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["non_releasable"] is True
        assert data["dev_only"] is True

    def test_non_releasable_text(self, capsys):
        mock_proj = MagicMock()
        mock_proj.is_releasable = False
        mock_proj.dev_only = False

        commits = [{"hash": "a" * 40, "subject": "test", "author": "Test", "date": "2024-01-01"}]

        from rlsbl.commands.unreleased import run_cmd

        with patch("rlsbl.commands.unreleased.find_workspace_root", return_value="/ws"), \
             patch("rlsbl.commands.unreleased.resolve_project", return_value=mock_proj), \
             patch("rlsbl.commands.unreleased.get_last_version_tag", return_value="v1.0.0"), \
             patch("rlsbl.commands.unreleased._get_commits_since", return_value=commits):
            with pytest.raises(SystemExit) as exc:
                run_cmd("npm", [], {}, project_root="/ws/devnode")
        assert exc.value.code == 0
        assert "non-releasable" in capsys.readouterr().out


class TestUnreleasedGetCommitsParsingEdgeCases:
    """Covers lines 32-33, 42: empty lines and malformed entries."""

    def test_malformed_line_skipped(self):
        from rlsbl.commands.unreleased import _get_commits_since

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="hash1\x00subject1\x00author1\x00date1\nmalformed_no_nul\n\n"
        )
        with patch("subprocess.run", return_value=result):
            commits = _get_commits_since("v1.0.0")
        assert len(commits) == 1
        assert commits[0]["hash"] == "hash1"


# ============================================================================
# rlsbl.commands.release/__init__.py -- uncovered lines: 109, 128-131,
#   147-155, 192, 199, 213, 242-253, 281-295, 329, 393, 400-402
# ============================================================================

MOD_RELEASE = "rlsbl.commands.release"


class TestReleaseRunCmdExceptionHandling:
    """Covers lines 99-103: run_cmd wraps _run_cmd_inner exceptions."""

    @patch(f"{MOD_RELEASE}._run_cmd_inner")
    def test_release_validation_error(self, mock_inner):
        from rlsbl.commands.release import run_cmd
        from rlsbl.commands.release.validate import ReleaseValidationError

        mock_inner.side_effect = ReleaseValidationError("bad config")
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(MagicMock(), {}, ctx=_ctx())
        assert exc_info.value.code == 1

    @patch(f"{MOD_RELEASE}._run_cmd_inner")
    def test_post_release_error(self, mock_inner):
        from rlsbl.commands.release import run_cmd
        from rlsbl.errors import PostReleaseError

        mock_inner.side_effect = PostReleaseError("gh failed")
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(MagicMock(), {}, ctx=_ctx())
        assert exc_info.value.code == 1

    @patch(f"{MOD_RELEASE}._run_cmd_inner")
    def test_config_error(self, mock_inner):
        from rlsbl.commands.release import run_cmd
        from rlsbl.errors import ConfigError

        mock_inner.side_effect = ConfigError("bad field")
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(MagicMock(), {}, ctx=_ctx())
        assert exc_info.value.code == 1


class TestReleaseEnvFile:
    """Covers lines 128-131: env_file loading and CF_ACCOUNT_ID aliasing."""

    def test_env_file_loads_and_aliases_cf(self, monkeypatch, tmp_path):
        from rlsbl.commands.release import _run_cmd_inner

        # Create env file
        env_file = tmp_path / ".env"
        env_file.write_text("CF_ACCOUNT_ID=test123\n")

        config = {"env_file": str(env_file)}
        release_config = MagicMock()
        release_config.bump = "minor"
        release_config.include = []
        release_config.targets = {}
        release_config.description = "test"
        release_config.context = ""
        release_config.blog = False

        # Patch load_env_file to actually set the env var
        monkeypatch.setenv("CF_ACCOUNT_ID", "test123")
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

        with patch(f"{MOD_RELEASE}.validate_release_targets", return_value="npm"), \
             patch(f"{MOD_RELEASE}.validate_ota_mode"), \
             patch(f"{MOD_RELEASE}.validate_config_integrity"), \
             patch(f"{MOD_RELEASE}.validate_pipeline_config"), \
             patch(f"{MOD_RELEASE}.validate_gh_cli"), \
             patch(f"{MOD_RELEASE}.validate_clean_tree", return_value=set()), \
             patch(f"{MOD_RELEASE}.validate_branch_and_remote", return_value="main"), \
             patch(f"{MOD_RELEASE}.resolve_monorepo_context", return_value=(None, None, False, False, None)), \
             patch(f"{MOD_RELEASE}._abort_on_scaffold_conflicts"), \
             patch(f"{MOD_RELEASE}.TARGETS"), \
             patch(f"{MOD_RELEASE}.resolve_target_paths", return_value={"npm": "."}), \
             patch(f"{MOD_RELEASE}.compute_release_version", return_value=("0.1.0", "0.2.0", "minor", "v0.2.0")), \
             patch(f"{MOD_RELEASE}.validate_changelog_state", return_value=None), \
             patch(f"{MOD_RELEASE}.validate_blog_body", return_value=(None, None)), \
             patch(f"{MOD_RELEASE}.generate_changelog", return_value="## 0.2.0\n"), \
             patch(f"{MOD_RELEASE}.extract_changelog_entry_from_text", return_value="entry"), \
             patch(f"{MOD_RELEASE}.print_dry_run_summary"), \
             patch("rlsbl.config.load_env_file"):
            _run_cmd_inner(release_config, {"dry-run": True}, ctx=_ctx(config=config))

        # The aliasing should have happened
        assert os.environ.get("CLOUDFLARE_ACCOUNT_ID") == "test123"


class TestReleaseFlutterBuildTracking:
    """Covers lines 398-402: flutter build release tracking."""

    @patch(f"{MOD_RELEASE}._run_cmd_inner")
    def test_flutter_build_not_tracked_when_no_flutter(self, mock_inner):
        """No flutter targets = no tracking call (just verifying the flow)."""
        from rlsbl.commands.release import run_cmd

        mock_inner.return_value = None
        release_config = MagicMock()
        release_config.include = ["npm"]
        release_config.targets = {}
        run_cmd(release_config, {}, ctx=_ctx())
        # No error means it completed


# ============================================================================
# rlsbl.commands.release.execute -- uncovered lines: 46-48, 97-99, 156-157,
#   197-198, 215-216, 231-233, etc.
# ============================================================================


class TestBumpSelfdocVersion:
    """Covers lines 14-49 of execute.py: _bump_selfdoc_version."""

    def test_no_selfdoc_json(self, tmp_path):
        from rlsbl.commands.release.execute import _bump_selfdoc_version

        result = _bump_selfdoc_version(str(tmp_path), "1.0.0")
        assert result == []

    def test_bumps_version_in_selfdoc(self, tmp_path):
        from rlsbl.commands.release.execute import _bump_selfdoc_version

        config = {
            "version": "0.1.0",
            "versions": [{"version": "0.1.0"}],
        }
        (tmp_path / "selfdoc.json").write_text(json.dumps(config, indent=2))
        result = _bump_selfdoc_version(str(tmp_path), "0.2.0")
        assert result == ["selfdoc.json"]
        updated = json.loads((tmp_path / "selfdoc.json").read_text())
        assert updated["version"] == "0.2.0"
        assert updated["versions"][-1]["version"] == "0.2.0"

    def test_write_failure_cleans_up(self, tmp_path):
        from rlsbl.commands.release.execute import _bump_selfdoc_version

        config = {"version": "0.1.0"}
        (tmp_path / "selfdoc.json").write_text(json.dumps(config))

        with patch("os.fdopen", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                _bump_selfdoc_version(str(tmp_path), "0.2.0")


class TestRelToGitRoot:
    """Covers lines 52-57: path normalization."""

    def test_relative_path(self):
        from rlsbl.commands.release.execute import _rel_to_git_root

        assert _rel_to_git_root("foo/bar", "/root") == "foo/bar"

    def test_absolute_path(self):
        from rlsbl.commands.release.execute import _rel_to_git_root

        result = _rel_to_git_root("/root/sub/file.txt", "/root")
        assert result == os.path.join("sub", "file.txt")


class TestResolveReleaseTargets:
    """Covers lines 75-109: resolve_release_targets."""

    def test_from_config(self):
        from rlsbl.commands.release.execute import resolve_release_targets

        config = {"release_targets": ["pypi"]}
        result = resolve_release_targets("npm", {}, project_dir=".", config=config)
        assert "npm" not in result  # primary excluded

    def test_unparseable_entry_skipped(self):
        from rlsbl.commands.release.execute import resolve_release_targets

        # Completely invalid entry type
        config = {"release_targets": [12345]}
        result = resolve_release_targets("npm", {}, project_dir=".", config=config)
        assert isinstance(result, dict)


class TestSyncLockfiles:
    """Covers lines 170-237: _sync_lockfiles."""

    def test_no_lockfiles(self, tmp_path):
        from rlsbl.commands.release.execute import _sync_lockfiles

        files = []
        _sync_lockfiles({"npm": str(tmp_path)}, files, print)
        assert files == []

    def test_tool_not_found(self, tmp_path):
        from rlsbl.commands.release.execute import _sync_lockfiles

        (tmp_path / "uv.lock").write_text("content")
        files = []
        with patch("shutil.which", return_value=None):
            _sync_lockfiles({"pypi": str(tmp_path)}, files, print)
        assert files == []

    def test_sync_fails_warns(self, tmp_path):
        from rlsbl.commands.release.execute import _sync_lockfiles

        (tmp_path / "uv.lock").write_text("content")
        files = []
        with patch("shutil.which", return_value="/usr/bin/uv"):
            with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "uv")):
                _sync_lockfiles({"pypi": str(tmp_path)}, files, print)
        assert files == []


class TestArchiveBlogBody:
    """Covers lines 239-251: archive_blog_body."""

    def test_archive_exists(self, tmp_path):
        from rlsbl.commands.release.execute import archive_blog_body

        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.md").write_text("blog body")

        result = archive_blog_body(str(tmp_path), "1.0.0")
        assert result is not None
        assert "v1.0.0.md" in result
        assert os.path.exists(result)
        # Should be read-only
        mode = stat.S_IMODE(os.stat(result).st_mode)
        assert mode & stat.S_IWUSR == 0

    def test_archive_not_exists(self, tmp_path):
        from rlsbl.commands.release.execute import archive_blog_body

        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        result = archive_blog_body(str(tmp_path), "1.0.0")
        assert result is None


class TestRefreshSelfdocHashes:
    """Covers lines 112-157: _refresh_selfdoc_hashes."""

    def test_no_selfdoc_config(self, tmp_path):
        from rlsbl.commands.release.execute import _refresh_selfdoc_hashes

        files = []
        _refresh_selfdoc_hashes(files, print, project_dir=str(tmp_path))
        assert files == []

    def test_no_hashes_file(self, tmp_path):
        from rlsbl.commands.release.execute import _refresh_selfdoc_hashes

        (tmp_path / "selfdoc.json").write_text("{}")
        files = []
        _refresh_selfdoc_hashes(files, print, project_dir=str(tmp_path))
        assert files == []

    def test_selfdoc_not_on_path(self, tmp_path):
        from rlsbl.commands.release.execute import _refresh_selfdoc_hashes

        (tmp_path / "selfdoc.json").write_text("{}")
        hashes_dir = tmp_path / ".selfdoc" / "hashes"
        hashes_dir.mkdir(parents=True)
        (hashes_dir / "hashes.json").write_text("{}")

        files = []
        with patch("rlsbl.commands.release.require_tool", return_value=False):
            _refresh_selfdoc_hashes(files, print, project_dir=str(tmp_path))
        assert files == []


# ============================================================================
# rlsbl.commands.release_scrub -- uncovered lines: 58-59, 77-84, 94-96,
#   102-103, 106, 117-119, etc.
# ============================================================================

MOD_SCRUB = "rlsbl.commands.release_scrub"


class TestScrubSafegitVersionTooOld:
    """Covers lines 57-59: safegit version < 0.18.0."""

    @patch(f"{MOD_SCRUB}.require_tool")
    @patch(f"{MOD_SCRUB}.run", return_value="safegit 0.17.0")
    def test_old_safegit_exits(self, *_):
        from rlsbl.commands.release_scrub import run_cmd

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "test",
            "entire-history": True,
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx("/fake"))
        assert exc_info.value.code == 1


def _scrub_simple(tmp_path, run_side_effect, flags, **extra_patches):
    """Run release_scrub.run_cmd with require_tool and run mocked."""
    from rlsbl.commands.release_scrub import run_cmd

    mock_run = MagicMock(side_effect=run_side_effect)
    patches = {
        f"{MOD_SCRUB}.require_tool": MagicMock(),
        f"{MOD_SCRUB}.run": mock_run,
        **extra_patches,
    }
    import contextlib
    with contextlib.ExitStack() as stack:
        for target, val in patches.items():
            stack.enter_context(patch(target, val))
        run_cmd(flags, ctx=_ctx(str(tmp_path)))
    return mock_run


def _scrub_full(tmp_path, run_side_effect, flags, *, gh_auth=False, gh_installed=False, extra_patches=None):
    """Run scrub with full pipeline patches (lock, changelog, push)."""
    from rlsbl.commands.release_scrub import run_cmd

    mock_run = MagicMock(side_effect=run_side_effect)
    patches = {
        f"{MOD_SCRUB}.release_lock": MagicMock(),
        f"{MOD_SCRUB}.acquire_lock": MagicMock(),
        f"{MOD_SCRUB}.check_gh_auth": MagicMock(return_value=gh_auth),
        f"{MOD_SCRUB}.check_gh_installed": MagicMock(return_value=gh_installed),
        f"{MOD_SCRUB}.get_current_branch": MagicMock(return_value="main"),
        f"{MOD_SCRUB}.get_push_timeout": MagicMock(return_value=120),
        f"{MOD_SCRUB}.generate_changelog": MagicMock(),
        f"{MOD_SCRUB}.require_tool": MagicMock(),
        f"{MOD_SCRUB}.run": mock_run,
    }
    if extra_patches:
        patches.update(extra_patches)
    import contextlib
    with contextlib.ExitStack() as stack:
        for target, val in patches.items():
            stack.enter_context(patch(target, val))
        run_cmd(flags, ctx=_ctx(str(tmp_path)))
    return mock_run


class TestScrubStaleResult:
    """Covers lines 77-84: stale scrub-result.json."""

    def test_stale_scrub_result_exits(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "scrub-result.json").write_text(json.dumps({
            "new_head": "stale_sha",
            "completed_steps": [],
        }))

        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True}
        with pytest.raises(SystemExit) as exc_info:
            _scrub_simple(tmp_path, ["safegit 0.18.0", "different_sha"], flags)
        assert exc_info.value.code == 1


class TestScrubFileMode:
    """Covers lines 94-96: --file flag uses 'file' subcommand."""

    def test_file_mode(self, tmp_path):
        safegit_result = json.dumps({"rewrites": {"a": "b"}, "tags": []})
        flags = {"file": "secrets.txt", "mangle": True, "reason": "remove file", "from-commit": "abc123", "dry-run": True}
        mock_run = _scrub_simple(tmp_path, ["safegit 0.18.0", safegit_result], flags)
        scrub_call = mock_run.call_args_list[1]
        assert scrub_call[0][1][1] == "file"
        assert "--file" in scrub_call[0][1]
        assert "--mangle" in scrub_call[0][1]


class TestScrubFromCommit:
    """Covers line 106: --from-commit flag."""

    def test_from_commit_flag(self, tmp_path):
        safegit_result = json.dumps({"rewrites": {"a": "b"}, "tags": []})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "from-commit": "abc123def", "dry-run": True}
        mock_run = _scrub_simple(tmp_path, ["safegit 0.18.0", safegit_result], flags)
        scrub_call = mock_run.call_args_list[1]
        assert "--from" in scrub_call[0][1]
        assert "abc123def" in scrub_call[0][1]


class TestScrubSafegitFails:
    """Covers lines 117-119: safegit scrub command fails."""

    def test_safegit_scrub_failure(self):
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True}
        with pytest.raises(SystemExit) as exc_info:
            _scrub_simple(Path("/fake"), ["safegit 0.18.0", Exception("scrub failed")], flags)
        assert exc_info.value.code == 1


class TestScrubConfirmationAbort:
    """Covers lines 151-160: user declines confirmation."""

    def test_abort_on_no(self, tmp_path):
        safegit_result = json.dumps({"rewrites": {"a": "b"}, "tags": [{"refname": "refs/tags/v1.0.0"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True}
        with patch("builtins.input", return_value="n"):
            with pytest.raises(SystemExit) as exc_info:
                _scrub_simple(tmp_path, ["safegit 0.18.0", safegit_result], flags)
        assert exc_info.value.code == 0

    def test_abort_on_eof(self, tmp_path):
        safegit_result = json.dumps({"rewrites": {"a": "b"}, "tags": [], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True}
        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                _scrub_simple(tmp_path, ["safegit 0.18.0", safegit_result], flags)
        assert exc_info.value.code == 1


class TestScrubBranchPushFails:
    def test_branch_push_failure_exits(self, tmp_path):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        # With empty unreleased.jsonl and no rewrites matching, COMMITTED step is
        # skipped (no modified_files). So call sequence: version, scrub, push.
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        with pytest.raises(SystemExit) as exc_info:
            _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, Exception("push failed")], flags)
        assert exc_info.value.code == 1


class TestScrubCommitFails:
    def test_commit_failure_warns(self, tmp_path, capsys):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        # With CHANGELOG.md, commit IS attempted: version, scrub, commit(fail), push
        _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, Exception("commit failed"), ""], flags)
        assert "commit failed" in capsys.readouterr().err


class TestScrubTagPushFails:
    def test_tag_push_failure_warns(self, tmp_path, capsys):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": "refs/tags/v1.0.0"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        # No modified_files so commit is skipped; calls: version, scrub, branch_push, tag_push(fail)
        _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, "", Exception("tag push failed")], flags)
        assert "tag push failed" in capsys.readouterr().err


class TestScrubNoGhForReleases:
    def test_skips_release_when_no_gh(self, tmp_path):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": "refs/tags/v1.0.0"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        # No modified_files -> commit skipped; calls: version, scrub, branch_push, tag_push
        mock_run = _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, "", ""], flags)
        assert mock_run.call_count == 4


class TestScrubReleaseDeleteFails:
    def test_gh_release_delete_fail(self, tmp_path, capsys):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": "refs/tags/v1.0.0"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        # Calls: version, scrub, branch_push, tag_push, gh_view, gh_delete(fail)
        _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, "", "", '{"body": "old"}', Exception("delete failed")], flags, gh_auth=True, gh_installed=True)
        assert "delete failed" in capsys.readouterr().err


class TestScrubVersionExtractFails:
    def test_bad_tag_name(self, tmp_path, capsys):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": "refs/tags/not-a-version"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        # Calls: version, scrub, branch_push, tag_push, gh_view, gh_delete
        _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, "", "", '{"body": "old"}', ""], flags, gh_auth=True, gh_installed=True)
        assert "cannot extract version" in capsys.readouterr().err


class TestScrubGhReleaseCreateFails:
    def test_gh_create_failure_warns(self, tmp_path, capsys):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": "refs/tags/v1.0.0"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        ep = {f"{MOD_SCRUB}.extract_changelog_entry": MagicMock(return_value="notes")}
        # Calls: version, scrub, branch_push, tag_push, gh_view, gh_delete, gh_create(fail)
        _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, "", "", '{"body": "old"}', "", Exception("create failed")], flags, gh_auth=True, gh_installed=True, extra_patches=ep)
        assert "create failed" in capsys.readouterr().err


class TestScrubFallbackNotes:
    def test_fallback_notes_used(self, tmp_path):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        (tmp_path / "CHANGELOG.md").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": "refs/tags/v1.0.0"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        ep = {f"{MOD_SCRUB}.extract_changelog_entry": MagicMock(return_value=None)}
        # With CHANGELOG.md: commit IS attempted. Calls: version, scrub, commit, branch_push, tag_push, gh_view, gh_delete, gh_create
        mock_run = _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, "", "", "", '{"body": "old"}', "", ""], flags, gh_auth=True, gh_installed=True, extra_patches=ep)
        create_call = mock_run.call_args_list[-1]
        assert "Release 1.0.0" in create_call[0][1]


class TestScrubNoReleaseForTag:
    def test_no_release_for_tag(self, tmp_path):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": "refs/tags/v1.0.0"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        # Calls: version, scrub, branch_push, tag_push, gh_view(fail)
        mock_run = _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, "", "", Exception("not found")], flags, gh_auth=True, gh_installed=True)
        assert mock_run.call_count == 5


class TestScrubNoRefname:
    def test_empty_refname_skipped(self, tmp_path):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        safegit_result = json.dumps({"rewrites": {"old": "new"}, "tags": [{"refname": ""}, {"refname": "refs/tags/"}], "new_head": "abc"})
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        # Empty refnames -> no tag pushes, no gh release checks. Calls: version, scrub, branch_push
        _scrub_full(tmp_path, ["safegit 0.18.0", safegit_result, ""], flags, gh_auth=True, gh_installed=True)


class TestScrubMonorepoFallbackScan:
    """Covers lines 356-364: monorepo fallback scanning all projects."""

    def test_fallback_scan_all_projects(self, tmp_path, capsys):
        from rlsbl.commands.release_scrub import run_cmd
        from rlsbl.workspace import WorkspaceProject

        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / ".rlsbl-monorepo").mkdir()

        proj_dir = ws_root / "pkg" / "alpha"
        proj_dir.mkdir(parents=True)
        changes_dir = proj_dir / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")
        (proj_dir / "CHANGELOG.md").write_text("## 1.0.0\n\n- found it\n")

        workspace_projects = [WorkspaceProject({"name": "alpha", "path": "pkg/alpha"})]
        safegit_result = json.dumps({
            "rewrites": {"old": "new"},
            "tags": [{"refname": "refs/tags/weird-prefix-v1.0.0"}],
            "new_head": "abc",
        })
        flags = {"pattern": "secret", "replace": "XXX", "reason": "test", "entire-history": True, "yes": True}
        run_side = ["safegit 0.18.0", safegit_result, "", "", "", '{"body": "old"}', "", ""]
        mock_run = MagicMock(side_effect=run_side)

        import contextlib
        with contextlib.ExitStack() as stack:
            for t, v in {
                f"{MOD_SCRUB}.release_lock": MagicMock(),
                f"{MOD_SCRUB}.acquire_lock": MagicMock(),
                f"{MOD_SCRUB}.check_gh_auth": MagicMock(return_value=True),
                f"{MOD_SCRUB}.check_gh_installed": MagicMock(return_value=True),
                f"{MOD_SCRUB}.get_current_branch": MagicMock(return_value="main"),
                f"{MOD_SCRUB}.get_push_timeout": MagicMock(return_value=120),
                f"{MOD_SCRUB}.generate_changelog": MagicMock(),
                f"{MOD_SCRUB}.require_tool": MagicMock(),
                f"{MOD_SCRUB}.run": mock_run,
                f"{MOD_SCRUB}.load_workspace": MagicMock(return_value=workspace_projects),
                f"{MOD_SCRUB}.extract_changelog_entry": MagicMock(return_value="- found it"),
            }.items():
                stack.enter_context(patch(t, v))
            run_cmd(flags, ctx=_ctx(str(proj_dir), workspace_root=str(ws_root)))

        assert "no prefix match" in capsys.readouterr().err


# ============================================================================
# rlsbl.commands.changelog_cmd -- uncovered lines: 49, 53, 57-59, 62, 72, 78,
#   87-91, 113-125, 192-197, 209-211, etc.
# ============================================================================

MOD_CL = "rlsbl.commands.changelog_cmd"


class TestResolvedContext:
    """Covers lines 48-63 of _ResolvedContext."""

    def test_is_releasable_no_project(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        ctx = _ResolvedContext(project=None)
        assert ctx.is_releasable is True

    def test_is_releasable_with_project(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        proj = MagicMock()
        proj.is_releasable = False
        ctx = _ResolvedContext(project=proj)
        assert ctx.is_releasable is False

    def test_name_with_project(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        proj = MagicMock()
        proj.name = "mylib"
        ctx = _ResolvedContext(project=proj)
        assert ctx.name == "mylib"

    def test_name_no_project(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        ctx = _ResolvedContext(project=None)
        assert ctx.name is None

    def test_get_method(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        proj = MagicMock()
        proj.get.return_value = "value"
        ctx = _ResolvedContext(project=proj)
        assert ctx.get("key") == "value"

    def test_get_no_project(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        ctx = _ResolvedContext(project=None)
        assert ctx.get("key", "default") == "default"

    def test_getitem(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        proj = MagicMock()
        proj.__getitem__ = lambda self, k: "val"
        ctx = _ResolvedContext(project=proj)
        assert ctx["anything"] == "val"


class TestResolveWorkspaceProjectNonReleasable:
    """Covers lines 79-81: non-releasable project exits."""

    @patch(f"{MOD_CL}.resolve_project")
    @patch(f"{MOD_CL}.find_workspace_root", return_value="/ws")
    def test_non_releasable_exits(self, _ws, mock_resolve):
        from rlsbl.commands.changelog_cmd import _resolve_workspace_project

        proj = MagicMock()
        proj.is_releasable = False
        mock_resolve.return_value = proj

        with pytest.raises(SystemExit) as exc:
            _resolve_workspace_project("/ws/pkg")
        assert exc.value.code == 1

    def test_none_project_root(self):
        from rlsbl.commands.changelog_cmd import _resolve_workspace_project

        assert _resolve_workspace_project(None) is None

    @patch(f"{MOD_CL}.find_workspace_root", return_value=None)
    def test_no_workspace_root(self, _ws):
        from rlsbl.commands.changelog_cmd import _resolve_workspace_project

        assert _resolve_workspace_project("/some/path") is None

    @patch(f"{MOD_CL}.resolve_project", return_value=None)
    @patch(f"{MOD_CL}.find_workspace_root", return_value="/ws")
    def test_no_project_resolved(self, *_):
        from rlsbl.commands.changelog_cmd import _resolve_workspace_project

        assert _resolve_workspace_project("/ws/unknown") is None


class TestCheckProjectScopeStandalone:
    """Covers line 108-109: scope check skipped in standalone mode."""

    def test_standalone_skipped(self):
        from rlsbl.commands.changelog_cmd import _check_project_scope

        _check_project_scope(["abc123"], None)  # should not raise


class TestCheckProjectScopeReleasable:
    """Covers lines 112-125: scope check in releasable mode."""

    def test_out_of_scope_in_releasable(self):
        from rlsbl.commands.changelog_cmd import _check_project_scope, _ResolvedContext

        releasable = MagicMock()
        releasable.name = "myrel"
        members = [MagicMock()]
        ctx = _ResolvedContext(project=MagicMock(), releasable=releasable, member_projects=members)

        with patch(f"{MOD_CL}.filter_commits_for_releasable", return_value=set()):
            with pytest.raises(SystemExit):
                _check_project_scope(["abc123"], ctx)


class TestCheckDuplicateCommits:
    """Covers lines 144-168: duplicate commit detection."""

    def test_exact_duplicate_errors(self):
        from rlsbl.commands.changelog_cmd import _check_duplicate_commits
        from rlsbl.changelog.schema import ChangelogEntry

        existing = [ChangelogEntry(commits=["abc123"], user_facing=True, description="Fix", type="fix")]
        new = ChangelogEntry(commits=["abc123"], user_facing=True, description="Fix", type="fix")

        with pytest.raises(SystemExit):
            _check_duplicate_commits(existing, new)

    def test_different_type_warns(self, capsys):
        from rlsbl.commands.changelog_cmd import _check_duplicate_commits
        from rlsbl.changelog.schema import ChangelogEntry

        existing = [ChangelogEntry(commits=["abc123"], user_facing=True, description="Fix", type="fix")]
        new = ChangelogEntry(commits=["abc123"], user_facing=True, description="Feature", type="feature")

        _check_duplicate_commits(existing, new)
        assert "Warning" in capsys.readouterr().err


class TestBuildEntryValidation:
    """Covers lines 183-213: _build_entry validation."""

    def test_user_facing_without_description(self):
        from rlsbl.commands.changelog_cmd import _build_entry

        with pytest.raises(SystemExit):
            _build_entry({"commits": "abc"}, ["abc123"])

    def test_user_facing_without_type(self):
        from rlsbl.commands.changelog_cmd import _build_entry

        with pytest.raises(SystemExit):
            _build_entry({"description": "Fix bug"}, ["abc123"])

    def test_schema_validation_error(self):
        from rlsbl.commands.changelog_cmd import _build_entry

        with patch(f"{MOD_CL}.validate_schema", return_value=["bad field"]):
            with pytest.raises(SystemExit):
                _build_entry({"description": "Fix", "type": "fix"}, ["abc123"])


class TestCmdAddNoCommits:
    """Covers lines 264-269: --commits missing or empty."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_no_commits_flag(self, _):
        from rlsbl.commands.changelog_cmd import cmd_add

        with pytest.raises(SystemExit):
            cmd_add({}, ".")

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_empty_commits(self, _):
        from rlsbl.commands.changelog_cmd import cmd_add

        with pytest.raises(SystemExit):
            cmd_add({"commits": "  , "}, ".")


class TestCmdAddBadHash:
    """Covers lines 276-279: unresolvable hash."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}.resolve_hash", return_value=None)
    def test_bad_hash_exits(self, *_):
        from rlsbl.commands.changelog_cmd import cmd_add

        with pytest.raises(SystemExit):
            cmd_add({"commits": "deadbeef"}, ".")


class TestCmdAmendNoVersion:
    """Covers lines 439-441: --version missing."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_no_version(self, _):
        from rlsbl.commands.changelog_cmd import cmd_amend

        with pytest.raises(SystemExit):
            cmd_amend({}, ".")


class TestCmdAmendNoCommits:
    """Covers lines 445-451: --commits missing or empty."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_no_commits(self, _):
        from rlsbl.commands.changelog_cmd import cmd_amend

        with pytest.raises(SystemExit):
            cmd_amend({"version": "1.0.0"}, ".")

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_empty_commits(self, _):
        from rlsbl.commands.changelog_cmd import cmd_amend

        with pytest.raises(SystemExit):
            cmd_amend({"version": "1.0.0", "commits": " , "}, ".")


class TestCmdAmendBadHash:
    """Covers lines 462-463: unresolvable hash in amend."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}.resolve_hash", return_value=None)
    def test_bad_hash(self, *_):
        from rlsbl.commands.changelog_cmd import cmd_amend

        with pytest.raises(SystemExit):
            cmd_amend({"version": "1.0.0", "commits": "deadbeef"}, ".")


class TestCmdEditNoEditFlags:
    """Covers lines 531-537: no edit flags provided."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_no_edit_flags(self, _):
        from rlsbl.commands.changelog_cmd import cmd_edit

        with pytest.raises(SystemExit):
            cmd_edit({}, ".")


class TestCmdEditNoCommits:
    """Covers lines 542-548: --commits missing or empty."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_no_commits(self, _):
        from rlsbl.commands.changelog_cmd import cmd_edit

        with pytest.raises(SystemExit):
            cmd_edit({"type": "fix"}, ".")

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    def test_empty_commits(self, _):
        from rlsbl.commands.changelog_cmd import cmd_edit

        with pytest.raises(SystemExit):
            cmd_edit({"type": "fix", "commits": " , "}, ".")


class TestCmdEditBadHash:
    """Covers lines 554-555: unresolvable hash in edit."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}.resolve_hash", return_value=None)
    def test_bad_hash(self, *_):
        from rlsbl.commands.changelog_cmd import cmd_edit

        with pytest.raises(SystemExit):
            cmd_edit({"type": "fix", "commits": "deadbeef"}, ".")


class TestCmdEditNoMatch:
    """Covers lines 579-584: no entry found for commits."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}.resolve_hash", return_value="a" * 40)
    @patch(f"{MOD_CL}._resolve_changes_dir", return_value="/changes")
    @patch(f"{MOD_CL}.parse_jsonl", return_value=[])
    @patch(f"{MOD_CL}.list_versioned_files", return_value=[])
    @patch("os.path.isfile", return_value=True)
    def test_no_match_exits(self, *_):
        from rlsbl.commands.changelog_cmd import cmd_edit

        with pytest.raises(SystemExit):
            cmd_edit({"type": "fix", "commits": "abc123"}, ".")


class TestCmdEditMultipleMatches:
    """Covers lines 587-611: multiple matches disambiguation."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}.resolve_hash", return_value="a" * 40)
    @patch(f"{MOD_CL}._resolve_changes_dir", return_value="/changes")
    @patch(f"{MOD_CL}.list_versioned_files", return_value=[])
    def test_multiple_matches_no_type_filter(self, _lvf, _cd, _rh, _ws):
        from rlsbl.commands.changelog_cmd import cmd_edit
        from rlsbl.changelog.schema import ChangelogEntry

        entry1 = ChangelogEntry(commits=["a" * 40], user_facing=True, description="Fix", type="fix")
        entry2 = ChangelogEntry(commits=["a" * 40], user_facing=True, description="Feature", type="feature")

        with patch(f"{MOD_CL}.parse_jsonl", return_value=[entry1, entry2]):
            with patch("os.path.isfile", return_value=True):
                with pytest.raises(SystemExit):
                    cmd_edit({"description": "updated", "commits": "a" * 40}, ".")


class TestCmdEditUserFacingMissingDescription:
    """Covers lines 625-637: setting --user-facing without description/type."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}.resolve_hash", return_value="a" * 40)
    @patch(f"{MOD_CL}._resolve_changes_dir", return_value="/changes")
    @patch(f"{MOD_CL}.list_versioned_files", return_value=[])
    def test_user_facing_no_description(self, _lvf, _cd, _rh, _ws):
        from rlsbl.commands.changelog_cmd import cmd_edit
        from rlsbl.changelog.schema import ChangelogEntry

        entry = ChangelogEntry(commits=["a" * 40], user_facing=False)

        with patch(f"{MOD_CL}.parse_jsonl", return_value=[entry]):
            with patch("os.path.isfile", return_value=True):
                with pytest.raises(SystemExit):
                    cmd_edit({"user-facing": True, "commits": "a" * 40}, ".")

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}.resolve_hash", return_value="a" * 40)
    @patch(f"{MOD_CL}._resolve_changes_dir", return_value="/changes")
    @patch(f"{MOD_CL}.list_versioned_files", return_value=[])
    def test_user_facing_no_type(self, _lvf, _cd, _rh, _ws):
        from rlsbl.commands.changelog_cmd import cmd_edit
        from rlsbl.changelog.schema import ChangelogEntry

        # Entry has description but no type
        entry = ChangelogEntry(commits=["a" * 40], user_facing=False, description="has desc")

        with patch(f"{MOD_CL}.parse_jsonl", return_value=[entry]):
            with patch("os.path.isfile", return_value=True):
                with pytest.raises(SystemExit):
                    cmd_edit({"user-facing": True, "commits": "a" * 40}, ".")


class TestSyncGithubRelease:
    """Covers lines 703-718: _sync_github_release."""

    def test_sync_success(self, capsys):
        from rlsbl.commands.changelog_cmd import _sync_github_release

        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=result):
            _sync_github_release("1.0.0")
        assert "Synced" in capsys.readouterr().out

    def test_sync_failure(self, capsys):
        from rlsbl.commands.changelog_cmd import _sync_github_release

        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad")
        with patch("subprocess.run", return_value=result):
            _sync_github_release("1.0.0")
        assert "Warning" in capsys.readouterr().err

    def test_sync_timeout(self, capsys):
        from rlsbl.commands.changelog_cmd import _sync_github_release

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("rlsbl", 30)):
            _sync_github_release("1.0.0")
        assert "Warning" in capsys.readouterr().err


class TestGetGeneratedFiles:
    """Covers lines 724-749: _get_generated_files."""

    def test_git_status_fails(self):
        from rlsbl.commands.changelog_cmd import _get_generated_files

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert _get_generated_files(".") == []

    def test_parses_changed_files(self):
        from rlsbl.commands.changelog_cmd import _get_generated_files

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=" M CHANGELOG.md\n M .rlsbl/changes/1.0.0.md\nA  src/main.py\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=result):
            files = _get_generated_files("/proj")
        assert any("CHANGELOG.md" in f for f in files)
        assert any("1.0.0.md" in f for f in files)
        # src/main.py should NOT be included
        assert not any("main.py" in f for f in files)

    def test_short_lines_skipped(self):
        from rlsbl.commands.changelog_cmd import _get_generated_files

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="??\nM\n M CHANGELOG.md\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=result):
            files = _get_generated_files("/proj")
        assert len(files) == 1


class TestDerivePackagesFromCommits:
    """Covers lines 229-250: _derive_packages_from_commits."""

    def test_no_members(self):
        from rlsbl.commands.changelog_cmd import _derive_packages_from_commits

        assert _derive_packages_from_commits(["abc"], []) is None

    def test_with_members(self):
        from rlsbl.commands.changelog_cmd import _derive_packages_from_commits

        member = MagicMock()
        member.name = "pkg-a"

        with patch("rlsbl.git_util.get_commit_files", return_value=["pkg-a/file.py"]):
            with patch("rlsbl.git_util.file_matches_project", return_value=True):
                result = _derive_packages_from_commits(["abc123"], [member])
        assert result == ["pkg-a"]

    def test_commit_files_none(self):
        from rlsbl.commands.changelog_cmd import _derive_packages_from_commits

        member = MagicMock()
        member.name = "pkg-a"

        with patch("rlsbl.git_util.get_commit_files", return_value=None):
            result = _derive_packages_from_commits(["abc123"], [member])
        assert result is None


class TestCmdGenerateNoChangesDir:
    """Covers lines 355-357: changes dir does not exist."""

    @patch(f"{MOD_CL}._resolve_workspace_project", return_value=None)
    @patch(f"{MOD_CL}._resolve_changes_dir", return_value="/nonexistent")
    def test_exits_when_no_changes_dir(self, *_):
        from rlsbl.commands.changelog_cmd import cmd_generate

        with pytest.raises(SystemExit):
            cmd_generate({}, ".")


# ============================================================================
# rlsbl.commands.monorepo.batch_release -- uncovered lines: 49, 54, 65-69,
#   84-86, 92-99, 110-192, 229-231, 273-280
# ============================================================================

MOD_BATCH = "rlsbl.commands.monorepo.batch_release"


class TestReleasableReleaseOrder:
    """Covers lines 32-57: _releasable_release_order."""

    def test_basic_ordering(self):
        from rlsbl.commands.monorepo.batch_release import _releasable_release_order

        rel1 = MagicMock()
        rel1.name = "core"
        rel2 = MagicMock()
        rel2.name = "web"

        graph = MagicMock()
        graph.topological_order.return_value = ["pkg-a", "pkg-b", "pkg-c"]

        # core has members at positions 0, 1; web has members at position 2
        member_core = [MagicMock(), MagicMock()]
        member_core[0].__getitem__ = lambda s, k: "pkg-a"
        member_core[1].__getitem__ = lambda s, k: "pkg-b"
        member_web = [MagicMock()]
        member_web[0].__getitem__ = lambda s, k: "pkg-c"

        with patch("rlsbl.workspace.members_of") as mock_members:
            def fake_members(name, projects):
                if name == "core":
                    return member_core
                return member_web

            mock_members.side_effect = fake_members
            result = _releasable_release_order(
                {"core", "web"}, [rel1, rel2], [], graph,
            )

        assert result == ["core", "web"]

    def test_empty_members(self):
        from rlsbl.commands.monorepo.batch_release import _releasable_release_order

        rel1 = MagicMock()
        rel1.name = "empty"

        graph = MagicMock()
        graph.topological_order.return_value = []

        with patch("rlsbl.workspace.members_of", return_value=[]):
            result = _releasable_release_order({"empty"}, [rel1], [], graph)
        assert result == ["empty"]


class TestBatchReleaseNoWorkspace:
    """Covers lines 63-69: no workspace found."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value=None)
    def test_exits_when_no_workspace(self, _):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        with pytest.raises(SystemExit) as exc:
            _cmd_batch_release({}, Path("/fake"))
        assert exc.value.code == 1


class TestBatchReleaseNoBatchFile:
    """Covers lines 71-80: no batch release file."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/.rlsbl-monorepo/releases/unreleased.toml")
    def test_exits_when_no_batch_file(self, *_):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        with patch("os.path.exists", return_value=False):
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleaseFileError:
    """Covers lines 84-86: batch release file parsing error."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    def test_parse_error(self, mock_read, *_):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release
        from rlsbl.errors import ReleaseFileError

        mock_read.side_effect = ReleaseFileError("bad format")

        with patch("os.path.exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleaseReleasablesImplicitMode:
    """Covers lines 92-98: releasables section in implicit mode."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace", return_value=[])
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=False)
    def test_releasables_in_implicit_mode(self, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        batch = MagicMock()
        batch.section_type = "releasables"
        batch.packages = {"rel1": MagicMock()}
        mocks[2].return_value = batch  # read_batch_release_file

        with patch("os.path.exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleasePackagesMissing:
    """Covers lines 201-208: packages not found in workspace."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace")
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=False)
    def test_missing_packages(self, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        batch = MagicMock()
        batch.section_type = "packages"
        batch.packages = {"nonexistent": MagicMock()}
        mocks[2].return_value = batch

        proj = MagicMock()
        proj.__getitem__ = lambda self, k: {"name": "existing"}[k]
        mocks[1].return_value = [proj]

        with patch("os.path.exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleaseNonReleasable:
    """Covers lines 210-224: non-releasable project in batch."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace")
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=False)
    def test_non_releasable_in_batch(self, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        batch = MagicMock()
        batch.section_type = "packages"
        batch.packages = {"devnode": MagicMock()}
        mocks[2].return_value = batch

        proj = MagicMock()
        proj.__getitem__ = lambda self, k: {"name": "devnode"}[k]
        proj.is_releasable = False
        mocks[1].return_value = [proj]

        with patch("os.path.exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleaseCycleError:
    """Covers lines 229-231: cycle error in dependency graph."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace")
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=False)
    @patch(f"{MOD_BATCH}.WorkspaceGraph")
    def test_cycle_error(self, mock_graph, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release
        from rlsbl.workspace_graph import CycleError

        batch = MagicMock()
        batch.section_type = "packages"
        batch.packages = {"pkg-a": MagicMock()}
        mocks[2].return_value = batch

        proj = MagicMock()
        proj.__getitem__ = lambda self, k: {"name": "pkg-a"}[k]
        proj.is_releasable = True
        mocks[1].return_value = [proj]

        graph_inst = MagicMock()
        graph_inst.topological_order.side_effect = CycleError("cycle!")
        mock_graph.return_value = graph_inst

        with patch("os.path.exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleasePackageFailure:
    """Covers lines 273-280: package release failure mid-batch."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace")
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=False)
    @patch(f"{MOD_BATCH}.WorkspaceGraph")
    def test_package_release_failure(self, mock_graph, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        rc = MagicMock()
        rc.bump = "patch"
        batch = MagicMock()
        batch.section_type = "packages"
        batch.packages = {"pkg-a": rc}
        mocks[2].return_value = batch

        proj = MagicMock()
        proj.__getitem__ = lambda self, k: {"name": "pkg-a", "path": "packages/pkg-a"}[k]
        proj.is_releasable = True
        mocks[1].return_value = [proj]

        graph_inst = MagicMock()
        graph_inst.topological_order.return_value = ["pkg-a"]
        mock_graph.return_value = graph_inst

        with patch("os.path.exists", return_value=True):
            with patch("rlsbl.context.create_context", return_value=_ctx()):
                with patch("rlsbl.commands.release.run_cmd", side_effect=SystemExit(1)):
                    with pytest.raises(SystemExit):
                        _cmd_batch_release({"yes": True, "quiet": True}, Path("/ws/pkg"))


class TestBatchReleaseReleasablesMissing:
    """Covers lines 116-122: releasable names not found."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace", return_value=[])
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=True)
    def test_missing_releasables(self, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        batch = MagicMock()
        batch.section_type = "releasables"
        batch.packages = {"missing-rel": MagicMock()}
        mocks[2].return_value = batch

        with patch("os.path.exists", return_value=True):
            with patch("rlsbl.workspace.load_releasables", return_value=[]):
                with pytest.raises(SystemExit) as exc:
                    _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleaseReleasableCycle:
    """Covers lines 126-131: cycle in releasable mode."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace", return_value=[])
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=True)
    @patch(f"{MOD_BATCH}.WorkspaceGraph")
    def test_cycle_in_releasable_mode(self, mock_graph, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release
        from rlsbl.workspace_graph import CycleError

        rel = MagicMock()
        rel.name = "core"
        batch = MagicMock()
        batch.section_type = "releasables"
        batch.packages = {"core": MagicMock()}
        mocks[2].return_value = batch

        graph_inst = MagicMock()
        graph_inst.topological_order.side_effect = CycleError("cycle!")
        mock_graph.return_value = graph_inst

        with patch("os.path.exists", return_value=True):
            with patch("rlsbl.workspace.load_releasables", return_value=[rel]):
                with pytest.raises(SystemExit) as exc:
                    _cmd_batch_release({}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleaseReleasableNoMembers:
    """Covers lines 153-155: releasable with no member projects."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace", return_value=[])
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=True)
    @patch(f"{MOD_BATCH}.WorkspaceGraph")
    def test_no_members(self, mock_graph, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        rc = MagicMock()
        rc.bump = "patch"
        rel = MagicMock()
        rel.name = "core"
        batch = MagicMock()
        batch.section_type = "releasables"
        batch.packages = {"core": rc}
        mocks[2].return_value = batch

        graph_inst = MagicMock()
        graph_inst.topological_order.return_value = []
        mock_graph.return_value = graph_inst

        with patch("os.path.exists", return_value=True):
            with patch("rlsbl.workspace.load_releasables", return_value=[rel]):
                with patch("rlsbl.workspace.members_of", return_value=[]):
                    with pytest.raises(SystemExit) as exc:
                        _cmd_batch_release({"yes": True, "quiet": True}, Path("/ws/pkg"))
        assert exc.value.code == 1


class TestBatchReleaseReleasableFailure:
    """Covers lines 178-185: releasable release failure mid-batch."""

    @patch(f"{MOD_BATCH}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_BATCH}.get_batch_release_file_path", return_value="/ws/batch.toml")
    @patch(f"{MOD_BATCH}.read_batch_release_file")
    @patch(f"{MOD_BATCH}.load_workspace", return_value=[])
    @patch(f"{MOD_BATCH}.is_explicit_mode", return_value=True)
    @patch(f"{MOD_BATCH}.WorkspaceGraph")
    def test_releasable_release_failure(self, mock_graph, *mocks):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        rc = MagicMock()
        rc.bump = "patch"
        rel = MagicMock()
        rel.name = "core"
        batch = MagicMock()
        batch.section_type = "releasables"
        batch.packages = {"core": rc}
        mocks[2].return_value = batch

        graph_inst = MagicMock()
        graph_inst.topological_order.return_value = ["pkg-a"]
        mock_graph.return_value = graph_inst

        member = MagicMock()
        member.__getitem__ = lambda self, k: {"name": "pkg-a", "path": "packages/pkg-a"}[k]

        with patch("os.path.exists", return_value=True):
            with patch("rlsbl.workspace.load_releasables", return_value=[rel]):
                with patch("rlsbl.workspace.members_of", return_value=[member]):
                    with patch("rlsbl.context.create_context", return_value=_ctx()):
                        with patch("rlsbl.commands.release.run_cmd", side_effect=SystemExit(1)):
                            with pytest.raises(SystemExit):
                                _cmd_batch_release({"yes": True, "quiet": True}, Path("/ws/pkg"))


class TestFinalizeBatchFile:
    """Covers lines 291-315: _finalize_batch_file."""

    def test_finalize(self, tmp_path):
        from rlsbl.commands.monorepo.batch_release import _finalize_batch_file

        batch_path = tmp_path / "unreleased.toml"
        batch_path.write_text("[packages.alpha]\nbump = 'patch'\n")

        with patch(f"{MOD_BATCH}.commit_files"):
            _finalize_batch_file(str(batch_path), print)

        # Original file should be empty now
        assert batch_path.read_text() == ""
        # Archived file should exist with read-only permission
        archived = list(tmp_path.glob("batch-*.toml"))
        assert len(archived) == 1
        mode = stat.S_IMODE(os.stat(str(archived[0])).st_mode)
        assert mode & stat.S_IWUSR == 0
