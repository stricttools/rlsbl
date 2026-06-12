"""Tests for the check feature support matrix in rlsbl.checks."""

import tomllib
from pathlib import Path

from rlsbl.checks import (
    CHECK_EXCLUDED_TARGETS,
    CHECK_TARGETS,
    MATRIX_COLUMNS,
    generate_feature_matrix_data,
    get_feature_matrix,
)


def _load_checks_toml_names() -> set[str]:
    """Load check names from checks.toml as the source of truth."""
    checks_path = Path(__file__).resolve().parent.parent / "rlsbl" / "data" / "checks.toml"
    with open(checks_path, "rb") as f:
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
        """Dep checks use PythonImportScanner, DartImportScanner, NpmImportScanner, GoImportScanner."""
        scanner_targets = frozenset({"pypi", "dart", "npm", "go"})
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

    def test_target_specific_checks_have_yes_no_or_na(self):
        matrix = get_feature_matrix()
        for name, value in CHECK_TARGETS.items():
            if isinstance(value, frozenset):
                row = matrix[name]
                excluded = CHECK_EXCLUDED_TARGETS.get(name, {})
                for col, cell in row.items():
                    if col in excluded:
                        assert cell == "n/a", (
                            f"{name}[{col}] should be 'n/a'"
                        )
                    elif col in value:
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


class TestGenerateFeatureMatrixData:
    """Tests for generate_feature_matrix_data()."""

    def test_returns_headers_and_rows(self):
        headers, rows = generate_feature_matrix_data()
        assert isinstance(headers, list)
        assert isinstance(rows, list)
        assert len(headers) >= 2  # "Check" + at least one target column
        assert len(rows) >= 1  # at least one data row
        assert headers[0] == "Check"

    def test_rows_match_header_length(self):
        headers, rows = generate_feature_matrix_data()
        for row in rows:
            assert len(row) == len(headers), (
                f"row {row[0]} has {len(row)} cells, expected {len(headers)}"
            )

    def test_only_target_specific_rows(self):
        """Only checks with yes/no values appear in the data."""
        headers, rows = generate_feature_matrix_data()
        check_names = {row[0] for row in rows}
        # Universal checks should not appear
        assert "version-consistency" not in check_names
        assert "changelog-entry" not in check_names
        # Workspace-only checks should not appear
        assert "workspace-ci-router" not in check_names
        # Target-specific checks should appear
        assert "dead-modules" in check_names
        assert "library-lint" in check_names
        assert "deps-unused" in check_names

    def test_only_active_columns(self):
        """Columns with no 'yes' among interesting rows are excluded."""
        headers, rows = generate_feature_matrix_data()
        # cargo has no target-specific checks that support it
        assert "cargo" not in headers
        # pypi, go, npm, dart should all appear
        assert "pypi" in headers
        assert "go" in headers
        assert "npm" in headers
        assert "dart" in headers

    def test_na_cells_in_data(self):
        """n/a cells must appear as 'n/a' in the data."""
        headers, rows = generate_feature_matrix_data()
        go_idx = headers.index("go")
        for row in rows:
            if row[0] == "circular-deps":
                assert row[go_idx] == "n/a", (
                    f"circular-deps x go should be 'n/a', got {row[go_idx]!r}"
                )
                break
        else:
            raise AssertionError("circular-deps row not found in data")

    def test_rows_sorted_by_check_name(self):
        """Rows must be sorted alphabetically by check name."""
        headers, rows = generate_feature_matrix_data()
        check_names = [row[0] for row in rows]
        assert check_names == sorted(check_names)


class TestCheckExcludedTargets:
    """CHECK_EXCLUDED_TARGETS must be well-formed and consistent."""

    def test_excluded_checks_exist(self):
        """Every check in CHECK_EXCLUDED_TARGETS must exist in CHECK_TARGETS."""
        for name in CHECK_EXCLUDED_TARGETS:
            assert name in CHECK_TARGETS, (
                f"CHECK_EXCLUDED_TARGETS[{name!r}] not in CHECK_TARGETS"
            )

    def test_excluded_targets_are_known(self):
        """Excluded target names must be in MATRIX_COLUMNS."""
        columns = set(MATRIX_COLUMNS)
        for name, excluded in CHECK_EXCLUDED_TARGETS.items():
            for target in excluded:
                assert target in columns, (
                    f"CHECK_EXCLUDED_TARGETS[{name!r}] references unknown target {target!r}"
                )

    def test_excluded_targets_not_in_supported(self):
        """Excluded targets must not also be in the check's supported set."""
        for name, excluded in CHECK_EXCLUDED_TARGETS.items():
            targets = CHECK_TARGETS[name]
            if isinstance(targets, frozenset):
                overlap = set(excluded) & targets
                assert not overlap, (
                    f"CHECK_EXCLUDED_TARGETS[{name!r}] overlaps with "
                    f"CHECK_TARGETS: {sorted(overlap)}"
                )

    def test_excluded_targets_have_reasons(self):
        """Every excluded target must have a non-empty reason string."""
        for name, excluded in CHECK_EXCLUDED_TARGETS.items():
            for target, reason in excluded.items():
                assert isinstance(reason, str) and reason.strip(), (
                    f"CHECK_EXCLUDED_TARGETS[{name!r}][{target!r}] "
                    f"must have a non-empty reason"
                )

    def test_circular_deps_go_excluded(self):
        """circular-deps must exclude go with a reason."""
        assert "circular-deps" in CHECK_EXCLUDED_TARGETS
        assert "go" in CHECK_EXCLUDED_TARGETS["circular-deps"]

    def test_circular_deps_go_is_na_in_matrix(self):
        """circular-deps x go must be 'n/a' in get_feature_matrix()."""
        matrix = get_feature_matrix()
        assert matrix["circular-deps"]["go"] == "n/a"
