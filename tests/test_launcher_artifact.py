"""Tests for the launcher artifact kind (Phase 12 Part 2).

The launcher artifact is a wrapper-package publish pipeline that downloads
a binary from a GitHub Release at install/run time. Two ecosystem variants:
- npm: postinstall-download (shim downloads binary during ``npm install``)
- pypi: first-run-download (shim downloads binary on first invocation)

Config keys: ``artifact: "launcher"``, ``wraps: "<pipeline-name>"``,
``binary_source: "github-release"``.

Coverage:
- Config validation (wraps, binary_source required; wraps references valid
  binary; launcher under publish_mode "none" is a hard error)
- Pipeline template_mappings selection (workflow + shim mappings)
- Template rendering (needs chain, verify step incl. checksums.txt probe,
  RELEASE_TAG env pattern)
- wrapper-producer check registration

Shim generation, checksum verification, and manifest-as-name-authority
(fill-once, missing-manifest hard error, field-deletion detection) are
covered by the companion test modules test_launcher_shim_npm.py,
test_launcher_shim_pypi.py, test_launcher_manifest.py, and
test_launcher_end_to_end.py.
"""

import os

import pytest

from rlsbl.config import validate_pipelines_config
from rlsbl.errors import ConfigError
from rlsbl.pipelines import load_pipelines
from rlsbl.pipelines.npm import NpmPipeline
from rlsbl.pipelines.pypi import PypiPipeline


# ---------------------------------------------------------------------------
# Helper: templates root for path assertions
# ---------------------------------------------------------------------------

def _templates_root():
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "rlsbl", "templates",
    )


# ---------------------------------------------------------------------------
# Config validation: launcher-specific fields
# ---------------------------------------------------------------------------


class TestLauncherConfigValidation:
    """validate_pipelines_config enforces launcher-specific fields."""

    def _base_config(self, **overrides):
        """Build a pipelines config with a Go binary + npm launcher."""
        npm_entry = {
            "type": "npm",
            "local": False,
            "target": "npm",
            "provenance": True,
            "artifact": "launcher",
            "wraps": "go",
            "binary_source": "github-release",
            "download": "postinstall",
        }
        npm_entry.update(overrides)
        return {
            "pipelines": {
                "go": {
                    "type": "go",
                    "local": False,
                    "target": "go",
                    "artifact": "binary",
                },
                "npm": npm_entry,
            },
        }

    def test_valid_launcher_config_passes(self):
        """A well-formed launcher config passes validation."""
        validate_pipelines_config(self._base_config())

    def test_launcher_missing_wraps_fails(self):
        """artifact=launcher without wraps is a hard error."""
        config = self._base_config()
        del config["pipelines"]["npm"]["wraps"]
        with pytest.raises(ConfigError, match="wraps"):
            validate_pipelines_config(config)

    def test_launcher_missing_binary_source_fails(self):
        """artifact=launcher without binary_source is a hard error."""
        config = self._base_config()
        del config["pipelines"]["npm"]["binary_source"]
        with pytest.raises(ConfigError, match="binary_source"):
            validate_pipelines_config(config)

    def test_launcher_invalid_binary_source_fails(self):
        """binary_source must be 'github-release'."""
        config = self._base_config(binary_source="local")
        with pytest.raises(ConfigError, match="github-release"):
            validate_pipelines_config(config)

    def test_launcher_wraps_nonexistent_pipeline_fails(self):
        """wraps must name an existing pipeline."""
        config = self._base_config(wraps="nonexistent")
        with pytest.raises(ConfigError, match="nonexistent"):
            validate_pipelines_config(config)

    def test_launcher_wraps_non_binary_fails(self):
        """wraps must reference a pipeline with artifact='binary'."""
        config = self._base_config()
        config["pipelines"]["go"]["artifact"] = "library"
        with pytest.raises(ConfigError, match="binary"):
            validate_pipelines_config(config)

    def test_launcher_wraps_non_go_pipeline_fails(self):
        """wraps must reference a pipeline that has artifact='binary'."""
        config = self._base_config(wraps="other")
        config["pipelines"]["other"] = {
            "type": "npm",
            "local": False,
            "target": "npm",
            "provenance": True,
        }
        with pytest.raises(ConfigError, match="binary"):
            validate_pipelines_config(config)

    def test_launcher_publish_mode_none_fails(self):
        """artifact=launcher under publish_mode 'none' is a hard error."""
        config = self._base_config()
        config["publish_mode"] = "none"
        with pytest.raises(ConfigError, match="publish_mode"):
            validate_pipelines_config(config)

    def test_launcher_publish_mode_ci_passes(self):
        """artifact=launcher under publish_mode 'ci' is fine."""
        config = self._base_config()
        config["publish_mode"] = "ci"
        validate_pipelines_config(config)

    def test_launcher_pypi_valid(self):
        """pypi launcher passes validation with all required keys."""
        config = {
            "pipelines": {
                "go": {
                    "type": "go",
                    "local": False,
                    "target": "go",
                    "artifact": "binary",
                },
                "pypi": {
                    "type": "pypi",
                    "local": False,
                    "target": "pypi",
                    "artifact": "launcher",
                    "wraps": "go",
                    "binary_source": "github-release",
                    "download": "first-run",
                },
            },
        }
        validate_pipelines_config(config)

    def test_launcher_invalid_artifact_value(self):
        """artifact value other than launcher/binary/library is an error
        only for go pipelines, but 'launcher' is valid for npm/pypi."""
        # This test verifies 'launcher' is accepted for npm
        config = self._base_config()
        validate_pipelines_config(config)  # should not raise


