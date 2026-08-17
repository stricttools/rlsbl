"""Job enumeration goes through the ATTEMPT-SCOPED endpoint, everywhere.

Observed live on a private monorepo whose release was stranded by it. With the
same authenticated token, on the same repository:

    repos/O/R/actions/runs                      -> 404
    repos/O/R/actions/runs/<id>/jobs            -> 404
    repos/O/R/commits/<sha>/check-runs          -> 404
    repos/O/R/actions/runs/<id>                 -> 200, the run object
    repos/O/R/actions/runs/<id>/attempts/<n>/jobs -> 200, every job
    repos/O/R/actions/jobs/<job-id>/logs        -> 200, the whole log

Two failures followed from reading the endpoints on the left:

1. A transient CI failure (a 429 on an action download) could not be
   classified, because the classifier fetched logs through ``gh run view
   --log-failed``, which walks the unscoped job list. Classification collapsed,
   the blind retry then also failed, and a release died on a flake the
   classified auto-retry exists to absorb.
2. After the flake was rerun by hand and CI went green, the release reported
   the workflow ``passed`` and then aborted on a bare ``gh: Not Found (HTTP
   404)`` -- the gate verifying each member's own job read a collection
   endpoint that 404s on this repository, on a candidate whose CI was green.

The attempt-scoped endpoint is not a second path tried when the first fails.
It is the only one rlsbl reads for jobs: it answers on repositories where the
collections do not, and it names the attempt, which is what an in-place rerun
(same run id, new attempt) leaves behind.
"""

import json
import re
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.ci_checks import (
    CheckFilter,
    ProjectCINotRunError,
    fetch_run_jobs,
    verify_project_ci_ran,
)
from rlsbl.commands import watch


SHA = "4d59e0ca92bdc34e4253edcbfb4baacc2f80a2ee"
RUN_ID = "32038759080"

# The 429 that stranded the real release: GitHub rate-limited an action
# download, so the job died below the code under test.
RATE_LIMITED_LOG = "\n".join(
    [
        "2026-08-17T14:23:40.0000000Z ##[group]Run astral-sh/setup-uv@v7",
        "2026-08-17T14:23:41.0000000Z Downloading uv from tag v0.9.9",
        "2026-08-17T14:23:43.4807064Z ##[error]Failed to fetch version data: "
        "429 Too Many Requests",
        # Everything after the marker is post-step housekeeping: a blind tail
        # of a long log holds only this.
        *[
            f"2026-08-17T14:23:5{i % 10}.0000000Z [command]/usr/bin/git config "
            f"--local --unset-all http.extraheader"
            for i in range(300)
        ],
        "2026-08-17T14:23:59.0000000Z Cleaning up orphan processes",
    ]
)


def _job(name, conclusion, *, job_id, run_id=RUN_ID, attempt=1,
         started_at="2026-08-17T14:21:36Z"):
    return {
        "id": job_id,
        "run_id": int(run_id),
        "run_attempt": attempt,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": started_at,
        "html_url": (
            f"https://github.com/o/r/actions/runs/{run_id}/job/{job_id}"
        ),
    }


