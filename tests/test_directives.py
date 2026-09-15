"""End-to-end tests for selfdoc directive resolve() functions.

These tests exercise the full resolve() path for the custom selfdoc directives,
verifying that the data reading + table rendering pipeline produces correct
markdown output. Nothing is injected: the directives read the committed support
matrix and render it with the table renderer vendored in ``_matrix.py``, which
is the whole point -- a directive script runs under a bare ``python3`` with no
packages installed for it.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_directive(name):
    """Load a directive module from .stricttools/docs/directives/ by name."""
    path = REPO_ROOT / ".stricttools" / "docs" / "directives" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"docs.directives.{name}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _feature_matrix_resolve(attrs, config, body):
    mod = _load_directive("feature_matrix")
    return mod.resolve(attrs, config, body)


def _target_table_resolve(attrs, config, body):
    mod = _load_directive("target_table")
    return mod.resolve(attrs, config, body)


def _pipeline_table_resolve(attrs, config, body):
    mod = _load_directive("pipeline_table")
    return mod.resolve(attrs, config, body)


# ---------------------------------------------------------------------------
# Feature matrix directive tests
# ---------------------------------------------------------------------------

ALL_TARGETS = sorted([
    "dart", "deno", "docker", "flutter",
    "go", "hex", "maven", "npm", "pgdesign", "plain",
    "pypi", "spec", "swift", "swift-apple", "zig",
])


class TestFeatureMatrixResolve:
    def test_returns_markdown(self):
        """resolve({}, None, None) returns a non-empty string with pipes."""
        result = _feature_matrix_resolve({}, None, None)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "|" in result

    def test_contains_check_names(self):
        """Output contains at least one known check name."""
        result = _feature_matrix_resolve({}, None, None)
        known_checks = ["dead-modules", "deps-unused", "library-lint",
                        "circular-deps", "deps-undeclared"]
        found = [name for name in known_checks if name in result]
        assert len(found) >= 1, (
            f"Expected at least one of {known_checks} in output, found none"
        )


# ---------------------------------------------------------------------------
# Target table directive tests
# ---------------------------------------------------------------------------


class TestTargetTableResolve:
    def test_returns_markdown(self):
        """resolve({}, None, None) returns a non-empty string with pipes."""
        result = _target_table_resolve({}, None, None)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "|" in result

    def test_contains_all_targets(self):
        """Output contains all 16 target names."""
        result = _target_table_resolve({}, None, None)
        missing = [t for t in ALL_TARGETS if t not in result]
        assert not missing, (
            f"Missing targets in output: {missing}"
        )

    def test_contains_checkmarks(self):
        """Output contains at least one checkmark character."""
        result = _target_table_resolve({}, None, None)
        assert "✓" in result, (
            "Expected at least one checkmark character in output"
        )


# ---------------------------------------------------------------------------
# Pipeline table directive tests
# ---------------------------------------------------------------------------

EXPECTED_PIPELINE_TYPES = sorted([
    "cloudflare-pages", "deno", "docker", "go",
    "hex", "maven", "maven-central", "npm", "pypi",
])


class TestPipelineTableResolve:
    def test_returns_markdown(self):
        """resolve({}, None, None) returns a non-empty string with pipes."""
        result = _pipeline_table_resolve({}, None, None)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "|" in result

    def test_contains_all_pipeline_types(self):
        """Output contains all 10 pipeline type names."""
        result = _pipeline_table_resolve({}, None, None)
        missing = [t for t in EXPECTED_PIPELINE_TYPES if t not in result]
        assert not missing, (
            f"Missing pipeline types in output: {missing}"
        )

    def test_contains_auth_methods(self):
        """Output contains at least one auth method value."""
        result = _pipeline_table_resolve({}, None, None)
        assert "token" in result or "credential" in result or "none" in result, (
            "Expected at least one auth method in output"
        )
