"""The generated CI router's ``run_all`` dispatch input.

The router gates every project's jobs on a ``dorny/paths-filter`` computed over
the diff a push carried. A release candidate whose push window touches only a
few members leaves every other member's job ``skipped`` -- and both gates
refuse a skipped check, correctly, because it proves nothing about the commit.

Until now the only sanctioned exits were "make the window wider" (a real commit
under each member's paths) or "release the members together with the commits
that touch them". Neither is available to a release whose fix-forward commits
are honestly narrow: the operator is left choosing between fabricating churn
and abandoning the version.

``workflow_dispatch`` with ``run_all: true`` is the third exit and the
structural one: it re-runs the SAME commit with the filter short-circuited, so
every member's job runs for real. The dispatched run's conclusions supersede
the skipped ones per name -- a skip is the absence of a verdict, so it loses to
any real conclusion of the same name whichever suite GitHub stamped first -- and
the release gate then sees a genuine success everywhere. Nothing is relaxed: the
jobs must still pass.
"""

import pytest
from ruamel.yaml import YAML

from rlsbl.ci_checks import (
    CheckFilter,
    ProjectCINotRunError,
    RUN_ALL_REMEDY,
    verify_project_ci_ran,
)
from rlsbl.commands.monorepo.sync import RUN_ALL_INPUT
from routerharness import generate_router


def _safe_load(text):
    return YAML(typ="safe").load(text)


def _ci_doc(job_if=None):
    job = {
        "runs-on": "ubuntu-latest",
        "steps": [{"uses": "actions/checkout@v6"}, {"run": "echo test"}],
    }
    if job_if is not None:
        job["if"] = job_if
    return {"jobs": {"test": job}}


def _projects(count=3, job_if=None):
    return [
        {
            "name": f"project-{i}",
            "path": f"packages/project-{i}",
            "_ci_docs": [(f"project-{i}-ci", _ci_doc(job_if))],
        }
        for i in range(1, count + 1)
    ]


class TestRunAllInputDeclaration:
    """The dispatch input exists, is a boolean, and defaults to off."""

    def test_workflow_dispatch_declares_a_boolean_run_all_input(self):
        parsed = _safe_load(generate_router(_projects()))
        dispatch = parsed["on"]["workflow_dispatch"]
        assert dispatch is not None, "workflow_dispatch must carry inputs"
        run_all = dispatch["inputs"]["run_all"]
        assert run_all["type"] == "boolean"
        assert run_all["default"] is False
        assert run_all["required"] is False
        assert "filter" in run_all["description"].lower()

    def test_push_and_pull_request_triggers_survive(self):
        parsed = _safe_load(generate_router(_projects()))
        on = parsed["on"]
        assert on["push"] == {"branches": ["main"]}
        assert "pull_request" in on


class TestRunAllShortCircuitsTheFilter:
    """Every inlined job runs when dispatched with ``run_all``."""

    def test_each_project_job_is_gated_on_the_filter_or_run_all(self):
        parsed = _safe_load(generate_router(_projects()))
        for i in range(1, 4):
            job = parsed["jobs"][f"project-{i}-ci-test"]
            assert job["if"] == (
                f"(needs.detect.outputs.project-{i} == 'true' "
                f"|| inputs.run_all)"
            )

    def test_a_job_s_own_condition_is_preserved_and_anded(self):
        """run_all overrides the paths filter, never the job's own condition."""
        parsed = _safe_load(
            generate_router(_projects(1, job_if="github.event_name == 'push'"))
        )
        job = parsed["jobs"]["project-1-ci-test"]
        assert job["if"] == (
            "(needs.detect.outputs.project-1 == 'true' || inputs.run_all) "
            "&& (github.event_name == 'push')"
        )

    def test_the_detect_job_stays_unconditional(self):
        """detect must still run: the jobs need it even when run_all is set."""
        parsed = _safe_load(generate_router(_projects()))
        assert "if" not in parsed["jobs"]["detect"]

    def test_every_project_job_carries_the_short_circuit(self):
        """No inlined job may be reachable only through the paths filter."""
        parsed = _safe_load(generate_router(_projects(5)))
        for key, job in parsed["jobs"].items():
            if key == "detect":
                continue
            assert "|| inputs.run_all" in job["if"], key


