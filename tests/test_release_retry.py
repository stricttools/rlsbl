"""Tests for rlsbl.commands.release_retry."""

import os
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock

from rlsbl.commands.release_retry import run_cmd, _find_dispatch_workflows
from rlsbl.release_file import RetryConfig


def _make_retry_config(version="0.41.7", dispatch=None, ref=None):
    """Create a RetryConfig with sensible defaults."""
    if dispatch is None:
        dispatch = ["ci.yml"]
    if ref is None:
        ref = f"v{version}"
    return RetryConfig(
        version=version,
        dispatch=dispatch,
        ref=ref,
    )


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
        if cmd == "gh" and cmd_args[:2] == ["workflow", "run"]:
            return ""
        if cmd == "git" and cmd_args[:2] == ["rev-list", "-1"]:
            return "abc123def456789012345678901234567890abcd"
        return ""

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_happy_path_with_retry_config(self, _gh_inst, _gh_auth, _ws_root,
                                           mock_targets_dict, mock_detect,
                                           _exists, mock_run, mock_cleanup):
        """Happy path: dispatch workflows, verify release exists, watch hint."""
        target = self._make_mock_target("0.41.7")
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config("0.41.7", dispatch=["publish.yml", "ci.yml"])

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(config, {"yes": True})

        # gh release view was called to verify existence
        mock_run.assert_any_call("gh", ["release", "view", "v0.41.7"])

        # gh workflow run was called for each workflow in config.dispatch
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertEqual(len(dispatch_calls), 2)
        self.assertEqual(dispatch_calls[0][0][1], ["workflow", "run", "publish.yml", "--ref", "v0.41.7"])
        self.assertEqual(dispatch_calls[1][0][1], ["workflow", "run", "ci.yml", "--ref", "v0.41.7"])

        # Cleanup (saferm) was called
        mock_cleanup.assert_called_once()

        # Watch hint was printed (not watching since watch=False by default)
        output = mock_stdout.getvalue()
        self.assertIn("Watch CI:", output)

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_retry_config_uses_dispatch_and_ref(self, _gh_inst, _gh_auth, _ws_root,
                                                  mock_targets_dict, mock_detect,
                                                  _exists, mock_run, mock_cleanup):
        """RetryConfig dispatch and ref fields are used for workflow dispatch."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config(
            "0.41.7",
            dispatch=["publish.yml", "ci.yml"],
            ref="v0.41.7",
        )

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(config, {"yes": True})

        output = mock_stdout.getvalue()
        self.assertIn("Dispatching workflows", output)

        # Verify gh workflow run was called for each configured workflow
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertEqual(len(dispatch_calls), 2)
        # Verify ref from config is used
        self.assertEqual(dispatch_calls[0][0][1], ["workflow", "run", "publish.yml", "--ref", "v0.41.7"])
        self.assertEqual(dispatch_calls[1][0][1], ["workflow", "run", "ci.yml", "--ref", "v0.41.7"])

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_all_workflows_dispatched_unconditionally(self, _gh_inst, _gh_auth, _ws_root,
                                                       mock_targets_dict, mock_detect,
                                                       _exists, mock_run, mock_cleanup):
        """All workflows in dispatch list are dispatched unconditionally."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config(
            "0.41.7",
            dispatch=["ci.yml", "publish.yml"],
            ref="v0.41.7",
        )

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(config, {"yes": True})

        # Both workflows must be dispatched
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertEqual(len(dispatch_calls), 2)
        dispatched_filenames = [c[0][1][2] for c in dispatch_calls]
        self.assertIn("ci.yml", dispatched_filenames)
        self.assertIn("publish.yml", dispatched_filenames)
        # All dispatches use ref from config
        for call in dispatch_calls:
            self.assertEqual(call[0][1][3:], ["--ref", "v0.41.7"])

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

        config = _make_retry_config("0.41.7")

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(config, {"yes": True})

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("no GitHub Release found", mock_stderr.getvalue())

    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_dry_run_prints_plan_no_mutations(self, _gh_inst, _gh_auth, _ws_root,
                                               mock_targets_dict, mock_detect,
                                               _exists, mock_run):
        """--dry-run prints what would dispatch without actually dispatching."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config("0.41.7")

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(config, {"dry-run": True})

        output = mock_stdout.getvalue()
        self.assertIn("Would dispatch", output)
        self.assertIn("v0.41.7", output)

        # gh release view should be called (to verify existence)
        mock_run.assert_any_call("gh", ["release", "view", "v0.41.7"])

        # gh workflow run should NOT be called in dry-run
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertEqual(len(dispatch_calls), 0)

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_yes_skips_confirmation(self, _gh_inst, _gh_auth, _ws_root,
                                     mock_targets_dict, mock_detect,
                                     _exists, mock_run, mock_cleanup):
        """--yes flag skips the interactive confirmation prompt."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config("0.41.7")

        with patch("builtins.input") as mock_input:
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(config, {"yes": True})

        # input() should never be called when --yes is set
        mock_input.assert_not_called()

        # Dispatch should have proceeded
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertGreater(len(dispatch_calls), 0)

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.resolve_project")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_monorepo_tag_format(self, _gh_inst, _gh_auth, _ws_root,
                                  mock_resolve, mock_targets_dict, mock_detect,
                                  _exists, mock_run, mock_cleanup):
        """In monorepo context, uses monorepo_tag_format for the tag and dispatch ref."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_resolve.return_value = {"name": "my-pkg", "path": "packages/my-pkg"}

        def run_effect(*args, **kwargs):
            cmd, cmd_args = args[0], args[1] if len(args) > 1 else []
            if cmd == "gh" and cmd_args[:2] == ["release", "view"]:
                return ""
            if cmd == "gh" and cmd_args[:2] == ["workflow", "run"]:
                return ""
            if cmd == "git" and cmd_args[:2] == ["rev-list", "-1"]:
                return "abc123def456"
            return ""

        mock_run.side_effect = run_effect

        config = _make_retry_config("0.41.7")

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(config, {"yes": True})

        # Monorepo tag format should be used
        target.monorepo_tag_format.assert_called_once_with("my-pkg", "0.41.7", path="packages/my-pkg")

        # gh release view should use the monorepo tag
        mock_run.assert_any_call("gh", ["release", "view", "my-pkg@v0.41.7"])

        # Dispatch should use the ref from config
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertGreater(len(dispatch_calls), 0)
        for call in dispatch_calls:
            self.assertEqual(call[0][1][3:], ["--ref", "v0.41.7"])

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_watch_flag_calls_watch_run_cmd(self, _gh_inst, _gh_auth, _ws_root,
                                             mock_targets_dict, mock_detect,
                                             _exists, mock_run, mock_cleanup):
        """--watch flag calls watch.run_cmd after dispatch instead of printing a hint."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config("0.41.7")

        with patch("rlsbl.commands.release_retry.watch_run_cmd") as mock_watch:
            with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                run_cmd(config, {"yes": True, "watch": True})

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
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_confirmation_prompt_abort(self, _gh_inst, _gh_auth, _ws_root,
                                        mock_targets_dict, mock_detect,
                                        _exists, mock_run):
        """User says 'n' at 'Will dispatch' prompt -- aborts without dispatching."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config("0.41.7")

        with patch("builtins.input", return_value="n"):
            with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                    with self.assertRaises(SystemExit) as ctx:
                        run_cmd(config, {})

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Aborted", mock_stderr.getvalue())

        # "Will dispatch" prompt should have been shown
        output = mock_stdout.getvalue()
        self.assertIn("Will dispatch", output)

        # No dispatch calls
        dispatch_calls = [c for c in mock_run.call_args_list
                          if len(c[0]) >= 2 and c[0][1][:2] == ["workflow", "run"]]
        self.assertEqual(len(dispatch_calls), 0)

    def test_no_gh_installed_exits(self):
        """Missing gh CLI exits with error."""
        with patch("rlsbl.commands.release_retry.check_gh_installed", return_value=False):
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run_cmd(None, {})
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("gh CLI is not installed", mock_stderr.getvalue())

    def test_no_gh_auth_exits(self):
        """Unauthenticated gh CLI exits with error."""
        with patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.release_retry.check_gh_auth", return_value=False):
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run_cmd(None, {})
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("gh CLI is not authenticated", mock_stderr.getvalue())

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_auto_scaffold_when_no_retry_file(self, _gh_inst, _gh_auth, _ws_root,
                                                mock_targets_dict, mock_detect,
                                                _exists, mock_run, mock_cleanup):
        """When retry_config is None and retry.toml doesn't exist, auto-scaffolds then exits
        because ref is empty and must be set by the user."""
        target = self._make_mock_target("0.41.7")
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        # _scaffold_retry_file raises ValueError because ref is empty
        scaffold_error = ValueError("ref must be set in retry.toml (e.g. a tag like v1.2.3 or a branch like main)")

        with patch("rlsbl.commands.release_retry._scaffold_retry_file", side_effect=scaffold_error) as mock_scaffold, \
             patch("rlsbl.commands.release_retry.get_retry_file_path", return_value="/fake/retry.toml"):
            def exists_side_effect(path):
                if "retry.toml" in str(path):
                    return False
                return True
            _exists.side_effect = exists_side_effect

            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run_cmd(None, {"yes": True})

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("ref must be set", mock_stderr.getvalue())

        # _scaffold_retry_file was called
        mock_scaffold.assert_called_once()

    @patch("rlsbl.commands.release_retry.read_retry_file")
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_existing_retry_toml_not_overwritten(self, _gh_inst, _gh_auth, _ws_root,
                                                   mock_targets_dict, mock_detect,
                                                   mock_read_retry):
        """When retry.toml already exists and retry_config is None, reads the file."""
        target = self._make_mock_target("0.41.7")
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target

        existing_config = _make_retry_config("0.41.7", dispatch=["custom.yml"])
        mock_read_retry.return_value = existing_config

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            retry_path = os.path.join(tmpdir, "retry.toml")
            # Create a dummy file so os.path.exists returns True
            with open(retry_path, "w") as f:
                f.write("dummy")

            with patch("rlsbl.commands.release_retry.get_retry_file_path", return_value=retry_path), \
                 patch("rlsbl.commands.release_retry.run") as mock_run, \
                 patch("os.path.exists", return_value=True), \
                 patch("rlsbl.commands.release_retry._cleanup_retry_file"):
                mock_run.side_effect = self._run_side_effect

                with patch("sys.stdout", new_callable=StringIO):
                    run_cmd(None, {"yes": True})

            # read_retry_file was called (existing file was read, not overwritten)
            mock_read_retry.assert_called_once_with(retry_path)

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.detect_targets")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_retry_file_deleted_after_success(self, _gh_inst, _gh_auth, _ws_root,
                                                mock_targets_dict, mock_detect,
                                                _exists, mock_run, mock_cleanup):
        """retry.toml is deleted via saferm after successful retry."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = [entry]
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = _make_retry_config("0.41.7")

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(config, {"yes": True})

        # _cleanup_retry_file was called (which uses saferm internally)
        mock_cleanup.assert_called_once()
        call_args = mock_cleanup.call_args[0]
        self.assertIn("retry.toml", call_args[0])


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


