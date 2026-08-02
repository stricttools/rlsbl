"""Tests for the launcher ``download`` mode key (Phase 8 prep).

The launcher artifact gains a REQUIRED ``download`` config key selecting how
the wrapped binary is fetched:

- ``"postinstall"`` (npm only): the wrapper ships a ``postinstall`` script
  that downloads the binary at ``npm install`` time.
- ``"first-run"``: the wrapper performs ZERO network I/O at install time and
  lazily downloads + checksum-verifies the binary on the FIRST CLI
  invocation, caching it under a platform cache dir. Subsequent invocations
  exec from cache.

Coverage:
- Config validation: missing ``download`` = hard error (actionable);
  invalid value = hard error listing valid values; pypi + postinstall is a
  hard error (pip has no postinstall hook).
- npm template_mappings: postinstall mode emits the postinstall+bin pair;
  first-run mode emits the single first-run bin stub and NO postinstall
  script template.
- Emitted package.json shape: bin entry present in both modes; the
  postinstall script is present ONLY in postinstall mode. Installing a
  first-run package performs zero network I/O (no postinstall).
- First-run template content: platform mapping, checksum verification,
  platform cache dir, zero runtime deps, exec.
"""

import json
import os
import re

import pytest

from rlsbl.config import validate_pipelines_config
from rlsbl.errors import ConfigError
from rlsbl.pipelines.npm import NpmPipeline
from rlsbl.pipelines.pypi import PypiPipeline


def _templates_root():
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates",
    )


# ---------------------------------------------------------------------------
# Config validation: the required download key
# ---------------------------------------------------------------------------


class TestDownloadConfigValidation:
    def _config(self, ptype="npm", download="postinstall", target="npm",
                omit_download=False):
        entry = {
            "type": ptype,
            "local": False,
            "target": target,
            "artifact": "launcher",
            "wraps": "go",
            "binary_source": "github-release",
        }
        if ptype == "npm":
            entry["provenance"] = True
        if not omit_download:
            entry["download"] = download
        return {
            "pipelines": {
                "go": {"type": "go", "local": False, "target": "go",
                       "artifact": "binary"},
                ptype: entry,
            },
        }

    def test_missing_download_is_hard_error(self):
        config = self._config(omit_download=True)
        with pytest.raises(ConfigError, match="download"):
            validate_pipelines_config(config)

    def test_missing_download_message_actionable(self):
        config = self._config(omit_download=True)
        with pytest.raises(ConfigError) as exc:
            validate_pipelines_config(config)
        msg = str(exc.value)
        assert "first-run" in msg and "postinstall" in msg

    def test_invalid_download_value_is_hard_error(self):
        config = self._config(download="lazy")
        with pytest.raises(ConfigError) as exc:
            validate_pipelines_config(config)
        msg = str(exc.value)
        # Lists the valid values.
        assert "first-run" in msg and "postinstall" in msg

    def test_npm_postinstall_valid(self):
        validate_pipelines_config(self._config(download="postinstall"))

    def test_npm_first_run_valid(self):
        validate_pipelines_config(self._config(download="first-run"))

    def test_pypi_first_run_valid(self):
        validate_pipelines_config(
            self._config(ptype="pypi", target="pypi", download="first-run")
        )

    def test_pypi_postinstall_is_hard_error(self):
        """pip has no postinstall hook -- postinstall is npm-only."""
        config = self._config(ptype="pypi", target="pypi",
                              download="postinstall")
        with pytest.raises(ConfigError, match="postinstall"):
            validate_pipelines_config(config)


# ---------------------------------------------------------------------------
# npm template_mappings: download-mode template selection
# ---------------------------------------------------------------------------


def _npm_launcher(download):
    p = NpmPipeline(
        name="npm", pipeline_type="npm", local=False,
        config={"type": "npm", "local": False, "artifact": "launcher",
                "wraps": "go", "binary_source": "github-release",
                "download": download},
    )
    p.target = "npm"
    return p


