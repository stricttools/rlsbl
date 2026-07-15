"""Tests for rlsbl.commands.watch — workflow audit reporting, --run-id flag, auto-retry, and notifications."""

import json
import os
import subprocess

import pytest
from unittest.mock import patch, MagicMock, call

from rlsbl.commands.watch import (
    _classify_failure,
    _fetch_failure_log,
    _has_publish_workflow_on_disk,
    _is_publish_workflow,
    _notify,
    _open_url,
    _print_workflow_audit,
    _release_url,
    _resolve_run_ids,
    _retry_workflow,
    _watch_runs,
    _watch_single_run,
    run_cmd,
)


class TestIsPublishWorkflow:
    """Unit tests for _is_publish_workflow name matching."""

    @pytest.mark.parametrize("name", ["Publish", "publish", "Deploy to prod", "release"])
    def test_matches_publish_keywords(self, name):
        assert _is_publish_workflow(name) is True

    @pytest.mark.parametrize("name", ["CI", "Lint", "Test suite", "Build"])
    def test_rejects_non_publish_names(self, name):
        assert _is_publish_workflow(name) is False


class TestHasPublishWorkflowOnDisk:
    """Unit tests for _has_publish_workflow_on_disk."""

    def test_returns_true_when_publish_yml_exists(self, tmp_project):
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\n")
        assert _has_publish_workflow_on_disk() is True

    def test_returns_true_for_deploy_yml(self, tmp_project):
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.yml").write_text("name: Deploy\n")
        assert _has_publish_workflow_on_disk() is True

    def test_returns_false_when_no_publish_file(self, tmp_project):
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("name: CI\n")
        assert _has_publish_workflow_on_disk() is False

    def test_returns_false_when_no_workflows_dir(self, tmp_project):
        assert _has_publish_workflow_on_disk() is False

    def test_resolves_workflows_from_git_repo_root(self, tmp_project, monkeypatch):
        """Regression: the audit must find .github/workflows at the git repo
        root even when watch runs from a subdirectory (monorepo package dir)."""
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_project), check=True)
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\n")
        pkg_dir = tmp_project / "packages" / "core"
        pkg_dir.mkdir(parents=True)
        monkeypatch.chdir(pkg_dir)
        assert _has_publish_workflow_on_disk() is True

    def test_subdir_without_git_falls_back_to_cwd(self, tmp_project, monkeypatch):
        """Outside a git repo, the audit keeps checking the current directory."""
        pkg_dir = tmp_project / "pkg"
        wf_dir = pkg_dir / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.yml").write_text("name: Deploy\n")
        monkeypatch.chdir(pkg_dir)
        assert _has_publish_workflow_on_disk() is True


class TestPrintWorkflowAudit:
    """Tests for _print_workflow_audit summary and warning output."""

    def test_both_ci_and_publish_pass(self, tmp_project, capsys):
        """When both CI and Publish run and pass, summary shows both without warning."""
        # Create a publish workflow on disk so it's "expected"
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\n")

        results = [
            {"name": "CI", "passed": True},
            {"name": "Publish", "passed": True},
        ]
        missing = _print_workflow_audit(results)

        assert missing is False
        err = capsys.readouterr().err
        assert "Workflows:" in err
        assert "CI" in err
        assert "passed" in err
        assert "Publish" in err
        # No warning about missing publish
        assert "(!) Publish workflow exists but did not run" not in err
        assert "Warning:" not in err

    def test_only_ci_runs_but_publish_yml_exists(self, tmp_project, capsys):
        """When only CI runs but publish.yml exists on disk, warning is printed."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\n")

        results = [
            {"name": "CI", "passed": True},
        ]
        missing = _print_workflow_audit(results)

        assert missing is True
        err = capsys.readouterr().err
        assert "Workflows:" in err
        assert "CI" in err
        assert "(!) Publish workflow exists but did not run" in err
        assert (
            "Warning: publish workflow exists but did not trigger for this "
            "commit. The package may not have been published."
        ) in err

    def test_ci_only_no_publish_on_disk(self, tmp_project, capsys):
        """When only CI runs and no publish.yml on disk, no warning."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("name: CI\n")

        results = [
            {"name": "CI", "passed": True},
        ]
        missing = _print_workflow_audit(results)

        assert missing is False
        err = capsys.readouterr().err
        assert "Workflows:" in err
        assert "CI" in err
        assert "Warning:" not in err

    def test_failed_workflow_shown_as_failed(self, tmp_project, capsys):
        """FAILED status is shown for workflows that didn't pass."""
        results = [
            {"name": "CI", "passed": False},
        ]
        _print_workflow_audit(results)

        err = capsys.readouterr().err
        assert "FAILED" in err

    def test_deploy_workflow_counts_as_publish(self, tmp_project, capsys):
        """A run named 'Deploy to prod' satisfies the publish expectation."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\n")

        results = [
            {"name": "CI", "passed": True},
            {"name": "Deploy to prod", "passed": True},
        ]
        missing = _print_workflow_audit(results)

        assert missing is False
        err = capsys.readouterr().err
        assert "Warning:" not in err


class TestNoRunsHint:
    """Tests for the release-retry hint when no CI runs are found."""

    @patch("rlsbl.commands.watch.poll_runs", return_value=[])
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_hint_printed_when_tag_and_release_exist(self, mock_run, mock_run_gh, mock_poll, capsys):
        """When no runs, commit has a tag, and GitHub Release exists, hint is printed."""
        mock_run.side_effect = [
            "abc123full",  # git rev-parse (resolve arg)
            "v1.2.0",  # git describe --tags --exact-match (label)
            "v1.2.0",  # git describe --tags --exact-match (hint check)
        ]
        mock_run_gh.side_effect = [
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            "Release v1.2.0\n...",  # gh release view
        ]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert "No CI runs found for abc123full" in err
        assert "rlsbl: hint: GitHub Release v1.2.0 exists but no workflows ran" in err
        assert "rlsbl release retry" in err

    @patch("rlsbl.commands.watch.poll_runs", return_value=[])
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_no_hint_when_tag_exists_but_no_release(self, mock_run, mock_run_gh, mock_poll, capsys):
        """When no runs, commit has a tag, but no GitHub Release, no hint."""
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            "v1.2.0",  # git describe --tags --exact-match (label)
            "v1.2.0",  # git describe --tags --exact-match (hint check)
        ]
        mock_run_gh.side_effect = [
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            Exception("release not found"),  # gh release view fails
        ]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert "No CI runs found for abc123full" in err
        assert "hint:" not in err

    @patch("rlsbl.commands.watch.poll_runs", return_value=[])
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_no_hint_when_no_tag(self, mock_run, mock_run_gh, mock_poll, capsys):
        """When no runs and commit has no tag, no hint."""
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            Exception("no tag"),  # git describe --tags --exact-match (label) fails
            Exception("no tag"),  # git describe --tags --exact-match (hint check) fails
        ]
        mock_run_gh.return_value = json.dumps({"nameWithOwner": "user/repo", "name": "repo"})  # gh repo view

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert "No CI runs found for abc123full" in err
        assert "hint:" not in err

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_hint_not_reached_when_runs_found(
        self, mock_run, mock_run_gh, mock_time, mock_poll, mock_watch, mock_audit, mock_notify, capsys
    ):
        """When CI runs are found, the hint logic is never reached."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        mock_poll.side_effect = [
            [ci_run],  # initial poll finds runs
            [ci_run],  # re-poll
        ]
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            "v1.2.0",  # git describe
        ]
        mock_run_gh.return_value = json.dumps({"nameWithOwner": "user/repo", "name": "repo"})  # gh repo view
        mock_watch.return_value = [{"name": "CI", "passed": True}]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert "hint:" not in err
        assert "no CI runs found" not in err