class TestRetryConfig(unittest.TestCase):
    """Tests for RetryConfig and read_retry_file."""

    def test_read_retry_file_valid(self):
        """Valid retry.toml with 3 fields is read correctly."""
        import tempfile
        import tomlkit as tk

        doc = tk.document()
        doc.add("version", "1.2.3")
        doc.add("dispatch", ["publish.yml", "ci.yml"])
        doc.add("ref", "v1.2.3")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            tk.dump(doc, f)
            path = f.name

        try:
            from rlsbl.release_file import read_retry_file
            config = read_retry_file(path)
            self.assertEqual(config.version, "1.2.3")
            self.assertEqual(config.dispatch, ["publish.yml", "ci.yml"])
            self.assertEqual(config.ref, "v1.2.3")
        finally:
            os.unlink(path)

    def test_read_retry_file_missing_version(self):
        """Missing version field raises ValueError."""
        import tempfile
        import tomlkit as tk

        doc = tk.document()
        doc.add("dispatch", ["publish.yml"])
        doc.add("ref", "v1.2.3")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            tk.dump(doc, f)
            path = f.name

        try:
            from rlsbl.release_file import read_retry_file
            with self.assertRaises(ValueError) as ctx:
                read_retry_file(path)
            self.assertIn("version", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_read_retry_file_empty_ref(self):
        """Empty ref field raises ValueError with helpful message."""
        import tempfile
        import tomlkit as tk

        doc = tk.document()
        doc.add("version", "1.2.3")
        doc.add("dispatch", ["publish.yml"])
        doc.add("ref", "")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            tk.dump(doc, f)
            path = f.name

        try:
            from rlsbl.release_file import read_retry_file
            with self.assertRaises(ValueError) as ctx:
                read_retry_file(path)
            self.assertIn("ref must be set in retry.toml", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_read_retry_file_missing_file(self):
        """Missing file raises FileNotFoundError."""
        from rlsbl.release_file import read_retry_file
        with self.assertRaises(FileNotFoundError):
            read_retry_file("/nonexistent/retry.toml")

    def test_get_retry_file_path(self):
        """get_retry_file_path returns expected path."""
        from rlsbl.release_file import get_retry_file_path
        self.assertEqual(
            get_retry_file_path("."),
            os.path.join(".", ".rlsbl", "releases", "retry.toml"),
        )
        self.assertEqual(
            get_retry_file_path("/project"),
            os.path.join("/project", ".rlsbl", "releases", "retry.toml"),
        )


class TestScaffoldRetryFile(unittest.TestCase):
    """Tests for the _scaffold_retry_file helper."""

    def test_scaffold_creates_file_with_empty_ref(self):
        """_scaffold_retry_file writes retry.toml with empty ref and a comment."""
        import tempfile
        import tomlkit as tk
        from rlsbl.commands.release_retry import _scaffold_retry_file

        target = MagicMock()
        target.read_version.return_value = "2.0.0"
        target.tag_format.side_effect = lambda v: f"v{v}"
        entry = MagicMock()
        entry.name = "pypi"
        entry.path = "."

        with tempfile.TemporaryDirectory() as tmpdir:
            retry_path = os.path.join(tmpdir, "retry.toml")

            with patch("rlsbl.commands.release_retry.detect_targets", return_value=[entry]), \
                 patch("rlsbl.commands.release_retry.TARGETS", {"pypi": target}), \
                 patch("rlsbl.commands.release_retry._find_dispatch_workflows", return_value=["publish.yml", "ci.yml"]):
                # read_retry_file will raise because ref is empty
                with self.assertRaises(ValueError) as ctx:
                    _scaffold_retry_file(
                        retry_path, ".", target, None, None, lambda msg: None,
                    )
                self.assertIn("ref must be set", str(ctx.exception))

            # Verify file on disk has correct structure
            with open(retry_path) as f:
                data = tk.load(f)
            self.assertEqual(data["version"], "2.0.0")
            self.assertEqual(list(data["dispatch"]), ["publish.yml", "ci.yml"])
            self.assertEqual(data["ref"], "")

            # Verify the comment is present in the raw file
            with open(retry_path) as f:
                raw = f.read()
            self.assertIn("# Git ref to dispatch CI against", raw)
            # No assets field
            self.assertNotIn("assets", data)


if __name__ == "__main__":
    unittest.main()
