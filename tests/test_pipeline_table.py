"""Tests for pipeline table data generation in rlsbl.pipelines.introspect."""

from rlsbl.pipelines import PIPELINE_TYPES
from rlsbl.pipelines.base import TokenPipeline, CredentialPipeline
from rlsbl.pipelines.introspect import (
    HEADERS,
    generate_pipeline_table_data,
)

EXPECTED_HEADERS = ["Type", "Auth method", "Required env vars", "Ecosystem"]


class TestHeaders:
    def test_returns_4_headers(self):
        headers, _ = generate_pipeline_table_data()
        assert len(headers) == 4

    def test_header_names_match(self):
        headers, _ = generate_pipeline_table_data()
        assert headers == EXPECTED_HEADERS


class TestRows:
    def test_returns_10_rows(self):
        _, rows = generate_pipeline_table_data()
        assert len(rows) == 10

    def test_rows_match_header_length(self):
        headers, rows = generate_pipeline_table_data()
        for row in rows:
            assert len(row) == len(headers), (
                f"Row {row[0]!r} has {len(row)} cells, expected {len(headers)}"
            )

    def test_rows_sorted_alphabetically(self):
        _, rows = generate_pipeline_table_data()
        names = [row[0] for row in rows]
        assert names == sorted(names)

    def test_all_pipeline_types_present(self):
        _, rows = generate_pipeline_table_data()
        row_names = {row[0] for row in rows}
        assert row_names == set(PIPELINE_TYPES.keys())


class TestAuthMethod:
    def test_token_pipelines(self):
        """Pipeline types that extend TokenPipeline show 'token'."""
        _, rows = generate_pipeline_table_data()
        by_name = {row[0]: row for row in rows}
        for type_name, cls in PIPELINE_TYPES.items():
            if issubclass(cls, TokenPipeline):
                assert by_name[type_name][1] == "token", (
                    f"{type_name} should have auth method 'token'"
                )

    def test_credential_pipelines(self):
        """Pipeline types that extend CredentialPipeline show 'credential'."""
        _, rows = generate_pipeline_table_data()
        by_name = {row[0]: row for row in rows}
        for type_name, cls in PIPELINE_TYPES.items():
            if issubclass(cls, CredentialPipeline):
                assert by_name[type_name][1] == "credential", (
                    f"{type_name} should have auth method 'credential'"
                )

    def test_no_auth_pipelines(self):
        """Pipeline types that extend neither Token nor Credential show 'none'."""
        _, rows = generate_pipeline_table_data()
        by_name = {row[0]: row for row in rows}
        for type_name, cls in PIPELINE_TYPES.items():
            if not issubclass(cls, (TokenPipeline, CredentialPipeline)):
                assert by_name[type_name][1] == "none", (
                    f"{type_name} should have auth method 'none'"
                )


class TestEnvVars:
    def test_npm_token_var(self):
        _, rows = generate_pipeline_table_data()
        by_name = {row[0]: row for row in rows}
        assert by_name["npm"][2] == "NPM_TOKEN"

    def test_pypi_token_var(self):
        _, rows = generate_pipeline_table_data()
        by_name = {row[0]: row for row in rows}
        assert by_name["pypi"][2] == "PYPI_TOKEN"

    def test_docker_credential_vars(self):
        _, rows = generate_pipeline_table_data()
        by_name = {row[0]: row for row in rows}
        assert "DOCKER_USERNAME" in by_name["docker"][2]
        assert "DOCKER_PASSWORD" in by_name["docker"][2]

    def test_go_no_env_vars(self):
        """Go pipeline has no default env vars (uses GITHUB_TOKEN from CI)."""
        _, rows = generate_pipeline_table_data()
        by_name = {row[0]: row for row in rows}
        assert by_name["go"][2] == ""


class TestEcosystem:
    def test_all_have_ecosystem(self):
        """Every row has a non-empty ecosystem string."""
        _, rows = generate_pipeline_table_data()
        for row in rows:
            assert row[3], f"Pipeline {row[0]!r} has empty ecosystem"