class TestRePoll:
    """Tests for the re-poll logic that catches late-starting workflows."""

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit")
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_late_run_discovered_on_repoll(
        self, mock_run, mock_run_gh, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
    ):
        """A run that appears only on the re-poll (not initial discovery) is still watched."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        publish_run = {"databaseId": 200, "name": "Publish", "status": "in_progress"}

        # First call: initial poll returns only CI
        # Second call: re-poll returns both CI and Publish
        mock_poll.side_effect = [
            [ci_run],
            [ci_run, publish_run],
        ]

        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            "v1.0.0",  # git describe
        ]
        mock_run_gh.return_value = json.dumps({"nameWithOwner": "user/repo", "name": "repo"})  # gh repo view

        mock_watch.side_effect = [
            [{"name": "CI", "passed": True}],       # initial watch
            [{"name": "Publish", "passed": True}],   # late watch
        ]
        mock_audit.return_value = False

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0

        # poll_runs called twice: initial discovery + re-poll
        assert mock_poll.call_count == 2
        # Re-poll uses max_attempts=1, interval=0
        mock_poll.assert_called_with("abc123full", max_attempts=1, interval=0)

        # _watch_runs called twice: once for CI, once for late Publish
        assert mock_watch.call_count == 2
        # Second call should only contain the late run (Publish)
        late_runs_arg = mock_watch.call_args_list[1][0][0]
        assert len(late_runs_arg) == 1
        assert late_runs_arg[0]["name"] == "Publish"

        # Audit sees all results (CI + Publish)
        audit_arg = mock_audit.call_args[0][0]
        assert len(audit_arg) == 2
        names = {r["name"] for r in audit_arg}
        assert names == {"CI", "Publish"}

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit")
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_no_late_runs_skips_second_watch(
        self, mock_run, mock_run_gh, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
    ):
        """When the re-poll finds no new runs, _watch_runs is called only once."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}

        # Both polls return the same single run
        mock_poll.side_effect = [
            [ci_run],
            [ci_run],
        ]

        mock_run.side_effect = [
            "abc123full",
            "v1.0.0",
        ]
        mock_run_gh.return_value = json.dumps({"nameWithOwner": "user/repo", "name": "repo"})

        mock_watch.return_value = [{"name": "CI", "passed": True}]
        mock_audit.return_value = False

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0
        # _watch_runs called only once (no late runs)
        assert mock_watch.call_count == 1


class TestResolveRunIds:
    """Tests for _resolve_run_ids that resolves run IDs via gh run view."""

    @patch("rlsbl.commands.watch.run_gh")
    def test_resolves_single_run_id(self, mock_run_gh):
        """A single run ID is resolved to a run info dict."""
        mock_run_gh.return_value = json.dumps(
            {"databaseId": 12345, "name": "CI", "status": "completed"}
        )
        result = _resolve_run_ids(["12345"])
        assert len(result) == 1
        assert result[0]["databaseId"] == 12345
        assert result[0]["name"] == "CI"
        assert mock_run_gh.call_count == 1
        assert mock_run_gh.call_args[0] == (["run", "view", "12345", "--json", "databaseId,name,status,headBranch,workflowName"],)

    @patch("rlsbl.commands.watch.run_gh")
    def test_resolves_multiple_run_ids(self, mock_run_gh):
        """Multiple run IDs are each resolved to a run info dict."""
        mock_run_gh.side_effect = [
            json.dumps({"databaseId": 111, "name": "CI", "status": "completed"}),
            json.dumps({"databaseId": 222, "name": "Publish", "status": "in_progress"}),
        ]
        result = _resolve_run_ids(["111", "222"])
        assert len(result) == 2
        assert result[0]["databaseId"] == 111
        assert result[1]["databaseId"] == 222

    @patch("rlsbl.commands.watch.run_gh")
    def test_invalid_run_id_exits(self, mock_run_gh):
        """An unresolvable run ID causes sys.exit(1)."""
        mock_run_gh.side_effect = Exception("not found")
        with pytest.raises(SystemExit) as exc_info:
            _resolve_run_ids(["99999"])
        assert exc_info.value.code == 1


