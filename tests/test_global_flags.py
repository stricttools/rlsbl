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
