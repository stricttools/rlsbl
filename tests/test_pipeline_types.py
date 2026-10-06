"""Tests for concrete pipeline type implementations (npm, pypi, go, cloudflare-pages)."""

import subprocess

import pytest

from rlsbl.pipelines import PIPELINE_TYPES, Pipeline
from rlsbl.pipelines.npm import NpmPipeline
from rlsbl.pipelines.pypi import PypiPipeline
from rlsbl.pipelines.go import GoPipeline
from rlsbl.pipelines.cloudflare_pages import CloudflarePagesPipeline


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestPipelineRegistry:
    def test_all_types_registered(self):
        expected = {"npm", "pypi", "go", "cloudflare-pages"}
        assert set(PIPELINE_TYPES.keys()) == expected

    def test_registry_maps_to_classes(self):
        assert PIPELINE_TYPES["npm"] is NpmPipeline
        assert PIPELINE_TYPES["pypi"] is PypiPipeline
        assert PIPELINE_TYPES["go"] is GoPipeline
        assert PIPELINE_TYPES["cloudflare-pages"] is CloudflarePagesPipeline


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    @pytest.mark.parametrize("cls", [
        NpmPipeline, PypiPipeline, GoPipeline, CloudflarePagesPipeline,
    ])
    def test_satisfies_pipeline_protocol(self, cls):
        p = cls(name="test", pipeline_type="test", local=False, config={})
        assert isinstance(p, Pipeline)


# ---------------------------------------------------------------------------
# Cross-pipeline parametrized tests
# ---------------------------------------------------------------------------


# All pipeline classes with (name, pipeline_type, class) for constructing instances
_ALL_PIPELINES = [
    ("npm", "npm", NpmPipeline),
    ("pypi", "pypi", PypiPipeline),
    ("go", "go", GoPipeline),
    ("cf", "cloudflare-pages", CloudflarePagesPipeline),
]


class TestRequiredEnvVarsLocalFalse:
    """All pipelines return [] for required_env_vars when local=False."""

    @pytest.mark.parametrize("name, ptype, cls", _ALL_PIPELINES,
                             ids=[t[0] for t in _ALL_PIPELINES])
    def test_required_env_vars_local_false(self, name, ptype, cls):
        p = cls(name=name, pipeline_type=ptype, local=False, config={})
        assert p.required_env_vars() == []


class TestPublishLocalFalseSkips:
    """All pipelines print 'local=false' and skip when local=False."""

    @pytest.mark.parametrize("name, ptype, cls", _ALL_PIPELINES,
                             ids=[t[0] for t in _ALL_PIPELINES])
    def test_publish_local_false_skips(self, capsys, name, ptype, cls):
        p = cls(name=name, pipeline_type=ptype, local=False, config={})
        p.publish(".", "1.0.0", None)
        assert "local=false" in capsys.readouterr().out


# Pipelines with a single token_var attribute and matching required_env_vars
_TOKEN_PIPELINES = [
    ("npm", "npm", NpmPipeline, "NPM_TOKEN"),
    ("pypi", "pypi", PypiPipeline, "PYPI_TOKEN"),
]


class TestDefaultTokenVar:
    """Token-based pipelines expose the correct default token_var."""

    @pytest.mark.parametrize("name, ptype, cls, expected_var", _TOKEN_PIPELINES,
                             ids=[t[0] for t in _TOKEN_PIPELINES])
    def test_default_token_var(self, name, ptype, cls, expected_var):
        p = cls(name=name, pipeline_type=ptype, local=True, config={})
        assert p.token_var == expected_var


class TestRequiredEnvVarsLocalTrue:
    """Token-based pipelines return [token_var] when local=True."""

    @pytest.mark.parametrize("name, ptype, cls, expected_var", _TOKEN_PIPELINES,
                             ids=[t[0] for t in _TOKEN_PIPELINES])
    def test_required_env_vars_local_true(self, name, ptype, cls, expected_var):
        p = cls(name=name, pipeline_type=ptype, local=True, config={})
        assert p.required_env_vars() == [expected_var]


# ---------------------------------------------------------------------------
# Token-based pipelines: npm
# ---------------------------------------------------------------------------


