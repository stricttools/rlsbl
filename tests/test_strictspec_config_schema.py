"""Tests for the strictspec config validator (rlsbl/strictspec_gen/config_validator.py).

The config schema (.strictspec/config.schema.toml) models the CORE document
shape of .rlsbl/config.json. It is deliberately NOT wired into config reading
yet: strictspec mandates a format_version gate that the fleet's config.json
files do not carry, and the full config surface has additional sections plus
cross-layer merge semantics that strictspec cannot see (see config_validator
docstring / .strictspec/config.schema.toml header). These tests lock in the
generated validator's shape coverage so the wiring can land safely later,
alongside the fleet-wide format_version stamp.
"""

import json

from rlsbl.strictspec_gen import config_validator as cf


def _v(doc):
    _root, diags = cf.validate_bytes(json.dumps(doc).encode(), "json")
    return [d.code for d in diags]


class TestValidConfigs:
    def test_minimal_ci_config(self):
        assert _v({"format_version": 1, "publish_mode": "ci"}) == []

    def test_full_pipelines_config(self):
        doc = {
            "format_version": 1,
            "publish_mode": "ci",
            "targets": ["npm", "pypi", {"name": "go", "path": "cmd/x"}],
            "pipelines": {
                "npm": {"type": "npm", "local": False, "provenance": True,
                        "target": "npm"},
                "pypi": {"type": "pypi", "local": False, "target": "pypi"},
                "go": {"type": "go", "local": False, "artifact": "binary",
                       "target": "go"},
                "docs": {"type": "cloudflare-pages", "local": True,
                         "target": None},
            },
        }
        assert _v(doc) == []

    def test_launcher_with_download_key(self):
        # The launcher `download` key is current-code truth (not in the paper draft).
        doc = {
            "format_version": 1,
            "publish_mode": "ci",
            "targets": ["pypi"],
            "pipelines": {
                "bin": {"type": "pypi", "local": False, "target": "pypi",
                        "artifact": "binary"},
                "launcher": {"type": "pypi", "local": False, "target": "pypi",
                             "artifact": "launcher", "wraps": "bin",
                             "binary_source": "github-release",
                             "download": "first-run"},
            },
        }
        assert _v(doc) == []


class TestGateAndEnums:
    def test_missing_format_version_gate(self):
        _root, diags = cf.validate_bytes(
            json.dumps({"publish_mode": "ci"}).encode(), "json"
        )
        assert [d.code for d in diags] == ["STRICTSPEC_GATE_ABSENT"]

    def test_missing_publish_mode(self):
        assert "STRICTSPEC_TYPE_MISSING_REQUIRED" in _v({"format_version": 1})

    def test_invalid_publish_mode(self):
        codes = _v({"format_version": 1, "publish_mode": "private"})
        assert "STRICTSPEC_TYPE_NOT_ENUM_MEMBER" in codes

    def test_invalid_pipeline_type(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci", "targets": ["x"],
            "pipelines": {"x": {"type": "bogus", "local": False, "target": "x"}},
        })
        assert "STRICTSPEC_TYPE_NOT_ENUM_MEMBER" in codes

    def test_invalid_download_enum(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci", "targets": ["pypi"],
            "pipelines": {
                "b": {"type": "pypi", "local": False, "target": "pypi",
                      "artifact": "binary"},
                "l": {"type": "pypi", "local": False, "target": "pypi",
                      "artifact": "launcher", "wraps": "b",
                      "binary_source": "github-release", "download": "eager"},
            },
        })
        assert "STRICTSPEC_TYPE_NOT_ENUM_MEMBER" in codes


class TestConditionalRequired:
    def test_npm_requires_provenance(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci", "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False,
                                  "target": "npm"}},
        })
        assert "STRICTSPEC_INTRA_CONDITIONAL_REQUIRED" in codes

    def test_go_requires_artifact(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci", "targets": ["go"],
            "pipelines": {"go": {"type": "go", "local": False, "target": "go"}},
        })
        assert "STRICTSPEC_INTRA_CONDITIONAL_REQUIRED" in codes

    def test_launcher_requires_download(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci", "targets": ["pypi"],
            "pipelines": {
                "b": {"type": "pypi", "local": False, "target": "pypi",
                      "artifact": "binary"},
                "l": {"type": "pypi", "local": False, "target": "pypi",
                      "artifact": "launcher", "wraps": "b",
                      "binary_source": "github-release"},
            },
        })
        assert "STRICTSPEC_INTRA_CONDITIONAL_REQUIRED" in codes


class TestReferencesAndUniqueness:
    def test_dangling_pipeline_target(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci", "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False,
                                  "provenance": True, "target": "pypi"}},
        })
        assert "STRICTSPEC_INTRA_REFERENCE_UNRESOLVED" in codes

    def test_duplicate_external_check_name(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci",
            "external_checks": [
                {"kind": "freeform", "name": "dup", "tag": "preflight",
                 "command": "true"},
                {"kind": "freeform", "name": "dup", "tag": "preflight",
                 "command": "false"},
            ],
        })
        assert "STRICTSPEC_INTRA_UNIQUE_BY" in codes


class TestExternalCheckDiscriminatedUnion:
    def test_structured_requires_tool_and_paths(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci",
            "external_checks": [
                {"kind": "structured", "name": "lint", "tag": "preflight"},
            ],
        })
        assert codes  # missing tool + paths -> diagnostics

    def test_freeform_rejects_unknown_key(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci",
            "external_checks": [
                {"kind": "freeform", "name": "c", "tag": "preflight",
                 "command": "true", "paths": ["x"]},
            ],
        })
        assert "STRICTSPEC_KEY_UNKNOWN" in codes

    def test_invalid_check_name_charset(self):
        codes = _v({
            "format_version": 1, "publish_mode": "ci",
            "external_checks": [
                {"kind": "freeform", "name": "BAD*", "tag": "preflight",
                 "command": "true"},
            ],
        })
        assert "STRICTSPEC_VALUE_STRING_REGEX" in codes


class TestNotWiredBoundary:
    """The config validator exists but is intentionally not wired into config
    reading. This documents the boundary: importing/using it is fine; nothing in
    the release/read path calls it yet.
    """

    def test_validator_is_importable_but_not_referenced_by_config_reader(self):
        import inspect

        from rlsbl import config as config_mod

        src = inspect.getsource(config_mod)
        assert "config_validator" not in src
        assert "strictspec_gen" not in src
