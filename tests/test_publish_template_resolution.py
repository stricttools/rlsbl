"""Tests for pipeline-driven publish template resolution.

Phase 6.7: _generate_merged_publish uses pipeline template_mappings
when pipelines are provided, killing the target-name-based template bypass.
"""

import os

import pytest

from rlsbl.commands.init_cmd import _resolve_publish_template
from rlsbl.pipelines.go import GoPipeline
from rlsbl.pipelines.npm import NpmPipeline


class TestResolvePublishTemplate:
    """_resolve_publish_template uses pipeline-driven resolution."""

    def _templates_root(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates",
        )

    def test_fallback_when_no_pipelines(self):
        """Without pipelines, falls back to hardcoded path."""
        result = _resolve_publish_template(
            "go", None, self._templates_root(),
        )
        assert result is not None
        assert result.endswith("publish.yml.tpl")

    def test_go_binary_uses_goreleaser_template(self):
        """Go binary pipeline resolves to publish.yml.tpl."""
        pipeline = GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False, "artifact": "binary"},
        )
        pipeline.target = "go"
        pipelines = {"go": pipeline}

        result = _resolve_publish_template(
            "go", pipelines, self._templates_root(),
        )
        assert result is not None
        assert result.endswith("publish.yml.tpl")
        assert "publish-library" not in result

    def test_go_library_uses_library_template(self):
        """Go library pipeline resolves to publish-library.yml.tpl."""
        pipeline = GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False, "artifact": "library"},
        )
        pipeline.target = "go"
        pipelines = {"go": pipeline}

        result = _resolve_publish_template(
            "go", pipelines, self._templates_root(),
        )
        assert result is not None
        assert result.endswith("publish-library.yml.tpl")

    def test_npm_pipeline_uses_pipeline_template(self):
        """npm pipeline resolves via template_mappings."""
        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=False,
            config={"type": "npm", "local": False},
        )
        pipeline.target = "npm"
        pipelines = {"npm": pipeline}

        result = _resolve_publish_template(
            "npm", pipelines, self._templates_root(),
        )
        assert result is not None
        assert "publish" in os.path.basename(result)

    def test_unlinked_pipeline_falls_back(self):
        """Pipeline not linked to this target falls back to hardcoded path."""
        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=False,
            config={"type": "npm", "local": False},
        )
        pipeline.target = "npm"  # linked to npm, not go
        pipelines = {"npm": pipeline}

        result = _resolve_publish_template(
            "go", pipelines, self._templates_root(),
        )
        # Falls back to hardcoded go/publish.yml.tpl
        assert result is not None
        assert os.path.join("go", "publish.yml.tpl") in result

    def test_no_template_returns_none(self):
        """Returns None when no template file exists."""
        result = _resolve_publish_template(
            "nonexistent", None, self._templates_root(),
        )
        assert result is None


class TestMergedPublishIdempotency:
    """_generate_merged_publish produces stable output across re-scaffolds."""

    def test_same_output_with_and_without_pipelines_for_simple_case(self):
        """For a target with a single publish template and no config-driven
        branching, the output should be the same whether pipelines are
        loaded or not (idempotency test for the fallback path)."""
        from rlsbl.commands.init_cmd import _generate_merged_publish

        targets = ["npm"]
        vars_dict = {
            "name": "test-pkg",
            "registryUrl": "https://registry.npmjs.org",
            "publishGate": "",
            "npm.provenance": "",
        }

        # Without pipelines (fallback)
        result_without = _generate_merged_publish(
            targets, vars_dict, target_paths={"npm": "."},
        )

        # With pipelines (pipeline-driven)
        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=False,
            config={"type": "npm", "local": False},
        )
        pipeline.target = "npm"
        result_with = _generate_merged_publish(
            targets, vars_dict, target_paths={"npm": "."},
            pipelines={"npm": pipeline},
        )

        assert result_without == result_with
