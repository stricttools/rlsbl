"""The release CI gate and the publish gate must answer ONE question the same way.

Regression suite for the divergence that tagged versions which could never
publish.

The monorepo CI router gates every project's inlined jobs on
``dorny/paths-filter``. When a candidate push touches nothing under a
project's paths, that project's job concludes ``skipped`` while the router's
WORKFLOW RUN still concludes ``success``. The release gate watched workflow
runs, saw green, and tagged. The publish gate polls CHECK RUNS filtered by the
releasing project's name and refuses ``skipped`` -- correctly, since a skipped
check proves nothing. Net effect: a git tag and a public GitHub Release for a
version with no path to any registry, and no re-run that could ever fix it.

Observed live on a real monorepo release: a releasable resumed individually,
whose candidate range contained only ANOTHER releasable's finalize commit, was
tagged on a commit where its own ``<name>-ci-<target> / test`` check was
``skipped``. A sibling releasable escaped only because an unrelated commit in
its range happened to match its filter.

Distinct from the batch single-push defect: that one was about a BATCH pushing
per member. This one bites when a project is released or resumed
INDIVIDUALLY, where the candidate window legitimately holds only another
project's commits.

The property under test, everywhere the release flow can tag:

    the release flow must not tag unless the releasing project's OWN CI check
    actually RAN and PASSED on the candidate SHA.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import git

from rlsbl.ci_checks import (
    CheckFilter,
    PASSING_CONCLUSION,
    ProjectCINotRunError,
    failing_check_runs,
    latest_check_runs,
    monorepo_check_filters,
    release_check_filters,
    standalone_check_filter,
    verify_project_ci_ran,
)
from rlsbl.commands.release import resume_cmd, run_cmd
from rlsbl.commands.release.execute import _ci_not_run_message
from rlsbl.commands.release.release_state import get_state_path, load_release_state
from rlsbl.context import create_context
from rlsbl.workspace import get_releasable_changes_dir, get_releasable_dir

from test_representative_write_elimination import (  # noqa: E402
    _rc,
    _release_patches,
    _setup_releasable_workspace,
)

from conftest import with_root_member, make_workspace


TAG = "alpha@v1.0.1"
VERSION = "1.0.1"

# The router prefixes packages/core's own ci-npm.yml as `core-ci-npm` and
# stamps every inlined job `"<prefix> / <job>"`.
CORE_CHECK = "core-ci-npm / test"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _check_run(name, conclusion, *, status="completed", run_id=1):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "id": run_id,
        "started_at": "2026-08-05T00:00:00Z",
        "details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{run_id}",
    }


def _add_ci_workflows(root, core):
    """A push-triggered router at the root and the member's own CI file.

    This is the real monorepo shape: the router is what triggers on push, the
    member's ci-npm.yml is what mints the `core-ci-npm` job prefix.
    """
    root_wf = root / ".github" / "workflows"
    root_wf.mkdir(parents=True, exist_ok=True)
    (root_wf / "ci-router.yml").write_text(
        "name: CI Router\non:\n  push:\n    branches: [main]\njobs:\n"
        "  detect:\n    runs-on: ubuntu-latest\n    steps: []\n"
    )
    member_wf = core / ".github" / "workflows"
    member_wf.mkdir(parents=True, exist_ok=True)
    (member_wf / "ci-npm.yml").write_text(
        "name: CI\non:\n  push:\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps: []\n"
    )
    git(root, "add", ".github", "packages/core/.github")
    git(root, "commit", "-q", "-m", "add CI workflows")
    # Keep the release's own changelog-coverage preflight satisfied, so the
    # only thing that can stop this release is the CI gate under test.
    sha = git(root, "rev-parse", "HEAD")
    unreleased = os.path.join(
        get_releasable_changes_dir(str(root), "alpha"), "unreleased.jsonl"
    )
    with open(unreleased, "a", encoding="utf-8") as f:
        f.write(json.dumps({"commits": [sha], "user_facing": False}) + "\n")
    git(root, "add", os.path.relpath(unreleased, str(root)))
    git(root, "commit", "-q", "-m", "changelog: ci workflows")


def _gate_patches(check_runs):
    """Run the REAL release CI gate against a fixed set of check runs.

    Workflow-run discovery and watching are stubbed green -- that is exactly
    the state the defect needed: the router run concludes success while the
    project's own job inside it did not.
    """
    return (
        patch(
            "rlsbl.commands.watch.poll_runs",
            side_effect=lambda sha, **kw: [
                {"databaseId": 99, "name": "CI Router", "status": "completed",
                 "headBranch": "main", "workflowName": "CI Router"},
            ],
        ),
        patch(
            "rlsbl.commands.watch._watch_runs",
            return_value=[{"name": "CI Router", "passed": True, "run_id": "99"}],
        ),
        patch("rlsbl.commands.watch.run_gh",
              return_value='{"nameWithOwner": "o/r"}'),
        patch("rlsbl.ci_checks.fetch_ci_jobs", return_value=list(check_runs)),
        # No sleeping between check-run discovery attempts in tests.
        patch("rlsbl.ci_checks.CHECK_DISCOVERY_ATTEMPTS", 1),
    )


def _gh_recorder(calls):
    def recorder(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["release", "view"]:
            raise subprocess.CalledProcessError(1, "gh release view")
        return ""

    return recorder


def _run(core, root, check_runs, gh_calls=None):
    ctx = create_context(Path(str(core)), workspace_root=Path(str(root)))
    extra = list(_gate_patches(check_runs))
    if gh_calls is not None:
        extra.append(
            patch("rlsbl.commands.release.run_gh", side_effect=_gh_recorder(gh_calls))
        )
    patches = _release_patches(tuple(extra))
    for p in patches:
        p.start()
    try:
        run_cmd(_rc(), {"quiet": True, "skip-lock": True}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


def _resume(core, root, check_runs):
    ctx = create_context(Path(str(core)), workspace_root=Path(str(root)))
    state = load_release_state(_state_path(root))
    assert state is not None, "a resumable release state must exist"
    patches = _release_patches(tuple(_gate_patches(check_runs)))
    for p in patches:
        p.start()
    try:
        resume_cmd(state, {"quiet": True, "skip-lock": True}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


def _state_path(root):
    return get_state_path("", releasable_dir=get_releasable_dir(str(root), "alpha"))


def _tags(root):
    return git(root, "tag", "-l").split()


# --------------------------------------------------------------------------- #
# The defect, end to end
# --------------------------------------------------------------------------- #


class TestSkippedProjectCheckBlocksTheTag:
    """A green workflow run whose project job was SKIPPED must not tag."""

    def test_a_skipped_project_check_is_never_tagged(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        _add_ci_workflows(tmp_project, core)
        gh_calls = []

        with pytest.raises(SystemExit) as exc:
            _run(core, tmp_project,
                 [_check_run(CORE_CHECK, "skipped")], gh_calls=gh_calls)
        assert exc.value.code == 1

        assert TAG not in _tags(tmp_project), (
            "a candidate whose own CI job was SKIPPED must never be tagged -- "
            "the publish gate refuses the same check, so the tag could never "
            "publish"
        )
        assert not [c for c in gh_calls if c[:2] == ["release", "create"]], (
            "no GitHub Release may exist for a version that cannot publish"
        )
        # Nothing finalized: the version is not burnt and the release resumes.
        state = load_release_state(_state_path(tmp_project))
        assert state is not None
        assert state["new_version"] == VERSION
        assert "CI_VERIFIED" not in state["completed_steps"]
        assert "CI_VERIFIED" in state.get("failed_steps", {})

    def test_the_error_names_the_paths_filter_and_the_remedy(
        self, tmp_project, capsys
    ):
        core = _setup_releasable_workspace(tmp_project)
        _add_ci_workflows(tmp_project, core)

        with pytest.raises(SystemExit):
            _run(core, tmp_project, [_check_run(CORE_CHECK, "skipped")])
        out = capsys.readouterr()
        text = out.out + out.err

        assert "skipped" in text
        assert CORE_CHECK in text
        assert "paths filter" in text, (
            "the operator must be told WHY the job did not run"
        )
        assert "ci-router.yml" in text and "watch" in text, (
            "the operator must be told WHERE the filter lives"
        )
        assert "rlsbl release resume" in text
        assert "was not" in text and "finalized" in text, (
            "the operator must be told the version is not burnt"
        )

    def test_a_passing_project_check_still_tags(self, tmp_project):
        """The gate must not fire on the happy path."""
        core = _setup_releasable_workspace(tmp_project)
        _add_ci_workflows(tmp_project, core)

        _run(core, tmp_project, [_check_run(CORE_CHECK, PASSING_CONCLUSION)])

        assert TAG in _tags(tmp_project)
        assert load_release_state(_state_path(tmp_project)) is None

    def test_no_check_run_at_all_blocks_the_tag(self, tmp_project):
        """CI ran, but produced nothing for this project: still not evidence."""
        core = _setup_releasable_workspace(tmp_project)
        _add_ci_workflows(tmp_project, core)

        with pytest.raises(SystemExit):
            _run(core, tmp_project, [_check_run("other-ci / test", "success")])
        assert TAG not in _tags(tmp_project)

    def test_resume_applies_the_same_gate(self, tmp_project):
        """The live incident was a RESUME of an individually-released project."""
        core = _setup_releasable_workspace(tmp_project)
        _add_ci_workflows(tmp_project, core)

        with pytest.raises(SystemExit):
            _run(core, tmp_project, [_check_run(CORE_CHECK, "skipped")])
        assert TAG not in _tags(tmp_project)

        # Resume with the check still skipped: still refused.
        with pytest.raises(SystemExit):
            _resume(core, tmp_project, [_check_run(CORE_CHECK, "skipped")])
        assert TAG not in _tags(tmp_project), (
            "a resume must not be a way around the gate the run refused"
        )

        # Resume once the project's own CI genuinely ran: same version.
        _resume(core, tmp_project, [_check_run(CORE_CHECK, PASSING_CONCLUSION)])
        assert TAG in _tags(tmp_project)


# --------------------------------------------------------------------------- #
# One matcher, not two
# --------------------------------------------------------------------------- #


class TestTheTwoGatesUseOneMatcher:

    def test_release_filter_equals_the_publish_router_regex(self, tmp_path):
        """The release gate's regex IS the one baked into the publish router."""
        from rlsbl.ci_router import _router_ci_check_regex

        proj_dir = tmp_path / "packages" / "core"
        wf = proj_dir / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci-npm.yml").write_text("name: ci\non: push\n")
        (wf / "ci-pypi.yml").write_text("name: ci\non: push\n")

        # What `monorepo sync` bakes into publish.yml's case arm, given the
        # `_ci_files` it discovered while inlining.
        baked = _router_ci_check_regex(
            {"name": "core", "_ci_files": ["core-ci-npm.yml", "core-ci-pypi.yml"]}
        )
        # What the release gate resolves at release time, from disk.
        resolved = monorepo_check_filters(
            str(tmp_path), {"core": str(proj_dir)}
        )[0].regex
        assert resolved == baked

    def test_standalone_filter_equals_the_scaffolded_publish_gate_regex(self):
        from rlsbl.publish_gate import (
            ci_check_regex_for_targets,
            gate_targets_from_config,
        )

        config = {"targets": ["npm", {"name": "go", "path": "go"}]}
        baked = ci_check_regex_for_targets(
            gate_targets_from_config(config, "npm")
        )
        assert standalone_check_filter(config, "npm").regex == baked

    def test_only_success_passes_on_both_sides(self):
        from rlsbl.publish_gate import GATE_POLL_SCRIPT

        # The publish gate's reject rule, in its own words.
        assert f'.conclusion != "{PASSING_CONCLUSION}"' in GATE_POLL_SCRIPT
        assert "the gate cannot treat a skipped check as passing" in \
            GATE_POLL_SCRIPT.lower() or "SKIPPED" in GATE_POLL_SCRIPT

        # The release gate's, in Python.
        for bad in ("skipped", "cancelled", "failure", "timed_out", "neutral"):
            assert failing_check_runs([_check_run("test", bad)]) == [("test", bad)]
        assert failing_check_runs([_check_run("test", PASSING_CONCLUSION)]) == []

    def test_a_retried_check_run_supersedes_the_stale_one(self):
        """Same collapse-to-latest rule the publish gate's jq applies."""
        stale = _check_run("core-ci / test", "failure", run_id=1)
        stale["started_at"] = "2026-08-05T00:00:00Z"
        fresh = _check_run("core-ci / test", "success", run_id=2)
        fresh["started_at"] = "2026-08-05T01:00:00Z"
        latest = latest_check_runs([stale, fresh], r"^(core\-ci) / ")
        assert [r["conclusion"] for r in latest] == ["success"]