class TestNpmPipeline:
    def test_publish_with_token_calls_command(self, monkeypatch):
        calls = []
        monkeypatch.setenv("NPM_TOKEN", "tok123")
        monkeypatch.setattr(
            "rlsbl.pipelines.npm.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = NpmPipeline(name="npm", pipeline_type="npm", local=True, config={})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 1
        # Local publish never uses --provenance: OIDC build-provenance
        # attestation is only possible inside GitHub Actions, never locally.
        assert calls[0] == ("npm", ["publish", "--access", "public"])

    def test_local_publish_omits_provenance(self, monkeypatch):
        calls = []
        monkeypatch.setenv("NPM_TOKEN", "tok123")
        monkeypatch.setattr(
            "rlsbl.pipelines.npm.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = NpmPipeline(name="npm", pipeline_type="npm", local=True, config={})
        p.publish(".", "2.3.4-beta.1", None)
        assert "--provenance" not in calls[0][1]

    def test_custom_token_var(self):
        p = NpmPipeline(name="npm", pipeline_type="npm", local=True,
                        config={"token_var": "MY_NPM_TOKEN"})
        assert p.token_var == "MY_NPM_TOKEN"
        assert p.required_env_vars() == ["MY_NPM_TOKEN"]


# ---------------------------------------------------------------------------
# Dual-token pipelines: pypi
# ---------------------------------------------------------------------------


class TestPypiPipeline:
    def test_required_env_vars_custom_token_var(self):
        p = PypiPipeline(name="pypi", pipeline_type="pypi", local=True,
                         config={"token_var": "CUSTOM_TOK"})
        assert p.required_env_vars() == ["CUSTOM_TOK"]

    def test_publish_with_pypi_token(self, monkeypatch):
        calls = []
        monkeypatch.setenv("PYPI_TOKEN", "pypi123")
        monkeypatch.delenv("TWINE_PASSWORD", raising=False)
        monkeypatch.setattr(
            "rlsbl.pipelines.pypi.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = PypiPipeline(name="pypi", pipeline_type="pypi", local=True, config={})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 2
        assert calls[0] == ("uv", ["build"])
        assert calls[1] == ("uv", ["publish", "--check-url", "https://pypi.org/simple/"])

    def test_publish_with_twine_password_fallback(self, monkeypatch):
        calls = []
        monkeypatch.delenv("PYPI_TOKEN", raising=False)
        monkeypatch.setenv("TWINE_PASSWORD", "twine456")
        monkeypatch.setattr(
            "rlsbl.pipelines.pypi.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = PypiPipeline(name="pypi", pipeline_type="pypi", local=True, config={})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 2

    def test_publish_neither_token_exits(self, monkeypatch):
        monkeypatch.delenv("PYPI_TOKEN", raising=False)
        monkeypatch.delenv("TWINE_PASSWORD", raising=False)
        p = PypiPipeline(name="pypi", pipeline_type="pypi", local=True, config={})
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_publish_custom_token_var(self, monkeypatch):
        calls = []
        monkeypatch.setenv("MY_TOK", "custom_value")
        monkeypatch.setattr(
            "rlsbl.pipelines.pypi.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = PypiPipeline(name="pypi", pipeline_type="pypi", local=True,
                         config={"token_var": "MY_TOK"})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 2

    def test_publish_custom_token_var_missing_exits(self, monkeypatch):
        monkeypatch.delenv("MY_TOK", raising=False)
        p = PypiPipeline(name="pypi", pipeline_type="pypi", local=True,
                         config={"token_var": "MY_TOK"})
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Standalone: go
# ---------------------------------------------------------------------------


class TestGoPipeline:
    def test_required_env_vars_always_empty(self):
        p = GoPipeline(name="go", pipeline_type="go", local=True, config={})
        assert p.required_env_vars() == []

    def test_publish_no_gomod_raises(self, tmp_path):
        from rlsbl.errors import ConfigError
        p = GoPipeline(name="go", pipeline_type="go", local=True,
                       config={"install_paths": ["."]})
        with pytest.raises(ConfigError, match="module path"):
            p.publish(str(tmp_path), "1.0.0", None)

    def test_publish_with_gomod_calls_proxy(self, tmp_path, monkeypatch):
        # Create a go.mod plus a main package matching the declared path
        gomod = tmp_path / "go.mod"
        gomod.write_text("module github.com/test/mymod\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")

        calls = []
        monkeypatch.setattr(
            "rlsbl.pipelines.go.require_tool",
            lambda name, purpose=None, fatal=True: "/usr/bin/go",
        )
        monkeypatch.setattr(
            "rlsbl.pipelines.go.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        installs = []
        monkeypatch.setattr(
            "rlsbl.pipelines.go.validate_install_paths",
            lambda d, paths: paths,
        )
        monkeypatch.setattr(
            "rlsbl.effects.run",
            lambda cmd, **kw: installs.append(cmd)
            or subprocess.CompletedProcess(args=cmd, returncode=0),
        )
        p = GoPipeline(name="go", pipeline_type="go", local=True,
                       config={"install_paths": ["."]})
        p.publish(str(tmp_path), "1.0.0", None)
        assert len(calls) == 1
        assert calls[0] == ("go", ["list", "-m", "github.com/test/mymod@v1.0.0"])
        assert installs == [["go", "install", "."]]


# ---------------------------------------------------------------------------
# Cloudflare Pages
# ---------------------------------------------------------------------------


class TestCloudflarePagesPipeline:
    def test_required_env_vars_local_true(self):
        p = CloudflarePagesPipeline(name="cf", pipeline_type="cloudflare-pages",
                                    local=True, config={})
        assert p.required_env_vars() == ["CF_ACCOUNT_ID", "CF_PAGES_API_TOKEN"]

    def test_publish_selfdoc_missing_exits(self, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.pipelines.cloudflare_pages.require_tool",
            lambda name, fatal=True: None,
        )
        p = CloudflarePagesPipeline(name="cf", pipeline_type="cloudflare-pages",
                                    local=True, config={})
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_publish_calls_selfdoc_deploy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "rlsbl.pipelines.cloudflare_pages.require_tool",
            lambda name, fatal=True: "/usr/bin/selfdoc",
        )
        monkeypatch.setattr(
            "rlsbl.effects.run",
            lambda cmd, **kw: calls.append(cmd),
        )
        p = CloudflarePagesPipeline(name="cf", pipeline_type="cloudflare-pages",
                                    local=True, config={})
        p.publish(".", "1.0.0", None)
        assert calls == [["selfdoc", "deploy", "--approve-consequential"]]
