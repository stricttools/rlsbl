"""Tests for Go artifact split: library vs binary pipeline template selection.

Phase 6.5: Go pipelines carry required artifact: library|binary.
Library uses module-verification publish template (no goreleaser).
Binary uses goreleaser publish template.
"""

import os

import pytest

from rlsbl.commands.init_cmd import _detect_go_artifact_kind
from rlsbl.pipelines.go import GoPipeline


class TestDetectGoArtifactKind:
    """_detect_go_artifact_kind auto-detects library vs binary."""

    def test_binary_when_main_exists(self, tmp_path, monkeypatch):
        """A Go project with a main package is detected as binary."""
        from rlsbl.go_introspect import GoPackage

        monkeypatch.setattr(
            "rlsbl.go_introspect.list_main_packages",
            lambda d: [GoPackage(name="main", import_path="example.com/cmd/x", rel_dir="./cmd/x")],
        )
        assert _detect_go_artifact_kind(str(tmp_path)) == "binary"

    def test_library_when_no_main(self, tmp_path, monkeypatch):
        """A Go project with no main package is detected as library."""
        monkeypatch.setattr(
            "rlsbl.go_introspect.list_main_packages",
            lambda d: [],
        )
        assert _detect_go_artifact_kind(str(tmp_path)) == "library"

    def test_fallback_to_binary_on_error(self, tmp_path, monkeypatch):
        """Falls back to binary when introspection fails."""
        def raise_err(d):
            raise RuntimeError("no go")
        monkeypatch.setattr(
            "rlsbl.go_introspect.list_main_packages",
            raise_err,
        )
        assert _detect_go_artifact_kind(str(tmp_path)) == "binary"


class TestGoPipelineTemplateMappings:
    """GoPipeline selects template based on artifact config key."""

    def test_binary_selects_goreleaser_template(self):
        pipeline = GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False, "artifact": "binary"},
        )
        mappings = pipeline.template_mappings(ctx=None)
        assert len(mappings) == 1
        assert mappings[0]["template"] == "publish.yml.tpl"

    def test_library_selects_library_template(self):
        pipeline = GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False, "artifact": "library"},
        )
        mappings = pipeline.template_mappings(ctx=None)
        assert len(mappings) == 1
        assert mappings[0]["template"] == "publish-library.yml.tpl"

    def test_default_to_binary_when_absent(self):
        """When artifact key is missing, defaults to binary (goreleaser)."""
        pipeline = GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False},
        )
        mappings = pipeline.template_mappings(ctx=None)
        assert mappings[0]["template"] == "publish.yml.tpl"


class TestGoLibraryPublishTemplate:
    """The library publish template exists and has no goreleaser job."""

    def test_template_exists(self):
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "go", "publish-library.yml.tpl",
        )
        assert os.path.isfile(tpl_path)

    def test_template_has_no_goreleaser(self):
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "go", "publish-library.yml.tpl",
        )
        with open(tpl_path) as f:
            content = f.read()
        assert "goreleaser" not in content
        assert "verify-module" in content

    def test_binary_template_has_goreleaser(self):
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "go", "publish.yml.tpl",
        )
        with open(tpl_path) as f:
            content = f.read()
        assert "goreleaser" in content
