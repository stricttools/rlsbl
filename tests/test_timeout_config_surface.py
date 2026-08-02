"""Timeouts are configured by config key or CLI flag -- never by env var.

The ``RLSBL_PUSH_TIMEOUT`` / ``RLSBL_CHECK_TIMEOUT`` / ``RLSBL_HOOK_TIMEOUT`` /
``RLSBL_BUILD_TIMEOUT`` (and per-target ``RLSBL_BUILD_TIMEOUT_<TARGET>``)
environment variables were deleted. An env var set in the environment must be
completely inert: the resolved budget comes from the explicit CLI flag, then
``.rlsbl/config.json``, then the shipped default.
"""

import re
from pathlib import Path

import pytest

import rlsbl
from rlsbl.utils import (
    DEFAULT_CHECK_TIMEOUT,
    DEFAULT_PUSH_TIMEOUT,
    get_check_timeout,
    get_hook_timeout,
    get_push_timeout,
)

app = rlsbl.app

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "rlsbl"

_ENV_VARS = [
    "RLSBL_PUSH_TIMEOUT",
    "RLSBL_CHECK_TIMEOUT",
    "RLSBL_HOOK_TIMEOUT",
    "RLSBL_BUILD_TIMEOUT",
    "RLSBL_BUILD_TIMEOUT_NPM",
]


@pytest.fixture(autouse=True)
def _poison_env(monkeypatch):
    """Every timeout env var is set to an absurd value; nothing may read it."""
    for name in _ENV_VARS:
        monkeypatch.setenv(name, "7")


class TestEnvVarsAreInert:

    def test_push_timeout_ignores_env(self):
        assert get_push_timeout(None) == DEFAULT_PUSH_TIMEOUT
        assert get_push_timeout({"push_timeout": 45}) == 45

    def test_check_timeout_ignores_env(self):
        assert get_check_timeout(None) == DEFAULT_CHECK_TIMEOUT
        assert get_check_timeout({"check_timeout": 45}) == 45

    def test_hook_timeout_ignores_env(self):
        assert get_hook_timeout(None) is None
        assert get_hook_timeout({"hook_timeout": 45}) == 45

    def test_build_timeout_ignores_env(self):
        from rlsbl.targets.npm import NpmTarget

        target = NpmTarget()
        assert target._resolve_build_timeout(None) == target.BUILD_TIMEOUT_DEFAULT
        assert target._resolve_build_timeout({"build_timeout": 55}) == 55
        assert target._resolve_build_timeout({"build_timeout": {"npm": 66}}) == 66


class TestConfigValidation:
    """A bad config value is still a hard error -- only the env path died."""

    @pytest.mark.parametrize("getter,key", [
        (get_push_timeout, "push_timeout"),
        (get_check_timeout, "check_timeout"),
        (get_hook_timeout, "hook_timeout"),
    ])
    @pytest.mark.parametrize("bad", [0, -5, "abc", 1.5])
    def test_invalid_config_value_raises(self, getter, key, bad):
        from rlsbl.errors import ConfigError

        with pytest.raises(ConfigError):
            getter({key: bad})


class TestOverrides:
    """The explicit CLI flag beats the config key."""

    def test_push_timeout_override(self):
        assert get_push_timeout({"push_timeout": 45}, override=600) == 600
        assert get_push_timeout(None, override=600) == 600
        assert get_push_timeout({"push_timeout": 45}, override=None) == 45


class TestPushTimeoutFlagSurface:

    @pytest.mark.parametrize("argv", [
        ["release", "run"],
        ["release", "resume"],
        ["monorepo", "release", "run"],
    ])
    def test_flag_is_registered(self, argv):
        result = app.test(argv + ["--help"])
        assert result.exit_code == 0, result.stderr
        assert "--push-timeout" in result.stdout


class TestRaisedDefaults:
    """Shipped defaults sit above observed real costs."""

    def test_push_default_raised(self):
        assert DEFAULT_PUSH_TIMEOUT >= 300

    def test_check_default_raised(self):
        assert DEFAULT_CHECK_TIMEOUT >= 600


class TestNoEnvVarReferences:

    def test_no_timeout_env_reads_in_production_code(self):
        pattern = re.compile(
            r"RLSBL_(PUSH|CHECK|HOOK|BUILD)_TIMEOUT"
        )
        offenders = []
        for path in sorted(PKG_ROOT.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".toml", ".tpl", ".sh"}:
                continue
            for lineno, line in enumerate(
                path.read_text(errors="replace").splitlines(), 1
            ):
                if pattern.search(line):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                    )
        assert not offenders, (
            "timeout env vars remain:\n" + "\n".join(offenders)
        )

    def test_remediation_hints_name_config_or_flag(self):
        from rlsbl.testing import CHECK_TIMEOUT_HINT

        assert "RLSBL_" not in CHECK_TIMEOUT_HINT
        assert "check_timeout" in CHECK_TIMEOUT_HINT
