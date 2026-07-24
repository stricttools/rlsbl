"""Tests for CI service-container config validation (``services`` / ``test_env``).

Covers :func:`rlsbl.config.validate_services_config` and its wiring into the
``config-schema`` check. A ``services`` key used to validate clean as a silent
no-op; these tests pin that a malformed one now hard-errors and a valid one is
consumed.
"""

import pytest

from rlsbl import app
from rlsbl.config import validate_services_config
from rlsbl.errors import ConfigError

from conftest import make_ctx


def _valid_services_config():
    return {
        "publish_mode": "ci",
        "targets": ["go", {"name": "pypi", "path": "pypi/"}],
        "services": {
            "postgres": {
                "targets": ["go"],
                "image": "postgres:17",
                "env": {
                    "POSTGRES_USER": "test",
                    "POSTGRES_PASSWORD": "test",
                    "POSTGRES_DB": "postgres",
                },
                "ports": ["5432:5432"],
                "health": {
                    "cmd": "pg_isready -U test",
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 5,
                },
                "setup": {
                    "commands": ["apt-get update && apt-get install -y foo"],
                    "verify_sql": "SELECT 1;",
                },
            }
        },
        "test_env": {
            "PGDESIGN_DB": "postgres://test:test@localhost:5432/postgres",
            "PGDESIGN_REQUIRE_DB": "1",
        },
    }


class TestValidateServicesConfig:
    def test_absent_is_valid(self):
        # No services, no test_env -> silent pass (returns None).
        assert validate_services_config({"publish_mode": "ci"}) is None

    def test_valid_full_config_passes(self):
        validate_services_config(_valid_services_config())

    def test_valid_minimal_service_passes(self):
        validate_services_config({
            "targets": ["go"],
            "services": {"cache": {"targets": ["go"], "image": "redis:7"}},
        })

    def test_services_not_dict_errors(self):
        with pytest.raises(ConfigError) as e:
            validate_services_config({"services": ["postgres"]})
        assert "services must be a map" in str(e.value)

    def test_unknown_service_key_errors(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["imagee"] = "typo"
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "unknown key" in str(e.value)
        assert "imagee" in str(e.value)

    def test_unknown_health_key_errors(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["health"]["retires"] = 3
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "health" in str(e.value)
        assert "retires" in str(e.value)

    def test_unknown_setup_key_errors(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["setup"]["verifi"] = "x"
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "setup" in str(e.value)
        assert "verifi" in str(e.value)

    def test_missing_image_errors(self):
        cfg = _valid_services_config()
        del cfg["services"]["postgres"]["image"]
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "image" in str(e.value)

    def test_missing_targets_errors(self):
        cfg = _valid_services_config()
        del cfg["services"]["postgres"]["targets"]
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "targets" in str(e.value)

    def test_targets_must_be_nonempty_list(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["targets"] = []
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "non-empty list" in str(e.value)

    def test_targets_references_unknown_target_errors(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["targets"] = ["goo"]
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "unknown target 'goo'" in str(e.value)

    def test_verify_sql_and_verify_cmd_mutually_exclusive(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["setup"]["verify_cmd"] = "psql ..."
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "mutually exclusive" in str(e.value)

    def test_setup_requires_commands(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["setup"] = {"verify_sql": "SELECT 1;"}
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "commands" in str(e.value)

    def test_health_requires_cmd(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["health"] = {"interval": "10s"}
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "cmd" in str(e.value)

    def test_retries_must_be_int(self):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["health"]["retries"] = "5"
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "retries" in str(e.value)

    def test_test_env_without_services_errors(self):
        with pytest.raises(ConfigError) as e:
            validate_services_config({"test_env": {"A": "1"}})
        assert "no services" in str(e.value)

    def test_test_env_non_scalar_value_errors(self):
        cfg = _valid_services_config()
        cfg["test_env"]["BAD"] = {"nested": "map"}
        with pytest.raises(ConfigError) as e:
            validate_services_config(cfg)
        assert "test_env" in str(e.value)


class TestConfigSchemaCheckWiring:
    """The config-schema check consumes services config (no longer a no-op)."""

    def test_malformed_services_fails_config_schema(self, tmp_path):
        cfg = _valid_services_config()
        cfg["services"]["postgres"]["imagee"] = "typo"
        ctx = make_ctx(tmp_path, config=cfg)
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "fail"
        problem_text = " ".join(p.text for p in result.problems)
        assert "imagee" in problem_text

    def test_valid_services_passes_config_schema(self, tmp_path):
        ctx = make_ctx(tmp_path, config=_valid_services_config())
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "pass"
