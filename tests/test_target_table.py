"""Tests for target table data generation in rlsbl.targets.introspect."""

from rlsbl.targets import TARGETS
from rlsbl.targets.introspect import (
    CAPABILITY_COLUMNS,
    HEADERS,
    generate_target_table_data,
)

EXPECTED_HEADERS = [
    "Name", "Ecosystem", "Detection files", "Version file", "Auto-detectable",
    "Tag format", "Monorepo tag format", "publish", "build_assets", "read_name",
    "read_metadata", "ci_templates", "dev_install",
]


def _rows_by_name(rows):
    """Index rows by target name (first cell)."""
    return {row[0]: row for row in rows}


class TestHeaders:
    def test_returns_13_headers(self):
        headers, _ = generate_target_table_data()
        assert len(headers) == 13

    def test_header_names_match(self):
        headers, _ = generate_target_table_data()
        assert headers == EXPECTED_HEADERS


class TestRows:
    def test_returns_18_rows(self):
        _, rows = generate_target_table_data()
        assert len(rows) == 18

    def test_rows_match_header_length(self):
        headers, rows = generate_target_table_data()
        for row in rows:
            assert len(row) == len(headers), (
                f"Row {row[0]!r} has {len(row)} cells, expected {len(headers)}"
            )

    def test_rows_sorted_alphabetically(self):
        _, rows = generate_target_table_data()
        names = [row[0] for row in rows]
        assert names == sorted(names)


class TestCapabilityCheckmarks:
    def test_capability_checkmarks_match_frozenset(self):
        """For each target, the checkmark columns match the target's capabilities."""
        headers, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        for target_name, target in TARGETS.items():
            row = by_name[target_name]
            for cap in CAPABILITY_COLUMNS:
                col_idx = headers.index(cap)
                cell = row[col_idx]
                if cap in target.capabilities:
                    assert cell == "✓", (
                        f"{target_name}.{cap}: expected checkmark, got {cell!r}"
                    )
                else:
                    assert cell == "", (
                        f"{target_name}.{cap}: expected empty, got {cell!r}"
                    )


class TestSpecificTargets:
    def test_docs_tag_format_is_dash(self):
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        tag_idx = headers.index("Tag format")
        assert by_name["docs"][tag_idx] == "---"

    def test_maven_version_file_is_dash(self):
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        vf_idx = headers.index("Version file")
        assert by_name["maven"][vf_idx] == "---"

    def test_plain_detection_files(self):
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        df_idx = headers.index("Detection files")
        assert by_name["plain"][df_idx] == "VERSION (conditional)"

    def test_spec_tag_format(self):
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        tag_idx = headers.index("Tag format")
        assert by_name["spec"][tag_idx] == "spec-v{version}"

    def test_go_monorepo_tag_format(self):
        """Go target uses path-based monorepo tag format."""
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        mono_idx = headers.index("Monorepo tag format")
        value = by_name["go"][mono_idx]
        # Go uses path-based format: "{path}/v{version}"
        assert "{path}" in value
        assert "v{version}" in value

    def test_flutter_detection_files(self):
        """Flutter targets show pubspec.yaml (flutter) as detection method."""
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        df_idx = headers.index("Detection files")
        assert by_name["flutter-ios"][df_idx] == "pubspec.yaml (flutter)"
        assert by_name["flutter-android"][df_idx] == "pubspec.yaml (flutter)"

    def test_flutter_monorepo_tag_format(self):
        """Flutter targets use name-based monorepo tag formats with platform suffix."""
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        mono_idx = headers.index("Monorepo tag format")
        assert by_name["flutter-ios"][mono_idx] == "{name}-ios@v{version}"
        assert by_name["flutter-android"][mono_idx] == "{name}-android@v{version}"

    def test_dev_install_formatting(self):
        """Targets with both global and venv show both; targets with only global show just global."""
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        dev_idx = headers.index("dev_install")
        # npm has both global and venv
        npm_val = by_name["npm"][dev_idx]
        assert npm_val.startswith("global: ")
        assert ", venv: " in npm_val
        # go has only global
        go_val = by_name["go"][dev_idx]
        assert go_val.startswith("global: ")
        assert ", venv: " not in go_val

    def test_dev_install_empty_for_non_capable(self):
        """Targets without dev_install capability have an empty string in that column."""
        _, rows = generate_target_table_data()
        by_name = _rows_by_name(rows)
        headers = EXPECTED_HEADERS
        dev_idx = headers.index("dev_install")
        assert by_name["docker"][dev_idx] == ""
        assert by_name["plain"][dev_idx] == ""