class TestNpmDownloadModeMappings:
    def test_postinstall_emits_postinstall_pair(self):
        mappings = _npm_launcher("postinstall").template_mappings(ctx=None)
        templates = {m["template"] for m in mappings}
        assert "shim-postinstall.cjs.tpl" in templates
        assert "shim-bin.cjs.tpl" in templates
        assert "shim-firstrun.cjs.tpl" not in templates

    def test_first_run_emits_single_bin_stub(self):
        mappings = _npm_launcher("first-run").template_mappings(ctx=None)
        templates = {m["template"] for m in mappings}
        assert "shim-firstrun.cjs.tpl" in templates
        # No postinstall script template in first-run mode.
        assert "shim-postinstall.cjs.tpl" not in templates
        # The first-run stub is the bin entry.
        firstrun = [m for m in mappings
                    if m["template"] == "shim-firstrun.cjs.tpl"]
        assert firstrun[0]["target"] == "bin/launcher.cjs"

    def test_both_modes_emit_publish_workflow(self):
        for mode in ("postinstall", "first-run"):
            mappings = _npm_launcher(mode).template_mappings(ctx=None)
            templates = {m["template"] for m in mappings}
            assert "publish-launcher.yml.tpl" in templates


# ---------------------------------------------------------------------------
# First-run npm template content
# ---------------------------------------------------------------------------


class TestFirstRunTemplateContent:
    def _render(self, vars_dict):
        from rlsbl.commands.init_cmd import process_template
        path = os.path.join(_templates_root(), "npm", "shim-firstrun.cjs.tpl")
        with open(path) as f:
            raw = f.read()
        content, _ = process_template(raw, vars_dict, template_path=path)
        return content

    def test_template_exists(self):
        tpl = os.path.join(_templates_root(), "npm", "shim-firstrun.cjs.tpl")
        assert os.path.isfile(tpl)

    def test_maps_platforms(self):
        content = self._render({})
        for token in ("linux", "darwin", "windows", "amd64", "arm64"):
            assert token in content

    def test_verifies_checksums(self):
        content = self._render({})
        assert "checksums.txt" in content
        assert "createHash" in content and "sha256" in content
        assert "verifyChecksum" in content

    def test_platform_cache_dir(self):
        content = self._render({})
        # Mirrors the pypi launcher cache-dir logic.
        assert "XDG_CACHE_HOME" in content
        assert "Library" in content and "Caches" in content
        assert "LOCALAPPDATA" in content

    def test_zero_runtime_deps(self):
        content = self._render({})
        requires = re.findall(r'require\("([^"]+)"\)', content)
        allowed = {"fs", "os", "path", "https", "crypto", "child_process"}
        for mod in requires:
            if mod.startswith(".") or mod.startswith("/"):
                continue
            assert mod in allowed, f"unexpected runtime dep: {mod}"

    def test_execs_binary(self):
        content = self._render({})
        assert "spawnSync" in content
        assert "process.argv.slice(2)" in content

    def test_no_placeholders_after_render(self):
        content = self._render({
            "githubRepo": "acme/mytool",
            "assetProject": "mytool",
            "binaryName": "mycli",
            "tagPrefix": "v",
        })
        assert "{{" not in content
        assert "acme/mytool" in content


# ---------------------------------------------------------------------------
# Emitted package.json shape per mode (manifest fill)
# ---------------------------------------------------------------------------


_SHIM_VARS = {"githubRepo": "acme/mytool", "assetProject": "mytool",
              "binaryName": "mycli"}