# --------------------------------------------------------------------------- #
# The three outcomes stay distinct
# --------------------------------------------------------------------------- #


class TestNoCIVersusSkipped:

    def test_a_repo_with_no_push_triggered_workflow_still_proceeds(self, tmp_path):
        """Outcome 1 is untouched: no CI at all is a loud notice, not an error."""
        from rlsbl.commands.watch import CI_NOT_CONFIGURED, wait_for_ci_green

        verdict, results = wait_for_ci_green(
            "0" * 40, timeout=1, check_filters=[CheckFilter("x", "^(test)$")],
            repo_root=str(tmp_path), log=lambda _m: None,
        )
        assert verdict == CI_NOT_CONFIGURED
        assert results == []

    def test_a_project_with_no_ci_workflow_of_its_own_is_a_notice(
        self, tmp_path, capsys
    ):
        """Outcome 2: nothing was ever inlined for it, so nothing to verify."""
        proj_dir = tmp_path / "packages" / "core"
        proj_dir.mkdir(parents=True)
        filters = monorepo_check_filters(str(tmp_path), {"core": str(proj_dir)})
        assert filters == [CheckFilter("core", None)]

        verify_project_ci_ran("a" * 40, filters, fetch=lambda: [])
        assert "has no CI workflow of its own" in capsys.readouterr().err

    def test_a_skipped_job_is_neither_and_hard_errors(self):
        """Outcome 3: workflows exist, the project has CI, its job was skipped."""
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [CheckFilter("core", r"^(core\-ci) / ")],
                fetch=lambda: [_check_run("core-ci / test", "skipped")],
                attempts=1,
            )
        assert "skipped" in str(exc.value)
        assert "paths filter" in str(exc.value)

    def test_missing_checks_name_what_was_actually_present(self):
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran(
                "a" * 40, [CheckFilter("core", r"^(core\-ci) / ")],
                fetch=lambda: [_check_run("other-ci / test", "success")],
                attempts=1,
            )
        msg = str(exc.value)
        assert "no check run matches" in msg
        assert "other-ci / test" in msg, (
            "naming the checks that DID run is how the operator spots a "
            "renamed job or the wrong project"
        )
        assert "paths filter" in msg


