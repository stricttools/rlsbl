"""Tests for rlsbl.commands.undo — happy-path full flow."""

import os
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, patch, call

from rlsbl.commands.undo import run_cmd
from rlsbl.context import ProjectContext


class TestUndoHappyPath(unittest.TestCase):
    """Verify the full undo flow succeeds when all subprocess calls pass."""

    @patch("rlsbl.commands.undo.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_full_undo_flow(self, mock_run, mock_run_gh, _gh_inst, _gh_auth,
                            _clean, mock_branch, mock_push, _ws_root):
        """Happy path: all steps succeed, exits cleanly with no exception."""

        # run() handles git commands; run_gh() handles gh commands.
        mock_run.side_effect = [
            "v1.0.0",   # git describe --tags --abbrev=0 --match v*
            "",         # git push origin :v1.0.0
            "",         # git tag -d v1.0.0
            "v1.0.0",  # git log -1 --format=%s
            "",         # git revert --no-edit HEAD
        ]
        # run_gh handles: gh release view, gh release delete (return_value="" covers both)

        # Run with --yes to skip interactive prompts; suppress stdout
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        # Verify git subprocess commands were issued via run()
        expected_run_calls = [
            call("git", ["describe", "--tags", "--abbrev=0", "--match", "v*"]),
            call("git", ["push", "origin", ":v1.0.0"], timeout=120, env=ANY),
            call("git", ["tag", "-d", "v1.0.0"]),
            call("git", ["log", "-1", "--format=%s"]),
            call("git", ["revert", "--no-edit", "HEAD"]),
        ]
        mock_run.assert_has_calls(expected_run_calls, any_order=False)
        self.assertEqual(mock_run.call_count, 5)

        # Verify gh commands went through run_gh
        mock_run_gh.assert_any_call(["release", "view", "v1.0.0"], config={})
        mock_run_gh.assert_any_call(["release", "delete", "v1.0.0", "--yes"], config={})

        # Verify push_if_needed was called with the current branch
        mock_push.assert_called_once_with("main", env=ANY, config={})