# ---------------------------------------------------------------------------
# Pipeline template_mappings: launcher template selection
# ---------------------------------------------------------------------------


class TestLauncherTemplateMappings:
    """Pipeline template_mappings selects launcher-specific templates."""

    def test_npm_launcher_selects_launcher_template(self):
        """npm pipeline with artifact=launcher selects publish-launcher.yml.tpl."""
        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=False,
            config={
                "type": "npm", "local": False,
                "artifact": "launcher",
                "wraps": "go",
                "binary_source": "github-release",
                "download": "postinstall",
            },
        )
        pipeline.target = "npm"
        mappings = pipeline.template_mappings(ctx=None)
        templates = {m["template"] for m in mappings}
        # Workflow + both shim files.
        assert "publish-launcher.yml.tpl" in templates
        assert "shim-postinstall.cjs.tpl" in templates
        assert "shim-bin.cjs.tpl" in templates
        wf = [m for m in mappings if m["template"] == "publish-launcher.yml.tpl"]
        assert wf[0]["target"] == ".github/workflows/publish.yml"

    def test_pypi_launcher_selects_launcher_template(self):
        """pypi pipeline with artifact=launcher selects publish-launcher.yml.tpl."""
        pipeline = PypiPipeline(
            name="pypi", pipeline_type="pypi", local=False,
            config={
                "type": "pypi", "local": False,
                "artifact": "launcher",
                "wraps": "go",
                "binary_source": "github-release",
                "download": "first-run",
            },
        )
        pipeline.target = "pypi"
        mappings = pipeline.template_mappings(ctx=None)
        templates = {m["template"] for m in mappings}
        # Workflow + first-run launcher module.
        assert "publish-launcher.yml.tpl" in templates
        assert "shim-launcher.py.tpl" in templates
        wf = [m for m in mappings if m["template"] == "publish-launcher.yml.tpl"]
        assert wf[0]["target"] == ".github/workflows/publish.yml"

    def test_npm_non_launcher_uses_regular_template(self):
        """npm pipeline without artifact=launcher uses the regular template."""
        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=False,
            config={"type": "npm", "local": False},
        )
        pipeline.target = "npm"
        mappings = pipeline.template_mappings(ctx=None)
        assert mappings[0]["template"] == "publish.yml.tpl"

    def test_pypi_non_launcher_uses_regular_template(self):
        """pypi pipeline without artifact=launcher uses the regular template."""
        pipeline = PypiPipeline(
            name="pypi", pipeline_type="pypi", local=False,
            config={"type": "pypi", "local": False},
        )
        pipeline.target = "pypi"
        mappings = pipeline.template_mappings(ctx=None)
        assert mappings[0]["template"] == "publish.yml.tpl"