class TestRunAllDoesNotCancelThePushRun:
    """A run-all dispatch must not cancel the push run for the same SHA.

    Both runs' check runs land on the same commit, and the release gate reads
    the workflow runs too: a cancelled push run is a red verdict before the
    per-check collapse-to-latest ever runs. Keying the concurrency group on the
    dispatch input keeps the two runs in separate groups.
    """

    def test_concurrency_group_distinguishes_a_run_all_dispatch(self):
        parsed = _safe_load(generate_router(_projects()))
        group = parsed["concurrency"]["group"]
        assert "github.sha" in group
        assert "inputs.run_all" in group
        assert parsed["concurrency"]["cancel-in-progress"] is True


def _check_run(name, conclusion, *, run_id=1, started_at="2026-08-08T10:00:00Z"):
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "id": run_id,
        "started_at": started_at,
        "details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{run_id}",
    }


class TestRunAllConclusionsSupersedeSkipped:
    """The release CI gate reads the run-all conclusions, not the skipped ones.

    This is the property the whole remedy rests on: the gate collapses matching
    check runs to the latest per NAME, so the dispatched run's real conclusion
    replaces the push run's ``skipped`` without either gate being relaxed.
    """

    FILTER = CheckFilter("core", r"^(core\-ci) / ")

    def test_a_later_run_all_success_supersedes_the_skipped_check(self):
        runs = [
            _check_run("core-ci / test", "skipped", run_id=1,
                       started_at="2026-08-08T10:00:00Z"),
            _check_run("core-ci / test", "success", run_id=2,
                       started_at="2026-08-08T11:00:00Z"),
        ]
        # No exception: the gate is satisfied by the dispatched run.
        verify_project_ci_ran(
            "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
        )

    def test_a_failing_run_all_job_still_blocks(self):
        """Nothing is waived: a real failure in the dispatched run is fatal."""
        runs = [
            _check_run("core-ci / test", "skipped", run_id=1,
                       started_at="2026-08-08T10:00:00Z"),
            _check_run("core-ci / test", "failure", run_id=2,
                       started_at="2026-08-08T11:00:00Z"),
        ]
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )
        assert "failure" in str(exc.value)

    def test_a_pending_check_run_never_counts_as_a_verdict(self):
        """An in-flight run of the same job is not a conclusion at all."""
        pending = _check_run("core-ci / test", None, run_id=2,
                             started_at="2026-08-08T11:00:00Z")
        pending["status"] = "in_progress"
        runs = [
            _check_run("core-ci / test", "skipped", run_id=1,
                       started_at="2026-08-08T10:00:00Z"),
            pending,
        ]
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )
        assert "in_progress" in str(exc.value)


