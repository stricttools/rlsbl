"""``--allow-dirty`` is gone from both release commands.

A release runs in the release checkout, so another session's uncommitted work
never blocks it and never leaks into it, and an uncommitted change to a path
the release writes is refused by name. There is no dirty-tree policy left for
a flag to choose, and the flag does not parse.
"""

import inspect

import pytest

import rlsbl

app = rlsbl.app

pytestmark = pytest.mark.repo_cwd


@pytest.mark.parametrize("argv", [
    ["release", "run", "--no-watch", "--allow-dirty"],
    ["release", "run", "--no-watch", "--no-allow-dirty"],
    ["monorepo", "release", "run", "--no-watch", "--allow-dirty"],
    ["monorepo", "release", "run", "--no-watch", "--no-allow-dirty"],
])
def test_the_flag_is_unknown(argv):
    result = app.test(argv)
    assert result.exit_code != 0
    assert "unknown flag" in result.stderr


@pytest.mark.parametrize("argv", [
    ["release", "run", "--help"],
    ["monorepo", "release", "run", "--help"],
])
def test_help_does_not_advertise_it(argv):
    result = app.test(argv)
    assert result.exit_code == 0, result.stderr
    assert "allow-dirty" not in result.stdout


@pytest.mark.parametrize("handler", [
    rlsbl.cmd_release_run, rlsbl.cmd_mono_release_run,
])
def test_the_handlers_take_no_such_parameter(handler):
    assert "allow_dirty" not in inspect.signature(handler).parameters


def test_the_release_flags_carry_no_such_key():
    from rlsbl.commands.release.shared import build_release_flags

    assert "allow-dirty" not in build_release_flags(False, False, watch=False)
