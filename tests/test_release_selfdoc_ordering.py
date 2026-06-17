"""Tests for selfdoc ordering and hash refresh in the release pipeline."""

import inspect
import os
import subprocess
from unittest.mock import MagicMock, patch

from rlsbl.commands.release import _refresh_selfdoc_hashes, _run_selfdoc_gen, run_cmd
from rlsbl.commands.release import _run_cmd_inner


class TestSelfdocBeforeTestsAndLint:
    """Verify the ordering: selfdoc check -> tests -> lint."""

    def test_selfdoc_failure_prevents_tests_and_lint(self):
        """When selfdoc check fails, tests and lint should not run.

        We verify the ordering by reading the source and checking that
        _run_selfdoc_check appears before _run_builtin_tests and
        _run_builtin_lint in the release function.
        """
        source = inspect.getsource(_run_cmd_inner)
        selfdoc_pos = source.index("_run_selfdoc_check(")
        tests_pos = source.index("_run_builtin_tests(")
        lint_pos = source.index("_run_builtin_lint(")

        assert selfdoc_pos < tests_pos, (
            "_run_selfdoc_check must appear before _run_builtin_tests"
        )
        assert selfdoc_pos < lint_pos, (
            "_run_selfdoc_check must appear before _run_builtin_lint"
        )

    def test_selfdoc_still_before_pre_release_hook(self):
        """Selfdoc check must also run before the pre-release hook."""
        source = inspect.getsource(_run_cmd_inner)
        selfdoc_pos = source.index("_run_selfdoc_check(")
        pre_release_pos = source.index("pre_release_script")

        assert selfdoc_pos < pre_release_pos, (
            "_run_selfdoc_check must appear before pre-release hook"
        )

    def test_ordering_selfdoc_tests_lint(self):
        """The full ordering must be: selfdoc, tests, lint."""
        source = inspect.getsource(_run_cmd_inner)

        selfdoc_pos = source.index("_run_selfdoc_check(")
        tests_pos = source.index("_run_builtin_tests(")
        lint_pos = source.index("_run_builtin_lint(")

        assert selfdoc_pos < tests_pos < lint_pos, (
            f"Expected ordering selfdoc ({selfdoc_pos}) < tests ({tests_pos}) "
            f"< lint ({lint_pos})"
        )

    def test_selfdoc_after_pre_checks_hook(self):
        """Selfdoc check must run after the pre-checks hook."""
        source = inspect.getsource(_run_cmd_inner)
        pre_checks_pos = source.index("pre_checks_script")
        selfdoc_pos = source.index("_run_selfdoc_check(")

        assert pre_checks_pos < selfdoc_pos, (
            "pre-checks hook must appear before _run_selfdoc_check"
        )

    def test_selfdoc_gen_before_selfdoc_check(self):
        """Selfdoc gen must run before selfdoc check."""
        source = inspect.getsource(_run_cmd_inner)
        gen_pos = source.index("_run_selfdoc_gen(")
        check_pos = source.index("_run_selfdoc_check(")

        assert gen_pos < check_pos, (
            "_run_selfdoc_gen must appear before _run_selfdoc_check"
        )

    def test_selfdoc_gen_after_strictcli_schema_dump(self):
        """Selfdoc gen must run after strictcli schema dump."""
        source = inspect.getsource(_run_cmd_inner)
        schema_pos = source.index("_run_strictcli_schema_dump(")
        gen_pos = source.index("_run_selfdoc_gen(")

        assert schema_pos < gen_pos, (
            "_run_strictcli_schema_dump must appear before _run_selfdoc_gen"
        )

    def test_selfdoc_gen_before_tests(self):
        """Selfdoc gen must run before tests."""
        source = inspect.getsource(_run_cmd_inner)
        gen_pos = source.index("_run_selfdoc_gen(")
        tests_pos = source.index("_run_builtin_tests(")

        assert gen_pos < tests_pos, (
            "_run_selfdoc_gen must appear before _run_builtin_tests"
        )


