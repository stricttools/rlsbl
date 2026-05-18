"""Tests for the config-driven per-target publish gate (Phase 5).

Verifies:
- The `get_publish_config` helper reads `.rlsbl/config.json` correctly.
- Each target's publish() honours `local: false` (skip), `local: true` (error if
  token missing, attempt if present), `token_var` override, and falls back to
  the historic env-var-only behaviour when no config entry exists.
- The docs target silently skips when Cloudflare credentials are absent.
"""

import json
import os
from unittest.mock import patch

import pytest

from rlsbl.config import get_publish_config
from rlsbl.targets.cargo import CargoTarget
from rlsbl.targets.deno import DenoTarget
from rlsbl.targets.docker import DockerTarget
from rlsbl.targets.docs import DocsTarget
from rlsbl.targets.hex import HexTarget
from rlsbl.targets.maven import MavenTarget
from rlsbl.targets.npm import NpmTarget
from rlsbl.targets.pypi import PypiTarget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path, payload):
    """Write `.rlsbl/config.json` in tmp_path with the given dict payload."""
    rlsbl_dir = tmp_path / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(payload))


def _clear_token_env(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


# ---------------------------------------------------------------------------
# get_publish_config helper
# ---------------------------------------------------------------------------


class TestGetPublishConfig:
    def test_returns_target_dict(self, tmp_project):
        _write_config(
            tmp_project,
            {"publish": {"pypi": {"local": True, "token_var": "PYPI_TOKEN"}}},
        )
        assert get_publish_config("pypi") == {"local": True, "token_var": "PYPI_TOKEN"}

    def test_empty_when_no_publish_section(self, tmp_project):
        _write_config(tmp_project, {})
        assert get_publish_config("pypi") == {}

    def test_empty_when_target_absent(self, tmp_project):
        _write_config(tmp_project, {"publish": {"npm": {"local": False}}})
        assert get_publish_config("pypi") == {}

    def test_empty_when_config_file_missing(self, tmp_project):
        # No .rlsbl/config.json at all
        assert get_publish_config("pypi") == {}

    def test_empty_when_publish_not_dict(self, tmp_project):
        _write_config(tmp_project, {"publish": "not-a-dict"})
        assert get_publish_config("pypi") == {}

    def test_empty_when_target_entry_not_dict(self, tmp_project):
        _write_config(tmp_project, {"publish": {"pypi": "garbage"}})
        assert get_publish_config("pypi") == {}


# ---------------------------------------------------------------------------
# NpmTarget config gate
# ---------------------------------------------------------------------------


class TestNpmPublishConfig:
    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"npm": {"local": False}}})
        monkeypatch.setenv("NPM_TOKEN", "should-not-be-used")
        with patch("rlsbl.targets.npm.run") as mock_run:
            NpmTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_without_token_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"npm": {"local": True}}})
        _clear_token_env(monkeypatch, "NPM_TOKEN")
        with pytest.raises(SystemExit) as excinfo:
            NpmTarget().publish(".", "1.0.0")
        assert excinfo.value.code == 1

    def test_local_true_with_token_publishes(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"npm": {"local": True}}})
        monkeypatch.setenv("NPM_TOKEN", "real-token")
        with patch("rlsbl.targets.npm.run") as mock_run:
            NpmTarget().publish(".", "1.0.0")
            mock_run.assert_called_once()

    def test_no_config_with_token_publishes(self, tmp_project, monkeypatch):
        monkeypatch.setenv("NPM_TOKEN", "real-token")
        with patch("rlsbl.targets.npm.run") as mock_run:
            NpmTarget().publish(".", "1.0.0")
            mock_run.assert_called_once()

    def test_no_config_without_token_skips(self, tmp_project, monkeypatch, capsys):
        _clear_token_env(monkeypatch, "NPM_TOKEN")
        with patch("rlsbl.targets.npm.run") as mock_run:
            NpmTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "Skipping local npm publish" in capsys.readouterr().out

    def test_token_var_override(self, tmp_project, monkeypatch):
        _write_config(
            tmp_project,
            {"publish": {"npm": {"local": True, "token_var": "CUSTOM_NPM"}}},
        )
        _clear_token_env(monkeypatch, "NPM_TOKEN")
        monkeypatch.setenv("CUSTOM_NPM", "real-token")
        with patch("rlsbl.targets.npm.run") as mock_run:
            NpmTarget().publish(".", "1.0.0")
            mock_run.assert_called_once()
            assert mock_run.call_args[1]["env"]["NPM_TOKEN"] == "real-token"


