"""Tests for get_check_timeout() / get_push_timeout() / get_hook_timeout().

Precedence is: explicit override (the CLI flag) > config dict key > shipped
default. There is deliberately no environment-variable layer -- see
tests/test_timeout_config_surface.py, which pins that env vars are inert.
"""

import pytest

from rlsbl.errors import ConfigError
from rlsbl.utils import (
    DEFAULT_CHECK_TIMEOUT,
    DEFAULT_PUSH_TIMEOUT,
    get_check_timeout,
    get_hook_timeout,
    get_push_timeout,
)


class TestGetCheckTimeout:
    """get_check_timeout() -- override > config dict > DEFAULT_CHECK_TIMEOUT."""

    def test_empty_config_returns_default(self):
        assert get_check_timeout({}) == DEFAULT_CHECK_TIMEOUT

    def test_config_returns_value(self):
        assert get_check_timeout({"check_timeout": 90}) == 90

    def test_override_beats_config(self):
        assert get_check_timeout({"check_timeout": 200}, override=45) == 45

    def test_invalid_config_value_raises_config_error(self):
        with pytest.raises(ConfigError) as exc_info:
            get_check_timeout({"check_timeout": "slow"})
        assert "Invalid check_timeout" in str(exc_info.value)

    def test_zero_config_value_raises_config_error(self):
        with pytest.raises(ConfigError):
            get_check_timeout({"check_timeout": 0})

    def test_invalid_override_raises_config_error(self):
        with pytest.raises(ConfigError):
            get_check_timeout({}, override="slow")

    def test_config_none_returns_default(self):
        assert get_check_timeout(None) == DEFAULT_CHECK_TIMEOUT

    def test_no_args_returns_default(self):
        assert get_check_timeout() == DEFAULT_CHECK_TIMEOUT


class TestGetPushTimeout:
    """get_push_timeout() -- override > config dict > DEFAULT_PUSH_TIMEOUT."""

    def test_config_none_returns_default(self):
        assert get_push_timeout(None) == DEFAULT_PUSH_TIMEOUT

    def test_no_args_returns_default(self):
        assert get_push_timeout() == DEFAULT_PUSH_TIMEOUT

    def test_config_returns_value(self):
        assert get_push_timeout({"push_timeout": 60}) == 60

    def test_override_beats_config(self):
        assert get_push_timeout({"push_timeout": 60}, override=900) == 900

    def test_invalid_config_value_raises_config_error(self):
        with pytest.raises(ConfigError) as exc_info:
            get_push_timeout({"push_timeout": -1})
        assert "Invalid push_timeout" in str(exc_info.value)


class TestGetHookTimeout:
    """get_hook_timeout() -- override > config dict > None (no timeout)."""

    def test_default_is_no_timeout(self):
        assert get_hook_timeout() is None
        assert get_hook_timeout(None) is None
        assert get_hook_timeout({}) is None

    def test_config_returns_value(self):
        assert get_hook_timeout({"hook_timeout": 1800}) == 1800

    def test_override_beats_config(self):
        assert get_hook_timeout({"hook_timeout": 1800}, override=60) == 60

    def test_invalid_config_value_raises_config_error(self):
        with pytest.raises(ConfigError) as exc_info:
            get_hook_timeout({"hook_timeout": "forever"})
        assert "Invalid hook_timeout" in str(exc_info.value)