class TestRunIdPath:
    """Tests for the --run-id code path in run_cmd."""

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch._resolve_run_ids")
    @patch("rlsbl.commands.watch.run_gh")
    def test_run_id_path_watches_resolved_runs(
        self, mock_run_gh, mock_resolve, mock_watch, mock_audit, mock_notify, capsys
    ):
        """--run-id resolves IDs and watches them, skipping SHA logic."""
        mock_resolve.return_value = [
            {"databaseId": 100, "name": "CI", "status": "in_progress"},
        ]
        mock_run_gh.return_value = json.dumps(
            {"nameWithOwner": "user/repo", "name": "repo"}
        )
        mock_watch.return_value = [{"name": "CI", "passed": True}]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {"run-id": ["100"]})

        assert exc_info.value.code == 0
        mock_resolve.assert_called_once_with(["100"])
        mock_watch.assert_called_once()
        mock_audit.assert_called_once()

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch._resolve_run_ids")
    @patch("rlsbl.commands.watch.run_gh")
    def test_run_id_path_failure_exits_1(
        self, mock_run_gh, mock_resolve, mock_watch, mock_audit, mock_notify
    ):
        """When a watched run fails, exit code is 1."""
        mock_resolve.return_value = [
            {"databaseId": 100, "name": "CI", "status": "in_progress"},
        ]
        mock_run_gh.return_value = json.dumps(
            {"nameWithOwner": "user/repo", "name": "repo"}
        )
        mock_watch.return_value = [{"name": "CI", "passed": False}]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {"run-id": ["100"]})

        assert exc_info.value.code == 1

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch._resolve_run_ids")
    @patch("rlsbl.commands.watch.run_gh")
    def test_run_id_path_multiple_ids(
        self, mock_run_gh, mock_resolve, mock_watch, mock_audit, mock_notify, capsys
    ):
        """Multiple --run-id values are all resolved and watched."""
        mock_resolve.return_value = [
            {"databaseId": 100, "name": "CI", "status": "in_progress"},
            {"databaseId": 200, "name": "Publish", "status": "in_progress"},
        ]
        mock_run_gh.return_value = json.dumps(
            {"nameWithOwner": "user/repo", "name": "repo"}
        )
        mock_watch.return_value = [
            {"name": "CI", "passed": True},
            {"name": "Publish", "passed": True},
        ]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {"run-id": ["100", "200"]})

        assert exc_info.value.code == 0
        mock_resolve.assert_called_once_with(["100", "200"])
        err = capsys.readouterr().err
        assert "watching 2 run(s)" in err

    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch._resolve_run_ids")
    def test_run_id_skips_sha_path(self, mock_resolve, mock_poll):
        """When --run-id is provided, poll_runs (SHA path) is never called."""
        mock_resolve.return_value = []

        with pytest.raises(SystemExit):
            run_cmd(None, [], {"run-id": ["100"]})

        mock_poll.assert_not_called()

    @patch("rlsbl.commands.watch._resolve_run_ids")
    def test_empty_resolved_runs_exits(self, mock_resolve):
        """When _resolve_run_ids returns empty, exit with error."""
        mock_resolve.return_value = []

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {"run-id": ["100"]})

        assert exc_info.value.code == 1


class TestMutualExclusivity:
    """Tests for SHA / --run-id mutual exclusivity at the CLI level."""

    def test_sha_and_run_id_both_provided(self):
        """cmd_watch rejects SHA + --run-id combination."""
        import rlsbl
        with pytest.raises(SystemExit) as exc_info:
            rlsbl.cmd_watch(target="", run_id=["123"], as_daemon_child=False,
                            stop=False, sha="abc123")
        assert exc_info.value.code == 1


