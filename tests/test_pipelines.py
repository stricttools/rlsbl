"""Tests for rlsbl.pipelines: protocol, base classes, registry, and config validation."""

import os

import pytest

import rlsbl.pipelines as _pipelines_mod
from rlsbl.pipelines import Pipeline, PIPELINE_TYPES, load_pipelines
from rlsbl.pipelines.base import BasePipeline, TokenPipeline, CredentialPipeline
from rlsbl.config import validate_pipelines_config


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestPipelineProtocol:
    def test_base_pipeline_satisfies_protocol(self):
        p = BasePipeline(name="test", pipeline_type="base", local=True, config={})
        assert isinstance(p, Pipeline)

    def test_token_pipeline_satisfies_protocol(self):
        p = TokenPipeline(name="test", pipeline_type="token", local=True, config={})
        assert isinstance(p, Pipeline)

    def test_credential_pipeline_satisfies_protocol(self):
        p = CredentialPipeline(name="test", pipeline_type="cred", local=True, config={})
        assert isinstance(p, Pipeline)


# ---------------------------------------------------------------------------
# BasePipeline
# ---------------------------------------------------------------------------


class TestBasePipeline:
    def test_instantiation_stores_all_args(self):
        config = {"type": "base", "local": True, "extra": "value"}
        p = BasePipeline(name="myname", pipeline_type="base", local=True, config=config)
        assert p.name == "myname"
        assert p.pipeline_type == "base"
        assert p.local is True
        assert p.config is config

    def test_publish_is_noop(self):
        p = BasePipeline(name="x", pipeline_type="base", local=True, config={})
        # Should not raise
        p.publish(".", "1.0.0", None)

    def test_build_assets_returns_empty_list(self):
        p = BasePipeline(name="x", pipeline_type="base", local=True, config={})
        assert p.build_assets(".", "1.0.0", "/tmp/dist", None) == []

    def test_template_dir_returns_none(self):
        p = BasePipeline(name="x", pipeline_type="base", local=True, config={})
        assert p.template_dir() is None

    def test_template_mappings_returns_empty_list(self):
        p = BasePipeline(name="x", pipeline_type="base", local=True, config={})
        assert p.template_mappings(None) == []

    def test_required_env_vars_returns_empty_list(self):
        p = BasePipeline(name="x", pipeline_type="base", local=True, config={})
        assert p.required_env_vars() == []


# ---------------------------------------------------------------------------
# TokenPipeline
# ---------------------------------------------------------------------------


class _TestTokenPipeline(TokenPipeline):
    """Concrete subclass for testing TokenPipeline."""

    _default_token_var = "TEST_TOKEN"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.publish_calls = []

    def _publish_command(self, dir_path, version, token):
        self.publish_calls.append((dir_path, version, token))


