"""The release commands' timeout flag surface.

``get_check_timeout`` / ``get_hook_timeout`` / ``get_ci_timeout`` all accept an
``override=`` argument, but for a long time only ``--push-timeout`` was wired
to a CLI flag: the other overrides had no caller at all, so a project whose
preflight suite or release hook exceeded the shipped default could only change
it by editing ``.rlsbl/config.json``.

All four are now flags on every release-run surface. ``--push-timeout`` and
``--ci-timeout`` reach their consumers through the flags dict; ``--check-timeout``
and ``--hook-timeout`` are consumed by the check framework and the hook runner,
which only ever see a ProjectContext, so their overrides are applied onto the
in-memory config -- exactly where those consumers already look.
"""

import pytest

from rlsbl.commands.release.shared import (
    apply_timeout_overrides,
    build_release_flags,
)
from rlsbl.errors import ConfigError
from rlsbl.utils import (
    DEFAULT_CI_TIMEOUT,
    get_check_timeout,
    get_ci_timeout,
    get_hook_timeout,
    get_push_timeout,
)


TIMEOUT_FLAGS = ("push-timeout", "ci-timeout", "check-timeout", "hook-timeout")


class TestGetCiTimeout:

    def test_default(self):
        assert get_ci_timeout({}) == DEFAULT_CI_TIMEOUT
        assert get_ci_timeout(None) == DEFAULT_CI_TIMEOUT

    def test_config_key(self):
        assert get_ci_timeout({"ci_timeout": 60}) == 60

    def test_override_beats_config(self):
        assert get_ci_timeout({"ci_timeout": 60}, override=15) == 15

    def test_invalid_value_is_a_hard_error(self):
        with pytest.raises(ConfigError):
            get_ci_timeout({"ci_timeout": 0})


class TestBuildReleaseFlags:

    def test_all_four_timeouts_are_carried(self):
        flags = build_release_flags(
            False, True, False, False, watch=False,
            push_timeout=10, ci_timeout=20, check_timeout=30, hook_timeout=40,
        )
        assert flags["push-timeout"] == 10
        assert flags["ci-timeout"] == 20
        assert flags["check-timeout"] == 30
        assert flags["hook-timeout"] == 40

    def test_unpassed_flags_are_none(self):
        flags = build_release_flags(False, True, False, False)
        for key in TIMEOUT_FLAGS:
            assert flags[key] is None, key


class TestApplyTimeoutOverrides:

    def test_overrides_land_where_the_consumers_read_them(self):
        config = {}
        flags = build_release_flags(
            False, True, False, False,
            push_timeout=11, ci_timeout=22, check_timeout=33, hook_timeout=44,
        )
        apply_timeout_overrides(config, flags)

        # Red-green: without the wiring these all returned the shipped
        # defaults, because nothing ever passed the override through.
        assert get_push_timeout(config) == 11
        assert get_ci_timeout(config) == 22
        assert get_check_timeout(config) == 33
        assert get_hook_timeout(config) == 44

    def test_an_unpassed_flag_leaves_the_config_key_alone(self):
        config = {"check_timeout": 111}
        apply_timeout_overrides(config, build_release_flags(False, True, False, False))
        assert config == {"check_timeout": 111}
        assert get_check_timeout(config) == 111

    def test_a_passed_flag_beats_the_config_key(self):
        config = {"check_timeout": 111, "hook_timeout": 222}
        apply_timeout_overrides(
            config,
            build_release_flags(False, True, False, False, check_timeout=7),
        )
        assert get_check_timeout(config) == 7
        assert get_hook_timeout(config) == 222

    def test_none_config_is_tolerated(self):
        assert apply_timeout_overrides(None, {"check-timeout": 5}) is None


class TestCliSurface:

    @pytest.mark.parametrize("argv", [
        ["release", "run"],
        ["release", "resume"],
        ["monorepo", "release", "run"],
    ])
    @pytest.mark.parametrize("flag", TIMEOUT_FLAGS)
    def test_flag_is_registered(self, argv, flag):
        from rlsbl import app

        result = app.test(argv + ["--help"])
        assert result.exit_code == 0, result.stderr
        assert f"--{flag}" in result.stdout, (
            f"`rlsbl {' '.join(argv)}` must expose --{flag}"
        )
