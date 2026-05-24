"""Tests for DocsTarget: protocol conformance, detect(), and version read/write."""

import json
import os
import tempfile

from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets.docs import DocsTarget
from rlsbl.targets import TARGETS


class TestDocsTargetProtocol:
    """Verify DocsTarget satisfies the ReleaseTarget protocol."""

    def test_is_release_target(self):
        target = DocsTarget()
        assert isinstance(target, ReleaseTarget)

    def test_name(self):
        target = DocsTarget()
        assert target.name == "docs"

    def test_version_file_returns_selfdoc_json(self):
        target = DocsTarget()
        assert target.version_file() == "selfdoc.json"

    def test_tag_format_none(self):
        target = DocsTarget()
        assert target.tag_format("1.0.0") is None

    def test_registered_in_targets(self):
        assert "docs" in TARGETS
        assert isinstance(TARGETS["docs"], DocsTarget)


class TestDocsTargetDetect:
    """Verify detect() looks for selfdoc.json."""

    def test_detect_true_with_selfdoc_json(self):
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "selfdoc.json"), "w") as f:
                f.write("{}")
            assert target.detect(d) is True

    def test_detect_false_empty_dir(self):
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_detect_false_with_old_docs_toml(self):
        """Old .rlsbl/docs.toml should NOT trigger detection anymore."""
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            rlsbl_dir = os.path.join(d, ".rlsbl")
            os.makedirs(rlsbl_dir)
            with open(os.path.join(rlsbl_dir, "docs.toml"), "w") as f:
                f.write("[source]\n")
            assert target.detect(d) is False


class TestDocsTargetReadVersion:
    """Verify read_version reads from selfdoc.json."""

    def test_reads_version_from_selfdoc_json(self):
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "selfdoc.json"), "w") as f:
                json.dump({"version": "1.2.3"}, f)
            assert target.read_version(d) == "1.2.3"

    def test_fallback_when_version_absent(self):
        """Returns '0.0.0' when selfdoc.json has no version field."""
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "selfdoc.json"), "w") as f:
                json.dump({"language": "python"}, f)
            assert target.read_version(d) == "0.0.0"

    def test_fallback_when_no_selfdoc_json(self):
        """Returns '0.0.0' when selfdoc.json does not exist."""
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.read_version(d) == "0.0.0"

    def test_fallback_on_invalid_json(self):
        """Returns '0.0.0' when selfdoc.json is not valid JSON."""
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "selfdoc.json"), "w") as f:
                f.write("{bad json")
            assert target.read_version(d) == "0.0.0"


class TestDocsTargetWriteVersion:
    """Verify write_version updates selfdoc.json atomically."""

    def test_writes_version_to_selfdoc_json(self):
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            config_path = os.path.join(d, "selfdoc.json")
            with open(config_path, "w") as f:
                json.dump({"language": "python", "version": "0.1.0"}, f, indent=2)
            result = target.write_version(d, "0.2.0")
            assert result == ["selfdoc.json"]
            with open(config_path) as f:
                data = json.load(f)
            assert data["version"] == "0.2.0"
            assert data["language"] == "python"

    def test_adds_version_field_when_absent(self):
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            config_path = os.path.join(d, "selfdoc.json")
            with open(config_path, "w") as f:
                json.dump({"language": "go"}, f, indent=2)
            target.write_version(d, "1.0.0")
            with open(config_path) as f:
                data = json.load(f)
            assert data["version"] == "1.0.0"
            assert data["language"] == "go"

    def test_preserves_other_fields(self):
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            config_path = os.path.join(d, "selfdoc.json")
            original = {
                "language": "python",
                "source": ["src/"],
                "versions": [{"version": "1.0", "indexed": True}],
                "version": "0.5.0",
            }
            with open(config_path, "w") as f:
                json.dump(original, f, indent=2)
            target.write_version(d, "0.6.0")
            with open(config_path) as f:
                data = json.load(f)
            assert data["version"] == "0.6.0"
            assert data["source"] == ["src/"]
            assert data["versions"] == [{"version": "1.0", "indexed": True}]

    def test_preserves_indentation(self):
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            config_path = os.path.join(d, "selfdoc.json")
            # Write with 4-space indent
            with open(config_path, "w") as f:
                json.dump({"language": "python", "version": "0.1.0"}, f, indent=4)
                f.write("\n")
            target.write_version(d, "0.2.0")
            with open(config_path) as f:
                content = f.read()
            # Should preserve 4-space indent
            assert '    "language"' in content