class TestTokenPipeline:
    def test_token_var_from_config(self):
        p = _TestTokenPipeline(
            name="t", pipeline_type="test", local=True,
            config={"token_var": "CUSTOM_TOKEN"},
        )
        assert p.token_var == "CUSTOM_TOKEN"

    def test_token_var_default(self):
        p = _TestTokenPipeline(
            name="t", pipeline_type="test", local=True, config={},
        )
        assert p.token_var == "TEST_TOKEN"

    def test_publish_with_token_calls_publish_command(self, monkeypatch):
        monkeypatch.setenv("TEST_TOKEN", "secret123")
        p = _TestTokenPipeline(
            name="t", pipeline_type="test", local=True, config={},
        )
        p.publish("./mydir", "2.0.0", None)
        assert p.publish_calls == [("./mydir", "2.0.0", "secret123")]

    def test_publish_without_token_local_true_exits(self, monkeypatch):
        monkeypatch.delenv("TEST_TOKEN", raising=False)
        p = _TestTokenPipeline(
            name="mypipe", pipeline_type="test", local=True, config={},
        )
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_publish_without_token_local_true_error_message(self, monkeypatch, capsys):
        monkeypatch.delenv("TEST_TOKEN", raising=False)
        p = _TestTokenPipeline(
            name="mypipe", pipeline_type="test", local=True, config={},
        )
        with pytest.raises(SystemExit):
            p.publish(".", "1.0.0", None)
        err = capsys.readouterr().err
        assert "mypipe" in err
        assert "TEST_TOKEN" in err

    def test_publish_local_false_skips(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_TOKEN", "should-not-be-used")
        p = _TestTokenPipeline(
            name="t", pipeline_type="test", local=False, config={},
        )
        p.publish(".", "1.0.0", None)
        assert p.publish_calls == []
        assert "local=false" in capsys.readouterr().out

    def test_required_env_vars_local_true(self):
        p = _TestTokenPipeline(
            name="t", pipeline_type="test", local=True, config={},
        )
        assert p.required_env_vars() == ["TEST_TOKEN"]

    def test_required_env_vars_local_false(self):
        p = _TestTokenPipeline(
            name="t", pipeline_type="test", local=False, config={},
        )
        assert p.required_env_vars() == []

    def test_publish_command_not_implemented_on_base(self, monkeypatch):
        monkeypatch.setenv("TEST_BASE_TOKEN", "x")
        p = TokenPipeline(
            name="t", pipeline_type="test", local=True,
            config={"token_var": "TEST_BASE_TOKEN"},
        )
        with pytest.raises(NotImplementedError):
            p.publish(".", "1.0.0", None)


# ---------------------------------------------------------------------------
# CredentialPipeline
# ---------------------------------------------------------------------------


class _TestCredentialPipeline(CredentialPipeline):
    """Concrete subclass for testing CredentialPipeline."""

    _default_username_var = "TEST_USER"
    _default_password_var = "TEST_PASS"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.publish_calls = []

    def _publish_command(self, dir_path, version, username, password):
        self.publish_calls.append((dir_path, version, username, password))


class TestCredentialPipeline:
    def test_credential_vars_from_config(self):
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=True,
            config={"username_var": "MY_USER", "password_var": "MY_PASS"},
        )
        assert p.username_var == "MY_USER"
        assert p.password_var == "MY_PASS"

    def test_credential_vars_default(self):
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=True, config={},
        )
        assert p.username_var == "TEST_USER"
        assert p.password_var == "TEST_PASS"

    def test_publish_with_credentials_calls_publish_command(self, monkeypatch):
        monkeypatch.setenv("TEST_USER", "admin")
        monkeypatch.setenv("TEST_PASS", "hunter2")
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=True, config={},
        )
        p.publish("./proj", "3.0.0", None)
        assert p.publish_calls == [("./proj", "3.0.0", "admin", "hunter2")]

    def test_publish_missing_username_exits(self, monkeypatch):
        monkeypatch.delenv("TEST_USER", raising=False)
        monkeypatch.setenv("TEST_PASS", "hunter2")
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=True, config={},
        )
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_publish_missing_password_exits(self, monkeypatch):
        monkeypatch.setenv("TEST_USER", "admin")
        monkeypatch.delenv("TEST_PASS", raising=False)
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=True, config={},
        )
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_publish_missing_both_exits_with_both_names(self, monkeypatch, capsys):
        monkeypatch.delenv("TEST_USER", raising=False)
        monkeypatch.delenv("TEST_PASS", raising=False)
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=True, config={},
        )
        with pytest.raises(SystemExit):
            p.publish(".", "1.0.0", None)
        err = capsys.readouterr().err
        assert "TEST_USER" in err
        assert "TEST_PASS" in err

    def test_publish_local_false_skips(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_USER", "admin")
        monkeypatch.setenv("TEST_PASS", "hunter2")
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=False, config={},
        )
        p.publish(".", "1.0.0", None)
        assert p.publish_calls == []
        assert "local=false" in capsys.readouterr().out

    def test_required_env_vars_local_true(self):
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=True, config={},
        )
        assert p.required_env_vars() == ["TEST_USER", "TEST_PASS"]

    def test_required_env_vars_local_false(self):
        p = _TestCredentialPipeline(
            name="c", pipeline_type="cred", local=False, config={},
        )
        assert p.required_env_vars() == []

    def test_publish_command_not_implemented_on_base(self, monkeypatch):
        monkeypatch.setenv("CRED_USER", "u")
        monkeypatch.setenv("CRED_PASS", "p")
        p = CredentialPipeline(
            name="c", pipeline_type="cred", local=True,
            config={"username_var": "CRED_USER", "password_var": "CRED_PASS"},
        )
        with pytest.raises(NotImplementedError):
            p.publish(".", "1.0.0", None)


# ---------------------------------------------------------------------------
# load_pipelines
# ---------------------------------------------------------------------------


class TestLoadPipelines:
    def test_empty_config_returns_empty_dict(self):
        assert load_pipelines({}) == {}

    def test_no_pipelines_key_returns_empty_dict(self):
        assert load_pipelines({"other": "stuff"}) == {}

    def test_none_pipelines_returns_empty_dict(self):
        assert load_pipelines({"pipelines": None}) == {}

    def test_empty_pipelines_dict_returns_empty_dict(self):
        assert load_pipelines({"pipelines": {}}) == {}

    def test_valid_config_instantiates_pipelines(self, monkeypatch):
        # Register a test pipeline type
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        config = {
            "pipelines": {
                "my_publish": {
                    "type": "test_token",
                    "local": True,
                    "token_var": "MY_TOK",
                }
            }
        }
        result = load_pipelines(config)
        assert "my_publish" in result
        p = result["my_publish"]
        assert p.name == "my_publish"
        assert p.pipeline_type == "test_token"
        assert p.local is True
        assert p.token_var == "MY_TOK"

    def test_multiple_pipelines(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline, "test_cred": _TestCredentialPipeline},
        )
        config = {
            "pipelines": {
                "pub1": {"type": "test_token", "local": True},
                "pub2": {"type": "test_cred", "local": False},
            }
        }
        result = load_pipelines(config)
        assert len(result) == 2
        assert result["pub1"].local is True
        assert result["pub2"].local is False

    def test_unknown_type_raises_key_error(self):
        config = {
            "pipelines": {
                "bad": {"type": "nonexistent", "local": True},
            }
        }
        with pytest.raises(KeyError):
            load_pipelines(config)


