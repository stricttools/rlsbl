"""End-to-end tests for selfdoc directive resolve() functions.

These tests exercise the full resolve() path for both custom selfdoc
directives (feature_matrix and target_table), verifying that the data
generation + table rendering pipeline produces correct markdown output.

Since selfdoc is not a runtime dependency of rlsbl, the tests inject a
minimal render_markdown_table mock into sys.modules before importing the
directive modules.
"""

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture: inject a minimal selfdoc.tables mock so directive imports succeed
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


def _render_markdown_table(headers, rows, *, align=None, pretty=False):
    """Minimal markdown table renderer matching selfdoc.tables API."""
    escaped = [str(h).replace("|", "\\|") for h in headers]
    lines = ["| " + " | ".join(escaped) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        cells = [str(c).replace("|", "\\|") for c in row]
        # Pad short rows
        while len(cells) < len(headers):
            cells.append("")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _load_directive(name):
    """Load a directive module from docs/directives/ by name."""
    path = REPO_ROOT / "docs" / "directives" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"docs.directives.{name}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _inject_selfdoc_tables():
    """Inject a mock selfdoc.tables module so directive imports work."""
    had_selfdoc = "selfdoc" in sys.modules
    had_tables = "selfdoc.tables" in sys.modules

    selfdoc_pkg = types.ModuleType("selfdoc")
    selfdoc_pkg.__path__ = []
    tables_mod = types.ModuleType("selfdoc.tables")
    tables_mod.render_markdown_table = _render_markdown_table

    sys.modules["selfdoc"] = selfdoc_pkg
    sys.modules["selfdoc.tables"] = tables_mod

    yield

    # Restore original state
    if had_selfdoc:
        pass  # leave whatever was there
    else:
        sys.modules.pop("selfdoc", None)
    if had_tables:
        pass
    else:
        sys.modules.pop("selfdoc.tables", None)


# ---------------------------------------------------------------------------
# Load directive modules (after mock is in place via autouse fixture)
# ---------------------------------------------------------------------------

# Module-level loading won't work because the autouse fixture runs after
# module-level code. Load lazily inside each test class instead.


def _feature_matrix_resolve(attrs, config, body):
    mod = _load_directive("feature_matrix")
    return mod.resolve(attrs, config, body)


def _target_table_resolve(attrs, config, body):
    mod = _load_directive("target_table")
    return mod.resolve(attrs, config, body)


# ---------------------------------------------------------------------------
# Feature matrix directive tests
# ---------------------------------------------------------------------------

ALL_TARGETS = sorted([
    "cargo", "dart", "deno", "docker", "docs", "flutter-android",
    "flutter-ios", "go", "hex", "maven", "npm", "pgdesign", "plain",
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
        """Output contains all 18 target names."""
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
