"""An infrastructure-killed CI run is rerun, not read as a code failure.

Observed on a real resumed release: a provider-wide outage killed the
candidate's run at the infrastructure layer -- every job died on runner
acquisition ("The job was not acquired by Runner of type hosted") or action
resolution ("Failed to resolve action download info. Error: Service
Unavailable"), and the one job that got a runner passed. Nothing about the
code was ever established.

The release engine reuses an existing run's verdict for the candidate SHA, so
on resume it found that stale failed run, matched an incidental deterministic
signature in the log tail (job-name prefixes and echoed commands are in there
even when nothing executed), reported "deterministic failure detected; not
retrying" and aborted. A resumed release pushes nothing, so no fresh run can
ever appear: the release was permanently unrunnable through the tool. The
operator's escape was a manual `gh run rerun <id> --failed`.

The properties under test:

- infrastructure-shaped failures are classified as such, ahead of the
  deterministic signatures they incidentally match;
- the engine performs the rerun itself -- ONE bounded `gh run rerun <id>
  --failed`, then waits on it;
- when it genuinely does not retry, the abort message names the manual remedy.
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.watch import (
    CI_GREEN,
    _classify_failure,
    _watch_single_run,
    wait_for_ci_green,
)


# The shape the outage produced: a job that never acquired a runner, a job
# whose action downloads 5xx'd, and -- crucially -- log lines that carry the
# project's own name, which the deterministic signature set matches.
STRANDED_LOG = (
    "strictcli-ci\tSet up job\t2026-08-06T20:11:04.1Z The job was not "
    "acquired by Runner of type hosted\n"
    "strictcli-ci\tSet up job\t2026-08-06T20:11:05.4Z Failed to resolve "
    "action download info. Error: Service Unavailable\n"
)


class TestInfraClassification:

    @pytest.mark.parametrize("log", [
        "The job was not acquired by Runner of type hosted",
        "Error: The job was not acquired by Runner of type 'hosted'",
        "Failed to resolve action download info. Error: Service Unavailable",
        "Failed to resolve action download info. Error: Server Error",
    ])
    def test_infrastructure_shapes_are_classified_as_infra(self, log):
        assert _classify_failure(log) == "infra"

    def test_infra_outranks_an_incidentally_matched_deterministic_signature(self):
        """The exact stranding: the log names the project, so `strictcli`
        matched the deterministic set even though nothing ever ran."""
        assert _classify_failure(STRANDED_LOG) == "infra"

    def test_a_failed_run_that_produced_no_output_at_all_is_infra(self):
        """No failing-step log means no step ever executed.

        Runner never acquired, actions never resolved, or the run was
        cancelled while still queued: whichever it was, nothing about the
        code was established, so the run is void rather than a verdict.
        """
        assert _classify_failure("") == "infra"
        assert _classify_failure("   \n  ") == "infra"
        assert _classify_failure(None) == "infra"

    def test_a_real_test_failure_is_still_deterministic(self):
        """The infra tier must not swallow the failures it sits in front of."""
        assert _classify_failure(
            "short test summary info\nFAILED tests/test_x.py::test_y - assert"
        ) == "deterministic"

    def test_an_infra_line_beside_a_real_test_failure_is_still_infra(self):
        """A run whose jobs died on acquisition proves nothing about a
        sibling job's output."""
        log = STRANDED_LOG + "FAILED tests/test_x.py::test_y - assert 1 == 2\n"
        assert _classify_failure(log) == "infra"


class TestBoundedRerunOfTheFailedJobs:

    def test_an_infra_failure_reruns_the_failed_jobs_and_waits(self, capsys):
        ci_run = {"databaseId": 4242, "name": "CI", "headBranch": "main"}
        with patch("rlsbl.commands.watch.time"), \
                patch("rlsbl.commands.watch._fetch_failure_log",
                      return_value=STRANDED_LOG), \
                patch("rlsbl.commands.watch.run_gh") as gh:
            gh.side_effect = [
                subprocess.CalledProcessError(1, "gh"),  # the stale run: failed
                "",                                      # run rerun --failed
                "",                                      # watching the rerun: green
            ]
            result = _watch_single_run(ci_run, "candidate", "user/repo")

        assert result["passed"] is True, (
            "an infra-killed run must be rerun, and a green rerun is a pass"
        )
        rerun_calls = [
            c for c in gh.call_args_list if c[0][0][:2] == ["run", "rerun"]
        ]
        assert len(rerun_calls) == 1, f"expected ONE rerun, got {rerun_calls}"
        assert rerun_calls[0][0][0] == ["run", "rerun", "4242", "--failed"], (
            "the rerun must re-run the FAILED jobs of the same run id, so the "
            "job that did pass is not thrown away and no duplicate check-run "
            f"is created; got {rerun_calls[0][0][0]}"
        )
        assert "infrastructure" in capsys.readouterr().err.lower()

    def test_the_rerun_is_bounded_to_one_attempt(self, capsys):
        ci_run = {"databaseId": 4242, "name": "CI", "headBranch": "main"}
        with patch("rlsbl.commands.watch.time"), \
                patch("rlsbl.commands.watch._fetch_failure_log",
                      return_value=STRANDED_LOG), \
                patch("rlsbl.commands.watch.run_gh") as gh:
            gh.side_effect = [
                subprocess.CalledProcessError(1, "gh"),  # the stale run: failed
                "",                                      # run rerun --failed
                subprocess.CalledProcessError(1, "gh"),  # the rerun fails too
            ]
            result = _watch_single_run(ci_run, "candidate", "user/repo")

        assert result["passed"] is False
        rerun_calls = [
            c for c in gh.call_args_list if c[0][0][:2] == ["run", "rerun"]
        ]
        assert len(rerun_calls) == 1, (
            f"the rerun is bounded to ONE attempt; got {rerun_calls}"
        )

    def test_a_transient_failure_still_gets_the_whole_run_rerun(self):
        """Only the infra tier reruns the failed jobs; a flake rewinds all."""
        ci_run = {"databaseId": 55, "name": "CI", "headBranch": "main"}
        with patch("rlsbl.commands.watch.time"), \
                patch("rlsbl.commands.watch._fetch_failure_log",
                      return_value="read tcp: i/o timeout"), \
                patch("rlsbl.commands.watch.run_gh") as gh:
            gh.side_effect = [
                subprocess.CalledProcessError(1, "gh"),
                "",
                "",
            ]
            _watch_single_run(ci_run, "candidate", "user/repo")

        rerun = [c for c in gh.call_args_list if c[0][0][:2] == ["run", "rerun"]]
        assert rerun[0][0][0] == ["run", "rerun", "55"], (
            "a transient flake keeps the historical full rerun"
        )