# --------------------------------------------------------------------------- #
# The gate is not optional
# --------------------------------------------------------------------------- #


class TestTheGateCannotBeForgotten:

    def test_wait_for_ci_green_requires_check_filters(self, tmp_path):
        """A caller that omits the filter would silently re-open the divergence."""
        from rlsbl.commands.watch import wait_for_ci_green

        with pytest.raises(TypeError):
            wait_for_ci_green("a" * 40, timeout=1, repo_root=str(tmp_path))

    def test_batch_gate_verifies_every_member(self, tmp_path):
        """The batch tags every member, so it verifies every member."""
        import inspect

        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(batch_release._batch_ci_gate)
        assert "workspace_check_filters" in source
        assert "check_filters=check_filters" in source
        # Both call sites pass the pending set.
        whole = inspect.getsource(batch_release)
        assert whole.count(
            "_batch_ci_gate(\n                    workspace_root, flags, log, "
            "candidate_sha, pending,\n                )"
        ) == 2


# --------------------------------------------------------------------------- #
# Message content
# --------------------------------------------------------------------------- #


class TestNotRunMessageIsItsOwnThing:

    def test_it_is_not_the_red_ci_message(self):
        msg = _ci_not_run_message(
            version="1.2.3", tag="v1.2.3", branch="main",
            candidate_sha="a" * 40,
            detail="core: core-ci-npm / test: skipped",
        )
        # A red CI says "fix the failure". This is not that.
        assert "CI never ran for this project" in msg
        assert "NOT a CI failure" in msg
        assert "paths filter that matched nothing matches nothing every" in msg
        # ... but re-running it with the filter SHORT-CIRCUITED is a different
        # act, and the message must not read as a blanket ban on re-running.
        assert "SHORT-CIRCUITED" in msg
        # The concrete remedy.
        assert "ci-router.yml" in msg
        assert "rlsbl release resume" in msg
        # The reassurance that nothing is burnt.
        assert "no orphan version file to clean up" in msg
        assert "no GitHub Release exists" in msg


