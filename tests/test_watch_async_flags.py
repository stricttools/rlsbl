"""Tests for the --watch-async flag surface on release commands.

--watch / --watch-async form a strictcli MutexGroup (exactly one required),
so every pre-existing invocation (--watch / --no-watch) still parses, and
--watch-async / --no-watch-async are new. Semantics:

- --watch: blocking in-process watch (unchanged)
- --watch-async: spawn a detached watcher, return immediately
- --no-watch / --no-watch-async: no watching, print the manual hint

Covers all four commands that carry the pair: release run, release resume,
release retry, monorepo release run -- plus build_release_flags threading
and the execute/batch/retry call sites.
"""

import json
import os
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rlsbl import app
from rlsbl.commands.release.shared import build_release_flags


# ---------------------------------------------------------------------------
# Parse-level mutex semantics (no project setup needed: mutex violations are
# raised at parse time, before any handler code runs)
# ---------------------------------------------------------------------------

RELEASE_ARGV = {
    "run": ["release", "run"],
    "resume": ["release", "resume"],
    "retry": ["release", "retry"],
    "mono-run": ["monorepo", "release", "run"],
}


class TestMutexParsing:

    @pytest.mark.parametrize("argv", RELEASE_ARGV.values(), ids=RELEASE_ARGV.keys())
    def test_watch_and_watch_async_are_mutually_exclusive(self, argv):
        result = app.test(argv + ["--watch", "--watch-async"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.stderr
        assert "--watch" in result.stderr and "--watch-async" in result.stderr

    @pytest.mark.parametrize("argv", RELEASE_ARGV.values(), ids=RELEASE_ARGV.keys())
    def test_one_of_the_pair_is_required(self, argv):
        result = app.test(argv)
        assert result.exit_code == 1
        assert "one of --watch, --watch-async is required" in result.stderr

    @pytest.mark.parametrize("argv", RELEASE_ARGV.values(), ids=RELEASE_ARGV.keys())
    @pytest.mark.parametrize("flag", ["--watch", "--no-watch", "--watch-async",
                                      "--no-watch-async"])
    def test_each_single_form_passes_the_mutex(self, argv, flag):
        """Every single member of the pair satisfies the group: any failure
        must come from the handler (missing project etc.), never the parser."""
        result = app.test(argv + [flag])
        combined = (result.stdout or "") + (result.stderr or "")
        assert "mutually exclusive" not in combined
        assert "one of --watch, --watch-async is required" not in combined


# ---------------------------------------------------------------------------
# build_release_flags threading
# ---------------------------------------------------------------------------


class TestBuildReleaseFlagsWatchAsync:

    def test_watch_async_key_present_and_default_false(self):
        result = build_release_flags(False, False, False, False)
        assert result["watch-async"] is False

    def test_watch_async_true(self):
        result = build_release_flags(False, False, False, False, watch_async=True)
        assert result["watch-async"] is True
        assert result["watch"] is False

    def test_watch_async_none_coerced_to_false(self):
        """Unset mutex flags arrive as None; the flags dict carries bools."""
        result = build_release_flags(False, False, False, False,
                                     watch=None, watch_async=None)
        assert result["watch"] is False
        assert result["watch-async"] is False


# ---------------------------------------------------------------------------
# Handler threading: argv -> flags dict, per command
# ---------------------------------------------------------------------------


def _setup_release_run_project(tmp_path):
    """Minimal npm project with a release file, enough to reach run_cmd."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (tmp_path / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": ["npm"], "publish_mode": "ci"}) + "\n"
    )
    releases_dir = tmp_path / ".rlsbl" / "releases"
    releases_dir.mkdir(parents=True)
    (releases_dir / "unreleased.toml").write_text(
        'bump = "patch"\ndescription = "Test release."\n'
        'include = ["npm"]\nexclude = []\n'
    )


class TestReleaseRunHandler:

    @pytest.mark.parametrize("flag,watch,watch_async", [
        ("--watch", True, False),
        ("--no-watch", False, False),
        ("--watch-async", False, True),
        ("--no-watch-async", False, False),  # ≡ --no-watch
    ])
    def test_flags_thread_into_run_cmd(self, tmp_project, flag, watch, watch_async):
        _setup_release_run_project(tmp_project)

        with patch("rlsbl.commands.release.run_cmd") as mock_run:
            result = app.test(
                ["release", "run", "--yes", "--no-allow-dirty", flag]
            )

        assert result.exit_code == 0, result.stderr
        flags = mock_run.call_args[0][1]
        assert flags["watch"] is watch
        assert flags["watch-async"] is watch_async


def _setup_resume_project(tmp_path):
    (tmp_path / ".rlsbl").mkdir()
    releases_dir = tmp_path / ".rlsbl" / "releases"
    releases_dir.mkdir(parents=True)
    (releases_dir / "in-progress.json").write_text(json.dumps({
        "new_version": "1.0.1",
        "branch": "main",
        "pre_release_sha": "",
        "completed_steps": [],
    }))


class TestReleaseResumeHandler:

    @pytest.mark.parametrize("flag,watch,watch_async", [
        ("--watch", True, False),
        ("--watch-async", False, True),
        ("--no-watch-async", False, False),
    ])
    def test_flags_thread_into_resume_cmd(self, tmp_project, flag, watch, watch_async):
        _setup_resume_project(tmp_project)

        with patch("rlsbl.utils.get_current_branch", return_value="main"), \
             patch("rlsbl.commands.release.resume_cmd") as mock_resume:
            result = app.test(["release", "resume", "--yes", flag])

        assert result.exit_code == 0, result.stderr
        flags = mock_resume.call_args[0][1]
        assert flags["watch"] is watch
        assert flags["watch-async"] is watch_async


class TestReleaseRetryHandler:

    @pytest.mark.parametrize("flag,watch,watch_async", [
        ("--watch", True, False),
        ("--watch-async", False, True),
        ("--no-watch-async", False, False),
    ])
    def test_flags_thread_into_retry_run_cmd(self, tmp_project, flag, watch,
                                             watch_async):
        (tmp_project / ".rlsbl").mkdir()

        with patch("rlsbl.commands.release_retry.run_cmd") as mock_retry:
            result = app.test(["release", "retry", "--yes", flag])

        assert result.exit_code == 0, result.stderr
        flags = mock_retry.call_args[0][1]
        assert flags["watch"] is watch
        assert flags["watch-async"] is watch_async


class TestMonorepoReleaseRunHandler:

    @pytest.mark.parametrize("flag,watch,watch_async", [
        ("--watch", True, False),
        ("--watch-async", False, True),
        ("--no-watch-async", False, False),
    ])
    def test_flags_thread_into_batch_release(self, tmp_project, flag, watch,
                                             watch_async):
        (tmp_project / ".rlsbl-monorepo").mkdir()

        with patch("rlsbl.commands.monorepo._cmd_batch_release") as mock_batch:
            result = app.test(
                ["monorepo", "release", "run", "--yes", "--no-allow-dirty", flag]
            )

        assert result.exit_code == 0, result.stderr
        flags = mock_batch.call_args[0][0]
        assert flags["watch"] is watch
        assert flags["watch-async"] is watch_async


# ---------------------------------------------------------------------------
# Call site: execute.py success epilogue (real-git harness, modeled on
# test_watch_cleanup.py)
# ---------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=str(repo), check=True,
                   capture_output=True, text=True)


def _git_head(repo):
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _setup_releasable_npm_project(repo):
    """Create an npm project at v1.0.0 with one unreleased commit."""
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    (repo / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "<!-- Generated by rlsbl from .rlsbl/changes/ -- do not edit -->\n\n"
        "# Changelog\n\n## 1.0.0\n\n- Initial release.\n"
    )
    changes_dir = repo / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n"
    )
    _git(repo, "add", "package.json", "CHANGELOG.md",
         ".rlsbl/changes/unreleased.jsonl", ".rlsbl/config.json")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v1.0.0")

    (repo / "feature.txt").write_text("new feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(repo)

    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**New feature.** A shiny new thing.",
        "type": "feature",
    }
    (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: add feature entry",
         "--trailer", "Autogenerated: true")

    releases_dir = repo / ".rlsbl" / "releases"
    releases_dir.mkdir(parents=True)
    (releases_dir / "unreleased.toml").write_text(
        'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
    )
    _git(repo, "add", ".rlsbl/releases/unreleased.toml")
    _git(repo, "commit", "-q", "-m", "add release file",
         "--trailer", "Autogenerated: true")


class TestExecuteEpilogueWatchAsync:

    def test_watch_async_spawns_detached_watcher(self, tmp_project, capsys):
        """With watch-async, the epilogue spawns a detached watcher for the
        pushed SHA, does not run the blocking watch, and returns."""
        from rlsbl.commands.release import run_cmd
        from rlsbl.context import ProjectContext
        from rlsbl.release_file import ReleaseConfig
        from rlsbl.utils import run as real_run

        _setup_releasable_npm_project(tmp_project)

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            if cmd == "gh":
                return ""
            if cmd == "git" and args and args[0] in ("push", "fetch"):
                return ""
            if (cmd == "git" and args and args[:2] == ["rev-list", "--count"]
                    and any("origin/" in a for a in args)):
                return "0"
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        ctx = ProjectContext(project_root=Path(str(tmp_project)),
                             workspace_root=None,
                             config={"publish_mode": "ci", "pipelines": {}})

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run_gh", return_value=""),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
            patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
            patch("rlsbl.commands.watch.spawn_detached_watcher") as mock_spawn,
            patch("rlsbl.commands.watch.run_cmd") as mock_watch,
        ):
            run_cmd(
                ReleaseConfig(bump="patch", include=["npm"], exclude=[]),
                {"yes": True, "quiet": True, "watch": False, "watch-async": True},
                ctx=ctx,
            )

        mock_watch.assert_not_called()
        mock_spawn.assert_called_once()
        pushed_sha = mock_spawn.call_args[0][0]
        assert pushed_sha == _git_head(tmp_project)
        # No manual hint when a watcher was spawned
        captured = capsys.readouterr()
        assert "Watch CI: rlsbl watch" not in captured.out + captured.err

    def test_watch_async_conflict_does_not_fail_release(self, tmp_project, capsys):
        """A live watcher for the pushed SHA must not turn a successful
        release into a failure: the epilogue runs AFTER release state
        cleanup, so an exit(1) there would misreport a release that
        actually completed. Real spawn_detached_watcher, simulated live
        conflict: run_cmd must return normally with a loud warning."""
        from rlsbl.commands.release import run_cmd
        from rlsbl.context import ProjectContext
        from rlsbl.release_file import ReleaseConfig
        from rlsbl.utils import run as real_run

        _setup_releasable_npm_project(tmp_project)

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            if cmd == "gh":
                return ""
            if cmd == "git" and args and args[0] in ("push", "fetch"):
                return ""
            if (cmd == "git" and args and args[:2] == ["rev-list", "--count"]
                    and any("origin/" in a for a in args)):
                return "0"
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        ctx = ProjectContext(project_root=Path(str(tmp_project)),
                             workspace_root=None,
                             config={"publish_mode": "ci", "pipelines": {}})

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run_gh", return_value=""),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
            patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
            # Simulate a live watcher for every pidfile lookup. Patch the
            # watch module's subprocess binding (not subprocess.Popen
            # globally, which would break the release's own git calls).
            patch("rlsbl.commands.watch._read_pidfile", return_value=12345),
            patch("rlsbl.commands.watch._pid_alive", return_value=True),
            patch("rlsbl.commands.watch.subprocess") as mock_subprocess,
        ):
            # Must return normally -- a SystemExit here is the bug
            run_cmd(
                ReleaseConfig(bump="patch", include=["npm"], exclude=[]),
                {"yes": True, "quiet": True, "watch": False, "watch-async": True},
                ctx=ctx,
            )

        mock_subprocess.Popen.assert_not_called()
        err = capsys.readouterr().err
        assert "already running" in err
        assert "12345" in err


# ---------------------------------------------------------------------------
# Call sites: batch_release.py end-of-batch watch blocks (source-level, same
# style as test_batch_watch.py)
# ---------------------------------------------------------------------------


class TestBatchWatchAsyncBlocks:

    @pytest.mark.parametrize("func_name", ["_batch_release_releasables",
                                           "_batch_release_packages"])
    def test_watch_async_branch_present(self, func_name):
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(getattr(batch_release, func_name))
        assert 'flags.get("watch")' in source
        assert 'flags.get("watch-async")' in source
        assert "spawn_detached_watcher" in source
        assert "Watch CI: rlsbl watch" in source


# ---------------------------------------------------------------------------
# Call site: release_retry.py
# ---------------------------------------------------------------------------


class TestRetryWatchAsync:

    def _make_mock_target(self, version="0.41.7"):
        target = MagicMock()
        target.read_version.return_value = version
        target.tag_format.side_effect = lambda v: f"v{v}"
        target.monorepo_tag_format.side_effect = \
            lambda name, v, path=None: f"{name}@v{v}"
        return target

    def _make_mock_entry(self, name="pypi", path="."):
        entry = MagicMock()
        entry.name = name
        entry.path = path
        return entry

    def _run_side_effect(self, *args, **kwargs):
        cmd, cmd_args = args[0], args[1] if len(args) > 1 else []
        if cmd == "git" and cmd_args[:2] == ["rev-list", "-1"]:
            return "abc123def456789012345678901234567890abcd"
        return ""

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run_gh", return_value="")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.resolve_member_context")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_watch_async_spawns_with_sha(self, _gh_inst, _gh_auth, _ws_root,
                                         mock_targets_dict, mock_detect, _exists,
                                         mock_run, mock_run_gh, _cleanup):
        from rlsbl.commands.release_retry import run_cmd
        from rlsbl.release_file import RetryConfig

        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        config = RetryConfig(version="0.41.7", dispatch=["ci.yml"], ref="v0.41.7", tag="v0.41.7")

        with patch("rlsbl.commands.release_retry.time.sleep"), \
             patch("rlsbl.commands.release_retry.spawn_detached_watcher") as mock_spawn, \
             patch("rlsbl.commands.release_retry.watch_run_cmd") as mock_watch, \
             patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(config, {"yes": True, "watch": False, "watch-async": True},
                    project_root=".")

        mock_watch.assert_not_called()
        mock_spawn.assert_called_once_with(
            "abc123def456789012345678901234567890abcd", run_ids=None,
        )
        assert "Watch CI:" not in mock_stdout.getvalue()

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run_gh")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.resolve_member_context")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_watch_async_passes_captured_run_ids(self, _gh_inst, _gh_auth,
                                                 _ws_root, mock_targets_dict,
                                                 mock_detect, _exists, mock_run,
                                                 mock_run_gh, _cleanup):
        from rlsbl.commands.release_retry import run_cmd
        from rlsbl.release_file import RetryConfig

        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target
        mock_run.side_effect = self._run_side_effect

        def run_gh_effect(args, **kwargs):
            if args[:2] == ["workflow", "run"]:
                return "https://github.com/o/r/actions/runs/555"
            return ""

        mock_run_gh.side_effect = run_gh_effect

        config = RetryConfig(version="0.41.7", dispatch=["ci.yml"], ref="v0.41.7", tag="v0.41.7")

        with patch("rlsbl.commands.release_retry.time.sleep"), \
             patch("rlsbl.commands.release_retry.spawn_detached_watcher") as mock_spawn, \
             patch("sys.stdout", new_callable=StringIO):
            run_cmd(config, {"yes": True, "watch": False, "watch-async": True},
                    project_root=".")

        mock_spawn.assert_called_once_with(
            "abc123def456789012345678901234567890abcd", run_ids=["555"],
        )