class TestNotRetryingNamesTheRemedy:
    """(c), unconditionally: the abort must say what to do by hand."""

    def test_the_deterministic_abort_names_the_manual_rerun_and_resume(
        self, capsys,
    ):
        ci_run = {"databaseId": 909, "name": "CI", "headBranch": "main"}
        with patch("rlsbl.commands.watch.time"), \
                patch("rlsbl.commands.watch._fetch_failure_log",
                      return_value=(
                          "short test summary info\n"
                          "FAILED tests/test_x.py::test_y"
                      )), \
                patch("rlsbl.commands.watch.run_gh") as gh:
            gh.side_effect = [
                subprocess.CalledProcessError(1, "gh"),
            ]
            _watch_single_run(ci_run, "candidate", "user/repo")

        err = capsys.readouterr().err
        assert "deterministic failure detected; not retrying" in err
        assert "gh run rerun 909 --failed" in err, (
            "the operator must be given the exact manual remedy for a run "
            "this classification got wrong"
        )
        assert "rlsbl release resume" in err, (
            "and told how to pick the release back up afterwards"
        )


class TestResumedGateRecoversFromAStaleInfraRun:
    """End to end through the CI wait: the shape that stranded the release.

    A resumed release re-enters the gate on an UNCHANGED candidate, so the
    push is a no-op and the only run that will ever exist for that SHA is the
    stale, infra-killed one. Without the engine's own rerun the release can
    never go green through the tool.
    """

    def _gh_dispatcher(self, watch_results):
        """Answer gh by command; ``watch_results`` is consumed per run watch."""
        calls = []

        def fake(args, **kwargs):
            calls.append(args)
            if args[:2] == ["repo", "view"]:
                return '{"nameWithOwner": "user/repo"}'
            if args[:2] == ["run", "list"]:
                return '[{"databaseId": 77, "name": "CI", "status": "completed"}]'
            if args[:2] == ["run", "watch"]:
                outcome = watch_results.pop(0)
                if outcome is not True:
                    raise subprocess.CalledProcessError(1, "gh")
                return ""
            if args[:2] == ["run", "rerun"]:
                return ""
            # The failure logs are read the way rlsbl reads every job: the run
            # object for its attempt, the attempt-scoped job list, then each
            # failing job's own log.
            if args[0] == "api":
                path = args[-1]
                if path.endswith("/actions/runs/77"):
                    return '{"run_attempt": 1}'
                if "/attempts/1/jobs" in path:
                    return json.dumps({"jobs": [{
                        "id": 4321, "run_id": 77, "run_attempt": 1,
                        "name": "strictcli-ci / test", "status": "completed",
                        "conclusion": "failure",
                        "started_at": "2026-08-06T20:11:04Z",
                        "html_url": "https://github.com/user/repo/actions/"
                                    "runs/77/job/4321",
                    }]})
                if path.endswith("/actions/jobs/4321/logs"):
                    return STRANDED_LOG
            return ""

        return fake, calls

    def test_the_gate_goes_green_after_rerunning_the_infra_killed_run(
        self, tmp_path,
    ):
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: ci\non: push\n")

        # The stale run fails; its rerun passes.
        fake, calls = self._gh_dispatcher([False, True])
        with patch("rlsbl.commands.watch.time") as fake_time, \
                patch("rlsbl.utils.run_gh", side_effect=fake), \
                patch("rlsbl.commands.watch.run_gh", side_effect=fake):
            fake_time.time.side_effect = lambda: 0.0
            verdict, results = wait_for_ci_green(
                "a" * 40, timeout=600, check_filters=[],
                repo_root=str(tmp_path), log=lambda _m: None,
            )

        assert verdict == CI_GREEN, (
            "a resumed gate must recover from an infra-killed run instead of "
            f"stranding the release; got {verdict} ({results})"
        )
        assert ["run", "rerun", "77", "--failed"] in calls