# --------------------------------------------------------------------------- #
# Standalone repositories agree too
# --------------------------------------------------------------------------- #


class TestStandaloneAgrees:

    def test_standalone_release_resolves_the_publish_gate_filter(self, tmp_path):
        filters = release_check_filters(
            config={"targets": ["pypi"]}, registry="pypi",
            project_dir=str(tmp_path),
        )
        assert len(filters) == 1
        assert filters[0].regex == r"^(test)( \(.*\))?$"

    def test_a_matrix_leg_still_matches(self):
        runs = [_check_run("test (3.12)", "success"),
                _check_run("test (3.13)", "skipped", run_id=2)]
        flt = standalone_check_filter({"targets": ["pypi"]}, "pypi")
        with pytest.raises(ProjectCINotRunError) as exc:
            verify_project_ci_ran("a" * 40, [flt], fetch=lambda: runs, attempts=1)
        assert "test (3.13)" in str(exc.value)


# --------------------------------------------------------------------------- #
# Releasable membership
# --------------------------------------------------------------------------- #


class TestReleasableMembersAreAllVerified:

    def test_every_member_of_the_releasable_is_in_scope(self, tmp_project):
        """One tag publishes every member, so every member must have run."""
        from rlsbl.workspace import Releasable, save_workspace

        for name in ("core", "extra"):
            wf = tmp_project / "packages" / name / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci-npm.yml").write_text("name: ci\non: push\n")
        save_workspace(
            str(tmp_project),
            with_root_member([
                {"path": "packages/core", "name": "core", "releasable": "alpha"},
                {"path": "packages/extra", "name": "extra", "releasable": "alpha"},
            ]),
            releasables=[Releasable(name="alpha")],
        )

        filters = release_check_filters(
            config={}, registry="npm",
            project_dir=str(tmp_project / "packages" / "core"),
            workspace_root=str(tmp_project),
            monorepo_name="core", releasable_name="alpha",
        )
        assert {f.label for f in filters} == {"core", "extra"}
        assert all(f.regex for f in filters)

    def test_the_releasing_project_is_covered_even_without_the_workspace(
        self, tmp_project
    ):
        """No workspace state may narrow the releasing project out of scope."""
        from rlsbl.workspace import save_workspace

        wf = tmp_project / "packages" / "core" / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci-npm.yml").write_text("name: ci\non: push\n")
        # A workspace that lists nothing: the widening lookup finds no
        # siblings, and the releasing project must still be verified.
        make_workspace(str(tmp_project), [])

        filters = release_check_filters(
            config={}, registry="npm",
            project_dir=str(tmp_project / "packages" / "core"),
            workspace_root=str(tmp_project),
            monorepo_name="core", releasable_name="alpha",
        )
        assert [f.label for f in filters] == ["core"]
        assert filters[0].regex == r"^(core\-ci\-npm) / "

    def test_a_directory_no_member_declares_is_the_root_members(self, tmp_project):
        """Inside a workspace, every directory belongs to some member.

        A directory no declared member claims used to be rejected as outside
        every project; the root member owns the residual, so it answers.
        """
        from rlsbl.ci_checks import workspace_check_filters

        make_workspace(str(tmp_project), [{"path": "packages/core", "name": "core"}])
        filters = workspace_check_filters(
            str(tmp_project), [str(tmp_project / "packages" / "ghost")]
        )
        assert filters is not None

    def test_batch_rejects_a_directory_outside_the_workspace(self, tmp_project):
        from rlsbl.ci_checks import workspace_check_filters

        make_workspace(str(tmp_project), [{"path": "packages/core", "name": "core"}])
        outside = tmp_project.parent / "outside-the-workspace"
        outside.mkdir(exist_ok=True)
        with pytest.raises(ProjectCINotRunError) as exc:
            workspace_check_filters(str(tmp_project), [str(outside)])
        assert "not inside any project registered in the workspace" in str(exc.value)
