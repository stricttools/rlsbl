"""Tests for launcher manifest-as-name-authority handling (Phase 6.4).

Covers:
- Absent manifest -> hard error (naming the path).
- Fill-once: missing non-name fields are added; the name and existing
  values are byte-preserved; a second scaffold is a byte-level no-op.
- The wrapper-producer check trips when a required manifest field is later
  deleted (naming the field).
"""

import json

import pytest

from rlsbl.commands.init_cmd import (
    _ensure_launcher_manifest,
    _fill_npm_launcher_manifest,
    _fill_pypi_launcher_manifest,
)
from rlsbl.pipelines.npm import NpmPipeline
from rlsbl.pipelines.pypi import PypiPipeline


def _npm_pipeline():
    p = NpmPipeline(
        name="npm", pipeline_type="npm", local=False,
        config={"type": "npm", "local": False, "artifact": "launcher",
                "wraps": "go", "binary_source": "github-release", "target": "npm",
                "download": "postinstall"},
    )
    p.target = "npm"
    return p


def _pypi_pipeline():
    p = PypiPipeline(
        name="pypi", pipeline_type="pypi", local=False,
        config={"type": "pypi", "local": False, "artifact": "launcher",
                "wraps": "go", "binary_source": "github-release", "target": "pypi"},
    )
    p.target = "pypi"
    return p


_SHIM_VARS = {"githubRepo": "acme/mytool", "assetProject": "mytool", "binaryName": "mycli"}


# ---------------------------------------------------------------------------
# Absent manifest -> hard error
# ---------------------------------------------------------------------------


class TestAbsentManifest:
    def test_npm_absent_manifest_hard_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _ensure_launcher_manifest(_npm_pipeline(), ".", _SHIM_VARS)
        err = capsys.readouterr().err
        assert "package.json" in err
        assert "never invents" in err

    def test_pypi_absent_manifest_hard_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _ensure_launcher_manifest(_pypi_pipeline(), ".", _SHIM_VARS)
        err = capsys.readouterr().err
        assert "pyproject.toml" in err


# ---------------------------------------------------------------------------
# npm fill-once
# ---------------------------------------------------------------------------


class TestNpmFill:
    def test_fills_missing_fields_preserves_name(self, tmp_path):
        manifest = tmp_path / "package.json"
        manifest.write_text('{"name": "mycli", "version": "1.0.0"}\n')
        dist = _fill_npm_launcher_manifest(str(manifest), _SHIM_VARS, "postinstall")
        assert dist == "mycli"
        pkg = json.loads(manifest.read_text())
        assert pkg["name"] == "mycli"  # never touched
        assert pkg["bin"] == {"mycli": "bin/launcher.cjs"}
        assert pkg["scripts"]["postinstall"] == "node scripts/postinstall.cjs"
        assert "bin" in pkg["files"] and "scripts" in pkg["files"]

    def test_second_call_byte_noop(self, tmp_path):
        manifest = tmp_path / "package.json"
        manifest.write_text('{"name": "mycli", "version": "1.0.0"}\n')
        _fill_npm_launcher_manifest(str(manifest), _SHIM_VARS, "postinstall")
        first = manifest.read_bytes()
        _fill_npm_launcher_manifest(str(manifest), _SHIM_VARS, "postinstall")
        assert manifest.read_bytes() == first

    def test_existing_fields_preserved(self, tmp_path):
        manifest = tmp_path / "package.json"
        manifest.write_text(
            '{"name": "mycli", "version": "1.0.0", '
            '"bin": {"custom": "bin/custom.js"}, '
            '"scripts": {"postinstall": "echo hi", "test": "x"}, '
            '"files": ["lib"]}\n'
        )
        _fill_npm_launcher_manifest(str(manifest), _SHIM_VARS, "postinstall")
        pkg = json.loads(manifest.read_text())
        assert pkg["bin"] == {"custom": "bin/custom.js"}  # untouched
        assert pkg["scripts"]["postinstall"] == "echo hi"  # untouched
        assert pkg["files"] == ["lib"]  # untouched


# ---------------------------------------------------------------------------
# pypi fill-once
# ---------------------------------------------------------------------------