class TestASkipRecordedAFTERTheDispatchedVerdict:
    """A skip stamped later than the dispatched run's real conclusion.

    This is the shape the batch gate actually meets. rlsbl dispatches the
    router with ``run_all=true`` immediately after pushing the candidate,
    while the push run's own project jobs are still queued behind the
    router's ``detect`` job. GitHub stamps a skipped check run only when the
    job is finally evaluated, so the push run's ``skipped`` routinely carries
    a LATER ``started_at`` (and a higher id) than the dispatched run's real
    conclusion for the same job name.

    Ordering by time therefore hands the gate the skip and the candidate is
    refused although every job ran and passed on it -- the exact promise
    :data:`RUN_ALL_REMEDY` makes ("the run-all conclusions supersede the
    skipped ones") going unmet. A ``skipped`` conclusion is the ABSENCE of a
    verdict, so any completed non-skipped conclusion of the same name
    supersedes it, whichever check suite recorded it and in whatever order.
    """

    FILTER = CheckFilter("go-strictcli", r"^(go\-strictcli\-ci) / ")

    def _runs(self, dispatched_conclusion, *, skip_first=False):
        """The two check runs one SHA carries after a run_all dispatch.

        With *skip_first* false the push run's skip is stamped AFTER the
        dispatched conclusion -- the ordering that broke the batch gate.
        """
        skip_at = "2026-08-08T10:00:00Z" if skip_first else "2026-08-08T11:00:00Z"
        skip_id = 1 if skip_first else 2
        return [
            _check_run("go-strictcli-ci / test", "skipped", run_id=skip_id,
                       started_at=skip_at),
            _check_run("go-strictcli-ci / test", dispatched_conclusion,
                       run_id=3 - skip_id,
                       started_at="2026-08-08T10:30:00Z"),
        ]

    def test_a_later_recorded_skip_does_not_hide_the_dispatched_success(self):
        verify_project_ci_ran(
            "a" * 40, [self.FILTER], fetch=lambda: self._runs("success"),
            attempts=1,
        )

    def test_a_later_recorded_skip_does_not_hide_a_dispatched_failure(self):
        """Nothing is waived: the dispatched run's failure still blocks."""
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: self._runs("failure"),
                attempts=1,
            )
        assert "failure" in str(exc.value)

    def test_the_earlier_recorded_skip_is_superseded_too(self):
        """Order is irrelevant now: the verdict wins either way."""
        verify_project_ci_ran(
            "a" * 40, [self.FILTER],
            fetch=lambda: self._runs("success", skip_first=True), attempts=1,
        )

    def test_a_member_with_only_skips_still_refuses(self):
        """Two suites, both skipped: no verdict exists, so the gate refuses."""
        runs = [
            _check_run("go-strictcli-ci / test", "skipped", run_id=1,
                       started_at="2026-08-08T10:00:00Z"),
            _check_run("go-strictcli-ci / test", "skipped", run_id=2,
                       started_at="2026-08-08T11:00:00Z"),
        ]
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )
        assert "go-strictcli: go-strictcli-ci / test: skipped" in str(exc.value)
        assert RUN_ALL_REMEDY in str(exc.value)

    def test_one_members_supersede_never_covers_another_members_skip(self):
        """The batch shape: the gate still names the member that never ran."""
        other = CheckFilter("core", r"^(core\-ci) / ")
        runs = self._runs("success") + [
            _check_run("core-ci / test", "skipped", run_id=9,
                       started_at="2026-08-08T11:00:00Z"),
        ]
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER, other], fetch=lambda: runs, attempts=1,
            )
        message = str(exc.value)
        assert "core: core-ci / test: skipped" in message
        assert "go-strictcli" not in message


class TestTheRemedyIsNamedWhereTheOperatorHitsIt:
    """Every refusal that the dispatch resolves must name the dispatch."""

    def test_the_skipped_refusal_names_the_dispatch(self):
        from rlsbl.ci_checks import _failed_checks_message

        message = _failed_checks_message(
            "a" * 40, [("core", "core-ci / test", "skipped")],
        )
        assert RUN_ALL_REMEDY in message
        assert f"-f {RUN_ALL_INPUT}=true" in message

    def test_the_absent_checks_refusal_names_the_dispatch(self):
        from rlsbl.ci_checks import _no_checks_message

        message = _no_checks_message("a" * 40, [CheckFilter("core", "x")], set())
        assert RUN_ALL_REMEDY in message

    def test_the_empty_window_refusal_names_the_dispatch(self):
        from rlsbl.commands.release.execute import (
            _empty_candidate_window_message,
        )

        message = _empty_candidate_window_message(
            version="0.5.0", tag="www@v0.5.0", branch="main",
            candidate_sha="a" * 40, base_sha="b" * 40,
            patterns=["core/**"], changed={"docs/x.md"}, pushing=True,
        )
        assert RUN_ALL_REMEDY in message