class FakeGitHub:
    """A repository shaped exactly like the one that stranded the release.

    Every request is recorded, so a test can assert not only that the right
    endpoint answered but that the 404-ing one was never asked.
    """

    def __init__(self, *, attempts, run_attempt=1, logs=None):
        # {run id: {attempt number: [job, ...]}}
        self.attempts = attempts
        self.run_attempt = run_attempt
        self.logs = logs or {}
        self.paths = []

    def _not_found(self, path):
        return subprocess.CalledProcessError(
            1, ["gh", "api", "--method", "GET", path],
            stderr="gh: Not Found (HTTP 404)\n",
        )

    def __call__(self, args, **kwargs):
        path = args[-1]
        self.paths.append(path)

        # --- The endpoints this repository refuses ------------------------
        if "check-runs" in path or re.search(r"/actions/runs\?", path):
            raise self._not_found(path)
        if re.search(r"/actions/runs/\d+/jobs", path):
            raise self._not_found(path)

        # --- The endpoints it answers -------------------------------------
        match = re.match(r"repos/\S+/actions/runs/(\d+)$", path)
        if match:
            return json.dumps({
                "id": int(match.group(1)),
                "status": "completed",
                "conclusion": "success",
                "run_attempt": self.run_attempt,
            })

        match = re.match(
            r"repos/\S+/actions/runs/(\d+)/attempts/(\d+)/jobs", path
        )
        if match:
            run_id, attempt = match.group(1), int(match.group(2))
            jobs = self.attempts.get(run_id, {}).get(attempt, [])
            return json.dumps({"total_count": len(jobs), "jobs": jobs})

        match = re.match(r"repos/\S+/actions/jobs/(\d+)/logs$", path)
        if match:
            return self.logs.get(int(match.group(1)), "")

        raise AssertionError(f"unexpected gh request: {path}")

    @property
    def asked_unscoped_jobs(self):
        return any(re.search(r"/actions/runs/\d+/jobs", p) for p in self.paths)

    @property
    def asked_check_runs(self):
        return any("check-runs" in p for p in self.paths)


# --------------------------------------------------------------------------- #
# The release gate
# --------------------------------------------------------------------------- #


