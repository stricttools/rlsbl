"""Verify that --dry-run, --yes, and --quiet are registered as global App flags."""

from rlsbl import app


def test_app_has_three_global_flags():
    flag_names = {f.name for f in app.flags}
    assert "dry-run" in flag_names
    assert "yes" in flag_names
    assert "quiet" in flag_names


def test_global_flag_types():
    by_name = {f.name: f for f in app.flags}
    assert by_name["dry-run"].type is bool
    assert by_name["yes"].type is bool
    assert by_name["quiet"].type is bool


def test_yes_flag_has_short_alias():
    by_name = {f.name: f for f in app.flags}
    assert by_name["yes"].short == "y"


def test_all_handlers_declare_global_flag_params():
    """Every command handler must explicitly name each global flag parameter.

    strictcli enforces (at app startup, inside run()/test()/call()) that every
    non-passthrough, non-framework command handler declares an explicit
    parameter for each declared global flag (--dry-run -> dry_run,
    --yes -> yes, --quiet -> quiet). A bare **kwargs does NOT satisfy the
    requirement -- relying on it silently swallows the global's value, which
    once let a command ignore --dry-run and act for real during a preview.

    Driving the public test/help entry point triggers that startup validation
    against the live rlsbl registry. When strictcli has the guard, a
    non-compliant handler makes app.test() raise ValueError (listing every
    offending command) and this test fails; on a strictcli release that
    predates the guard, the entry point is simply a no-op for validation and
    the test still passes. Either way a future non-compliant handler fails CI
    here rather than at first real invocation.
    """
    # Does not raise: full compliance. Raises ValueError: a handler is missing
    # a global-flag parameter (message names every offender).
    app.test(["--help"])