class TestASkippedMatrixJobIsSupersededByItsLegs:
    """A skipped matrix job has no legs; the run that runs it has nothing else.

    GitHub does not expand a matrix for a job its ``if`` skipped: the whole job
    collapses to ONE check run named without the matrix suffix (``cli-ci /
    test``). The run that actually executes it emits one check run per leg
    (``cli-ci / test (3.12)``). The two therefore never share a name, so the
    plain collapse-to-latest-per-name cannot see that the later run answered the
    exact question the skip left open -- and the skip would block a release
    whose every job had in fact run and passed.

    A ``skipped`` conclusion is the ABSENCE of a verdict, not a verdict. It is
    dropped when a completed, non-skipped check run for the SAME job (its
    matrix expansion) exists, and those legs are then judged on their own
    conclusions. Which suite stamped which first is irrelevant -- a push run
    records its skip whenever its ``detect`` job finally releases it, often
    after the dispatched run has already concluded. Nothing else can cover a
    skip: not a sibling job, not a merely prefix-sharing name.
    """

    FILTER = CheckFilter("cli", r"^(cli\-ci) / ")

    def _runs(self, *specs):
        return [
            _check_run(name, conclusion, run_id=rid,
                       started_at=f"2026-08-08T10:{minute:02d}:00Z")
            for name, conclusion, rid, minute in specs
        ]

    def test_later_legs_supersede_the_skipped_placeholder(self):
        runs = self._runs(
            ("cli-ci / test", "skipped", 1, 0),
            ("cli-ci / test (3.12)", "success", 2, 1),
            ("cli-ci / test (3.13)", "success", 3, 1),
        )
        verify_project_ci_ran(
            "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
        )

    def test_a_failing_leg_still_blocks(self):
        runs = self._runs(
            ("cli-ci / test", "skipped", 1, 0),
            ("cli-ci / test (3.12)", "success", 2, 1),
            ("cli-ci / test (3.13)", "failure", 3, 1),
        )
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )
        assert "failure" in str(exc.value)

    def test_earlier_legs_supersede_a_later_recorded_skip(self):
        """A skip stamped after the legs is still the absence of a verdict."""
        runs = self._runs(
            ("cli-ci / test (3.12)", "success", 1, 0),
            ("cli-ci / test", "skipped", 2, 1),
        )
        verify_project_ci_ran(
            "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
        )

    def test_skipped_legs_never_cover_a_skipped_job(self):
        """Legs that were themselves skipped answer nothing."""
        runs = self._runs(
            ("cli-ci / test", "skipped", 1, 0),
            ("cli-ci / test (3.12)", "skipped", 2, 1),
        )
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )
        assert "skipped" in str(exc.value)

    def test_a_sibling_job_never_covers_a_skip(self):
        """Only the same job's own legs may supersede it -- never another job."""
        runs = self._runs(
            ("cli-ci / test", "skipped", 1, 0),
            ("cli-ci / lint", "success", 2, 1),
        )
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )
        assert "skipped" in str(exc.value)

    def test_a_name_that_merely_shares_a_prefix_never_covers_a_skip(self):
        """``test-extra`` is a different job from ``test``, not its leg."""
        runs = self._runs(
            ("cli-ci / test", "skipped", 1, 0),
            ("cli-ci / test-extra", "success", 2, 1),
        )
        with pytest.raises(ProjectCINotRunError):
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )

    def test_the_lone_skip_still_blocks(self):
        runs = self._runs(("cli-ci / test", "skipped", 1, 0))
        with pytest.raises(ProjectCINotRunError):
            verify_project_ci_ran(
                "a" * 40, [self.FILTER], fetch=lambda: runs, attempts=1,
            )