class TestReleaseGateJobEnumeration:

    def _gate(self, gh, *, run_ids=(RUN_ID,), regex=r"^core\-ci / "):
        with patch("rlsbl.utils.run_gh", side_effect=gh):
            verify_project_ci_ran(
                SHA, [CheckFilter("core", regex)], run_ids=list(run_ids),
                attempts=1,
            )

    def test_a_green_candidate_is_verified_where_the_collections_404(self):
        """The defect, end to end: a green candidate must not abort on a 404."""
        gh = FakeGitHub(attempts={RUN_ID: {1: [
            _job("detect", "success", job_id=1),
            _job("core-ci / test (3.12)", "success", job_id=2),
            _job("core-ci / test (3.13)", "success", job_id=3),
        ]}})
        self._gate(gh)
        assert not gh.asked_unscoped_jobs, (
            "the unscoped job collection 404s on this repository; the gate "
            "must never read it"
        )
        assert not gh.asked_check_runs

    def test_the_latest_attempt_is_the_one_read(self):
        """After an in-place rerun the run id is unchanged; the attempt is not.

        Attempt 1 failed and attempt 2 passed. Reading anything but the latest
        attempt reports a verdict the rerun already replaced.
        """
        gh = FakeGitHub(
            run_attempt=2,
            attempts={RUN_ID: {
                1: [_job("core-ci / test (3.12)", "failure", job_id=2)],
                2: [_job("core-ci / test (3.12)", "success", job_id=9,
                         attempt=2)],
            }},
        )
        self._gate(gh)
        assert any("/attempts/2/jobs" in p for p in gh.paths)
        assert not any("/attempts/1/jobs" in p for p in gh.paths)

    def test_a_failing_job_still_blocks_the_release(self):
        gh = FakeGitHub(attempts={RUN_ID: {1: [
            _job("core-ci / test (3.12)", "failure", job_id=2),
        ]}})
        with pytest.raises(ProjectCINotRunError) as exc:
            self._gate(gh)
        assert "core-ci / test (3.12): failure" in str(exc.value)

    def test_a_skipped_job_still_blocks_the_release(self):
        gh = FakeGitHub(attempts={RUN_ID: {1: [
            _job("core-ci / test (3.12)", "skipped", job_id=2),
        ]}})
        with pytest.raises(ProjectCINotRunError) as exc:
            self._gate(gh)
        assert "skipped" in str(exc.value)

    def test_a_project_with_no_job_at_all_blocks_the_release(self):
        gh = FakeGitHub(attempts={RUN_ID: {1: [_job("detect", "success", job_id=1)]}})
        with pytest.raises(ProjectCINotRunError) as exc:
            self._gate(gh)
        assert "no check run matches" in str(exc.value)

    def test_a_dispatched_runs_verdict_supersedes_the_push_runs_skip(self):
        """Two runs on one commit, collapsed by job name across both.

        This is the ``run_all`` shape: the push run's paths filter skipped the
        member, the dispatched run really ran it.
        """
        other = "32038759999"
        gh = FakeGitHub(attempts={
            RUN_ID: {1: [
                _job("core-ci / test (3.12)", "skipped", job_id=2,
                     started_at="2026-08-17T14:30:00Z"),
            ]},
            other: {1: [
                _job("core-ci / test (3.12)", "success", job_id=7,
                     run_id=other, started_at="2026-08-17T14:21:00Z"),
            ]},
        })
        self._gate(gh, run_ids=(RUN_ID, other))
        assert not gh.asked_unscoped_jobs

    def test_paginated_attempts_are_all_decoded(self):
        """151 jobs arrive as two concatenated pages, as they do live."""
        page1 = json.dumps({"jobs": [_job("core-ci / test (3.12)", "success",
                                          job_id=2)]})
        page2 = json.dumps({"jobs": [_job("core-ci / test (3.13)", "success",
                                          job_id=3)]})

        def gh(args, **kwargs):
            path = args[-1]
            if path.endswith(f"/actions/runs/{RUN_ID}"):
                return json.dumps({"run_attempt": 1})
            assert "/attempts/1/jobs" in path
            assert "--paginate" in args
            return page1 + "\n" + page2

        with patch("rlsbl.utils.run_gh", side_effect=gh):
            jobs = fetch_run_jobs(RUN_ID)
        assert [j["name"] for j in jobs] == [
            "core-ci / test (3.12)", "core-ci / test (3.13)",
        ]

    def test_the_reads_match_the_get_pinned_observe_prefix(self):
        """Without --method GET the read is not an observe.

        The allowlist pins ``gh api`` to ``--method GET`` because the bare
        prefix would legalize POST. A read that omits the flag matches
        nothing, so under --dry-run it is RECORDED instead of performed and
        the gate reads a carrier where it expected JSON.
        """
        from rlsbl import observe_allowlist as oa

        seen = []

        def gh(args, **kwargs):
            seen.append(["gh", *args])
            if args[-1].endswith(f"/actions/runs/{RUN_ID}"):
                return json.dumps({"run_attempt": 1})
            return json.dumps({"jobs": []})

        with patch("rlsbl.utils.run_gh", side_effect=gh):
            fetch_run_jobs(RUN_ID)

        assert len(seen) == 2
        for argv in seen:
            assert any(
                len(e.argv) <= len(argv) and tuple(argv[: len(e.argv)]) == e.argv
                for e in oa.OBSERVE_ALLOWLIST
            ), f"{' '.join(argv)} matches no observe prefix"

    def test_a_gate_with_neither_run_ids_nor_fetch_is_a_hard_error(self):
        """A caller that forgot the run ids must not verify nothing."""
        with pytest.raises(TypeError) as exc:
            verify_project_ci_ran(SHA, [CheckFilter("core", r"^core\-ci / ")])
        assert "run_ids" in str(exc.value)


# --------------------------------------------------------------------------- #
# The watch's failure classification
# --------------------------------------------------------------------------- #


