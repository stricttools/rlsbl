"""Tests that selfdoc check runs before tests and lint in the release pipeline."""

import inspect

from rlsbl.commands.release import run_cmd


class TestSelfdocBeforeTestsAndLint:
    """Verify the ordering: selfdoc check -> tests -> lint."""

    def test_selfdoc_failure_prevents_tests_and_lint(self):
        """When selfdoc check fails, tests and lint should not run.

        We verify the ordering by reading the source and checking that
        _run_selfdoc_check appears before _run_builtin_tests and
        _run_builtin_lint in the release function.
        """
        source = inspect.getsource(run_cmd)
        selfdoc_pos = source.index("_run_selfdoc_check(")
        tests_pos = source.index("_run_builtin_tests(")
        lint_pos = source.index("_run_builtin_lint(")

        assert selfdoc_pos < tests_pos, (
            "_run_selfdoc_check must appear before _run_builtin_tests"
        )
        assert selfdoc_pos < lint_pos, (
            "_run_selfdoc_check must appear before _run_builtin_lint"
        )

    def test_selfdoc_still_before_pre_release_hook(self):
        """Selfdoc check must also run before the pre-release hook."""
        source = inspect.getsource(run_cmd)
        selfdoc_pos = source.index("_run_selfdoc_check(")
        pre_release_pos = source.index("pre_release_script")

        assert selfdoc_pos < pre_release_pos, (
            "_run_selfdoc_check must appear before pre-release hook"
        )

    def test_ordering_selfdoc_tests_lint(self):
        """The full ordering must be: selfdoc, tests, lint."""
        source = inspect.getsource(run_cmd)

        selfdoc_pos = source.index("_run_selfdoc_check(")
        tests_pos = source.index("_run_builtin_tests(")
        lint_pos = source.index("_run_builtin_lint(")

        assert selfdoc_pos < tests_pos < lint_pos, (
            f"Expected ordering selfdoc ({selfdoc_pos}) < tests ({tests_pos}) "
            f"< lint ({lint_pos})"
        )