# ---------------------------------------------------------------------------
# PypiTarget config gate (special: dual default token var)
# ---------------------------------------------------------------------------


class TestPypiPublishConfig:
    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"pypi": {"local": False}}})
        monkeypatch.setenv("PYPI_TOKEN", "should-not-be-used")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_without_token_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"pypi": {"local": True}}})
        _clear_token_env(monkeypatch, "PYPI_TOKEN", "TWINE_PASSWORD")
        with pytest.raises(SystemExit) as excinfo:
            PypiTarget().publish(".", "1.0.0")
        assert excinfo.value.code == 1

    def test_local_true_with_token_publishes(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"pypi": {"local": True}}})
        monkeypatch.setenv("PYPI_TOKEN", "real-token")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            assert mock_run.call_count == 2  # uv build + uv publish

    def test_no_config_pypi_token_works(self, tmp_project, monkeypatch):
        _clear_token_env(monkeypatch, "TWINE_PASSWORD")
        monkeypatch.setenv("PYPI_TOKEN", "real-token")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            assert mock_run.call_count == 2

    def test_no_config_twine_fallback_works(self, tmp_project, monkeypatch):
        _clear_token_env(monkeypatch, "PYPI_TOKEN")
        monkeypatch.setenv("TWINE_PASSWORD", "twine-secret")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            assert mock_run.call_count == 2
            assert mock_run.call_args_list[1][1]["env"]["UV_PUBLISH_TOKEN"] == "twine-secret"

    def test_no_config_no_token_skips(self, tmp_project, monkeypatch, capsys):
        _clear_token_env(monkeypatch, "PYPI_TOKEN", "TWINE_PASSWORD")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "no PYPI_TOKEN or TWINE_PASSWORD" in capsys.readouterr().out

    def test_token_var_override_missing_skip_message(
        self, tmp_project, monkeypatch, capsys
    ):
        """When token_var is configured but unset, the skip message names it."""
        _write_config(
            tmp_project,
            {"publish": {"pypi": {"token_var": "CUSTOM_PYPI"}}},
        )
        # PYPI_TOKEN/TWINE_PASSWORD must be irrelevant when token_var is set.
        monkeypatch.setenv("PYPI_TOKEN", "should-not-be-used")
        monkeypatch.setenv("TWINE_PASSWORD", "should-not-be-used")
        _clear_token_env(monkeypatch, "CUSTOM_PYPI")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "no CUSTOM_PYPI" in out
        assert "PYPI_TOKEN" not in out.replace("no CUSTOM_PYPI", "")
        assert "TWINE_PASSWORD" not in out

    def test_twine_only_publishes_no_skip_message(
        self, tmp_project, monkeypatch, capsys
    ):
        """When PYPI_TOKEN is missing but TWINE_PASSWORD is set, publish proceeds."""
        _clear_token_env(monkeypatch, "PYPI_TOKEN")
        monkeypatch.setenv("TWINE_PASSWORD", "twine-secret")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            assert mock_run.call_count == 2
        out = capsys.readouterr().out
        # No "Skipping" message should appear since TWINE_PASSWORD covers the gap.
        assert "Skipping" not in out

    def test_local_true_no_token_error_message_lists_both_vars(
        self, tmp_project, monkeypatch, capsys
    ):
        """When local=true and both default vars are absent, the error names both."""
        _write_config(tmp_project, {"publish": {"pypi": {"local": True}}})
        _clear_token_env(monkeypatch, "PYPI_TOKEN", "TWINE_PASSWORD")
        with pytest.raises(SystemExit):
            PypiTarget().publish(".", "1.0.0")
        err = capsys.readouterr().err
        assert "PYPI_TOKEN or TWINE_PASSWORD" in err

    def test_token_var_override_ignores_twine(self, tmp_project, monkeypatch):
        """With token_var set, TWINE_PASSWORD is no longer consulted."""
        _write_config(
            tmp_project,
            {"publish": {"pypi": {"local": True, "token_var": "CUSTOM_PYPI"}}},
        )
        _clear_token_env(monkeypatch, "PYPI_TOKEN")
        monkeypatch.setenv("TWINE_PASSWORD", "should-not-be-used")
        monkeypatch.setenv("CUSTOM_PYPI", "real-token")
        with patch("rlsbl.targets.pypi.run") as mock_run:
            PypiTarget().publish(".", "1.0.0")
            assert mock_run.call_args_list[1][1]["env"]["UV_PUBLISH_TOKEN"] == "real-token"


