"""Tests for rlsbl.commands.watch — workflow audit reporting, --run-id flag, auto-retry, and notifications."""

import json
import os
import subprocess

import pytest
from unittest.mock import patch, MagicMock, call

from rlsbl.commands.watch import (
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
    @patch("rlsbl.commands.watch.run")
    def test_hint_printed_when_tag_and_release_exist(self, mock_run, mock_poll, capsys):
        """When no runs, commit has a tag, and GitHub Release exists, hint is printed."""
        mock_run.side_effect = [
            "abc123full",  # git rev-parse (resolve arg)
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            "v1.2.0",  # git describe --tags --exact-match (label)
            "v1.2.0",  # git describe --tags --exact-match (hint check)
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
    @patch("rlsbl.commands.watch.run")
    def test_no_hint_when_tag_exists_but_no_release(self, mock_run, mock_poll, capsys):
        """When no runs, commit has a tag, but no GitHub Release, no hint."""
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            "v1.2.0",  # git describe --tags --exact-match (label)
            "v1.2.0",  # git describe --tags --exact-match (hint check)
            Exception("release not found"),  # gh release view fails
        ]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["abc123"], {})

        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert "No CI runs found for abc123full" in err
        assert "hint:" not in err

    @patch("rlsbl.commands.watch.poll_runs", return_value=[])
    @patch("rlsbl.commands.watch.run")
    def test_no_hint_when_no_tag(self, mock_run, mock_poll, capsys):
        """When no runs and commit has no tag, no hint."""
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            Exception("no tag"),  # git describe --tags --exact-match (label) fails
            Exception("no tag"),  # git describe --tags --exact-match (hint check) fails
        ]

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
    @patch("rlsbl.commands.watch.run")
    def test_hint_not_reached_when_runs_found(
        self, mock_run, mock_time, mock_poll, mock_watch, mock_audit, mock_notify, capsys
    ):
        """When CI runs are found, the hint logic is never reached."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        mock_poll.side_effect = [
            [ci_run],  # initial poll finds runs
            [ci_run],  # re-poll
        ]
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            "v1.2.0",  # git describe
        ]
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
    @patch("rlsbl.commands.watch.run")
    def test_late_run_discovered_on_repoll(
        self, mock_run, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
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
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            "v1.0.0",  # git describe
        ]

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
    @patch("rlsbl.commands.watch.run")
    def test_no_late_runs_skips_second_watch(
        self, mock_run, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
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
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),
            "v1.0.0",
        ]

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
    @patch("rlsbl.commands.watch.run")
    def test_run_id_path_watches_resolved_runs(
        self, mock_run, mock_resolve, mock_watch, mock_audit, mock_notify, capsys
    ):
        """--run-id resolves IDs and watches them, skipping SHA logic."""
        mock_resolve.return_value = [
            {"databaseId": 100, "name": "CI", "status": "in_progress"},
        ]
        mock_run.return_value = json.dumps(
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
    @patch("rlsbl.commands.watch.run")
    def test_run_id_path_failure_exits_1(
        self, mock_run, mock_resolve, mock_watch, mock_audit, mock_notify
    ):
        """When a watched run fails, exit code is 1."""
        mock_resolve.return_value = [
            {"databaseId": 100, "name": "CI", "status": "in_progress"},
        ]
        mock_run.return_value = json.dumps(
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
    @patch("rlsbl.commands.watch.run")
    def test_run_id_path_multiple_ids(
        self, mock_run, mock_resolve, mock_watch, mock_audit, mock_notify, capsys
    ):
        """Multiple --run-id values are all resolved and watched."""
        mock_resolve.return_value = [
            {"databaseId": 100, "name": "CI", "status": "in_progress"},
            {"databaseId": 200, "name": "Publish", "status": "in_progress"},
        ]
        mock_run.return_value = json.dumps(
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
            rlsbl.cmd_watch(target="", run_id=["123"], sha="abc123")
        assert exc_info.value.code == 1


class TestAutoRetry:
    """Tests for auto-retry logic when a CI workflow fails."""

    @patch("rlsbl.commands.watch.time")
    @patch("rlsbl.commands.watch.run_gh")
    def test_retry_attempted_on_first_failure(self, mock_run_gh, mock_time, capsys):
        """When a workflow fails, _watch_single_run triggers a retry."""
        ci_run = {"databaseId": 100, "name": "CI", "headBranch": "main"}

        # run_gh calls: original watch, retry trigger, run list poll, retry watch
        mock_run_gh.side_effect = [
            subprocess.CalledProcessError(1, "gh"),  # original watch fails
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

        result = _retry_workflow("CI", "main", "user/repo", "test-label")
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

        result = _retry_workflow("CI", "main", "user/repo", "test-label")
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

    @patch("rlsbl.commands.watch.run")
    def test_returns_url_for_latest_tag(self, mock_run):
        """Returns a release URL when gh release list succeeds."""
        mock_run.return_value = "v1.2.3"
        url = _release_url("user/repo")
        assert url == "https://github.com/user/repo/releases/tag/v1.2.3"

    @patch("rlsbl.commands.watch.run", side_effect=Exception("no releases"))
    def test_returns_none_on_failure(self, mock_run):
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
    @patch("rlsbl.commands.watch.run")
    def test_failure_notification_passes_actions_url(
        self, mock_run, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
    ):
        """On failure, _notify is called with the failed run's Actions URL."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        mock_poll.side_effect = [
            [ci_run],
            [ci_run],
        ]
        mock_run.side_effect = [
            "abc123full",
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),
            "v1.0.0",
        ]
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
    @patch("rlsbl.commands.watch.run")
    def test_success_notification_passes_release_url_with_tag(
        self, mock_run, mock_time, mock_poll, mock_watch, mock_audit, mock_notify
    ):
        """On success with a tag, _notify is called with the release page URL."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        mock_poll.side_effect = [
            [ci_run],
            [ci_run],
        ]
        mock_run.side_effect = [
            "abc123full",
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),
            "v2.0.0",  # tag found
        ]
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


if __name__ == "__main__":
    pytest.main([__file__])
