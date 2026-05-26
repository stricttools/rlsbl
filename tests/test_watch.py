"""Tests for rlsbl.commands.watch — workflow audit reporting."""

import json
import os

import pytest
from unittest.mock import patch, MagicMock, call

from rlsbl.commands.watch import (
    _has_publish_workflow_on_disk,
    _is_publish_workflow,
    _print_workflow_audit,
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

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "no CI runs found after 30s" in err
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

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "no CI runs found after 30s" in err
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

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "no CI runs found after 30s" in err
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


if __name__ == "__main__":
    pytest.main([__file__])
