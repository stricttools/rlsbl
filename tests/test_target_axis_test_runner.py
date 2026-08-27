"""Per-axis conformance: the built-in test runner.

``run_project_tests`` used to be a chain of target-name comparisons ending in a
bare ``return True``. A target with no runner -- or a name that was not a
target at all -- therefore reported a PASSING test step for a suite that never
ran, both in ``rlsbl check --tag quality`` and in the release's own test step.

The dispatch is now ``ReleaseTarget.run_tests``, and the base implementation
answers SKIPPED naming the target. Both sides are pinned here: a supported
target still shells out to exactly the same runner, and an unsupported one
produces a named skip that reaches the step summary.
"""

from unittest.mock import patch

import pytest

from rlsbl.targets import TARGETS, targets_with_builtin_tests
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.outcomes import SuiteRunOutcome, SuiteRunStatus
from rlsbl.testing import run_project_tests


class TestUnsupportedIsAnExplicitSkip:
    """The silent success is gone; every non-runner answers SKIPPED by name."""

    def test_a_registered_target_without_a_runner(self):
        outcome = TARGETS["zig"].run_tests(project_dir=".", config={})
        assert outcome.status is SuiteRunStatus.SKIPPED
        assert not outcome.passed
        assert outcome.skipped
        assert "zig" in outcome.message

    def test_a_name_that_is_not_a_target_at_all(self):
        outcome = run_project_tests("cargo", project_dir=".")
        assert outcome.status is SuiteRunStatus.SKIPPED
        assert "cargo" in outcome.message

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_every_target_either_runs_or_says_it_cannot(self, name):
        target = TARGETS[name]
        if target.has_builtin_test_runner:
            assert type(target).run_tests is not BaseTarget.run_tests
            return
        outcome = target.run_tests(project_dir=".", config={})
        assert outcome.status is SuiteRunStatus.SKIPPED
        assert name in outcome.message

    def test_a_skip_runs_no_subprocess(self):
        with patch("rlsbl.effects.run") as mock_run:
            run_project_tests("zig", project_dir=".")
        mock_run.assert_not_called()

    def test_a_skip_prints_no_running_tests_banner(self, capsys):
        """"Running tests..." must not appear for a suite that never runs."""
        run_project_tests("zig", project_dir=".")
        assert "Running tests" not in capsys.readouterr().out


class TestSupportedTargetsAreUnchanged:
    """Each runner still calls exactly the function it called before."""

    @pytest.mark.parametrize(
        "target_name,runner",
        [
            ("pypi", "_run_pypi_tests"),
            ("go", "_run_go_tests"),
            ("npm", "_run_npm_tests"),
            ("maven", "_run_maven_tests"),
        ],
    )
    def test_pass_and_fail_both_reach_the_original_runner(self, target_name, runner):
        with patch(f"rlsbl.testing.{runner}", return_value=True) as ok:
            outcome = run_project_tests(target_name, project_dir=".", config={})
        assert ok.called
        assert isinstance(outcome, SuiteRunOutcome)
        assert outcome.status is SuiteRunStatus.PASSED

        with patch(f"rlsbl.testing.{runner}", return_value=False):
            outcome = run_project_tests(target_name, project_dir=".", config={})
        assert outcome.status is SuiteRunStatus.FAILED
        assert not outcome.passed
        assert not outcome.skipped

    def test_pypi_still_receives_workspace_root_and_skip_sync(self):
        with patch("rlsbl.testing._run_pypi_tests", return_value=True) as run:
            run_project_tests(
                "pypi",
                project_dir="/proj",
                workspace_root="/ws",
                skip_sync=True,
                config={"uv_sync_verbose": True},
            )
        kwargs = run.call_args.kwargs
        assert kwargs["project_dir"] == "/proj"
        assert kwargs["workspace_root"] == "/ws"
        assert kwargs["skip_sync"] is True
        assert kwargs["config"] == {"uv_sync_verbose": True}

    def test_an_explicit_timeout_overrides_the_configured_one(self):
        from rlsbl.testing import resolve_test_timeout

        assert resolve_test_timeout({"check_timeout": 10}, 99) == 99
        assert resolve_test_timeout({"check_timeout": 10}, None) == 10


class TestDerivedRunnerSet:
    """The recognized set is computed from the registry, never hand-listed."""

    def test_derivation_matches_the_overrides(self):
        assert targets_with_builtin_tests() == frozenset(
            n for n, t in TARGETS.items() if t.has_builtin_test_runner
        )

    def test_the_four_that_had_runners_still_do(self):
        assert targets_with_builtin_tests() == {"pypi", "go", "npm", "maven"}


class TestStepSummaryShowsTheSkip:
    """The release's test step must render the skip, not a bare pass."""

    def _run_check(self, name, ctx):
        from rlsbl import app

        return app._check_defs[name].impl(ctx)

    def test_test_suite_skip_names_the_detected_targets(self, tmp_path, monkeypatch):
        from conftest import make_ctx
        from rlsbl.targets import TargetEntry

        repo = tmp_path / "repo"
        (repo / ".rlsbl").mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text('{"targets": ["zig"]}')
        monkeypatch.chdir(repo)

        ctx = make_ctx(repo)
        with patch(
            "rlsbl.targets.detect_targets",
            return_value=[TargetEntry("zig", str(repo))],
        ):
            result = self._run_check("test-suite", ctx)

        assert result.status == "skip"
        assert "zig" in result.message
        assert "built-in test runner" in result.message

    def test_test_suite_skip_names_the_targets_that_do_have_runners(
        self, tmp_path, monkeypatch
    ):
        from conftest import make_ctx
        from rlsbl.targets import TargetEntry

        repo = tmp_path / "repo"
        (repo / ".rlsbl").mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text('{"targets": ["docker"]}')
        monkeypatch.chdir(repo)

        ctx = make_ctx(repo)
        with patch(
            "rlsbl.targets.detect_targets",
            return_value=[TargetEntry("docker", str(repo))],
        ):
            result = self._run_check("test-suite", ctx)

        assert result.status == "skip"
        for runnable in targets_with_builtin_tests():
            assert runnable in result.message

    def test_a_supported_target_still_reports_a_pass(self, tmp_path, monkeypatch):
        from conftest import make_ctx
        from rlsbl.targets import TargetEntry

        repo = tmp_path / "repo"
        (repo / ".rlsbl").mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text('{"targets": ["pypi"]}')
        monkeypatch.chdir(repo)

        ctx = make_ctx(repo)
        with (
            patch(
                "rlsbl.targets.detect_targets",
                return_value=[TargetEntry("pypi", str(repo))],
            ),
            patch("rlsbl.testing._run_pypi_tests", return_value=True),
        ):
            result = self._run_check("test-suite", ctx)

        assert result.status == "pass"
        assert "pypi tests passed" in result.message