class TestAutoRetry:
    """Tests for auto-retry logic when a CI workflow fails."""

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_attempted_on_first_failure(self, mock_run_gh, mock_time, capsys):
        """When a workflow fails, _watch_single_run triggers a retry."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        # run_gh calls: original watch, log fetch, retry trigger, run list poll, retry watch
        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),  # original watch fails
            "",  # gh run view --log-failed (empty tail -> unknown -> retry)
            "",  # gh workflow run succeeds
            json.dumps([{"databaseId": 200, "name": "CI", "status": "in_progress", "createdAt": "2026-01-01"}]),  # gh run list
            "",  # retry watch succeeds
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")

        assert result["passed"] is True
        assert result["name"] == "CI"
        err = capsys.readouterr().err
        assert "CI failed, retrying once..." in err
        assert "retry passed" in err

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_success_reports_overall_success(self, mock_run_gh, mock_time, capsys):
        """When the retry passes, the overall result is success."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),  # original watch fails
            "",  # gh run view --log-failed (empty tail -> unknown -> retry)
            "",  # gh workflow run trigger
            json.dumps([{"databaseId": 200, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]),  # gh run list
            "",  # retry watch succeeds
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")
        assert result["passed"] is True
        assert result["run_id"] == "200"

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_double_failure_reports_failure(self, mock_run_gh, mock_time, capsys):
        """When both original and retry fail, the result is failure."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),  # original watch fails
            "",  # gh run view --log-failed (empty tail -> unknown -> retry)
            "",  # gh workflow run trigger
            json.dumps([{"databaseId": 200, "name": "CI", "status": "in_progress", "createdAt": "2026-01-01"}]),  # gh run list
            subprocess.CalledProcessError(1, "gh"),  # retry watch also fails
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")

        assert result["passed"] is False
        assert result["run_id"] == "200"
        err = capsys.readouterr().err
        assert "retry also failed" in err

    @patch("rlsbl.commands.watch.run_gh")
    def test_no_retry_without_branch(self, mock_run_gh, capsys):
        """When headBranch is missing, no retry is attempted."""
        ci_run = {"databaseId": 100, "name": "CI"}  # no headBranch

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),  # original watch fails
            "",  # gh run view --log-failed (empty tail; retry still gated by branch)
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")
        assert result["passed"] is False
        err = capsys.readouterr().err
        assert "retrying" not in err

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_trigger_failure_returns_original_failure(self, mock_run_gh, mock_time, capsys):
        """When the retry trigger itself fails, the original failure is returned."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),  # original watch fails
            "",  # gh run view --log-failed (empty tail -> unknown -> retry)
            subprocess.CalledProcessError(1, "gh"),  # gh workflow run trigger fails
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")
        assert result["passed"] is False
        assert result["run_id"] == "100"  # original run ID, not retry
        err = capsys.readouterr().err
        assert "retry trigger failed" in err


class TestRetryWorkflow:
    """Unit tests for _retry_workflow."""

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_passes(self, mock_run_gh, mock_time, capsys):
        """Successful retry returns passed=True."""
        mock_run_gh.side_effect = [
            "",  # gh workflow run trigger
            json.dumps([{"databaseId": 300, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]),  # gh run list
            "",  # retry watch succeeds
        ]

        result = _retry_workflow("CI", "main", "user/repo", "test-label", "100")
        assert result is not None
        assert result["passed"] is True
        assert result["run_id"] == "300"

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_run_not_found(self, mock_run_gh, mock_time, capsys):
        """When the retry run never appears, returns None."""
        # First call succeeds (trigger), remaining calls fail (all polls)
        mock_run_gh.side_effect = [
            "",  # gh workflow run trigger
        ] + [Exception("not found")] * 15  # all poll attempts fail

        result = _retry_workflow("CI", "main", "user/repo", "test-label", "100")
        assert result is None
        err = capsys.readouterr().err
        assert "retry run not found" in err


class TestNotifyUrl:
    """Tests for _notify with URL opening and _open_url."""

    @patch("rlsbl.commands.watch._open_url")
    @patch("rlsbl.commands.watch.subprocess.run")
    @patch("rlsbl.commands.watch.require_tool", return_value=True)
    def test_notify_opens_url_on_action_click(self, mock_tool, mock_run, mock_open):
        """When user clicks the notification action, _open_url is called."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="open\n", stderr="")
        with patch("rlsbl.commands.watch.sys") as mock_sys:
            mock_sys.platform = "linux"
            _notify("title", "body", url="https://example.com")
        mock_open.assert_called_once_with("https://example.com")

    @patch("rlsbl.commands.watch._open_url")
    @patch("rlsbl.commands.watch.subprocess.run")
    @patch("rlsbl.commands.watch.require_tool", return_value=True)
    def test_notify_no_open_on_dismiss(self, mock_tool, mock_run, mock_open):
        """When notification is dismissed without clicking, _open_url is not called."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("rlsbl.commands.watch.sys") as mock_sys:
            mock_sys.platform = "linux"
            _notify("title", "body", url="https://example.com")
        mock_open.assert_not_called()

    @patch("rlsbl.commands.watch._open_url")
    @patch("rlsbl.commands.watch.require_tool", return_value=None)
    def test_notify_skips_url_when_none(self, mock_tool, mock_open):
        """When url is None, _open_url is not called."""
        _notify("title", "body")
        mock_open.assert_not_called()

    @patch("rlsbl.commands.watch._open_url")
    @patch("rlsbl.commands.watch.require_tool", return_value=None)
    def test_notify_skips_url_when_not_passed(self, mock_tool, mock_open):
        """When url kwarg is omitted, _open_url is not called."""
        _notify("title", "body")
        mock_open.assert_not_called()

    @patch("rlsbl.commands.watch.subprocess.run")
    def test_open_url_linux(self, mock_subproc):
        """On Linux, _open_url calls xdg-open."""
        with patch("rlsbl.commands.watch.sys") as mock_sys:
            mock_sys.platform = "linux"
            _open_url("https://example.com")
            mock_subproc.assert_called_once_with(
                ["xdg-open", "https://example.com"], timeout=5, capture_output=True
            )

    @patch("rlsbl.commands.watch.subprocess.run")
    def test_open_url_macos(self, mock_subproc):
        """On macOS, _open_url calls open."""
        with patch("rlsbl.commands.watch.sys") as mock_sys:
            mock_sys.platform = "darwin"
            _open_url("https://example.com")
            mock_subproc.assert_called_once_with(
                ["open", "https://example.com"], timeout=5, capture_output=True
            )

    @patch("rlsbl.commands.watch.subprocess.run", side_effect=FileNotFoundError)
    def test_open_url_nonfatal(self, mock_subproc):
        """_open_url silently ignores errors."""
        _open_url("https://example.com")  # should not raise


class TestReleaseUrl:
    """Tests for _release_url helper."""

    @patch("rlsbl.commands.watch.run_gh")
    def test_returns_url_for_latest_tag(self, mock_run_gh):
        """Returns a release URL when gh release list succeeds."""
        mock_run_gh.return_value = "v1.2.3"
        url = _release_url("user/repo")
        assert url == "https://github.com/user/repo/releases/tag/v1.2.3"

    @patch("rlsbl.commands.watch.run_gh", side_effect=Exception("no releases"))
    def test_returns_none_on_failure(self, mock_run_gh):
        """Returns None when gh release list fails."""
        url = _release_url("user/repo")
        assert url is None

    def test_returns_none_for_empty_slug(self):
        """Returns None when repo_slug is empty."""
        url = _release_url("")
        assert url is None

    def test_returns_none_for_none_slug(self):
        """Returns None when repo_slug is None."""
        url = _release_url(None)
        assert url is None


class TestNotifyUrlInRunCmd:
    """Tests that run_cmd passes the right URL to _notify."""

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_failure_notification_passes_actions_url(
        self, mock_run, mock_run_gh, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
    ):
        """On failure, _notify is called with the failed run's Actions URL."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        mock_poll.side_effect = [
            [ci_run],
            [ci_run],
        ]
        mock_run.side_effect = [
            "abc123full",
            "v1.0.0",
        ]
        mock_run_gh.return_value = json.dumps({"nameWithOwner": "user/repo", "name": "repo"})
        mock_watch.return_value = [{"name": "CI", "passed": False, "run_id": "100"}]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 1
        mock_notify.assert_called_once()
        assert mock_notify.call_args[1]["url"] == "https://github.com/user/repo/actions/runs/100"

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_success_notification_passes_release_url_with_tag(
        self, mock_run, mock_run_gh, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
    ):
        """On success with a tag, _notify is called with the release page URL."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        mock_poll.side_effect = [
            [ci_run],
            [ci_run],
        ]
        mock_run.side_effect = [
            "abc123full",
            "v2.0.0",  # tag found
        ]
        mock_run_gh.return_value = json.dumps({"nameWithOwner": "user/repo", "name": "repo"})
        mock_watch.return_value = [{"name": "CI", "passed": True, "run_id": "100"}]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0
        mock_notify.assert_called_once()
        assert mock_notify.call_args[1]["url"] == "https://github.com/user/repo/releases/tag/v2.0.0"


class TestRetryDedup:
    """Tests for concurrent retry deduplication in _watch_runs.

    When multiple runs of the same workflow fail concurrently, only one
    retry should be dispatched (the first thread to acquire the lock adds
    the workflow name to retried_workflows; subsequent threads find it
    already present and skip).
    """

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_multiple_runs_same_workflow_retry_once(self, mock_run_gh, mock_time, capsys):
        """Three CI runs fail concurrently but only one retry is dispatched."""
        runs = [
            {"databaseId": 100, "name": "CI", "headBranch": "main"},
            {"databaseId": 200, "name": "CI", "headBranch": "main"},
            {"databaseId": 300, "name": "CI", "headBranch": "main"},
        ]

        # All gh calls now go through run_gh(args, ...) where args is the
        # list without "gh".  Since threads run concurrently, we use
        # side_effect as a function that inspects the args.

        workflow_run_call_count = 0

        def run_gh_side_effect(args, **kwargs):
            nonlocal workflow_run_call_count
            if args[:2] == ["run", "watch"]:
                run_id = args[2]
                # Original watches (IDs 100, 200, 300) all fail
                if run_id in ("100", "200", "300"):
                    raise subprocess.CalledProcessError(1, "gh")
                # Retry watch (ID 400) succeeds
                return ""
            elif args[:2] == ["workflow", "run"]:
                workflow_run_call_count += 1
                return ""
            elif args[:2] == ["run", "list"]:
                return json.dumps(
                    [{"databaseId": 400, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]
                )
            return ""

        mock_run_gh.side_effect = run_gh_side_effect

        results = _watch_runs(runs, "test-label", "user/repo")

        # Only ONE retry should have been dispatched
        assert workflow_run_call_count == 1, (
            f"Expected exactly 1 retry dispatch, got {workflow_run_call_count}"
        )

        # All 3 runs should produce results
        assert len(results) == 3

        # Exactly one result should be the retry success (run_id 400),
        # the other two should be the original failures (run_ids 100/200/300)
        retry_results = [r for r in results if r.get("run_id") == "400"]
        original_failures = [r for r in results if r.get("run_id") in ("100", "200", "300")]

        assert len(retry_results) == 1
        assert retry_results[0]["passed"] is True
        assert retry_results[0]["name"] == "CI"

        assert len(original_failures) == 2
        for r in original_failures:
            assert r["passed"] is False
            assert r["name"] == "CI"


class TestLatePollRetryDedup:
    """Tests that retry runs dispatched during the initial watch are not
    re-watched or re-retried when they reappear in the late re-poll.

    A retry dispatched via `gh workflow run` executes on the same commit SHA,
    so the late re-poll (`gh run list --commit`) includes it. It must be
    recognized as a known retry run, not treated as a late-starting workflow.
    """

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_retry_run_not_treated_as_late_run(
        self, mock_run, mock_run_gh, mock_time, mock_poll, mock_audit, mock_notify
    ):
        """A failed CI run is retried once; the retry run showing up in the
        re-poll is neither watched again nor retried again."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}
        lint_run = {"databaseId": 101, "name": "Lint", "headBranch": "main"}
        retry_run = {"databaseId": 400, "name": "CI", "headBranch": "main"}

        mock_poll.side_effect = [
            [ci_run, lint_run],             # initial discovery
            [ci_run, lint_run, retry_run],  # late re-poll sees the retry run
        ]
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            "v1.0.0",      # git describe
        ]

        dispatch_count = 0
        watch_calls = []

        def run_gh_side_effect(args, **kwargs):
            nonlocal dispatch_count
            if args[:2] == ["repo", "view"]:
                return json.dumps({"nameWithOwner": "user/repo", "name": "repo"})
            if args[:2] == ["run", "watch"]:
                run_id = args[2]
                watch_calls.append(run_id)
                if run_id == "101":
                    return ""  # Lint passes
                # CI original and any retry runs fail
                raise subprocess.CalledProcessError(1, "gh")
            if args[:2] == ["workflow", "run"]:
                dispatch_count += 1
                return ""
            if args[:2] == ["run", "list"]:
                # First dispatch produces run 400; a buggy second dispatch
                # would produce run 500.
                rid = 400 if dispatch_count == 1 else 500
                return json.dumps(
                    [{"databaseId": rid, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]
                )
            return ""

        mock_run_gh.side_effect = run_gh_side_effect

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 1  # CI and its retry failed

        # Exactly ONE retry dispatch: the retry run reappearing in the
        # re-poll must not trigger another `gh workflow run`.
        assert dispatch_count == 1, (
            f"Expected exactly 1 retry dispatch, got {dispatch_count}"
        )
        # The retry run is watched exactly once (during the retry), never
        # again as a "late run".
        assert watch_calls.count("400") == 1, (
            f"Retry run 400 watched {watch_calls.count('400')} times: {watch_calls}"
        )
        # Audit sees one result per workflow: CI retry failure + Lint pass.
        audit_arg = mock_audit.call_args[0][0]
        assert len(audit_arg) == 2, f"Expected 2 results, got {audit_arg}"

    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit", return_value=False)
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    def test_single_initial_run_late_poll_no_double_retry(
        self, mock_run, mock_run_gh, mock_time, mock_poll, mock_audit, mock_notify
    ):
        """Retry dedup must also apply when the initial pool has exactly one
        run: the single-run path must use the same shared-state machinery, so
        the retry run appearing in the re-poll is not re-watched/re-retried."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}
        retry_run = {"databaseId": 400, "name": "CI", "headBranch": "main"}

        mock_poll.side_effect = [
            [ci_run],             # initial discovery: single run
            [ci_run, retry_run],  # late re-poll sees the retry run
        ]
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            "v1.0.0",      # git describe
        ]

        dispatch_count = 0
        watch_calls = []

        def run_gh_side_effect(args, **kwargs):
            nonlocal dispatch_count
            if args[:2] == ["repo", "view"]:
                return json.dumps({"nameWithOwner": "user/repo", "name": "repo"})
            if args[:2] == ["run", "watch"]:
                watch_calls.append(args[2])
                # Original run and all retry runs fail
                raise subprocess.CalledProcessError(1, "gh")
            if args[:2] == ["workflow", "run"]:
                dispatch_count += 1
                return ""
            if args[:2] == ["run", "list"]:
                rid = 400 if dispatch_count == 1 else 500
                return json.dumps(
                    [{"databaseId": rid, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]
                )
            return ""

        mock_run_gh.side_effect = run_gh_side_effect

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 1

        assert dispatch_count == 1, (
            f"Expected exactly 1 retry dispatch, got {dispatch_count}"
        )
        assert watch_calls.count("400") == 1, (
            f"Retry run 400 watched {watch_calls.count('400')} times: {watch_calls}"
        )
        # Audit sees exactly one result: the CI retry failure.
        audit_arg = mock_audit.call_args[0][0]
        assert len(audit_arg) == 1, f"Expected 1 result, got {audit_arg}"


class TestRetryAttachment:
    """Tests that _retry_workflow attaches to the actual dispatched retry run,
    not to the just-failed original run (or another known run) that `gh run
    list --limit 1` may still report as the newest run before the dispatched
    run appears."""

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_does_not_attach_to_original_failed_run(self, mock_run_gh, mock_time, capsys):
        """When the dispatched retry run has not appeared yet, the poll must
        keep waiting instead of attaching to the original failed run."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        list_calls = 0

        def run_gh_side_effect(args, **kwargs):
            nonlocal list_calls
            if args[:2] == ["run", "watch"]:
                if args[2] == "100":
                    # Original run fails (both on the initial watch and if a
                    # buggy retry attaches to it and watches it again)
                    raise subprocess.CalledProcessError(1, "gh")
                return ""  # the real retry run (400) passes
            if args[:2] == ["workflow", "run"]:
                return ""
            if args[:2] == ["run", "list"]:
                list_calls += 1
                if list_calls == 1:
                    # Dispatched run not visible yet: newest run is still the
                    # just-failed original
                    return json.dumps(
                        [{"databaseId": 100, "name": "CI", "status": "completed", "createdAt": "2026-01-01"}]
                    )
                return json.dumps(
                    [{"databaseId": 400, "name": "CI", "status": "queued", "createdAt": "2026-01-02"}]
                )
            return ""

        mock_run_gh.side_effect = run_gh_side_effect

        result = _watch_single_run(ci_run, "test-label", "user/repo")

        # The retry must attach to the dispatched run (400), not report a
        # bogus result from re-watching the original failed run (100).
        assert result["run_id"] == "400", (
            f"Retry attached to wrong run: {result}"
        )
        assert result["passed"] is True
        assert list_calls >= 2, "Poll must retry until a new run ID appears"

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_does_not_attach_to_known_run(self, mock_run_gh, mock_time):
        """A run ID already in known_ids (e.g. another initial run or a
        previously dispatched retry) is never mistaken for the new retry."""
        list_calls = 0

        def run_gh_side_effect(args, **kwargs):
            nonlocal list_calls
            if args[:2] == ["workflow", "run"]:
                return ""
            if args[:2] == ["run", "list"]:
                list_calls += 1
                if list_calls == 1:
                    # Newest run is another already-known run, not our retry
                    return json.dumps(
                        [{"databaseId": 101, "name": "CI", "status": "in_progress", "createdAt": "2026-01-01"}]
                    )
                return json.dumps(
                    [{"databaseId": 400, "name": "CI", "status": "queued", "createdAt": "2026-01-02"}]
                )
            if args[:2] == ["run", "watch"]:
                return ""
            return ""

        mock_run_gh.side_effect = run_gh_side_effect

        known_ids = {"100", "101"}
        result = _retry_workflow("CI", "main", "user/repo", "test-label", "100", known_ids)

        assert result is not None
        assert result["run_id"] == "400"
        # The identified retry run is recorded in the shared known-IDs set
        assert "400" in known_ids

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_uses_shared_known_ids_lock(self, mock_run_gh, mock_time, capsys):
        """A caller-provided known_ids_lock is honored: known_ids reads and
        writes happen under it (lock discipline for pool-thread mutation)."""
        import threading

        def run_gh_side_effect(args, **kwargs):
            if args[:2] == ["workflow", "run"]:
                return ""
            if args[:2] == ["run", "list"]:
                return json.dumps(
                    [{"databaseId": 500, "name": "CI", "status": "queued", "createdAt": "2026-01-02"}]
                )
            if args[:2] == ["run", "watch"]:
                return ""
            return ""

        mock_run_gh.side_effect = run_gh_side_effect

        class TrackingLock:
            def __init__(self):
                self._lock = threading.Lock()
                self.acquisitions = 0

            def __enter__(self):
                self.acquisitions += 1
                return self._lock.__enter__()

            def __exit__(self, *exc):
                return self._lock.__exit__(*exc)

        known_ids = {"100"}
        lock = TrackingLock()
        result = _retry_workflow("CI", "main", "user/repo", "test-label", "100",
                                 known_ids, lock)

        assert result is not None
        assert result["run_id"] == "500"
        assert "500" in known_ids
        # At least two guarded accesses: the unknown-candidate read and the
        # retry-id write.
        assert lock.acquisitions >= 2

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_not_found_when_only_original_appears(self, mock_run_gh, mock_time, capsys):
        """If every poll only ever shows the original failed run, the retry
        is reported as not found instead of attaching to the original."""

        def run_gh_side_effect(args, **kwargs):
            if args[:2] == ["workflow", "run"]:
                return ""
            if args[:2] == ["run", "list"]:
                return json.dumps(
                    [{"databaseId": 100, "name": "CI", "status": "completed", "createdAt": "2026-01-01"}]
                )
            return ""

        mock_run_gh.side_effect = run_gh_side_effect

        result = _retry_workflow("CI", "main", "user/repo", "test-label", "100")

        assert result is None
        assert "retry run not found" in capsys.readouterr().err


class TestClassifyFailure:
    """Unit tests for _classify_failure signature matching."""

    @pytest.mark.parametrize("log", [
        "=========================== 3 failed, 5 passed in 1.2s",
        "==== 1 failed ====",
        "short test summary info",
        "FAILED tests/test_foo.py::test_bar - AssertionError",
        "--- FAIL: TestThing (0.00s)",
        "FAIL\tgithub.com/x/y\t0.5s",
        "ok   github.com/x/z [build failed]",
        "Tests:       2 failed, 10 passed",
        "npm ERR! Test failed.  See above for more details.",
        "compilation failed",
        "error: build failed",
        "error[E0433]: failed to resolve",
        "SyntaxError: invalid syntax",
        "Error: Cannot find module 'foo'",
        "undefined reference to `symbol'",
        "./main.go:10:2: undefined: Foo",
        "strictcli: FlagRegistrationError: bad flag",
        "registration error: duplicate command",
        "rlsbl.utils.ConfigError: invalid config",
        "pydantic ValidationError: 2 validation errors",
        "invalid configuration",
        "goreleaser: error: invalid config",
        "only configuration files are allowed",
        "Invalid workflow file: .github/workflows/ci.yml",
        "yaml: line 12: mapping values are not allowed",
        "workflow syntax error near line 3",
        "Error: Input required and not supplied: token",
        "fatal: could not read Username for 'https://github.com'",
        "Permission denied (publickey).",
        "denied: permission_denied: write_package",
        "remote: authentication failed",
        "remote: Permission to smm-h/repo.git denied to bot",
    ])
    def test_deterministic_signatures(self, log):
        assert _classify_failure(log) == "deterministic"

    @pytest.mark.parametrize("log", [
        "API rate limit exceeded for user",
        "HTTP 429 Too Many Requests",
        "read tcp: i/o timeout",
        "dial tcp 10.0.0.1:443: connection refused",
        "connection reset by peer",
        "connection timed out",
        "network is unreachable",
        "Temporary failure in name resolution",
        "net/http: TLS handshake timeout",
        "dial tcp: lookup api.github.com: no such host",
        "HTTP 503 returned from server",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "500 Internal Server Error",
        "The runner has received a shutdown signal",
        "The runner lost communication with the server",
        "The operation was canceled.",
        "received request to deprovision: instance terminated",
    ])
    def test_transient_signatures(self, log):
        assert _classify_failure(log) == "transient"

    def test_unknown_when_no_signature_matches(self):
        assert _classify_failure("some unremarkable log line") == "unknown"

    def test_empty_log_is_unknown(self):
        assert _classify_failure("") == "unknown"
        assert _classify_failure(None) == "unknown"

    def test_deterministic_wins_over_transient(self):
        """A log with both a hard error and network chatter is deterministic."""
        log = "connection timed out\nFAILED tests/test_x.py::test_y - assert 1 == 2"
        assert _classify_failure(log) == "deterministic"


class TestFetchFailureLog:
    """Unit tests for _fetch_failure_log tail-capping and gh invocation."""

    @patch("rlsbl.commands.watch.run_gh")
    def test_returns_last_n_lines(self, mock_run_gh):
        """Only the last _LOG_TAIL_LINES lines are returned."""
        from rlsbl.commands.watch import _LOG_TAIL_LINES
        lines = [f"line {i}" for i in range(_LOG_TAIL_LINES + 50)]
        mock_run_gh.return_value = "\n".join(lines)

        tail = _fetch_failure_log("123")
        tail_lines = tail.splitlines()
        assert len(tail_lines) == _LOG_TAIL_LINES
        assert tail_lines[-1] == lines[-1]
        assert tail_lines[0] == lines[50]

    @patch("rlsbl.commands.watch.run_gh")
    def test_invokes_gh_log_failed_with_timeout(self, mock_run_gh):
        """Uses `gh run view <id> --log-failed` with a bounded timeout."""
        from rlsbl.commands.watch import _LOG_FETCH_TIMEOUT
        mock_run_gh.return_value = "boom"
        _fetch_failure_log("777")
        args = mock_run_gh.call_args
        assert args[0][0] == ["run", "view", "777", "--log-failed"]
        assert args[1]["timeout"] == _LOG_FETCH_TIMEOUT

    @patch("rlsbl.commands.watch.run_gh")
    def test_propagates_gh_exception(self, mock_run_gh):
        """A gh failure propagates so the caller can emit a loud note."""
        mock_run_gh.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(subprocess.CalledProcessError):
            _fetch_failure_log("1")


class TestRetryClassification:
    """Tests that _watch_single_run gates retries on failure classification
    and always prints the fetched log tail on failure."""

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_deterministic_failure_not_retried(self, mock_run_gh, mock_time, capsys):
        """A deterministic-signature failure prints the tail and does NOT retry."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        det_log = "short test summary info\nFAILED tests/test_x.py::test_y - assert"
        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),  # original watch fails
            det_log,                                 # gh run view --log-failed
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")

        assert result["passed"] is False
        assert result["run_id"] == "100"
        # Exactly two gh calls: the watch and the log fetch -- NO retry trigger.
        assert mock_run_gh.call_count == 2
        err = capsys.readouterr().err
        assert "failure log tail:" in err
        assert "FAILED tests/test_x.py" in err  # tail is printed
        assert "deterministic failure detected; not retrying" in err
        assert "retrying once" not in err

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_transient_failure_retried_once(self, mock_run_gh, mock_time, capsys):
        """A transient-signature failure retries once (and prints the tail)."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),   # original watch fails
            "read tcp: i/o timeout",                  # gh run view --log-failed
            "",                                       # gh workflow run trigger
            json.dumps([{"databaseId": 200, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]),
            "",                                       # retry watch succeeds
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")

        assert result["passed"] is True
        assert result["run_id"] == "200"
        err = capsys.readouterr().err
        assert "failure log tail:" in err
        assert "i/o timeout" in err
        assert "CI failed, retrying once..." in err

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_unknown_failure_retried_once(self, mock_run_gh, mock_time, capsys):
        """An unrecognized failure retries once (preserves historical behavior)."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),   # original watch fails
            "something totally unrecognized happened",  # gh run view --log-failed
            "",                                       # gh workflow run trigger
            json.dumps([{"databaseId": 200, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]),
            "",                                       # retry watch succeeds
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")

        assert result["passed"] is True
        assert result["run_id"] == "200"
        err = capsys.readouterr().err
        assert "CI failed, retrying once..." in err

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_log_fetch_failure_loud_note_then_retry(self, mock_run_gh, mock_time, capsys):
        """When the log fetch itself fails, a LOUD note is printed and the
        watcher falls back to retrying once (broken gh must not break watching)."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),          # original watch fails
            subprocess.TimeoutExpired("gh", 30),             # gh run view --log-failed fails
            "",                                              # gh workflow run trigger
            json.dumps([{"databaseId": 200, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]),
            "",                                              # retry watch succeeds
        ]

        result = _watch_single_run(ci_run, "test-label", "user/repo")

        assert result["passed"] is True
        assert result["run_id"] == "200"
        err = capsys.readouterr().err
        assert "could not fetch failure logs" in err
        assert "retrying without classification" in err
        assert "CI failed, retrying once..." in err

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_tail_printed_even_when_not_retried(self, mock_run_gh, mock_time, capsys):
        """The log tail is printed on a deterministic (non-retried) failure --
        the operator gets the diagnosis, not just a run URL."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),
            "goreleaser: error: invalid config\nyaml: line 4: bad",
        ]

        _watch_single_run(ci_run, "test-label", "user/repo")
        err = capsys.readouterr().err
        assert "failure log tail:" in err
        assert "goreleaser: error: invalid config" in err


if __name__ == "__main__":
    pytest.main([__file__])
