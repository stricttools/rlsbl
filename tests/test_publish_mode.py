"""Tests for the publish_mode enum replacing the deprecated `private` key.

Covers:
- get_publish_mode / suppresses_publish helpers (config.py)
- the deprecated `private` key hard-error (exact-edit message)
- enum validation (invalid value, missing key)
- validate_config_schema surfacing all of the above
- the fleet sweep script (scripts/sweep_publish_mode.py)

NOTE: this file intentionally uses the deprecated ``private`` key in fixtures
to exercise the ban. Do not "migrate" these to publish_mode.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from rlsbl.config import (
    PUBLISH_MODES,
    get_publish_mode,
    suppresses_publish,
    validate_config_schema,
)
from rlsbl.errors import ConfigError

_OLD = "pri" "vate"  # deprecated key name, split so fixture-sweeps skip it


class TestGetPublishMode:
    def test_ci(self):
        assert get_publish_mode({"publish_mode": "ci"}) == "ci"

    def test_none(self):
        assert get_publish_mode({"publish_mode": "none"}) == "none"

    def test_missing_key_raises(self):
        with pytest.raises(ConfigError, match="missing required"):
            get_publish_mode({})

    def test_invalid_value_raises(self):
        with pytest.raises(ConfigError, match="invalid"):
            get_publish_mode({"publish_mode": "local"})

    def test_invalid_type_raises(self):
        with pytest.raises(ConfigError, match="invalid"):
            get_publish_mode({"publish_mode": "yes"})

    def test_deprecated_private_key_raises_with_exact_edit(self):
        with pytest.raises(ConfigError) as exc:
            get_publish_mode({_OLD: False})
        msg = str(exc.value)
        assert '"private"' in msg
        assert '"publish_mode": "ci"' in msg
        assert '"publish_mode": "none"' in msg

    def test_private_key_takes_precedence_over_valid_mode(self):
        # A config with BOTH keys is still an error -- private must be removed.
        with pytest.raises(ConfigError, match="replaced by"):
            get_publish_mode({_OLD: True, "publish_mode": "none"})

    def test_modes_constant(self):
        assert PUBLISH_MODES == frozenset({"ci", "none"})


class TestSuppressesPublish:
    def test_none_suppresses(self):
        assert suppresses_publish({"publish_mode": "none"}) is True

    def test_ci_does_not_suppress(self):
        assert suppresses_publish({"publish_mode": "ci"}) is False

    def test_missing_raises(self):
        with pytest.raises(ConfigError):
            suppresses_publish({})


class TestValidateConfigSchemaPublishMode:
    def test_valid_ci_passes(self):
        validate_config_schema({"publish_mode": "ci"})

    def test_missing_publish_mode_fails(self):
        with pytest.raises(ConfigError, match="missing required"):
            validate_config_schema({})

    def test_deprecated_private_fails(self):
        with pytest.raises(ConfigError, match="replaced by"):
            validate_config_schema({_OLD: False})

    def test_invalid_publish_mode_fails(self):
        with pytest.raises(ConfigError, match="invalid"):
            validate_config_schema({"publish_mode": "public"})


# ---------------------------------------------------------------------------
# Sweep script
# ---------------------------------------------------------------------------


def _load_sweep():
    path = Path(__file__).resolve().parent.parent / "scripts" / "sweep_publish_mode.py"
    spec = importlib.util.spec_from_file_location("sweep_publish_mode", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSweepScript:
    def test_check_private_false(self, tmp_path):
        sweep = _load_sweep()
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({_OLD: False, "targets": ["npm"]}))
        vtype, detail = sweep.check_config(str(cfg))
        assert vtype == "private_key"
        assert '"publish_mode": "ci"' in detail

    def test_check_private_true(self, tmp_path):
        sweep = _load_sweep()
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({_OLD: True}))
        vtype, detail = sweep.check_config(str(cfg))
        assert vtype == "private_key"
        assert '"publish_mode": "none"' in detail

    def test_check_already_migrated_is_clean(self, tmp_path):
        sweep = _load_sweep()
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"publish_mode": "ci"}))
        assert sweep.check_config(str(cfg)) is None

    def test_non_bool_private_flagged(self, tmp_path):
        sweep = _load_sweep()
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({_OLD: "yes"}))
        vtype, _ = sweep.check_config(str(cfg))
        assert vtype == "non_bool_private"

    def test_migrate_preserves_key_order(self):
        sweep = _load_sweep()
        cfg = {"env_file": "x", _OLD: True, "targets": ["npm"]}
        migrated = sweep.migrate_config_dict(cfg)
        assert list(migrated.keys()) == ["env_file", "publish_mode", "targets"]
        assert migrated["publish_mode"] == "none"

    def test_fix_config_rewrites_file(self, tmp_path):
        sweep = _load_sweep()
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({_OLD: False, "targets": ["npm"]}, indent=2))
        sweep.fix_config(str(cfg))
        result = json.loads(cfg.read_text())
        assert "private" not in result
        assert result["publish_mode"] == "ci"
        assert result["targets"] == ["npm"]
        # Result must pass the real validator (no deprecated key).
        validate_config_schema(result)
