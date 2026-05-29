"""Tests for pre-release and post-release hook output streaming."""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.release_file import ReleaseConfig
from rlsbl.utils import get_hook_timeout
from rlsbl.utils import run as real_run


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


def _setup_project(tmp_path, hook_name, hook_body):
    """Create a minimal project with a hook script.

    Returns the path to the hook script.
    """
    # package.json so npm registry is detected
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    # Changelog with bumped version entry
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release with improvements.\n"
    )
    # JSONL changelog
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    # Config with required private key
    (tmp_path / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
    )
    # Hook script
    hooks_dir = tmp_path / ".rlsbl" / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_script = hooks_dir / hook_name
    hook_script.write_text(hook_body)
    hook_script.chmod(0o755)
    return str(hook_script)


class TestPreReleaseHookOutput:
    """Tests for pre-release hook streaming and error handling."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_hook_streams_output(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
    ):
        """A successful hook is called via subprocess.run without capture_output."""
        _setup_project(tmp_project, "pre-release.sh", "#!/bin/bash\necho hello\n")
        # mock_run side effects: fetch, rev-list, tag -l current, tag -l bumped
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            # dry-run to avoid needing full release mocks
            run_cmd(_rc(), {"dry-run": True, "quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            # Verify subprocess.run was called for the hook
            assert mock_sp.run.call_count == 1
            call_args = mock_sp.run.call_args
            # Should NOT have capture_output in kwargs
            assert "capture_output" not in call_args.kwargs
            # Should have check=True
            assert call_args.kwargs.get("check") is True

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_hook_exit_code_in_error_message(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """A hook exiting with code 2 produces an error mentioning 'exited with code 2'."""
        _setup_project(tmp_project, "pre-release.sh", "#!/bin/bash\nexit 2\n")
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.side_effect = subprocess.CalledProcessError(2, "bash")
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            with pytest.raises(SystemExit) as exc_info:
                run_cmd(_rc(), {"quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "exited with code 2" in captured.err

    def test_missing_hook_is_skipped(self, tmp_project):
        """When no pre-release hook exists, release does not error on it."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
        )
        (tmp_project / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 1.0.1\n\nPatch release.\n"
        )
        changes_dir = tmp_project / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")
        (tmp_project / ".rlsbl" / "config.json").write_text(
            json.dumps({"private": False}) + "\n"
        )
        # No .rlsbl/hooks/pre-release.sh created

        with (
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run") as mock_run,
            patch("rlsbl.commands.release.commit_files", return_value=True),
            patch("rlsbl.commands.release.get_current_branch", return_value="main"),
            patch("rlsbl.commands.release.is_clean_tree", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.generate_changelog"),
            patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}}),
            patch("rlsbl.commands.release.subprocess") as mock_sp,
        ):
            mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            # dry-run: should complete without subprocess.run being called for hooks
            run_cmd(_rc(), {"dry-run": True, "quiet": True, "yes": True}, project_root=".", monorepo_root=None)
            mock_sp.run.assert_not_called()


