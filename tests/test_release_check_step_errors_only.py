"""The release's check step blocks only on error-level failures, and names them.

A check at warn -- registered that way, or softened to warn by an options
entry -- is reported but never blocks a release, for every bump type. What
decides the verdict is the presence of an error-level failure among the
results, never the run's exit code: a run whose only non-passing results were
warnings used to exit nonzero, list no failure, and abort the release with
"Preflight checks failed (0 failure(s))" and nothing else.

"A non-infra release needs at least one user-facing changelog entry" is a
release validation rule beside the infra rule, not a warning the check step
has to promote into an error.
"""

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import write_covered_unreleased
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


class _Result:
    """Stand-in for strictcli's CheckRunResult: the accessors the release reads."""

    def __init__(self, name, status, message):
        self.name = name
        self.status = status
        self.message = message

    def gated(self):
        return self.status == "fail"


def _setup_project(root, *, user_facing):
    (root / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    (root / "CHANGELOG.md").write_text("# Changelog\n")
    changes_dir = root / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (root / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n"
    )
    if user_facing:
        write_covered_unreleased(root, changes_dir=changes_dir)
    else:
        (changes_dir / "unreleased.jsonl").write_text(
            json.dumps({
                "format_version": 1, "id": "x", "commits": ["abc1234"],
                "user_facing": False,
            }) + "\n"
        )


_R = "rlsbl.commands.release."

# Everything around the check step is stubbed so the release runs from its
# changelog preflight to its mutating half, which is recorded instead of run.
_STUBS = {
    "_run_release_mutating": None,
    "resolve_release_targets": [],
    "run": "",
    "commit_files": True,
    "generate_changelog": None,
    "validate_release_targets": "npm",
    "validate_pipeline_config": None,
    "validate_config_integrity": None,
    "validate_ota_mode": None,
    "validate_gh_cli": None,
    "validate_gh_push_access": None,
    "validate_branch_and_remote": "main",
    "resolve_monorepo_context": (None, None, False, False, None),
    "validate_blog_body": (None, None),
    "_abort_on_scaffold_conflicts": None,
    "resolve_target_paths": {},
    "extract_changelog_entry_from_text": "- test",
    "working_tree_paths": [],
    "build_hook_env": {},
    "get_hook_timeout": 30,
    "_run_strictcli_schema_dump": None,
    "_run_selfdoc_gen": None,
    "_run_selfdoc_check": None,
    "_run_selfdoc_blog_post_generate": None,
    "commit_files_if_changed": None,
    "run_release_hook": None,
}


def _release(root, *, bump, run_checks, hook_customized=False,
             external_checks=None):
    """Run the release against stubs; returns the mutating-half mock."""
    new_version = "1.0.1"
    with contextlib.ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(_R + name, return_value=value))
            for name, value in _STUBS.items()
        }
        stack.enter_context(patch(
            _R + "compute_release_version",
            return_value=("1.0.0", new_version, bump, f"v{new_version}"),
        ))
        stack.enter_context(patch(
            _R + "is_hook_customized", return_value=hook_customized,
        ))
        stack.enter_context(patch("rlsbl.app.run_checks", side_effect=run_checks))
        if external_checks is not None:
            stack.enter_context(patch(
                "rlsbl.external_checks.run_external_preflight_checks",
                side_effect=external_checks,
            ))
        from rlsbl.commands.release import run_cmd

        run_cmd(
            ReleaseConfig(bump=bump, include=["npm"], exclude=[]),
            {"quiet": False},
            ctx=ProjectContext(
                project_root=Path(str(root)), workspace_root=None,
                config={"publish_mode": "ci", "pipelines": {}},
            ),
        )
        return mocks["_run_release_mutating"]


def _checks(changelog=(), preflight=()):
    """A run_checks stand-in returning *changelog* for the changelog preflight
    and *preflight* for the main one, each with the exit code strictcli's
    run_checks gives them (nonzero on any failure or warning)."""

    def run_checks(ctx, *, tag_expr=None, **kwargs):
        results = list(changelog if tag_expr == "preflight-changelog" else preflight)
        exit_code = 1 if any(r.status in ("fail", "warn") for r in results) else 0
        return (results, [], exit_code)

    return run_checks