class TestManifestFillPerMode:
    def _manifest(self, tmp_path):
        m = tmp_path / "package.json"
        m.write_text('{"name": "mycli", "version": "1.0.0"}\n')
        return m

    def test_postinstall_mode_has_postinstall_script(self, tmp_path):
        from rlsbl.commands.init_cmd import _fill_npm_launcher_manifest
        m = self._manifest(tmp_path)
        _fill_npm_launcher_manifest(str(m), _SHIM_VARS, "postinstall")
        pkg = json.loads(m.read_text())
        # bin entry present in both modes.
        assert pkg["bin"] == {"mycli": "bin/launcher.cjs"}
        # postinstall script present ONLY in postinstall mode.
        assert pkg["scripts"]["postinstall"] == "node scripts/postinstall.cjs"
        assert "scripts" in pkg["files"]

    def test_first_run_mode_has_no_postinstall_script(self, tmp_path):
        from rlsbl.commands.init_cmd import _fill_npm_launcher_manifest
        m = self._manifest(tmp_path)
        _fill_npm_launcher_manifest(str(m), _SHIM_VARS, "first-run")
        pkg = json.loads(m.read_text())
        # bin entry present.
        assert pkg["bin"] == {"mycli": "bin/launcher.cjs"}
        # No postinstall script: install performs zero network I/O.
        assert "scripts" not in pkg or "postinstall" not in pkg.get("scripts", {})
        # Ships only the bin dir (binary cached outside the package).
        assert pkg["files"] == ["bin"]

    def test_first_run_second_call_byte_noop(self, tmp_path):
        from rlsbl.commands.init_cmd import _fill_npm_launcher_manifest
        m = self._manifest(tmp_path)
        _fill_npm_launcher_manifest(str(m), _SHIM_VARS, "first-run")
        first = m.read_bytes()
        _fill_npm_launcher_manifest(str(m), _SHIM_VARS, "first-run")
        assert m.read_bytes() == first


# ---------------------------------------------------------------------------
# wrapper-producer check: first-run manifest needs no postinstall
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


def _ctx_with_download(tmp_path, download):
    from rlsbl.context import ProjectContext

    config = {
        "publish_mode": "ci",
        "targets": [
            {"name": "go", "path": "."},
            {"name": "npm", "path": "packaging/npm"},
        ],
        "pipelines": {
            "go": {"type": "go", "local": False, "target": "go",
                   "artifact": "binary"},
            "npm": {"type": "npm", "local": False, "target": "npm",
                    "artifact": "launcher", "wraps": "go",
                    "binary_source": "github-release", "provenance": True,
                    "download": download},
        },
    }
    return ProjectContext(project_root=tmp_path, workspace_root=None,
                          config=config)


class TestFirstRunCheckMode:
    def _write_manifest(self, tmp_path, body):
        d = tmp_path / "packaging" / "npm"
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text(body)

    def test_first_run_manifest_without_postinstall_passes(self, tmp_path):
        # first-run manifest has bin + files but NO postinstall script.
        self._write_manifest(
            tmp_path,
            '{"name": "mycli", "bin": {"mycli": "bin/launcher.cjs"}, '
            '"files": ["bin"]}\n',
        )
        check = _register_project_check("wrapper-producer")
        result = check(_ctx_with_download(tmp_path, "first-run"))
        assert result.kind == "passed", result

    def test_first_run_missing_bin_trips_check(self, tmp_path):
        self._write_manifest(tmp_path, '{"name": "mycli", "files": ["bin"]}\n')
        check = _register_project_check("wrapper-producer")
        result = check(_ctx_with_download(tmp_path, "first-run"))
        assert result.kind == "found"
        errors = " ".join(p.text for p in result.problems)
        assert "bin" in errors

    def test_postinstall_manifest_still_needs_postinstall(self, tmp_path):
        # postinstall mode without the postinstall script trips the check.
        self._write_manifest(
            tmp_path,
            '{"name": "mycli", "bin": {"mycli": "bin/launcher.cjs"}, '
            '"files": ["bin", "scripts"]}\n',
        )
        check = _register_project_check("wrapper-producer")
        result = check(_ctx_with_download(tmp_path, "postinstall"))
        assert result.kind == "found"
        errors = " ".join(p.text for p in result.problems)
        assert "postinstall" in errors