class TestPypiFill:
    def test_fills_scripts_preserves_name(self, tmp_path):
        manifest = tmp_path / "pyproject.toml"
        manifest.write_text(
            '[project]\nname = "my-cli"\nversion = "1.0.0"\n'
        )
        dist = _fill_pypi_launcher_manifest(str(manifest), _SHIM_VARS)
        assert dist == "my-cli"
        text = manifest.read_text()
        assert 'name = "my-cli"' in text  # never touched
        assert "[project.scripts]" in text
        # module derived from normalized dist name.
        assert 'mycli = "my_cli:main"' in text

    def test_second_call_byte_noop(self, tmp_path):
        manifest = tmp_path / "pyproject.toml"
        manifest.write_text('[project]\nname = "my-cli"\nversion = "1.0.0"\n')
        _fill_pypi_launcher_manifest(str(manifest), _SHIM_VARS)
        first = manifest.read_bytes()
        _fill_pypi_launcher_manifest(str(manifest), _SHIM_VARS)
        assert manifest.read_bytes() == first

    def test_existing_script_preserved(self, tmp_path):
        manifest = tmp_path / "pyproject.toml"
        manifest.write_text(
            '[project]\nname = "my-cli"\nversion = "1.0.0"\n\n'
            '[project.scripts]\nmycli = "existing:entry"\n'
        )
        _fill_pypi_launcher_manifest(str(manifest), _SHIM_VARS)
        assert 'mycli = "existing:entry"' in manifest.read_text()


# ---------------------------------------------------------------------------
# wrapper-producer check: field deletion detection
# ---------------------------------------------------------------------------


def _register_project_check(name):
    from unittest.mock import MagicMock
    from strictcli import ErrorReporter, WarnReporter
    from rlsbl.checks.project import register_project_checks

    mock_app = MagicMock()
    checks = {}

    def _make_capture(reporter_cls):
        def capture_check(check_name):
            def decorator(fn):
                def run(ctx):
                    return fn(ctx, reporter_cls())
                checks[check_name] = run
                return fn
            return decorator
        return capture_check

    mock_app.error_check = _make_capture(ErrorReporter)
    mock_app.warn_check = _make_capture(WarnReporter)
    register_project_checks(mock_app)
    return checks[name]


def _ctx(tmp_path):
    from rlsbl.context import ProjectContext

    config = {
        "publish_mode": "ci",
        "targets": [
            {"name": "go", "path": "."},
            {"name": "npm", "path": "packaging/npm"},
        ],
        "pipelines": {
            "go": {"type": "go", "local": False, "target": "go", "artifact": "binary"},
            "npm": {"type": "npm", "local": False, "target": "npm",
                    "artifact": "launcher", "wraps": "go",
                    "binary_source": "github-release", "provenance": True,
                    "download": "postinstall"},
        },
    }
    return ProjectContext(project_root=tmp_path, workspace_root=None, config=config)


class TestFieldDeletionCheck:
    def _write_manifest(self, tmp_path, body):
        d = tmp_path / "packaging" / "npm"
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text(body)

    def test_complete_manifest_passes(self, tmp_path):
        self._write_manifest(
            tmp_path,
            '{"name": "mycli", "bin": {"mycli": "bin/launcher.cjs"}, '
            '"scripts": {"postinstall": "node scripts/postinstall.cjs"}, '
            '"files": ["bin", "scripts"]}\n',
        )
        check = _register_project_check("wrapper-producer")
        result = check(_ctx(tmp_path))
        assert result.kind == "passed", result

    def test_deleted_field_trips_check(self, tmp_path):
        # bin field deleted.
        self._write_manifest(
            tmp_path,
            '{"name": "mycli", '
            '"scripts": {"postinstall": "node scripts/postinstall.cjs"}, '
            '"files": ["bin", "scripts"]}\n',
        )
        check = _register_project_check("wrapper-producer")
        result = check(_ctx(tmp_path))
        assert result.kind == "found"
        errors = " ".join(p.text for p in result.problems)
        assert "bin" in errors

    def test_absent_manifest_not_flagged_by_check(self, tmp_path):
        # No manifest at all -> field guard stays quiet (scaffold-time error
        # handles absence); wraps reference itself is valid, so check passes.
        check = _register_project_check("wrapper-producer")
        result = check(_ctx(tmp_path))
        assert result.kind == "passed", result