class TestPostReleaseHookOutput:
    """Tests for post-release hook streaming and error handling."""

    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_failed_hook_is_non_fatal(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        _push,
        _should_tag,
        _deploy,
        tmp_project,
        capsys,
    ):
        """A failing post-release hook prints a warning but does not abort."""
        _setup_project(tmp_project, "post-release.sh", "#!/bin/bash\nexit 3\n")

        def fake_run(cmd, args=None, timeout=120, env=None):
            """Return sensible defaults for all run() calls in the release flow."""
            full = [cmd] + (args or [])
            joined = " ".join(full)
            if "rev-list" in joined:
                return "0"
            if "tag" in joined and "-l" in joined:
                # First tag check (current version): exists; second (bumped): does not
                if "v1.0.0" in joined:
                    return "v1.0.0"
                return ""
            if "rev-parse" in joined:
                return "abc123"
            if "status --porcelain" in joined:
                return ""
            if "diff --name-only" in joined:
                return ""
            return ""

        def fake_subprocess_run(cmd, **kwargs):
            script = cmd[1] if len(cmd) > 1 else ""
            if "post-release.sh" in script:
                raise subprocess.CalledProcessError(3, "bash")
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.run", side_effect=fake_run),
            patch("rlsbl.commands.release.subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = fake_subprocess_run
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            # Should NOT raise -- post-release hook failure is non-fatal
            run_cmd(_rc(), {"quiet": True, "yes": True}, project_root=".", monorepo_root=None)

        captured = capsys.readouterr()
        assert "exited with code 3" in captured.err


class TestWatchSHABeforePostHook:
    """Test that the watch SHA is captured before post-release hooks run."""

    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_watch_sha_is_pre_hook_commit(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        _push,
        _should_tag,
        _deploy,
        tmp_project,
        capsys,
    ):
        """Watch SHA should be the pushed commit, not HEAD after post-release hook."""
        _setup_project(tmp_project, "post-release.sh", "#!/bin/bash\necho ok\n")

        pre_hook_sha = "aaa111"
        post_hook_sha = "bbb222"
        rev_parse_call_count = 0

        def fake_run(cmd, args=None, timeout=120, env=None):
            nonlocal rev_parse_call_count
            full = [cmd] + (args or [])
            joined = " ".join(full)
            if "rev-list" in joined:
                return "0"
            if "tag" in joined and "-l" in joined:
                if "v1.0.0" in joined:
                    return "v1.0.0"
                return ""
            if "rev-parse" in joined and "--show-toplevel" in joined:
                return "/tmp/fake-repo"
            if "rev-parse" in joined:
                # 1st call: pre_release_sha capture (before mutations)
                # 2nd call: pushed_sha capture (after push, before post-release hook)
                # Any subsequent call would return post-hook SHA
                rev_parse_call_count += 1
                if rev_parse_call_count <= 2:
                    return pre_hook_sha
                return post_hook_sha
            if "status --porcelain" in joined:
                return ""
            if "diff --name-only" in joined:
                return ""
            return ""

        def fake_subprocess_run(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch("rlsbl.commands.release.run", side_effect=fake_run),
            patch("rlsbl.commands.release.subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = fake_subprocess_run
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(), {"yes": True}, project_root=".", monorepo_root=None)

        captured = capsys.readouterr()
        # The watch message must use the SHA captured before the post-release hook
        assert f"rlsbl watch {pre_hook_sha}" in captured.out
        assert post_hook_sha not in captured.out


class TestHookTimeout:
    """Tests for RLSBL_HOOK_TIMEOUT integration with hooks."""

    def test_get_hook_timeout_no_env(self, monkeypatch):
        monkeypatch.delenv("RLSBL_HOOK_TIMEOUT", raising=False)
        assert get_hook_timeout() is None

    def test_get_hook_timeout_valid(self, monkeypatch):
        monkeypatch.setenv("RLSBL_HOOK_TIMEOUT", "60")
        assert get_hook_timeout() == 60

    def test_get_hook_timeout_invalid_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("RLSBL_HOOK_TIMEOUT", "abc")
        assert get_hook_timeout() is None
        captured = capsys.readouterr()
        assert "invalid RLSBL_HOOK_TIMEOUT" in captured.err

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_pre_release_timeout_message_includes_seconds(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        monkeypatch,
        capsys,
    ):
        """When a pre-release hook times out, the error message includes the configured seconds."""
        monkeypatch.setenv("RLSBL_HOOK_TIMEOUT", "45")
        _setup_project(tmp_project, "pre-release.sh", "#!/bin/bash\nsleep 999\n")
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.side_effect = subprocess.TimeoutExpired("bash", 45)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            with pytest.raises(SystemExit) as exc_info:
                run_cmd(_rc(), {"quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "timed out after 45s" in captured.err


class TestHookCwdStandalone:
    """Tests that hook subprocess.run calls receive cwd=None in standalone mode."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_pre_checks_hook_cwd_none_standalone(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
    ):
        """In standalone mode, pre-checks hook subprocess.run gets cwd=None."""
        _setup_project(tmp_project, "pre-checks.sh", "#!/bin/bash\necho ok\n")
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(), {"dry-run": True, "quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            assert mock_sp.run.call_count >= 1
            # The pre-checks hook call should have cwd=None
            pre_checks_call = mock_sp.run.call_args_list[0]
            assert pre_checks_call.kwargs.get("cwd") is None

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_pre_release_hook_cwd_none_standalone(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
    ):
        """In standalone mode, pre-release hook subprocess.run gets cwd=None."""
        _setup_project(tmp_project, "pre-release.sh", "#!/bin/bash\necho ok\n")
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(), {"dry-run": True, "quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            assert mock_sp.run.call_count >= 1
            # The pre-release hook call should have cwd=None
            pre_release_call = mock_sp.run.call_args_list[0]
            assert pre_release_call.kwargs.get("cwd") is None

    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_post_release_hook_cwd_none_standalone(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        _push,
        _should_tag,
        _deploy,
        tmp_project,
    ):
        """In standalone mode, post-release hook subprocess.run gets cwd=None."""
        _setup_project(tmp_project, "post-release.sh", "#!/bin/bash\necho ok\n")

        def fake_run(cmd, args=None, timeout=120, env=None):
            full = [cmd] + (args or [])
            joined = " ".join(full)
            if "rev-list" in joined:
                return "0"
            if "tag" in joined and "-l" in joined:
                if "v1.0.0" in joined:
                    return "v1.0.0"
                return ""
            if "rev-parse" in joined:
                return "abc123"
            if "status --porcelain" in joined:
                return ""
            if "diff --name-only" in joined:
                return ""
            return ""

        with (
            patch("rlsbl.commands.release.run", side_effect=fake_run),
            patch("rlsbl.commands.release.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(), {"quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            # Find the post-release hook call (it's the one with post-release.sh in args)
            post_release_calls = [
                c for c in mock_sp.run.call_args_list
                if len(c[0][0]) > 1 and "post-release.sh" in c[0][0][1]
            ]
            assert len(post_release_calls) == 1
            assert post_release_calls[0].kwargs.get("cwd") is None


class TestHookCwdMonorepo:
    """Tests that hook subprocess.run calls receive cwd=project_dir in monorepo mode."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release._run_builtin_tests", return_value=True)
    @patch("rlsbl.commands.release._run_builtin_lint", return_value=True)
    def test_pre_checks_hook_cwd_monorepo(
        self,
        _lint,
        _tests,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        monorepo_fixture,
    ):
        """In monorepo mode, pre-checks hook subprocess.run gets cwd=project_dir."""
        ns = monorepo_fixture
        # Create pre-checks hook in the python subproject
        hooks_dir = ns.python_dir / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "pre-checks.sh").write_text("#!/bin/bash\necho ok\n")
        (hooks_dir / "pre-checks.sh").chmod(0o755)
        # CHANGELOG.md must exist for extract_changelog_entry after generate_changelog mock
        (ns.python_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.1\n\nPatch release.\n"
        )

        # chdir to the python subproject (run_cmd detects monorepo from here)
        os.chdir(ns.python_dir)

        mock_run.side_effect = ["", "0", "mypylib@v0.1.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(include=["pypi"]), {"dry-run": True, "quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            # Find the pre-checks hook call
            pre_checks_calls = [
                c for c in mock_sp.run.call_args_list
                if len(c[0][0]) > 1 and "pre-checks.sh" in c[0][0][1]
            ]
            assert len(pre_checks_calls) == 1
            assert pre_checks_calls[0].kwargs.get("cwd") == str(ns.python_dir)
            # Script path must be absolute (not relative, which would double-prefix)
            script_path = pre_checks_calls[0][0][0][1]
            assert os.path.isabs(script_path)
            assert script_path.endswith(".rlsbl/hooks/pre-checks.sh")

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release._run_builtin_tests", return_value=True)
    @patch("rlsbl.commands.release._run_builtin_lint", return_value=True)
    def test_pre_release_hook_cwd_monorepo(
        self,
        _lint,
        _tests,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        monorepo_fixture,
    ):
        """In monorepo mode, pre-release hook subprocess.run gets cwd=project_dir."""
        ns = monorepo_fixture
        hooks_dir = ns.python_dir / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "pre-release.sh").write_text("#!/bin/bash\necho ok\n")
        (hooks_dir / "pre-release.sh").chmod(0o755)
        (ns.python_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.1\n\nPatch release.\n"
        )

        os.chdir(ns.python_dir)

        mock_run.side_effect = ["", "0", "mypylib@v0.1.0", "", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(include=["pypi"]), {"dry-run": True, "quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            # Find the pre-release hook call
            pre_release_calls = [
                c for c in mock_sp.run.call_args_list
                if len(c[0][0]) > 1 and "pre-release.sh" in c[0][0][1]
            ]
            assert len(pre_release_calls) == 1
            assert pre_release_calls[0].kwargs.get("cwd") == str(ns.python_dir)
            # Script path must be absolute (not relative, which would double-prefix)
            script_path = pre_release_calls[0][0][0][1]
            assert os.path.isabs(script_path)
            assert script_path.endswith(".rlsbl/hooks/pre-release.sh")

    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release._run_builtin_tests", return_value=True)
    @patch("rlsbl.commands.release._run_builtin_lint", return_value=True)
    def test_post_release_hook_cwd_monorepo(
        self,
        _lint,
        _tests,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        _push,
        _should_tag,
        _deploy,
        monorepo_fixture,
    ):
        """In monorepo mode, post-release hook subprocess.run gets cwd=project_dir."""
        ns = monorepo_fixture
        hooks_dir = ns.python_dir / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "post-release.sh").write_text("#!/bin/bash\necho ok\n")
        (hooks_dir / "post-release.sh").chmod(0o755)
        (ns.python_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.1\n\nPatch release.\n"
        )

        os.chdir(ns.python_dir)

        def fake_run(cmd, args=None, timeout=120, env=None):
            full = [cmd] + (args or [])
            joined = " ".join(full)
            if "rev-list" in joined:
                return "0"
            if "tag" in joined and "-l" in joined:
                if "mypylib@v0.1.0" in joined:
                    return "mypylib@v0.1.0"
                return ""
            if "rev-parse" in joined:
                return "abc123"
            if "status --porcelain" in joined:
                return ""
            if "diff --name-only" in joined:
                return ""
            return ""

        with (
            patch("rlsbl.commands.release.run", side_effect=fake_run),
            patch("rlsbl.commands.release.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(include=["pypi"]), {"quiet": True, "yes": True}, project_root=".", monorepo_root=None)

            # Find the post-release hook call
            post_release_calls = [
                c for c in mock_sp.run.call_args_list
                if len(c[0][0]) > 1 and "post-release.sh" in c[0][0][1]
            ]
            assert len(post_release_calls) == 1
            assert post_release_calls[0].kwargs.get("cwd") == str(ns.python_dir)
            # Script path must be absolute (not relative, which would double-prefix)
            script_path = post_release_calls[0][0][0][1]
            assert os.path.isabs(script_path)
            assert script_path.endswith(".rlsbl/hooks/post-release.sh")


def _git(repo, *args):
    """Run a git command in the given repo directory."""
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(repo):
    """Return the HEAD commit SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_show_files(repo, ref="HEAD"):
    """Return set of files changed in a commit."""
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", ref],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.strip().splitlines())


def _setup_releasable_project_with_hook(repo, hook_name, hook_body):
    """Create a git repo with a tagged v1.0.0 release, an unreleased commit,
    and a hook script. Returns the repo path."""
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    # Initial release: package.json @ 1.0.0
    (repo / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.0\n\n- Initial release.\n"
    )
    changes_dir = repo / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
    )
    _git(repo, "add", "package.json", "CHANGELOG.md", ".rlsbl/changes/unreleased.jsonl", ".rlsbl/config.json")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v1.0.0")

    # Make an unreleased feature commit
    (repo / "feature.txt").write_text("new feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(repo)

    # Cover it with a JSONL entry
    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**Add feature.** New feature.",
        "type": "feature",
    }
    (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(
        repo, "commit", "-q", "-m", "changelog: add feature entry",
        "--trailer", "Autogenerated: true",
    )

    # Install the hook
    hooks_dir = repo / ".rlsbl" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / hook_name
    hook.write_text(hook_body)
    hook.chmod(0o755)
    _git(repo, "add", f".rlsbl/hooks/{hook_name}")
    _git(repo, "commit", "-q", "-m", f"add {hook_name}")

    # Cover the hook commit
    hook_sha = _git_head(repo)
    with open(changes_dir / "unreleased.jsonl", "a") as f:
        f.write(json.dumps({"commits": [hook_sha], "user_facing": False}) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(
        repo, "commit", "-q", "-m", "changelog: cover hook commit",
        "--trailer", "Autogenerated: true",
    )


def _fake_run_intercepting_remote(cmd, args=None, timeout=120, env=None):
    """Intercept gh and git-push calls; let everything else through."""
    if cmd == "gh":
        return ""
    if cmd == "git" and args and args[0] == "push":
        return ""
    return real_run(cmd, args=args, timeout=timeout, env=env)


class TestHookGeneratedFiles:
    """Tests that files created or modified by pre-release hooks are
    included in the release commit."""

    def test_hook_modified_file_included_in_commit(self, tmp_project, capsys):
        """A pre-release hook that modifies an existing tracked file should
        have that file included in the release commit."""
        # Create a tracked file that the hook will modify
        _setup_releasable_project_with_hook(
            tmp_project,
            "pre-release.sh",
            "#!/bin/bash\necho 'updated by hook' > schema.json\n",
        )
        # Create and commit schema.json so the hook modifies a tracked file
        (tmp_project / "schema.json").write_text("original\n")
        _git(tmp_project, "add", "schema.json")
        _git(tmp_project, "commit", "-q", "-m", "add schema.json")
        schema_sha = _git_head(tmp_project)
        changes_dir = tmp_project / ".rlsbl" / "changes"
        with open(changes_dir / "unreleased.jsonl", "a") as f:
            f.write(json.dumps({"commits": [schema_sha], "user_facing": False}) + "\n")
        _git(tmp_project, "add", ".rlsbl/changes/unreleased.jsonl")
        _git(
            tmp_project, "commit", "-q", "-m", "changelog: cover schema commit",
            "--trailer", "Autogenerated: true",
        )

        from rlsbl.commands.release import run_cmd

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run", side_effect=_fake_run_intercepting_remote),
        ):
            run_cmd(_rc(), {"yes": True}, project_root=".", monorepo_root=None)

        # Verify the release commit includes schema.json
        # The release commit is HEAD~1 (before the finalize commit)
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=str(tmp_project),
            capture_output=True, text=True, check=True,
        )
        # Find the release commit (message is "v1.0.1")
        release_sha = None
        for line in result.stdout.strip().splitlines():
            if "v1.0.1" in line and "finalize" not in line:
                release_sha = line.split()[0]
                break
        assert release_sha is not None, (
            f"could not find release commit in log:\n{result.stdout}"
        )

        files_in_release_commit = _git_show_files(tmp_project, release_sha)
        assert "schema.json" in files_in_release_commit, (
            f"schema.json should be in the release commit, got: {files_in_release_commit}"
        )

        captured = capsys.readouterr()
        assert "Including hook-generated file: schema.json" in captured.out

    def test_hook_created_new_file_included_in_commit(self, tmp_project, capsys):
        """A pre-checks hook that creates a new untracked file should have
        that file included in the release commit."""
        _setup_releasable_project_with_hook(
            tmp_project,
            "pre-checks.sh",
            "#!/bin/bash\necho 'generated' > generated_output.txt\n",
        )

        from rlsbl.commands.release import run_cmd

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run", side_effect=_fake_run_intercepting_remote),
        ):
            run_cmd(_rc(), {"yes": True}, project_root=".", monorepo_root=None)

        # Find the release commit
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=str(tmp_project),
            capture_output=True, text=True, check=True,
        )
        release_sha = None
        for line in result.stdout.strip().splitlines():
            if "v1.0.1" in line and "finalize" not in line:
                release_sha = line.split()[0]
                break
        assert release_sha is not None, (
            f"could not find release commit in log:\n{result.stdout}"
        )

        files_in_release_commit = _git_show_files(tmp_project, release_sha)
        assert "generated_output.txt" in files_in_release_commit, (
            f"generated_output.txt should be in the release commit, got: {files_in_release_commit}"
        )

        captured = capsys.readouterr()
        assert "Including hook-generated file: generated_output.txt" in captured.out

    def test_no_hooks_no_extra_files(self, tmp_project):
        """When no hooks exist, no extra hook-generated files are included."""
        # Set up a project WITHOUT any hooks
        _git(tmp_project, "init", "-q", "-b", "main")
        _git(tmp_project, "config", "user.email", "test@test.local")
        _git(tmp_project, "config", "user.name", "Test")

        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
        )
        (tmp_project / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 1.0.0\n\n- Initial release.\n"
        )
        changes_dir = tmp_project / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")
        (tmp_project / ".rlsbl" / "config.json").write_text(
            json.dumps({"private": False}) + "\n"
        )
        _git(tmp_project, "add", "package.json", "CHANGELOG.md", ".rlsbl/changes/unreleased.jsonl", ".rlsbl/config.json")
        _git(tmp_project, "commit", "-q", "-m", "initial")
        _git(tmp_project, "tag", "v1.0.0")

        (tmp_project / "feature.txt").write_text("new feature\n")
        _git(tmp_project, "add", "feature.txt")
        _git(tmp_project, "commit", "-q", "-m", "add feature")
        feature_sha = _git_head(tmp_project)
        entry = {
            "commits": [feature_sha],
            "user_facing": True,
            "description": "**Feature.** Something new.",
            "type": "feature",
        }
        (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
        _git(tmp_project, "add", ".rlsbl/changes/unreleased.jsonl")
        _git(
            tmp_project, "commit", "-q", "-m", "changelog: add feature",
            "--trailer", "Autogenerated: true",
        )

        from rlsbl.commands.release import run_cmd

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run", side_effect=_fake_run_intercepting_remote),
        ):
            run_cmd(_rc(), {"yes": True, "quiet": True}, project_root=".", monorepo_root=None)

        # Verify that the release completed
        pkg = json.loads((tmp_project / "package.json").read_text())
        assert pkg["version"] == "1.0.1"

        # Verify no unexpected files snuck into the release commit
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=str(tmp_project),
            capture_output=True, text=True, check=True,
        )
        release_sha = None
        for line in result.stdout.strip().splitlines():
            if "v1.0.1" in line and "finalize" not in line:
                release_sha = line.split()[0]
                break
        assert release_sha is not None

        files_in_release_commit = _git_show_files(tmp_project, release_sha)
        # Only expected files: package.json, CHANGELOG.md, .rlsbl/version
        for f in files_in_release_commit:
            assert f in (
                "package.json", "CHANGELOG.md", ".rlsbl/version",
            ), f"unexpected file in release commit: {f}"

    def test_pre_existing_dirty_file_not_included(self, tmp_project):
        """A file that was dirty BEFORE hooks ran should NOT be treated as
        hook-generated, even if it's still dirty after hooks complete."""
        _setup_releasable_project_with_hook(
            tmp_project,
            "pre-release.sh",
            "#!/bin/bash\necho 'hook ran'\n",  # Hook does NOT touch the dirty file
        )

        # Create and commit a file, then dirty it before release
        (tmp_project / "notes.txt").write_text("original\n")
        _git(tmp_project, "add", "notes.txt")
        _git(tmp_project, "commit", "-q", "-m", "add notes")
        notes_sha = _git_head(tmp_project)
        changes_dir = tmp_project / ".rlsbl" / "changes"
        with open(changes_dir / "unreleased.jsonl", "a") as f:
            f.write(json.dumps({"commits": [notes_sha], "user_facing": False}) + "\n")
        _git(tmp_project, "add", ".rlsbl/changes/unreleased.jsonl")
        _git(
            tmp_project, "commit", "-q", "-m", "changelog: cover notes commit",
            "--trailer", "Autogenerated: true",
        )

        # Dirty the file BEFORE the release starts
        (tmp_project / "notes.txt").write_text("modified before release\n")

        from rlsbl.commands.release import run_cmd

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run", side_effect=_fake_run_intercepting_remote),
        ):
            run_cmd(
                _rc(),
                {"yes": True, "quiet": True, "allow-dirty": True},
            
                project_root=".",
                monorepo_root=None,
)

        # Verify the release completed
        pkg = json.loads((tmp_project / "package.json").read_text())
        assert pkg["version"] == "1.0.1"

        # notes.txt should NOT be in the release commit (it was pre-existing dirty)
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=str(tmp_project),
            capture_output=True, text=True, check=True,
        )
        release_sha = None
        for line in result.stdout.strip().splitlines():
            if "v1.0.1" in line and "finalize" not in line:
                release_sha = line.split()[0]
                break
        assert release_sha is not None

        files_in_release_commit = _git_show_files(tmp_project, release_sha)
        assert "notes.txt" not in files_in_release_commit, (
            "notes.txt was dirty before hooks and should NOT be in the release commit"
        )