class TestRefreshSelfdocHashes:
    """Tests for _refresh_selfdoc_hashes after version bump."""

    def test_hashes_added_when_dirty(self, tmp_path):
        """When selfdoc.json and hashes exist and hashes change, the file is added to files_to_commit."""
        # Set up selfdoc.json and hashes file
        (tmp_path / "selfdoc.json").write_text("{}")
        hashes_dir = tmp_path / ".selfdoc" / "hashes"
        hashes_dir.mkdir(parents=True)
        hashes_file = hashes_dir / "hashes.json"
        hashes_file.write_text('{"old": "hash"}')

        files_to_commit = []
        log = MagicMock()

        norm_hashes = os.path.normpath(str(hashes_file))

        def fake_subprocess_run(cmd, **kwargs):
            if cmd[0] == "selfdoc":
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd[0] == "git" and cmd[1] == "status":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=f" M {norm_hashes}\n",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch("rlsbl.commands.release.subprocess.run", side_effect=fake_subprocess_run),
        ):
            _refresh_selfdoc_hashes(files_to_commit, log, project_dir=str(tmp_path))

        assert norm_hashes in files_to_commit

    def test_hashes_not_added_when_clean(self, tmp_path):
        """When hashes.json is unchanged after selfdoc check, nothing is added."""
        (tmp_path / "selfdoc.json").write_text("{}")
        hashes_dir = tmp_path / ".selfdoc" / "hashes"
        hashes_dir.mkdir(parents=True)
        (hashes_dir / "hashes.json").write_text('{"old": "hash"}')

        files_to_commit = []
        log = MagicMock()

        def fake_subprocess_run(cmd, **kwargs):
            if cmd[0] == "selfdoc":
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd[0] == "git" and cmd[1] == "status":
                # Empty output means file is clean
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch("rlsbl.commands.release.subprocess.run", side_effect=fake_subprocess_run),
        ):
            _refresh_selfdoc_hashes(files_to_commit, log, project_dir=str(tmp_path))

        assert files_to_commit == []

    def test_skipped_when_no_selfdoc_json(self, tmp_path):
        """When selfdoc.json does not exist, the function is a no-op."""
        files_to_commit = []
        log = MagicMock()

        with patch("rlsbl.commands.release.subprocess.run") as mock_run:
            _refresh_selfdoc_hashes(files_to_commit, log, project_dir=str(tmp_path))

        mock_run.assert_not_called()
        assert files_to_commit == []

    def test_skipped_when_no_hashes_file(self, tmp_path):
        """When selfdoc.json exists but hashes.json does not, the function is a no-op."""
        (tmp_path / "selfdoc.json").write_text("{}")

        files_to_commit = []
        log = MagicMock()

        with patch("rlsbl.commands.release.subprocess.run") as mock_run:
            _refresh_selfdoc_hashes(files_to_commit, log, project_dir=str(tmp_path))

        mock_run.assert_not_called()
        assert files_to_commit == []

    def test_skipped_when_selfdoc_not_installed(self, tmp_path):
        """When selfdoc is not installed, the function is a no-op."""
        (tmp_path / "selfdoc.json").write_text("{}")
        hashes_dir = tmp_path / ".selfdoc" / "hashes"
        hashes_dir.mkdir(parents=True)
        (hashes_dir / "hashes.json").write_text('{"old": "hash"}')

        files_to_commit = []
        log = MagicMock()

        with (
            patch("rlsbl.commands.release.require_tool", return_value=None),
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            _refresh_selfdoc_hashes(files_to_commit, log, project_dir=str(tmp_path))

        mock_run.assert_not_called()
        assert files_to_commit == []

    def test_selfdoc_failure_is_non_fatal(self, tmp_path):
        """When selfdoc check fails, a warning is logged but no error is raised."""
        (tmp_path / "selfdoc.json").write_text("{}")
        hashes_dir = tmp_path / ".selfdoc" / "hashes"
        hashes_dir.mkdir(parents=True)
        (hashes_dir / "hashes.json").write_text('{"old": "hash"}')

        files_to_commit = []
        log = MagicMock()

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=OSError("selfdoc crashed")),
        ):
            # Should not raise
            _refresh_selfdoc_hashes(files_to_commit, log, project_dir=str(tmp_path))

        assert files_to_commit == []
        # Warning should be logged
        log_messages = [call[0][0] for call in log.call_args_list]
        assert any("failed" in msg for msg in log_messages)

    def test_duplicate_not_added(self, tmp_path):
        """When hashes.json is already in files_to_commit, it is not duplicated."""
        (tmp_path / "selfdoc.json").write_text("{}")
        hashes_dir = tmp_path / ".selfdoc" / "hashes"
        hashes_dir.mkdir(parents=True)
        hashes_file = hashes_dir / "hashes.json"
        hashes_file.write_text('{"old": "hash"}')

        norm_hashes = os.path.normpath(str(hashes_file))
        files_to_commit = [norm_hashes]
        log = MagicMock()

        def fake_subprocess_run(cmd, **kwargs):
            if cmd[0] == "selfdoc":
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd[0] == "git" and cmd[1] == "status":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=f" M {norm_hashes}\n",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch("rlsbl.commands.release.subprocess.run", side_effect=fake_subprocess_run),
        ):
            _refresh_selfdoc_hashes(files_to_commit, log, project_dir=str(tmp_path))

        assert files_to_commit.count(norm_hashes) == 1

    def test_uses_project_dir_when_provided(self, tmp_path):
        """When project_dir is provided (monorepo), it is used for paths and cwd."""
        project_dir = tmp_path / "subproject"
        project_dir.mkdir()
        (project_dir / "selfdoc.json").write_text("{}")
        hashes_dir = project_dir / ".selfdoc" / "hashes"
        hashes_dir.mkdir(parents=True)
        (hashes_dir / "hashes.json").write_text('{"old": "hash"}')

        files_to_commit = []
        log = MagicMock()

        selfdoc_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            if cmd[0] == "selfdoc":
                selfdoc_calls.append(kwargs.get("cwd"))
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd[0] == "git" and cmd[1] == "status":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch("rlsbl.commands.release.subprocess.run", side_effect=fake_subprocess_run),
        ):
            _refresh_selfdoc_hashes(
                files_to_commit, log,
                project_dir=str(project_dir),
            )

        # selfdoc check should have been called with project_dir as cwd
        assert len(selfdoc_calls) == 1
        assert selfdoc_calls[0] == str(project_dir)

    def test_refresh_runs_after_lockfile_sync_in_mutating(self):
        """_refresh_selfdoc_hashes is called after _sync_lockfiles in _run_release_mutating."""
        from rlsbl.commands.release import _run_release_mutating

        source = inspect.getsource(_run_release_mutating)
        lockfile_pos = source.index("_sync_lockfiles(")
        refresh_pos = source.index("_refresh_selfdoc_hashes(")
        commit_pos = source.index("commit_files(commit_msg")

        assert lockfile_pos < refresh_pos, (
            "_refresh_selfdoc_hashes must appear after _sync_lockfiles"
        )
        assert refresh_pos < commit_pos, (
            "_refresh_selfdoc_hashes must appear before commit_files"
        )
