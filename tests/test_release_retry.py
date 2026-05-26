"""Tests for rlsbl.commands.release_retry."""

import os
import unittest
from io import StringIO
from unittest.mock import patch, call, MagicMock

from rlsbl.commands.release_retry import run_cmd, _find_dispatch_workflows


# Shared changelog content for tests
CHANGELOG = """\
# Changelog

## 0.41.7

### Features
- Added retry command for CI failures

## 0.41.6

- Previous release
"""


class TestReleaseRetry(unittest.TestCase):
    """Tests for the release retry command."""

    def _make_mock_target(self, version="0.41.7"):
        """Create a mock target with read_version and tag_format."""
        target = MagicMock()
        target.read_version.return_value = version
        target.tag_format.side_effect = lambda v: f"v{v}"
        target.monorepo_tag_format.side_effect = lambda name, v, path=None: f"{name}@v{v}"
        return target

    def _make_mock_entry(self, name="pypi", path="."):
        """Create a mock TargetEntry."""
        entry = MagicMock()
        entry.name = name
        entry.path = path
        return entry

    def _run_side_effect(self, *args, **kwargs):
        """Side effect for the run mock that handles common git/gh commands."""
        cmd, cmd_args = args[0], args[1] if len(args) > 1 else []
        if cmd == "gh" and cmd_args[:2] == ["release", "view"]:
            return ""
        if cmd == "gh" and cmd_args[:2] == ["release", "delete"]:
            return ""
        if cmd == "gh" and cmd_args[:2] == ["release", "create"]:
            return ""
        if cmd == "git" and cmd_args[:2] == ["rev-list", "-1"]:
            return "abc123def456789012345678901234567890abcd"
        return ""

    @patch("rlsbl.commands.release_retry.poll_runs", return_value=[{"databaseId": 1, "name": "CI", "status": "completed"}])
    @patch("rlsbl.commands.release_retry.upload_release_assets")
    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Added retry command")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_happy_path_version_from_project(self, _gh_inst, _gh_auth, _ws_root,
                                              mock_targets_dict, mock_detect,
                                              _exists, mock_extract, mock_run,
                                              mock_upload, mock_poll):
        """Happy path: version auto-detected, delete+create+assets+watch hint."""
        target = self._make_mock_target("0.41.7")
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                run_cmd([], {"yes": True})

        # Version was read from the target
        target.read_version.assert_called_once_with(".")

        # gh release view was called to verify existence
        mock_run.assert_any_call("gh", ["release", "view", "v0.41.7"])

        # gh release delete was called
        mock_run.assert_any_call("gh", ["release", "delete", "v0.41.7", "--yes"])

        # gh release create was called
        create_calls = [c for c in mock_run.call_args_list
                        if len(c[0]) >= 2 and c[0][1][:2] == ["release", "create"]]
        self.assertEqual(len(create_calls), 1)
        self.assertIn("v0.41.7", create_calls[0][0][1])

        # Assets were uploaded
        mock_upload.assert_called_once()

        # Watch hint was printed (not watching since watch=False by default)
        output = mock_stdout.getvalue()
        self.assertIn("Watch CI:", output)

    @patch("rlsbl.commands.release_retry.poll_runs", return_value=[{"databaseId": 1, "name": "CI", "status": "completed"}])
    @patch("rlsbl.commands.release_retry.upload_release_assets")
    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_explicit_version_argument(self, _gh_inst, _gh_auth, _ws_root,
                                        mock_targets_dict, mock_detect,
                                        _exists, mock_extract, mock_run,
                                        mock_upload, mock_poll):
        """Explicit version argument is used instead of reading from project."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["0.41.7"], {"yes": True})

        # read_version should NOT be called when version is explicit
        target.read_version.assert_not_called()

        # Changelog extracted for the right version
        mock_extract.assert_called_once()
        self.assertEqual(mock_extract.call_args[0][1], "0.41.7")

        # Tag uses the explicit version
        mock_run.assert_any_call("gh", ["release", "view", "v0.41.7"])

    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_no_github_release_exits_error(self, _gh_inst, _gh_auth, _ws_root,
                                            mock_targets_dict, mock_detect,
                                            _exists, mock_run):
        """No GitHub Release exists for the tag -- exits with error."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target

        # gh release view fails
        mock_run.side_effect = Exception("release not found")

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(["0.41.7"], {"yes": True})

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("no GitHub Release found", mock_stderr.getvalue())

    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_dry_run_prints_plan_no_mutations(self, _gh_inst, _gh_auth, _ws_root,
                                               mock_targets_dict, mock_detect,
                                               _exists, mock_extract, mock_run):
        """--dry-run prints what would happen without deleting or creating."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.41.7"], {"dry-run": True})

        output = mock_stdout.getvalue()
        self.assertIn("Would delete and re-create", output)
        self.assertIn("v0.41.7", output)

        # gh release view should be called (to verify existence)
        mock_run.assert_any_call("gh", ["release", "view", "v0.41.7"])

        # gh release delete and create should NOT be called
        delete_calls = [c for c in mock_run.call_args_list
                        if len(c[0]) >= 2 and c[0][1][:2] == ["release", "delete"]]
        self.assertEqual(len(delete_calls), 0)
        create_calls = [c for c in mock_run.call_args_list
                        if len(c[0]) >= 2 and c[0][1][:2] == ["release", "create"]]
        self.assertEqual(len(create_calls), 0)

    @patch("rlsbl.commands.release_retry.poll_runs", return_value=[{"databaseId": 1, "name": "CI", "status": "completed"}])
    @patch("rlsbl.commands.release_retry.upload_release_assets")
    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_yes_skips_confirmation(self, _gh_inst, _gh_auth, _ws_root,
                                     mock_targets_dict, mock_detect,
                                     _exists, mock_extract, mock_run,
                                     mock_upload, mock_poll):
        """--yes flag skips the interactive confirmation prompt."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"), \
             patch("builtins.input") as mock_input:
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["0.41.7"], {"yes": True})

        # input() should never be called when --yes is set
        mock_input.assert_not_called()

        # Release operations should proceed
        mock_run.assert_any_call("gh", ["release", "delete", "v0.41.7", "--yes"])

    @patch("rlsbl.commands.release_retry.poll_runs", return_value=[{"databaseId": 1, "name": "CI", "status": "completed"}])
    @patch("rlsbl.commands.release_retry.upload_release_assets")
    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_asset_reupload(self, _gh_inst, _gh_auth, _ws_root,
                             mock_targets_dict, mock_detect,
                             _exists, mock_extract, mock_run,
                             mock_upload, mock_poll):
        """Verify upload_release_assets is called with correct arguments."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["0.41.7"], {"yes": True})

        mock_upload.assert_called_once_with(
            "v0.41.7",  # tag
            ".",         # version_dir
            "0.41.7",   # version
            unittest.mock.ANY,  # log function
            {"yes": True},  # flags
        )

    @patch("rlsbl.commands.release_retry._find_dispatch_workflows", return_value=["publish.yml", "ci.yml"])
    @patch("rlsbl.commands.release_retry.poll_runs", return_value=[])
    @patch("rlsbl.commands.release_retry.upload_release_assets")
    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_dispatch_fallback_when_no_runs(self, _gh_inst, _gh_auth, _ws_root,
                                             mock_targets_dict, mock_detect,
                                             _exists, mock_extract, mock_run,
                                             mock_upload, mock_poll, mock_dispatch):
        """When poll_runs returns empty, falls back to gh workflow run."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                run_cmd(["0.41.7"], {"yes": True})

        output = mock_stdout.getvalue()
        self.assertIn("Dispatching workflows manually", output)

        # Verify gh workflow run was called for each dispatch file
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertEqual(len(dispatch_calls), 2)
        self.assertIn("publish.yml", dispatch_calls[0][0][1])
        self.assertIn("ci.yml", dispatch_calls[1][0][1])

    @patch("rlsbl.commands.release_retry.poll_runs", return_value=[{"databaseId": 1, "name": "CI", "status": "completed"}])
    @patch("rlsbl.commands.release_retry.upload_release_assets")
    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.resolve_project")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_monorepo_tag_format(self, _gh_inst, _gh_auth, _ws_root,
                                  mock_resolve, mock_targets_dict, mock_detect,
                                  _exists, mock_extract, mock_run,
                                  mock_upload, mock_poll):
        """In monorepo context, uses monorepo_tag_format for the tag."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_resolve.return_value = {"name": "my-pkg", "path": "packages/my-pkg"}

        def run_effect(*args, **kwargs):
            cmd, cmd_args = args[0], args[1] if len(args) > 1 else []
            if cmd == "gh" and cmd_args[:2] == ["release", "view"]:
                return ""
            if cmd == "gh" and cmd_args[:2] == ["release", "delete"]:
                return ""
            if cmd == "gh" and cmd_args[:2] == ["release", "create"]:
                return ""
            if cmd == "git" and cmd_args[:2] == ["rev-list", "-1"]:
                return "abc123def456"
            return ""

        mock_run.side_effect = run_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"), \
             patch("os.chdir"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["0.41.7"], {"yes": True})

        # Monorepo tag format should be used
        target.monorepo_tag_format.assert_called_once_with("my-pkg", "0.41.7", path="packages/my-pkg")

        # gh release view should use the monorepo tag
        mock_run.assert_any_call("gh", ["release", "view", "my-pkg@v0.41.7"])

    @patch("rlsbl.commands.release_retry.poll_runs", return_value=[{"databaseId": 1, "name": "CI", "status": "completed"}])
    @patch("rlsbl.commands.release_retry.upload_release_assets")
    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_watch_flag_calls_watch_run_cmd(self, _gh_inst, _gh_auth, _ws_root,
                                             mock_targets_dict, mock_detect,
                                             _exists, mock_extract, mock_run,
                                             mock_upload, mock_poll):
        """--watch flag calls watch.run_cmd instead of printing a hint."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("rlsbl.commands.release_retry.watch_run_cmd") as mock_watch:
                with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                    run_cmd(["0.41.7"], {"yes": True, "watch": True})

                # Watch was called with the commit SHA
                mock_watch.assert_called_once_with(
                    None,
                    ["abc123def456789012345678901234567890abcd"],
                    {},
                )

                # Watch hint should NOT be in output
                output = mock_stdout.getvalue()
                self.assertNotIn("Watch CI:", output)

    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_confirmation_prompt_abort(self, _gh_inst, _gh_auth, _ws_root,
                                        mock_targets_dict, mock_detect,
                                        _exists, mock_extract, mock_run):
        """User says 'n' at confirmation prompt -- aborts without mutating."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("builtins.input", return_value="n"):
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run_cmd(["0.41.7"], {})

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Aborted", mock_stderr.getvalue())

        # No delete or create calls
        delete_calls = [c for c in mock_run.call_args_list
                        if len(c[0]) >= 2 and c[0][1][:2] == ["release", "delete"]]
        self.assertEqual(len(delete_calls), 0)

    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_delete_succeeds_create_fails_warns(self, _gh_inst, _gh_auth, _ws_root,
                                                  mock_targets_dict, mock_detect,
                                                  _exists, mock_extract, mock_run):
        """Delete succeeds but create fails -- prints warning about deleted-but-not-recreated."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target

        call_count = {"n": 0}
        def run_effect(*args, **kwargs):
            cmd, cmd_args = args[0], args[1] if len(args) > 1 else []
            if cmd == "gh" and cmd_args[:2] == ["release", "view"]:
                return ""
            if cmd == "gh" and cmd_args[:2] == ["release", "delete"]:
                return ""
            if cmd == "gh" and cmd_args[:2] == ["release", "create"]:
                raise Exception("API error")
            if cmd == "git" and cmd_args[:2] == ["rev-list", "-1"]:
                return "abc123def456"
            return ""

        mock_run.side_effect = run_effect

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run_cmd(["0.41.7"], {"yes": True})

        self.assertEqual(ctx.exception.code, 1)
        stderr_output = mock_stderr.getvalue()
        self.assertIn("was deleted but re-creation failed", stderr_output)

    @patch("rlsbl.commands.release_retry.run")
    @patch("rlsbl.commands.release_retry.extract_changelog_entry", return_value="### Features\n- Fix")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_version_with_v_prefix_stripped(self, _gh_inst, _gh_auth, _ws_root,
                                             mock_targets_dict, mock_detect,
                                             _exists, mock_extract, mock_run):
        """Version arg 'v0.41.7' has the 'v' stripped for changelog lookup."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(["v0.41.7"], {"dry-run": True})

        # Changelog lookup should use "0.41.7" (without "v")
        mock_extract.assert_called_once()
        self.assertEqual(mock_extract.call_args[0][1], "0.41.7")

    def test_no_gh_installed_exits(self):
        """Missing gh CLI exits with error."""
        with patch("rlsbl.commands.release_retry.check_gh_installed", return_value=False):
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run_cmd([], {})
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("gh CLI is not installed", mock_stderr.getvalue())

    def test_no_gh_auth_exits(self):
        """Unauthenticated gh CLI exits with error."""
        with patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.release_retry.check_gh_auth", return_value=False):
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run_cmd([], {})
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("gh CLI is not authenticated", mock_stderr.getvalue())


class TestFindDispatchWorkflows(unittest.TestCase):
    """Tests for the _find_dispatch_workflows helper."""

    @patch("os.path.isdir", return_value=False)
    def test_no_workflows_dir(self, _isdir):
        """Returns empty list when .github/workflows/ doesn't exist."""
        self.assertEqual(_find_dispatch_workflows(), [])

    @patch("os.listdir", return_value=["ci.yml", "publish.yml", "readme.md"])
    @patch("os.path.isdir", return_value=True)
    def test_filters_by_workflow_dispatch_content(self, _isdir, _listdir):
        """Only returns YAML files that contain 'workflow_dispatch'."""
        file_contents = {
            os.path.join(".github", "workflows", "ci.yml"): "on:\n  push:\n",
            os.path.join(".github", "workflows", "publish.yml"): "on:\n  workflow_dispatch:\n  release:\n",
        }
        def mock_open_fn(path, *args, **kwargs):
            content = file_contents.get(path, "")
            return unittest.mock.mock_open(read_data=content)()

        with patch("builtins.open", side_effect=mock_open_fn):
            result = _find_dispatch_workflows()

        self.assertEqual(result, ["publish.yml"])


if __name__ == "__main__":
    unittest.main()