# ---------------------------------------------------------------------------
# validate_pipelines_config
# ---------------------------------------------------------------------------


class TestValidatePipelinesConfig:
    def test_no_pipelines_key_passes(self):
        # Should not raise
        validate_pipelines_config({})

    def test_none_pipelines_passes(self):
        validate_pipelines_config({"pipelines": None})

    def test_pipelines_not_dict_fails(self):
        with pytest.raises(ValueError, match="must be a dict"):
            validate_pipelines_config({"pipelines": "not-a-dict"})

    def test_pipelines_list_fails(self):
        with pytest.raises(ValueError, match="must be a dict"):
            validate_pipelines_config({"pipelines": [{"type": "x"}]})

    def test_entry_not_dict_fails(self):
        with pytest.raises(ValueError, match="pipeline 'bad' must be a dict"):
            validate_pipelines_config({"pipelines": {"bad": "string"}})

    def test_missing_type_fails(self):
        with pytest.raises(ValueError, match="missing required key 'type'"):
            validate_pipelines_config({"pipelines": {"p": {"local": True}}})

    def test_type_not_string_fails(self):
        with pytest.raises(ValueError, match="type must be a string"):
            validate_pipelines_config({"pipelines": {"p": {"type": 42, "local": True}}})

    def test_type_not_registered_fails(self):
        with pytest.raises(ValueError, match="not a registered pipeline type"):
            validate_pipelines_config(
                {"pipelines": {"p": {"type": "nonexistent", "local": True}}}
            )

    def test_missing_local_fails(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="missing required key 'local'"):
            validate_pipelines_config(
                {"pipelines": {"p": {"type": "test_token"}}}
            )

    def test_local_not_bool_fails(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="local must be a boolean"):
            validate_pipelines_config(
                {"pipelines": {"p": {"type": "test_token", "local": "yes"}}}
            )

    def test_valid_config_passes(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        validate_pipelines_config(
            {"pipelines": {"p": {"type": "test_token", "local": True}}}
        )

    def test_assets_true_without_max_size_fails(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="max_asset_size_mb is not set"):
            validate_pipelines_config(
                {"pipelines": {"p": {"type": "test_token", "local": True, "assets": True}}}
            )

    def test_assets_true_with_max_size_passes(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        validate_pipelines_config(
            {"pipelines": {"p": {
                "type": "test_token", "local": True,
                "assets": True, "max_asset_size_mb": 50,
            }}}
        )

    def test_assets_true_max_size_zero_fails(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="positive integer"):
            validate_pipelines_config(
                {"pipelines": {"p": {
                    "type": "test_token", "local": True,
                    "assets": True, "max_asset_size_mb": 0,
                }}}
            )

    def test_custom_assets_requires_max_size(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="max_asset_size_mb is not set"):
            validate_pipelines_config(
                {"pipelines": {"p": {
                    "type": "test_token", "local": True,
                    "custom_assets": [{"name": "x.tar.gz", "build": "make dist"}],
                }}}
            )

    def test_custom_assets_with_max_size_passes(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        validate_pipelines_config(
            {"pipelines": {"p": {
                "type": "test_token", "local": True,
                "custom_assets": [{"name": "x.tar.gz", "build": "make dist"}],
                "max_asset_size_mb": 100,
            }}}
        )

    def test_custom_assets_missing_name_fails(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="missing required string key 'name'"):
            validate_pipelines_config(
                {"pipelines": {"p": {
                    "type": "test_token", "local": True,
                    "custom_assets": [{"build": "make dist"}],
                    "max_asset_size_mb": 100,
                }}}
            )

    def test_custom_assets_missing_build_fails(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="missing required string key 'build'"):
            validate_pipelines_config(
                {"pipelines": {"p": {
                    "type": "test_token", "local": True,
                    "custom_assets": [{"name": "x.tar.gz"}],
                    "max_asset_size_mb": 100,
                }}}
            )

    def test_custom_assets_not_list_fails(self, monkeypatch):
        monkeypatch.setattr(
            _pipelines_mod, "PIPELINE_TYPES",
            {**PIPELINE_TYPES, "test_token": _TestTokenPipeline},
        )
        with pytest.raises(ValueError, match="custom_assets must be a list"):
            validate_pipelines_config(
                {"pipelines": {"p": {
                    "type": "test_token", "local": True,
                    "custom_assets": "not-a-list",
                    "max_asset_size_mb": 100,
                }}}
            )
