"""Tests for concrete pipeline type implementations (npm, pypi, go, cargo, deno, hex, maven, docker, cloudflare-pages)."""

import os
import subprocess

import pytest

from rlsbl.pipelines import PIPELINE_TYPES, Pipeline
from rlsbl.pipelines.npm import NpmPipeline
from rlsbl.pipelines.pypi import PypiPipeline
from rlsbl.pipelines.go import GoPipeline
from rlsbl.pipelines.cargo import CargoPipeline
from rlsbl.pipelines.deno import DenoPipeline
from rlsbl.pipelines.hex import HexPipeline
from rlsbl.pipelines.maven import MavenPipeline, MavenCentralPipeline
from rlsbl.pipelines.docker import DockerPipeline
from rlsbl.pipelines.cloudflare_pages import CloudflarePagesPipeline


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestPipelineRegistry:
    def test_all_10_types_registered(self):
        expected = {"npm", "pypi", "go", "cargo", "deno", "hex", "maven", "maven-central", "docker", "cloudflare-pages"}
        assert set(PIPELINE_TYPES.keys()) == expected

    def test_registry_maps_to_classes(self):
        assert PIPELINE_TYPES["npm"] is NpmPipeline
        assert PIPELINE_TYPES["pypi"] is PypiPipeline
        assert PIPELINE_TYPES["go"] is GoPipeline
        assert PIPELINE_TYPES["cargo"] is CargoPipeline
        assert PIPELINE_TYPES["deno"] is DenoPipeline
        assert PIPELINE_TYPES["hex"] is HexPipeline
        assert PIPELINE_TYPES["maven"] is MavenPipeline
        assert PIPELINE_TYPES["maven-central"] is MavenCentralPipeline
        assert PIPELINE_TYPES["docker"] is DockerPipeline
        assert PIPELINE_TYPES["cloudflare-pages"] is CloudflarePagesPipeline


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    @pytest.mark.parametrize("cls", [
        NpmPipeline, PypiPipeline, GoPipeline, CargoPipeline,
        DenoPipeline, HexPipeline, MavenPipeline, MavenCentralPipeline,
        DockerPipeline, CloudflarePagesPipeline,
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
    ("cargo", "cargo", CargoPipeline),
    ("deno", "deno", DenoPipeline),
    ("hex", "hex", HexPipeline),
    ("maven", "maven", MavenPipeline),
    ("maven-central", "maven-central", MavenCentralPipeline),
    ("docker", "docker", DockerPipeline),
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
    ("cargo", "cargo", CargoPipeline, "CARGO_REGISTRY_TOKEN"),
    ("hex", "hex", HexPipeline, "HEX_API_KEY"),
    ("pypi", "pypi", PypiPipeline, "PYPI_TOKEN"),
    ("deno", "deno", DenoPipeline, "DENO_TOKEN"),
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
# Token-based pipelines: npm, cargo, hex
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


class TestCargoPipeline:
    def test_publish_with_token_calls_command(self, monkeypatch):
        calls = []
        monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "tok456")
        monkeypatch.setattr(
            "rlsbl.pipelines.cargo.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = CargoPipeline(name="cargo", pipeline_type="cargo", local=True, config={})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 1
        assert calls[0] == ("cargo", ["publish"])


class TestHexPipeline:
    def test_publish_with_token_calls_command(self, monkeypatch):
        calls = []
        monkeypatch.setenv("HEX_API_KEY", "hexkey")
        monkeypatch.setattr(
            "rlsbl.pipelines.hex.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = HexPipeline(name="hex", pipeline_type="hex", local=True, config={})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 1
        assert calls[0] == ("mix", ["hex.publish", "--yes"])


# ---------------------------------------------------------------------------
# Dual-token pipelines: pypi, deno
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
        assert calls[1] == ("uv", ["publish"])

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


class TestDenoPipeline:
    def test_publish_with_deno_token(self, monkeypatch):
        calls = []
        monkeypatch.setenv("DENO_TOKEN", "deno123")
        monkeypatch.delenv("JSR_TOKEN", raising=False)
        monkeypatch.setattr(
            "rlsbl.pipelines.deno.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = DenoPipeline(name="deno", pipeline_type="deno", local=True, config={})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 1
        assert calls[0] == ("deno", ["publish"])

    def test_publish_with_jsr_token_fallback(self, monkeypatch):
        calls = []
        monkeypatch.delenv("DENO_TOKEN", raising=False)
        monkeypatch.setenv("JSR_TOKEN", "jsr456")
        monkeypatch.setattr(
            "rlsbl.pipelines.deno.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = DenoPipeline(name="deno", pipeline_type="deno", local=True, config={})
        p.publish(".", "1.0.0", None)
        assert len(calls) == 1

    def test_publish_neither_token_exits(self, monkeypatch):
        monkeypatch.delenv("DENO_TOKEN", raising=False)
        monkeypatch.delenv("JSR_TOKEN", raising=False)
        p = DenoPipeline(name="deno", pipeline_type="deno", local=True, config={})
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Credential pipeline: docker
# ---------------------------------------------------------------------------


class TestDockerPipeline:
    def test_default_credential_vars(self):
        p = DockerPipeline(name="docker", pipeline_type="docker", local=True, config={})
        assert p.username_var == "DOCKER_USERNAME"
        assert p.password_var == "DOCKER_PASSWORD"

    def test_required_env_vars_local_true(self):
        p = DockerPipeline(name="docker", pipeline_type="docker", local=True, config={})
        assert p.required_env_vars() == ["DOCKER_USERNAME", "DOCKER_PASSWORD"]

    def test_publish_missing_image_raises(self, monkeypatch):
        monkeypatch.setenv("DOCKER_USERNAME", "user")
        monkeypatch.setenv("DOCKER_PASSWORD", "pass")
        p = DockerPipeline(name="docker", pipeline_type="docker", local=True,
                           config={"registry": "ghcr.io"})
        with pytest.raises(RuntimeError, match="image.*registry"):
            p.publish(".", "1.0.0", None)

    def test_publish_missing_registry_raises(self, monkeypatch):
        monkeypatch.setenv("DOCKER_USERNAME", "user")
        monkeypatch.setenv("DOCKER_PASSWORD", "pass")
        p = DockerPipeline(name="docker", pipeline_type="docker", local=True,
                           config={"image": "myapp"})
        with pytest.raises(RuntimeError, match="image.*registry"):
            p.publish(".", "1.0.0", None)

    def test_publish_with_credentials_calls_docker(self, monkeypatch):
        calls = []
        monkeypatch.setenv("DOCKER_USERNAME", "user")
        monkeypatch.setenv("DOCKER_PASSWORD", "pass")
        monkeypatch.setattr(
            "rlsbl.pipelines.docker.require_tool",
            lambda name, fatal=True: "/usr/bin/docker",
        )
        monkeypatch.setattr(
            "rlsbl.pipelines.docker.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = DockerPipeline(name="docker", pipeline_type="docker", local=True,
                           config={"image": "myapp", "registry": "ghcr.io/org"})
        p.publish(".", "2.0.0", None)
        assert len(calls) == 4
        # build, push versioned, tag latest, push latest
        assert calls[0][0] == "docker"
        assert "build" in calls[0][1]
        assert calls[1] == ("docker", ["push", "ghcr.io/org/myapp:2.0.0"])
        assert calls[2] == ("docker", ["tag", "ghcr.io/org/myapp:2.0.0", "ghcr.io/org/myapp:latest"])
        assert calls[3] == ("docker", ["push", "ghcr.io/org/myapp:latest"])


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
            "rlsbl.pipelines.go.subprocess.run",
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
# Standalone: maven
# ---------------------------------------------------------------------------


class TestMavenPipeline:
    def test_required_env_vars_local_true(self):
        p = MavenPipeline(name="maven", pipeline_type="maven", local=True, config={})
        assert p.required_env_vars() == ["GITHUB_TOKEN"]

    def test_required_env_vars_custom_token_var(self):
        p = MavenPipeline(name="maven", pipeline_type="maven", local=True,
                          config={"token_var": "MAVEN_TOKEN"})
        assert p.required_env_vars() == ["MAVEN_TOKEN"]

    def test_publish_missing_token_exits(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        p = MavenPipeline(name="maven", pipeline_type="maven", local=True, config={})
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_publish_with_gradlew(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
        # Create gradlew
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/sh\n")
        gradlew.chmod(0o755)
        monkeypatch.setattr(
            "rlsbl.pipelines.maven.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = MavenPipeline(name="maven", pipeline_type="maven", local=True, config={})
        p.publish(str(tmp_path), "1.0.0", None)
        assert len(calls) == 1
        assert calls[0] == ("./gradlew", ["publish"])

    def test_publish_with_pom(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
        # Create pom.xml (no gradlew)
        pom = tmp_path / "pom.xml"
        pom.write_text("<project></project>")
        monkeypatch.setattr(
            "rlsbl.pipelines.maven.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = MavenPipeline(name="maven", pipeline_type="maven", local=True, config={})
        p.publish(str(tmp_path), "1.0.0", None)
        assert len(calls) == 1
        assert calls[0] == ("mvn", ["deploy"])

    def test_publish_no_build_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
        p = MavenPipeline(name="maven", pipeline_type="maven", local=True, config={})
        with pytest.raises(RuntimeError, match="no gradlew or pom.xml"):
            p.publish(str(tmp_path), "1.0.0", None)


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
            "subprocess.run",
            lambda cmd, **kw: calls.append(cmd),
        )
        p = CloudflarePagesPipeline(name="cf", pipeline_type="cloudflare-pages",
                                    local=True, config={})
        p.publish(".", "1.0.0", None)
        assert calls == [["selfdoc", "deploy"]]
