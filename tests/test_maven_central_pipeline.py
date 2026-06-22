"""Tests for MavenCentralPipeline (type: maven-central)."""

import os

import pytest

from rlsbl.pipelines import PIPELINE_TYPES, Pipeline
from rlsbl.pipelines.maven import MavenPipeline, MavenCentralPipeline


_REQUIRED_VARS = [
    "ORG_GRADLE_PROJECT_mavenCentralUsername",
    "ORG_GRADLE_PROJECT_mavenCentralPassword",
    "ORG_GRADLE_PROJECT_signingInMemoryKey",
    "ORG_GRADLE_PROJECT_signingInMemoryKeyPassword",
]


def _set_all_env_vars(monkeypatch):
    """Set all required Maven Central env vars to dummy values."""
    for var in _REQUIRED_VARS:
        monkeypatch.setenv(var, f"dummy_{var}")


def _clear_all_env_vars(monkeypatch):
    """Clear all required Maven Central env vars."""
    for var in _REQUIRED_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestMavenCentralRegistration:
    def test_registered_as_maven_central(self):
        assert "maven-central" in PIPELINE_TYPES
        assert PIPELINE_TYPES["maven-central"] is MavenCentralPipeline

    def test_satisfies_pipeline_protocol(self):
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=False, config={}
        )
        assert isinstance(p, Pipeline)


# ---------------------------------------------------------------------------
# required_env_vars
# ---------------------------------------------------------------------------


class TestMavenCentralRequiredEnvVars:
    def test_returns_4_vars_when_local_true(self):
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        result = p.required_env_vars()
        assert result == _REQUIRED_VARS

    def test_returns_empty_when_local_false(self):
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=False, config={}
        )
        assert p.required_env_vars() == []


# ---------------------------------------------------------------------------
# publish -- Gradle path
# ---------------------------------------------------------------------------


class TestMavenCentralPublishGradle:
    def test_runs_gradle_publish_and_release(self, tmp_path, monkeypatch):
        calls = []
        _set_all_env_vars(monkeypatch)
        # Create gradlew
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/sh\n")
        gradlew.chmod(0o755)
        monkeypatch.setattr(
            "rlsbl.pipelines.maven.run",
            lambda cmd, args, **kw: calls.append((cmd, args, kw.get("cwd"))),
        )
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        p.publish(str(tmp_path), "1.2.3", None)
        assert len(calls) == 1
        assert calls[0][0] == "./gradlew"
        assert calls[0][1] == ["publishAndReleaseToMavenCentral"]
        assert calls[0][2] == str(tmp_path)

    def test_env_vars_passed_to_subprocess(self, tmp_path, monkeypatch):
        captured_env = {}
        _set_all_env_vars(monkeypatch)
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/sh\n")
        gradlew.chmod(0o755)

        def fake_run(cmd, args, **kw):
            captured_env.update(kw.get("env", {}))

        monkeypatch.setattr("rlsbl.pipelines.maven.run", fake_run)
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        p.publish(str(tmp_path), "1.0.0", None)
        # The env passed to run should contain all required vars
        for var in _REQUIRED_VARS:
            assert var in captured_env
            assert captured_env[var] == f"dummy_{var}"


# ---------------------------------------------------------------------------
# publish -- Maven path
# ---------------------------------------------------------------------------


class TestMavenCentralPublishMaven:
    def test_runs_mvn_deploy_for_pom_only(self, tmp_path, monkeypatch):
        calls = []
        _set_all_env_vars(monkeypatch)
        # Create pom.xml (no gradlew)
        pom = tmp_path / "pom.xml"
        pom.write_text("<project></project>")
        monkeypatch.setattr(
            "rlsbl.pipelines.maven.run",
            lambda cmd, args, **kw: calls.append((cmd, args, kw.get("cwd"))),
        )
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        p.publish(str(tmp_path), "2.0.0", None)
        assert len(calls) == 1
        assert calls[0][0] == "mvn"
        assert calls[0][1] == ["deploy"]
        assert calls[0][2] == str(tmp_path)

    def test_no_build_file_raises(self, tmp_path, monkeypatch):
        _set_all_env_vars(monkeypatch)
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        with pytest.raises(RuntimeError, match="no gradlew or pom.xml"):
            p.publish(str(tmp_path), "1.0.0", None)


# ---------------------------------------------------------------------------
# publish -- skip when local=False
# ---------------------------------------------------------------------------


class TestMavenCentralPublishSkip:
    def test_skips_when_local_false(self, capsys):
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=False, config={}
        )
        p.publish(".", "1.0.0", None)
        assert "local=false" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# publish -- missing env vars
# ---------------------------------------------------------------------------


class TestMavenCentralMissingEnvVars:
    def test_missing_all_vars_exits(self, monkeypatch):
        _clear_all_env_vars(monkeypatch)
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_missing_one_var_exits(self, monkeypatch):
        _set_all_env_vars(monkeypatch)
        # Remove just one
        monkeypatch.delenv("ORG_GRADLE_PROJECT_signingInMemoryKey")
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        with pytest.raises(SystemExit) as exc_info:
            p.publish(".", "1.0.0", None)
        assert exc_info.value.code == 1

    def test_error_message_lists_missing_vars(self, monkeypatch, capsys):
        _clear_all_env_vars(monkeypatch)
        p = MavenCentralPipeline(
            name="mc", pipeline_type="maven-central", local=True, config={}
        )
        with pytest.raises(SystemExit):
            p.publish(".", "1.0.0", None)
        err = capsys.readouterr().err
        for var in _REQUIRED_VARS:
            assert var in err


# ---------------------------------------------------------------------------
# Regression: existing "maven" pipeline unchanged
# ---------------------------------------------------------------------------


class TestMavenPipelineUnchanged:
    def test_maven_type_still_registered(self):
        assert "maven" in PIPELINE_TYPES
        assert PIPELINE_TYPES["maven"] is MavenPipeline

    def test_maven_required_env_vars_unchanged(self):
        p = MavenPipeline(
            name="maven", pipeline_type="maven", local=True, config={}
        )
        assert p.required_env_vars() == ["GITHUB_TOKEN"]

    def test_maven_publish_still_uses_gradlew_publish(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/sh\n")
        gradlew.chmod(0o755)
        monkeypatch.setattr(
            "rlsbl.pipelines.maven.run",
            lambda cmd, args, **kw: calls.append((cmd, args)),
        )
        p = MavenPipeline(
            name="maven", pipeline_type="maven", local=True, config={}
        )
        p.publish(str(tmp_path), "1.0.0", None)
        assert len(calls) == 1
        assert calls[0] == ("./gradlew", ["publish"])

    def test_maven_and_maven_central_are_distinct_classes(self):
        assert MavenPipeline is not MavenCentralPipeline