class TestWarnOnlyOutcomes:
    def test_a_warn_only_preflight_does_not_abort_with_zero_failures(
        self, tmp_project, capsys,
    ):
        """The old silent abort: exit 1, nothing listed, "0 failure(s)"."""
        _setup_project(tmp_project, user_facing=True)

        mutating = _release(
            tmp_project, bump="patch",
            run_checks=_checks(preflight=[
                _Result("dead-modules", "warn", "1 dead module"),
                _Result("test-suite", "pass", "ok"),
            ]),
        )

        captured = capsys.readouterr()
        assert "0 failure(s)" not in captured.err, captured.err
        assert mutating.called, captured.err
        assert "WARN  dead-modules: 1 dead module" in captured.err

    def test_a_warn_only_changelog_preflight_does_not_block_a_patch_release(
        self, tmp_project, capsys,
    ):
        _setup_project(tmp_project, user_facing=True)

        mutating = _release(
            tmp_project, bump="patch",
            run_checks=_checks(changelog=[
                _Result("changelog-coverage", "warn", "softened to warn"),
            ]),
        )

        captured = capsys.readouterr()
        assert mutating.called, captured.err
        assert "WARN  changelog-coverage: softened to warn" in captured.err

    def test_a_warn_only_external_preflight_does_not_abort(
        self, tmp_project, capsys,
    ):
        """The customized pre-release hook path decides the same way."""
        _setup_project(tmp_project, user_facing=True)

        def external(ctx, config, **kwargs):
            return ([_Result("my-lint", "warn", "style nit")], [], 1)

        mutating = _release(
            tmp_project, bump="patch", run_checks=_checks(),
            hook_customized=True, external_checks=external,
        )

        captured = capsys.readouterr()
        assert "0 failure(s)" not in captured.err, captured.err
        assert mutating.called, captured.err


class TestErrorLevelFailuresBlockAndAreNamed:
    def test_a_preflight_failure_is_named(self, tmp_project, capsys):
        _setup_project(tmp_project, user_facing=True)

        with pytest.raises(SystemExit) as exc:
            _release(
                tmp_project, bump="patch",
                run_checks=_checks(preflight=[
                    _Result("test-suite", "fail", "3 tests failed"),
                    _Result("dead-modules", "warn", "1 dead module"),
                ]),
            )

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "FAIL  test-suite: 3 tests failed" in err
        assert "Preflight checks failed (1 failure(s))" in err

    def test_a_changelog_failure_blocks_an_infra_release(
        self, tmp_project, capsys,
    ):
        _setup_project(tmp_project, user_facing=False)

        with pytest.raises(SystemExit):
            _release(
                tmp_project, bump="infra",
                run_checks=_checks(changelog=[
                    _Result("changelog-coverage", "fail", "1 uncovered commit"),
                ]),
            )

        err = capsys.readouterr().err
        assert "FAIL  changelog-coverage: 1 uncovered commit" in err
        assert "Changelog preflight checks failed (1 failure(s))" in err

    def test_an_external_failure_is_named(self, tmp_project, capsys):
        _setup_project(tmp_project, user_facing=True)

        def external(ctx, config, **kwargs):
            return ([_Result("my-lint", "fail", "broken")], [], 1)

        with pytest.raises(SystemExit):
            _release(
                tmp_project, bump="patch", run_checks=_checks(),
                hook_customized=True, external_checks=external,
            )

        err = capsys.readouterr().err
        assert "FAIL  my-lint: broken" in err
        assert "Preflight checks failed (1 failure(s))" in err


class TestUserFacingRuleIsReleaseValidation:
    def test_a_patch_release_without_a_user_facing_entry_is_refused(
        self, tmp_project, capsys,
    ):
        _setup_project(tmp_project, user_facing=False)

        with pytest.raises(SystemExit) as exc:
            _release(tmp_project, bump="patch", run_checks=_checks())

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert (
            "a patch release needs at least one user-facing changelog entry"
            in err
        ), err
        assert 'bump = "infra"' in err

    def test_the_named_fix_clears_the_refusal(self, tmp_project, capsys):
        """The refusal names `bump = "infra"`; an infra release passes it."""
        _setup_project(tmp_project, user_facing=False)

        mutating = _release(tmp_project, bump="infra", run_checks=_checks())

        assert mutating.called, capsys.readouterr().err

    def test_a_release_with_a_user_facing_entry_passes(
        self, tmp_project, capsys,
    ):
        _setup_project(tmp_project, user_facing=True)

        mutating = _release(tmp_project, bump="minor", run_checks=_checks())

        assert mutating.called, capsys.readouterr().err