class TestUndoMonorepo(unittest.TestCase):
    """Verify monorepo-aware undo finds project-scoped tags and commit messages."""

    @patch("rlsbl.commands.undo.find_workspace_root", return_value="/fake/monorepo")
    @patch("rlsbl.commands.undo.resolve_project", return_value={"name": "mylib", "path": "packages/mylib"})
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_monorepo_finds_scoped_tag(self, mock_run, mock_run_gh,
                                       _gh_inst, _gh_auth, _clean,
                                       mock_branch, mock_push, _resolve,
                                       _ws_root):
        """In monorepo mode, undo uses --match '<project>@v*' to find the tag."""

        mock_run.side_effect = [
            "mylib@v2.1.0",              # git describe --tags --abbrev=0 --match mylib@v*
            "",                           # git push origin :mylib@v2.1.0
            "",                           # git tag -d mylib@v2.1.0
            "mylib: release v2.1.0",     # git log -1 --format=%s (matches expected)
            "",                           # git revert --no-edit HEAD
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        # Verify tag discovery uses project-scoped match pattern
        expected_run_calls = [
            call("git", ["describe", "--tags", "--abbrev=0", "--match", "mylib@v*"]),
            call("git", ["push", "origin", ":mylib@v2.1.0"], timeout=120, env=ANY),
            call("git", ["tag", "-d", "mylib@v2.1.0"]),
            call("git", ["log", "-1", "--format=%s"]),
            call("git", ["revert", "--no-edit", "HEAD"]),
        ]
        mock_run.assert_has_calls(expected_run_calls, any_order=False)
        self.assertEqual(mock_run.call_count, 5)

        # Verify gh commands went through run_gh
        mock_run_gh.assert_any_call(["release", "view", "mylib@v2.1.0"], config={})
        mock_run_gh.assert_any_call(["release", "delete", "mylib@v2.1.0", "--yes"], config={})
        mock_push.assert_called_once_with("main", env=ANY, config={})

    @patch("rlsbl.commands.undo.find_workspace_root", return_value="/fake/monorepo")
    @patch("rlsbl.commands.undo.resolve_project", return_value={"name": "mylib", "path": "packages/mylib"})
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_monorepo_skips_revert_on_mismatch(self, mock_run, _run_gh,
                                                _gh_inst, _gh_auth, _clean,
                                                mock_branch, mock_push,
                                                _resolve, _ws_root):
        """In monorepo mode, revert is skipped if HEAD doesn't match the expected commit message format."""

        mock_run.side_effect = [
            "mylib@v2.1.0",        # git describe --tags --abbrev=0 --match mylib@v*
            "",                     # git push origin :mylib@v2.1.0
            "",                     # git tag -d mylib@v2.1.0
            "some other commit",   # git log -1 --format=%s (does NOT match)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        # Only 4 git calls: no revert issued (gh calls go through run_gh)
        self.assertEqual(mock_run.call_count, 4)
        mock_push.assert_not_called()

    @patch("rlsbl.commands.undo.find_workspace_root", return_value="/fake/monorepo")
    @patch("rlsbl.commands.undo.resolve_project", return_value=None)
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    def test_monorepo_exits_if_not_inside_project(self, _gh_inst, _gh_auth,
                                                   _clean, _resolve, _ws_root):
        """Exits with error if in a monorepo but not inside any registered project."""

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not inside any project", mock_stderr.getvalue())

    @patch("rlsbl.commands.undo.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_standalone_skips_revert_on_mismatch(self, mock_run, _run_gh,
                                                  _gh_inst, _gh_auth, _clean,
                                                  mock_branch, mock_push,
                                                  _ws_root):
        """In standalone mode, revert is skipped if HEAD doesn't match the tag."""

        mock_run.side_effect = [
            "v1.0.0",              # git describe --tags --abbrev=0 --match v*
            "",                     # git push origin :v1.0.0
            "",                     # git tag -d v1.0.0
            "some other commit",   # git log -1 --format=%s (does NOT match)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        # Only 4 git calls: no revert issued (gh calls go through run_gh)
        self.assertEqual(mock_run.call_count, 4)
        mock_push.assert_not_called()


class TestUndoTwoCommitPattern(unittest.TestCase):
    """Verify undo handles the two-commit release pattern where HEAD is a
    finalize commit and HEAD~1 is the version-bump commit."""

    @patch("rlsbl.commands.undo.generate_changelog")
    @patch("rlsbl.commands.undo.unfinalize_version", return_value=["unreleased.jsonl"])
    @patch("rlsbl.commands.undo.get_changes_dir", return_value="/fake/.rlsbl/changes")
    @patch("rlsbl.commands.undo.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_undo_handles_finalize_commit_at_head(self, mock_run, _run_gh,
                                                   _gh_inst, _gh_auth, _clean,
                                                   mock_branch, mock_push,
                                                   _ws_root, _changes_dir,
                                                   mock_unfinalize,
                                                   mock_generate):
        """When HEAD is a finalize commit (chore: finalize changelog for X.Y.Z)
        and HEAD~1 is the version-bump commit (vX.Y.Z), undo should revert
        both commits and restore changelog state."""

        mock_run.side_effect = [
            "v1.0.0",                                      # git describe --tags --abbrev=0 --match v*
            "",                                             # git push origin :v1.0.0
            "",                                             # git tag -d v1.0.0
            "chore: finalize changelog for 1.0.0",         # git log -1 --format=%s (HEAD — finalize commit)
            "",                                             # git revert --no-edit HEAD (revert finalize)
            "v1.0.0",                                      # git log -1 --format=%s (HEAD is now version-bump)
            "",                                             # git revert --no-edit HEAD (revert version-bump)
            "",                                             # git add (changelog restoration)
            "",                                             # git commit (changelog restoration)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        expected_run_calls = [
            call("git", ["describe", "--tags", "--abbrev=0", "--match", "v*"]),
            call("git", ["push", "origin", ":v1.0.0"], timeout=120, env=ANY),
            call("git", ["tag", "-d", "v1.0.0"]),
            call("git", ["log", "-1", "--format=%s"]),
            call("git", ["revert", "--no-edit", "HEAD"]),
            call("git", ["log", "-1", "--format=%s"]),
            call("git", ["revert", "--no-edit", "HEAD"]),
        ]
        mock_run.assert_has_calls(expected_run_calls, any_order=False)
        # 7 git calls + 2 for changelog restoration (git add + git commit)
        self.assertEqual(mock_run.call_count, 9)

        # Verify changelog restoration was called
        mock_unfinalize.assert_called_once_with("/fake/.rlsbl/changes", "1.0.0")
        mock_generate.assert_called_once()

        # Push should still be called after reverting
        mock_push.assert_called_once_with("main", env=ANY, config={})


class TestUndoReleaseFileRestore(unittest.TestCase):
    """Verify undo repairs a finalized release file left on disk."""

    @patch("rlsbl.commands.undo.unfinalize_release_file",
           return_value=["unreleased.toml", "v1.0.0.toml"])
    @patch("rlsbl.commands.undo.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_commits_restored_release_file(self, mock_run, _run_gh,
                                           _gh_inst, _gh_auth, _clean,
                                           mock_branch, mock_push, _ws_root,
                                           mock_unfinalize):
        """When unfinalize_release_file restores files, undo commits them."""

        mock_run.side_effect = [
            "v1.0.0",   # git describe --tags --abbrev=0 --match v*
            "",         # git push origin :v1.0.0
            "",         # git tag -d v1.0.0
            "v1.0.0",   # git log -1 --format=%s (version-bump at HEAD)
            "",         # git revert --no-edit HEAD
            "",         # git add <releases_dir> (release-file restoration)
            "",         # git commit (release-file restoration)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        releases_dir = os.path.join(".", ".rlsbl", "releases")
        mock_unfinalize.assert_called_once_with(releases_dir, "1.0.0")
        mock_run.assert_has_calls([
            call("git", ["add", releases_dir]),
            call("git", ["commit", "-m", "chore: restore release file after undo of v1.0.0"]),
        ], any_order=False)
        self.assertEqual(mock_run.call_count, 7)

    @patch("rlsbl.commands.undo.unfinalize_release_file", return_value=[])
    @patch("rlsbl.commands.undo.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_no_commit_when_nothing_to_restore(self, mock_run, _run_gh,
                                               _gh_inst, _gh_auth, _clean,
                                               mock_branch, mock_push,
                                               _ws_root, mock_unfinalize):
        """When the release file needs no repair, no extra commit is made."""

        mock_run.side_effect = [
            "v1.0.0",   # git describe --tags --abbrev=0 --match v*
            "",         # git push origin :v1.0.0
            "",         # git tag -d v1.0.0
            "v1.0.0",   # git log -1 --format=%s
            "",         # git revert --no-edit HEAD
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        mock_unfinalize.assert_called_once()
        self.assertEqual(mock_run.call_count, 5)


class TestUndoNoGitHubRelease(unittest.TestCase):
    """Verify undo handles a missing GitHub Release gracefully."""

    @patch("rlsbl.commands.undo.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.undo.push_if_needed")
    @patch("rlsbl.commands.undo.get_current_branch", return_value="main")
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run_gh", return_value="")
    @patch("rlsbl.commands.undo.run")
    def test_skips_delete_when_release_does_not_exist(self, mock_run,
                                                       mock_run_gh, _gh_inst,
                                                       _gh_auth, _clean,
                                                       mock_branch, mock_push,
                                                       _ws_root):
        """When gh release view fails, the step is SKIPPED (not FAILED)
        and the rest of undo proceeds normally."""
        import subprocess as sp

        # run_gh raises on gh release view (release doesn't exist)
        mock_run_gh.side_effect = sp.CalledProcessError(1, "gh release view")

        # git commands only (gh calls go through run_gh)
        mock_run.side_effect = [
            "v1.0.0",   # git describe --tags --abbrev=0 --match v*
            "",          # git push origin :v1.0.0
            "",          # git tag -d v1.0.0
            "v1.0.0",   # git log -1 --format=%s
            "",          # git revert --no-edit HEAD
        ]

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        output = mock_stdout.getvalue()

        # The step should be SKIPPED, not FAILED
        assert "FAILED" not in output, f"Expected no FAILED steps, got:\n{output}"

        # Verify git commands via run()
        expected_run_calls = [
            call("git", ["describe", "--tags", "--abbrev=0", "--match", "v*"]),
            call("git", ["push", "origin", ":v1.0.0"], timeout=120, env=ANY),
            call("git", ["tag", "-d", "v1.0.0"]),
            call("git", ["log", "-1", "--format=%s"]),
            call("git", ["revert", "--no-edit", "HEAD"]),
        ]
        mock_run.assert_has_calls(expected_run_calls, any_order=False)
        self.assertEqual(mock_run.call_count, 5)

        # run_gh was called once for view, which failed (no delete follows)
        self.assertEqual(mock_run_gh.call_count, 1)

        # Push should still happen (undo completed successfully)
        mock_push.assert_called_once_with("main", env=ANY, config={})


if __name__ == "__main__":
    unittest.main()