# ---------------------------------------------------------------------------
# Template file existence and content
# ---------------------------------------------------------------------------


class TestLauncherTemplateFiles:
    """Launcher template files exist and have required content."""

    def test_npm_launcher_template_exists(self):
        tpl = os.path.join(_templates_root(), "npm", "publish-launcher.yml.tpl")
        assert os.path.isfile(tpl), f"Missing: {tpl}"

    def test_pypi_launcher_template_exists(self):
        tpl = os.path.join(_templates_root(), "pypi", "publish-launcher.yml.tpl")
        assert os.path.isfile(tpl), f"Missing: {tpl}"

    def test_npm_template_has_release_tag_env(self):
        """npm launcher template uses the RELEASE_TAG env pattern."""
        tpl = os.path.join(_templates_root(), "npm", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "RELEASE_TAG: ${{ inputs.tag || github.event.release.tag_name }}" in content

    def test_pypi_template_has_release_tag_env(self):
        """pypi launcher template uses the RELEASE_TAG env pattern."""
        tpl = os.path.join(_templates_root(), "pypi", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "RELEASE_TAG: ${{ inputs.tag || github.event.release.tag_name }}" in content

    def test_npm_template_has_verify_step(self):
        """npm launcher has a verify-before-publish step (curl check)."""
        tpl = os.path.join(_templates_root(), "npm", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "curl" in content or "wget" in content
        assert "404" in content or "verify" in content.lower()

    def test_pypi_template_has_verify_step(self):
        """pypi launcher has a verify-before-publish step (curl check)."""
        tpl = os.path.join(_templates_root(), "pypi", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "curl" in content or "wget" in content
        assert "404" in content or "verify" in content.lower()

    def test_npm_template_has_checksums_probe(self):
        """npm launcher verify step probes checksums.txt existence."""
        tpl = os.path.join(_templates_root(), "npm", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "checksums.txt" in content

    def test_pypi_template_has_checksums_probe(self):
        """pypi launcher verify step probes checksums.txt existence."""
        tpl = os.path.join(_templates_root(), "pypi", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "checksums.txt" in content

    def test_npm_template_needs_chain(self):
        """npm launcher template has needs referencing gate and producer."""
        tpl = os.path.join(_templates_root(), "npm", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        # The template uses a placeholder for the producer job key.
        # At minimum, needs must list both gate and the producer.
        assert "needs:" in content
        assert "gate" in content

    def test_pypi_template_needs_chain(self):
        """pypi launcher template has needs referencing gate and producer."""
        tpl = os.path.join(_templates_root(), "pypi", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "needs:" in content
        assert "gate" in content

    def test_npm_template_no_goreleaser_action(self):
        """npm launcher template does not use goreleaser-action."""
        tpl = os.path.join(_templates_root(), "npm", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "goreleaser-action" not in content
        assert "goreleaser/goreleaser" not in content

    def test_pypi_template_no_goreleaser_action(self):
        """pypi launcher template does not use goreleaser-action."""
        tpl = os.path.join(_templates_root(), "pypi", "publish-launcher.yml.tpl")
        with open(tpl) as f:
            content = f.read()
        assert "goreleaser-action" not in content
        assert "goreleaser/goreleaser" not in content


# ---------------------------------------------------------------------------
# Merged publish: needs chain threading
# ---------------------------------------------------------------------------


class TestLauncherNeedsChain:
    """_generate_merged_publish threads wraps -> needs correctly."""

    def _make_pipelines(self):
        """Build pipelines dict with go binary + npm launcher."""
        go = __import__("rlsbl.pipelines.go", fromlist=["GoPipeline"]).GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False, "artifact": "binary"},
        )
        go.target = "go"
        npm = NpmPipeline(
            name="npm", pipeline_type="npm", local=False,
            config={
                "type": "npm", "local": False,
                "artifact": "launcher",
                "wraps": "go",
                "binary_source": "github-release",
                "provenance": True,
                "download": "postinstall",
            },
        )
        npm.target = "npm"
        return {"go": go, "npm": npm}

    def test_merged_publish_npm_depends_on_go(self, tmp_path, monkeypatch):
        """In the merged publish, the npm launcher job depends on the go job."""
        from rlsbl.commands.init_cmd import _generate_merged_publish

        # A launcher target's publish job bakes in the producer's asset
        # project + tag prefix, so the producer must be resolvable on disk.
        (tmp_path / "go.mod").write_text(
            "module github.com/test/test-pkg\n\ngo 1.23\n"
        )
        (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
        monkeypatch.chdir(tmp_path)

        pipelines = self._make_pipelines()
        targets = ["go", "npm"]
        vars_dict = {
            "name": "test-pkg",
            "registryUrl": "https://registry.npmjs.org",
            "publishGate": "",
            "npm.provenance": "",
            "modulePath": "github.com/test/test-pkg",
        }
        content = _generate_merged_publish(
            targets, vars_dict, {"go": ".", "npm": "packaging/npm"},
            pipelines=pipelines,
        )
        # The rendered YAML should have npm job with needs including "go"
        assert content is not None
        assert "needs:" in content
        # The npm job should depend on the go job key (which is "go" after
        # the target-name rename in the merged generator)
        from ruamel.yaml import YAML
        yml = YAML(typ="safe")
        from io import StringIO
        data = yml.load(StringIO(content))
        npm_job = data.get("jobs", {}).get("npm")
        assert npm_job is not None, f"No 'npm' job found. Jobs: {list(data.get('jobs', {}).keys())}"
        needs = npm_job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "go" in needs, f"npm job needs={needs}, expected 'go' in the list"


# ---------------------------------------------------------------------------
# wrapper-producer check registration
# ---------------------------------------------------------------------------


class TestWrapperProducerCheck:
    """The wrapper-producer check validates wraps references."""

    def test_check_registered(self):
        """wrapper-producer is in the CHECK_TARGETS registry."""
        from rlsbl.checks import CHECK_TARGETS
        assert "wrapper-producer" in CHECK_TARGETS


# ---------------------------------------------------------------------------
# Resolve publish template: launcher path
# ---------------------------------------------------------------------------


class TestResolvePublishTemplateLauncher:
    """_resolve_publish_template selects launcher templates."""

    def test_npm_launcher_template_resolution(self):
        from rlsbl.commands.init_cmd import _resolve_publish_template
        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=False,
            config={
                "type": "npm", "local": False,
                "artifact": "launcher",
                "wraps": "go",
                "binary_source": "github-release",
                "download": "postinstall",
            },
        )
        pipeline.target = "npm"
        result = _resolve_publish_template(
            "npm", {"npm": pipeline}, _templates_root(),
        )
        assert result is not None
        assert "publish-launcher" in result

    def test_pypi_launcher_template_resolution(self):
        from rlsbl.commands.init_cmd import _resolve_publish_template
        pipeline = PypiPipeline(
            name="pypi", pipeline_type="pypi", local=False,
            config={
                "type": "pypi", "local": False,
                "artifact": "launcher",
                "wraps": "go",
                "binary_source": "github-release",
                "download": "first-run",
            },
        )
        pipeline.target = "pypi"
        result = _resolve_publish_template(
            "pypi", {"pypi": pipeline}, _templates_root(),
        )
        assert result is not None
        assert "publish-launcher" in result
