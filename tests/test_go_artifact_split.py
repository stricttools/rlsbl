"""Tests for Go artifact split: library vs binary pipeline template selection.

Phase 6.5: Go pipelines carry required artifact: library|binary.
Library uses module-verification publish template (no goreleaser).
Binary uses goreleaser publish template.
"""

import os
import subprocess

import pytest

from rlsbl.commands.init_cmd import _detect_go_artifact_kind
from rlsbl.config import validate_pipelines_config
from rlsbl.errors import ConfigError
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

    def test_missing_artifact_is_hard_error(self):
        """When artifact key is missing, template selection is a hard error.

        There is no silent default -- the key is mandatory.
        """
        pipeline = GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False},
        )
        with pytest.raises(ConfigError, match="requires 'artifact'"):
            pipeline.template_mappings(ctx=None)

    def test_invalid_artifact_is_hard_error(self):
        """An artifact value other than binary/library is a hard error."""
        pipeline = GoPipeline(
            name="go", pipeline_type="go", local=False,
            config={"type": "go", "local": False, "artifact": "bogus"},
        )
        with pytest.raises(ConfigError, match="binary.*library|artifact"):
            pipeline.template_mappings(ctx=None)


class TestValidatePipelinesGoArtifact:
    """validate_pipelines_config hard-requires artifact on go pipelines."""

    def test_missing_artifact_fails(self):
        with pytest.raises(ConfigError, match="missing required key 'artifact'"):
            validate_pipelines_config(
                {"pipelines": {"go": {"type": "go", "local": False}}}
            )

    def test_error_names_key_and_both_values(self):
        with pytest.raises(ConfigError) as exc:
            validate_pipelines_config(
                {"pipelines": {"go": {"type": "go", "local": False}}}
            )
        msg = str(exc.value).lower()
        assert "artifact" in msg
        assert "binary" in msg and "library" in msg

    def test_error_includes_detection_suggestion(self, monkeypatch):
        """The error message carries an auto-detected suggestion."""
        monkeypatch.setattr(
            "rlsbl.go_introspect.list_main_packages",
            lambda d: [],
        )
        with pytest.raises(ConfigError, match='suggestion.*"library"'):
            validate_pipelines_config(
                {"pipelines": {"go": {"type": "go", "local": False}}}
            )

    def test_invalid_value_fails(self):
        with pytest.raises(ConfigError, match="must be"):
            validate_pipelines_config(
                {"pipelines": {"go": {
                    "type": "go", "local": False, "artifact": "bogus",
                }}}
            )

    def test_binary_passes(self):
        validate_pipelines_config(
            {"pipelines": {"go": {
                "type": "go", "local": False, "artifact": "binary",
            }}}
        )

    def test_library_passes(self):
        validate_pipelines_config(
            {"pipelines": {"go": {
                "type": "go", "local": False, "artifact": "library",
            }}}
        )


class TestLibraryTemplateVersionExtraction:
    """The library template's tag->version shell logic handles all formats."""

    # The exact expansion chain used in publish-library.yml.tpl.
    _EXTRACT = 'TAG="$1"; TAG="${TAG##*@}"; TAG="${TAG##*/}"; echo "${TAG#v}"'

    def _version(self, tag):
        out = subprocess.run(
            ["bash", "-c", self._EXTRACT, "_", tag],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()

    def test_plain_tag(self):
        assert self._version("v0.22.0") == "0.22.0"

    def test_releasable_tag(self):
        assert self._version("go-strictcli@v0.22.0") == "0.22.0"

    def test_subdir_companion_tag(self):
        assert self._version("packages/strictcli/v0.22.0") == "0.22.0"

    def test_nested_subdir_tag(self):
        assert self._version("a/b/c/v1.2.3") == "1.2.3"

    def test_template_uses_this_extraction_chain(self):
        """Guard: the template must contain the exact expansion chain tested."""
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "go", "publish-library.yml.tpl",
        )
        with open(tpl_path) as f:
            content = f.read()
        assert 'TAG="${TAG##*@}"' in content
        assert 'TAG="${TAG##*/}"' in content
        assert 'VERSION="${TAG#v}"' in content
        # module path is baked at scaffold time, not read from go.mod at runtime
        assert 'MODULE="{{modulePath}}"' in content
        assert "head -1 go.mod" not in content
        # tag fallback present
        assert "inputs.tag || github.ref_name" in content


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