class TestFailureClassificationJobEnumeration:

    def _fetch(self, gh):
        with patch("rlsbl.utils.run_gh", side_effect=gh), \
             patch("rlsbl.commands.watch.run_gh", side_effect=gh):
            return watch._fetch_failure_log(RUN_ID)

    def test_the_rate_limited_flake_is_classified_transient(self):
        gh = FakeGitHub(
            attempts={RUN_ID: {1: [
                _job("detect", "success", job_id=1),
                _job("deploy-strategy-ci / test (3.13)", "failure", job_id=2),
                _job("auth-sdk-js-ci / test", "skipped", job_id=3),
            ]}},
            logs={2: RATE_LIMITED_LOG},
        )
        text = self._fetch(gh)
        assert not gh.asked_unscoped_jobs
        assert "429 Too Many Requests" in text, (
            "the failure marker must survive the log-region selection, or the "
            "classifier sees only post-step housekeeping"
        )
        assert watch._classify_failure(text) == "transient"

    def test_the_failing_jobs_are_named(self):
        """A fifty-job router run names the workflow; the operator needs the job."""
        gh = FakeGitHub(
            attempts={RUN_ID: {1: [
                _job("deploy-strategy-ci / test (3.13)", "failure", job_id=2),
                _job("email-ci / test (3.12)", "failure", job_id=4),
            ]}},
            logs={2: RATE_LIMITED_LOG, 4: RATE_LIMITED_LOG},
        )
        text = self._fetch(gh)
        assert "deploy-strategy-ci / test (3.13)" in text
        assert "email-ci / test (3.12)" in text

    def test_only_a_bounded_number_of_failing_jobs_is_fetched(self):
        failures = [
            _job(f"m{i}-ci / test", "failure", job_id=100 + i)
            for i in range(watch._LOG_FETCH_MAX_JOBS + 3)
        ]
        gh = FakeGitHub(
            attempts={RUN_ID: {1: failures}},
            logs={job["id"]: RATE_LIMITED_LOG for job in failures},
        )
        text = self._fetch(gh)
        fetched = [p for p in gh.paths if p.endswith("/logs")]
        assert len(fetched) == watch._LOG_FETCH_MAX_JOBS
        assert "and 3 more failing job(s)" in text
        # The unfetched ones are still named.
        assert failures[-1]["name"] in text

    def test_a_run_whose_jobs_produced_nothing_is_infra(self):
        """No failing job, or no output from it, established nothing."""
        gh = FakeGitHub(
            attempts={RUN_ID: {1: [_job("detect", "cancelled", job_id=1)]}},
            logs={1: ""},
        )
        assert watch._classify_failure(self._fetch(gh)) == "infra"

    def test_a_job_with_no_error_marker_falls_back_to_its_tail(self):
        tail = "\n".join(f"line {i}" for i in range(500)) + "\nFAILED tests/test_x.py"
        gh = FakeGitHub(
            attempts={RUN_ID: {1: [_job("core-ci / test", "failure", job_id=2)]}},
            logs={2: tail},
        )
        text = self._fetch(gh)
        assert "FAILED tests/test_x.py" in text
        assert watch._classify_failure(text) == "deterministic"


# --------------------------------------------------------------------------- #
# The wiring: the gate is told which runs were watched
# --------------------------------------------------------------------------- #


class TestGateReceivesTheWatchedRuns:

    def test_wait_for_ci_green_passes_the_watched_run_ids(self, tmp_path):
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci-router.yml").write_text(
            "name: CI Router\non:\n  push:\n    branches: [main]\njobs:\n"
            "  detect:\n    runs-on: ubuntu-latest\n    steps: []\n"
        )
        seen = {}

        def fake_verify(sha, filters, **kwargs):
            seen.update(kwargs)

        runs = [
            {"databaseId": 99, "name": "CI Router", "status": "completed"},
        ]
        with patch("rlsbl.commands.watch.poll_runs",
                   side_effect=lambda sha, **kw: list(runs)), \
             patch("rlsbl.commands.watch._watch_runs",
                   return_value=[{"name": "CI Router", "passed": True,
                                  "run_id": "99"}]), \
             patch("rlsbl.commands.watch.run_gh",
                   return_value='{"nameWithOwner": "o/r"}'), \
             patch("rlsbl.commands.watch.time.sleep", lambda *_: None), \
             patch("rlsbl.commands.watch.verify_project_ci_ran",
                   side_effect=fake_verify):
            verdict, _ = watch.wait_for_ci_green(
                SHA, timeout=600, check_filters=[CheckFilter("core", r"^core")],
                repo_root=str(tmp_path), log=lambda _m: None,
            )

        assert verdict == watch.CI_GREEN
        assert seen["run_ids"] == ["99"]