# ---------------------------------------------------------------------------
# CargoTarget config gate
# ---------------------------------------------------------------------------


class TestCargoPublishConfig:
    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"cargo": {"local": False}}})
        monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "x")
        with patch("rlsbl.targets.cargo.run") as mock_run:
            CargoTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_without_token_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"cargo": {"local": True}}})
        _clear_token_env(monkeypatch, "CARGO_REGISTRY_TOKEN")
        with pytest.raises(SystemExit):
            CargoTarget().publish(".", "1.0.0")

    def test_token_var_override(self, tmp_project, monkeypatch):
        _write_config(
            tmp_project,
            {"publish": {"cargo": {"local": True, "token_var": "CUSTOM_CARGO"}}},
        )
        _clear_token_env(monkeypatch, "CARGO_REGISTRY_TOKEN")
        monkeypatch.setenv("CUSTOM_CARGO", "real-token")
        with patch("rlsbl.targets.cargo.run") as mock_run:
            CargoTarget().publish(".", "1.0.0")
            mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# HexTarget / MavenTarget / DenoTarget config gate (smoke)
# ---------------------------------------------------------------------------


class TestHexPublishConfig:
    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"hex": {"local": False}}})
        monkeypatch.setenv("HEX_API_KEY", "x")
        with patch("rlsbl.targets.hex.run") as mock_run:
            HexTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_without_token_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"hex": {"local": True}}})
        _clear_token_env(monkeypatch, "HEX_API_KEY")
        with pytest.raises(SystemExit):
            HexTarget().publish(".", "1.0.0")


class TestMavenPublishConfig:
    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"maven": {"local": False}}})
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        with patch("rlsbl.targets.maven.run") as mock_run:
            MavenTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_without_token_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"maven": {"local": True}}})
        _clear_token_env(monkeypatch, "GITHUB_TOKEN")
        with pytest.raises(SystemExit):
            MavenTarget().publish(".", "1.0.0")


class TestDenoPublishConfig:
    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"deno": {"local": False}}})
        monkeypatch.setenv("DENO_TOKEN", "x")
        with patch("rlsbl.targets.deno.run") as mock_run:
            DenoTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_without_token_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"deno": {"local": True}}})
        _clear_token_env(monkeypatch, "DENO_TOKEN", "JSR_TOKEN")
        with pytest.raises(SystemExit):
            DenoTarget().publish(".", "1.0.0")

    def test_token_var_override(self, tmp_project, monkeypatch):
        _write_config(
            tmp_project,
            {"publish": {"deno": {"local": True, "token_var": "CUSTOM_DENO"}}},
        )
        _clear_token_env(monkeypatch, "DENO_TOKEN", "JSR_TOKEN")
        monkeypatch.setenv("CUSTOM_DENO", "real-token")
        with patch("rlsbl.targets.deno.run") as mock_run:
            DenoTarget().publish(".", "1.0.0")
            mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# DockerTarget config gate (special: two env vars)
# ---------------------------------------------------------------------------


