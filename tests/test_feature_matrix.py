"""Tests for the check feature support matrix in rlsbl.checks."""

import tomllib

from rlsbl.checks import (
    CHECK_TARGETS,
    MATRIX_COLUMNS,
    generate_feature_matrix_markdown,
    get_feature_matrix,
)


def _load_checks_toml_names() -> set[str]:
    """Load check names from checks.toml as the source of truth."""
    with open("rlsbl/data/checks.toml", "rb") as f:
        data = tomllib.load(f)
    return set(data["checks"].keys())


class TestCheckTargetsCompleteness:
    """CHECK_TARGETS must cover every check in checks.toml."""

    def test_no_missing_checks(self):
        """Every check in checks.toml must have a CHECK_TARGETS entry."""
        toml_names = _load_checks_toml_names()
        meta_names = set(CHECK_TARGETS.keys())
        missing = toml_names - meta_names
        assert not missing, (
            f"checks.toml has checks without CHECK_TARGETS entries: "
            f"{sorted(missing)}"
        )

    def test_no_extra_checks(self):
        """CHECK_TARGETS must not have entries absent from checks.toml."""
        toml_names = _load_checks_toml_names()
        meta_names = set(CHECK_TARGETS.keys())
        extra = meta_names - toml_names
        assert not extra, (
            f"CHECK_TARGETS has entries not in checks.toml: "
            f"{sorted(extra)}"
        )


class TestCheckTargetsValues:
    """CHECK_TARGETS values must be well-formed."""

    def test_value_types(self):
        """Each value must be None, 'workspace', or a frozenset of strings."""
        for name, value in CHECK_TARGETS.items():
            assert value is None or value == "workspace" or isinstance(value, frozenset), (
                f"CHECK_TARGETS[{name!r}] has invalid type: {type(value)}"
            )
            if isinstance(value, frozenset):
                for t in value:
                    assert isinstance(t, str), (
                        f"CHECK_TARGETS[{name!r}] contains non-string: {t!r}"
                    )

    def test_frozenset_targets_are_known(self):
        """Target names in frozensets must be in MATRIX_COLUMNS."""
        columns = set(MATRIX_COLUMNS)
        for name, value in CHECK_TARGETS.items():
            if isinstance(value, frozenset):
                unknown = value - columns
                assert not unknown, (
                    f"CHECK_TARGETS[{name!r}] references unknown targets: "
                    f"{sorted(unknown)}"
                )

    def test_frozenset_targets_nonempty(self):
        """frozenset values must not be empty (use None for universal)."""
        for name, value in CHECK_TARGETS.items():
            if isinstance(value, frozenset):
                assert len(value) > 0, (
                    f"CHECK_TARGETS[{name!r}] has empty frozenset "
                    f"(use None for universal checks)"
                )


class TestCheckTargetsConsistencyWithCode:
    """Verify CHECK_TARGETS matches the actual skip logic in check functions."""

    def test_dead_modules_targets(self):
        """dead-modules check supports pypi, go, npm, dart per its implementation."""
        targets = CHECK_TARGETS["dead-modules"]
        assert targets == frozenset({"pypi", "go", "npm", "dart"})

    def test_library_lint_targets(self):
        """library-lint supports python/pypi, go, npm per _detect_languages."""
        targets = CHECK_TARGETS["library-lint"]
        assert targets == frozenset({"pypi", "go", "npm"})

    def test_dep_checks_use_import_scanners(self):
        """Dep checks use PythonImportScanner, DartImportScanner, NpmImportScanner."""
        scanner_targets = frozenset({"pypi", "dart", "npm"})
        for check_name in ("deps-unused", "deps-undeclared",
                           "deps-runtime-test-only", "deps-dev-in-lib"):
            assert CHECK_TARGETS[check_name] == scanner_targets, (
                f"{check_name} should match import scanner targets"
            )

    def test_workspace_checks_are_workspace(self):
        """Workspace-only checks must be marked as 'workspace'."""
        workspace_checks = {
            "workspace-ci-router", "workspace-ci-synced",
            "workspace-targets", "workspace-unregistered",
            "workspace-stale-entries", "dev-node-boundary",
            "layers-violations", "deps-stale",
        }
        for name in workspace_checks:
            assert CHECK_TARGETS[name] == "workspace", (
                f"{name} should be marked as 'workspace'"
            )

    def test_changelog_checks_are_universal(self):
        """All changelog checks work for any target."""
        changelog_checks = {
            name for name in CHECK_TARGETS
            if name.startswith("changelog-")
        }
        for name in changelog_checks:
            assert CHECK_TARGETS[name] is None, (
                f"{name} should be universal (None)"
            )


class TestGetFeatureMatrix:
    """Tests for get_feature_matrix()."""

    def test_returns_all_checks(self):
        matrix = get_feature_matrix()
        assert set(matrix.keys()) == set(CHECK_TARGETS.keys())

    def test_universal_checks_have_all(self):
        matrix = get_feature_matrix()
        for name, value in CHECK_TARGETS.items():
            if value is None:
                row = matrix[name]
                assert all(v == "all" for v in row.values()), (
                    f"universal check {name} should have all 'all' values"
                )

    def test_workspace_checks_have_workspace(self):
        matrix = get_feature_matrix()
        for name, value in CHECK_TARGETS.items():
            if value == "workspace":
                row = matrix[name]
                assert all(v == "workspace" for v in row.values()), (
                    f"workspace check {name} should have all 'workspace' values"
                )

    def test_target_specific_checks_have_yes_no(self):
        matrix = get_feature_matrix()
        for name, value in CHECK_TARGETS.items():
            if isinstance(value, frozenset):
                row = matrix[name]
                for col, cell in row.items():
                    if col in value:
                        assert cell == "yes", (
                            f"{name}[{col}] should be 'yes'"
                        )
                    else:
                        assert cell == "no", (
                            f"{name}[{col}] should be 'no'"
                        )

    def test_all_columns_present(self):
        """Every row must have all MATRIX_COLUMNS."""
        matrix = get_feature_matrix()
        for name, row in matrix.items():
            assert set(row.keys()) == set(MATRIX_COLUMNS), (
                f"{name} has wrong columns"
            )


class TestGenerateFeatureMatrixMarkdown:
    """Tests for generate_feature_matrix_markdown()."""

    def test_produces_markdown_table(self):
        md = generate_feature_matrix_markdown()
        lines = md.strip().split("\n")
        # Header + separator + at least one data row
        assert len(lines) >= 3
        assert lines[0].startswith("| Check |")
        assert lines[1].startswith("|---")

    def test_only_target_specific_rows(self):
        """Only checks with yes/no values appear in the table."""
        md = generate_feature_matrix_markdown()
        # Universal checks should not appear
        assert "version-consistency" not in md
        assert "changelog-entry" not in md
        # Workspace-only checks should not appear
        assert "workspace-ci-router" not in md
        # Target-specific checks should appear
        assert "dead-modules" in md
        assert "library-lint" in md
        assert "deps-unused" in md

    def test_only_active_columns(self):
        """Columns with no 'yes' among interesting rows are excluded."""
        md = generate_feature_matrix_markdown()
        # cargo has no target-specific checks that support it
        assert "cargo" not in md.split("\n")[0]
        # pypi, go, npm, dart should all appear
        header = md.split("\n")[0]
        assert "pypi" in header
        assert "go" in header
        assert "npm" in header
        assert "dart" in header
