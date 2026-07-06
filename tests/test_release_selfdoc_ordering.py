"""Tests for selfdoc ordering in the release pipeline."""

import inspect

from rlsbl.commands.release import _run_cmd_inner


class TestSelfdocBeforeTestsAndLint:
    """Verify the ordering: selfdoc check -> preflight checks."""

    def test_selfdoc_failure_prevents_preflight(self):
        """When selfdoc check fails, test/lint preflight checks should not run.

        We verify the ordering by reading the source and checking that
        _run_selfdoc_check appears before the test/lint preflight
        (tag_expr="preflight") in the release function.  The changelog
        preflight (tag_expr="preflight-changelog") runs earlier in the
        validation phase.
        """
        source = inspect.getsource(_run_cmd_inner)
        selfdoc_pos = source.index("_run_selfdoc_check(")
        # Find the test/lint preflight call (tag_expr="preflight"), not
        # the changelog preflight (tag_expr="preflight-changelog")
        preflight_pos = source.index('tag_expr="preflight"')

        assert selfdoc_pos < preflight_pos, (
            "_run_selfdoc_check must appear before test/lint preflight"
        )

    def test_selfdoc_still_before_pre_release_hook(self):
        """Selfdoc check must also run before the pre-release hook."""
        source = inspect.getsource(_run_cmd_inner)
        selfdoc_pos = source.index("_run_selfdoc_check(")
        pre_release_pos = source.index("pre_release_script")

        assert selfdoc_pos < pre_release_pos, (
            "_run_selfdoc_check must appear before pre-release hook"
        )

    def test_ordering_selfdoc_then_preflight(self):
        """The ordering must be: selfdoc check, then test/lint preflight checks."""
        source = inspect.getsource(_run_cmd_inner)

        selfdoc_pos = source.index("_run_selfdoc_check(")
        preflight_pos = source.index('tag_expr="preflight"')

        assert selfdoc_pos < preflight_pos, (
            f"Expected ordering selfdoc ({selfdoc_pos}) < test/lint preflight ({preflight_pos})"
        )

    def test_selfdoc_after_pre_checks_hook(self):
        """Selfdoc check must run after the pre-checks hook."""
        source = inspect.getsource(_run_cmd_inner)
        pre_checks_pos = source.index("pre_checks_script")
        selfdoc_pos = source.index("_run_selfdoc_check(")

        assert pre_checks_pos < selfdoc_pos, (
            "pre-checks hook must appear before _run_selfdoc_check"
        )

    def test_selfdoc_gen_before_selfdoc_check(self):
        """Selfdoc gen must run before selfdoc check."""
        source = inspect.getsource(_run_cmd_inner)
        gen_pos = source.index("_run_selfdoc_gen(")
        check_pos = source.index("_run_selfdoc_check(")

        assert gen_pos < check_pos, (
            "_run_selfdoc_gen must appear before _run_selfdoc_check"
        )

    def test_selfdoc_gen_after_strictcli_schema_dump(self):
        """Selfdoc gen must run after strictcli schema dump."""
        source = inspect.getsource(_run_cmd_inner)
        schema_pos = source.index("_run_strictcli_schema_dump(")
        gen_pos = source.index("_run_selfdoc_gen(")

        assert schema_pos < gen_pos, (
            "_run_strictcli_schema_dump must appear before _run_selfdoc_gen"
        )

    def test_selfdoc_gen_before_preflight(self):
        """Selfdoc gen must run before test/lint preflight checks."""
        source = inspect.getsource(_run_cmd_inner)
        gen_pos = source.index("_run_selfdoc_gen(")
        preflight_pos = source.index('tag_expr="preflight"')

        assert gen_pos < preflight_pos, (
            "_run_selfdoc_gen must appear before test/lint preflight"
        )


