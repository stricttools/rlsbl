"""Tests for get_check_timeout() -- env var > config dict > default 120."""

import pytest

from rlsbl.errors import ConfigError
from rlsbl.utils import get_check_timeout


class TestGetCheckTimeout:
    """Tests for get_check_timeout() -- env var > config dict > default 120."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.delenv("RLSBL_CHECK_TIMEOUT", raising=False)

    def test_default_returns_120(self):
        assert get_check_timeout({}) == 120

    def test_env_var_returns_value(self, monkeypatch):
        monkeypatch.setenv("RLSBL_CHECK_TIMEOUT", "60")
        assert get_check_timeout({}) == 60

    def test_config_returns_value(self):
        assert get_check_timeout({"check_timeout": 90}) == 90

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("RLSBL_CHECK_TIMEOUT", "45")
        assert get_check_timeout({"check_timeout": 200}) == 45

    def test_invalid_env_var_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("RLSBL_CHECK_TIMEOUT", "not-a-number")
        with pytest.raises(ConfigError) as exc_info:
            get_check_timeout({})
        assert "Invalid RLSBL_CHECK_TIMEOUT" in str(exc_info.value)

    def test_negative_env_var_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("RLSBL_CHECK_TIMEOUT", "-5")
        with pytest.raises(ConfigError) as exc_info:
            get_check_timeout({})
        assert "Invalid RLSBL_CHECK_TIMEOUT" in str(exc_info.value)

    def test_invalid_config_value_raises_config_error(self):
        with pytest.raises(ConfigError) as exc_info:
            get_check_timeout({"check_timeout": "slow"})
        assert "Invalid check_timeout" in str(exc_info.value)

    def test_config_none_no_env_returns_120(self):
        assert get_check_timeout(None) == 120

    def test_config_none_with_env_returns_env(self, monkeypatch):
        monkeypatch.setenv("RLSBL_CHECK_TIMEOUT", "30")
        assert get_check_timeout(None) == 30