class TestDockerPublishConfig:
    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"docker": {"local": False}}})
        monkeypatch.setenv("DOCKER_USERNAME", "u")
        monkeypatch.setenv("DOCKER_PASSWORD", "p")
        with patch("rlsbl.targets.docker.run") as mock_run:
            DockerTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_without_creds_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"docker": {"local": True}}})
        _clear_token_env(monkeypatch, "DOCKER_USERNAME", "DOCKER_PASSWORD")
        with pytest.raises(SystemExit):
            DockerTarget().publish(".", "1.0.0")

    def test_username_password_var_override(self, tmp_project, monkeypatch):
        """Config can rename DOCKER_USERNAME/DOCKER_PASSWORD via username_var/password_var."""
        _write_config(
            tmp_project,
            {
                "publish": {
                    "docker": {
                        "local": True,
                        "username_var": "REG_USER",
                        "password_var": "REG_PASS",
                    }
                },
                "docker": {"image": "myapp", "registry": "ghcr.io"},
            },
        )
        _clear_token_env(monkeypatch, "DOCKER_USERNAME", "DOCKER_PASSWORD")
        monkeypatch.setenv("REG_USER", "user")
        monkeypatch.setenv("REG_PASS", "pass")
        (tmp_project / "Dockerfile").write_text("FROM python:3.12\n")
        with patch("rlsbl.targets.docker.require_tool", return_value="/usr/bin/docker"):
            with patch("rlsbl.targets.docker.run") as mock_run:
                DockerTarget().publish(str(tmp_project), "1.0.0")
                assert mock_run.call_count >= 2


# ---------------------------------------------------------------------------
# DocsTarget config gate (special: selfdoc + CF credentials)
# ---------------------------------------------------------------------------


class TestDocsPublishConfig:
    def test_no_config_no_cf_credentials_skips_silently(
        self, tmp_project, monkeypatch, capsys
    ):
        """Without config and without CF creds, docs publish skips and prints a notice."""
        _clear_token_env(monkeypatch, "CF_ACCOUNT_ID", "CF_PAGES_API_TOKEN")
        with patch("rlsbl.targets.docs.subprocess.run") as mock_run:
            DocsTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "Skipping local docs publish" in out
        # The message should reference the missing creds (not a stack trace).
        assert "CF_ACCOUNT_ID" in out or "CF_PAGES_API_TOKEN" in out

    def test_no_config_with_cf_credentials_runs_selfdoc(self, tmp_project, monkeypatch):
        monkeypatch.setenv("CF_ACCOUNT_ID", "acct")
        monkeypatch.setenv("CF_PAGES_API_TOKEN", "tok")
        with patch("rlsbl.targets.docs.require_tool", return_value="/usr/bin/selfdoc"):
            with patch("rlsbl.targets.docs.subprocess.run") as mock_run:
                DocsTarget().publish(".", "1.0.0")
                mock_run.assert_called_once()
                assert mock_run.call_args[0][0] == ["selfdoc", "deploy"]

    def test_local_false_skips(self, tmp_project, monkeypatch, capsys):
        _write_config(tmp_project, {"publish": {"docs": {"local": False}}})
        monkeypatch.setenv("CF_ACCOUNT_ID", "acct")
        monkeypatch.setenv("CF_PAGES_API_TOKEN", "tok")
        with patch("rlsbl.targets.docs.subprocess.run") as mock_run:
            DocsTarget().publish(".", "1.0.0")
            mock_run.assert_not_called()
        assert "config: local=false" in capsys.readouterr().out

    def test_local_true_selfdoc_missing_exits(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"docs": {"local": True}}})
        with patch("rlsbl.targets.docs.require_tool", return_value=None):
            with pytest.raises(SystemExit) as excinfo:
                DocsTarget().publish(".", "1.0.0")
            assert excinfo.value.code == 1

    def test_local_true_selfdoc_present_runs(self, tmp_project, monkeypatch):
        _write_config(tmp_project, {"publish": {"docs": {"local": True}}})
        # CF creds intentionally absent: local=true bypasses the credential check.
        _clear_token_env(monkeypatch, "CF_ACCOUNT_ID", "CF_PAGES_API_TOKEN")
        with patch("rlsbl.targets.docs.require_tool", return_value="/usr/bin/selfdoc"):
            with patch("rlsbl.targets.docs.subprocess.run") as mock_run:
                DocsTarget().publish(".", "1.0.0")
                mock_run.assert_called_once()
                assert mock_run.call_args[0][0] == ["selfdoc", "deploy"]
