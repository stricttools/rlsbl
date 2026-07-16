"""Tests for npm wrapper provenance support in build_npm_publish_jobs.

Phase 6.6: provenance config threads --provenance flag + id-token: write
permission into the generated wrapper publish job.
"""

from rlsbl.npm_wrapper import PlatformArtifact, build_npm_publish_jobs


def _minimal_artifact():
    return PlatformArtifact(
        npm_platform="linux-x64",
        os_constraint="linux",
        cpu_constraint="x64",
        asset_pattern="{name}_{version}_linux_amd64.tar.gz",
        extract_cmd="tar xzf",
        binary_name="myapp",
    )


class TestNpmWrapperProvenance:
    """build_npm_publish_jobs threads provenance into the generated workflow."""

    def test_without_provenance(self):
        result = build_npm_publish_jobs(
            npm_scope="@scope",
            bin_command="myapp",
            artifacts=[_minimal_artifact()],
            provenance=False,
        )
        assert "--provenance" not in result
        assert "id-token" not in result

    def test_with_provenance_adds_flag(self):
        result = build_npm_publish_jobs(
            npm_scope="@scope",
            bin_command="myapp",
            artifacts=[_minimal_artifact()],
            provenance=True,
        )
        assert "--provenance" in result

    def test_with_provenance_adds_id_token_permission(self):
        result = build_npm_publish_jobs(
            npm_scope="@scope",
            bin_command="myapp",
            artifacts=[_minimal_artifact()],
            provenance=True,
        )
        assert "id-token: write" in result

    def test_provenance_on_platform_publish(self):
        """--provenance appears in the platform package publish commands."""
        result = build_npm_publish_jobs(
            npm_scope="@scope",
            bin_command="myapp",
            artifacts=[_minimal_artifact()],
            provenance=True,
        )
        # Platform publish lines have --provenance
        assert "npm publish --access public --provenance" in result

    def test_provenance_on_wrapper_publish(self):
        """--provenance appears in the wrapper package publish command."""
        result = build_npm_publish_jobs(
            npm_scope="@scope",
            bin_command="myapp",
            artifacts=[_minimal_artifact()],
            provenance=True,
        )
        # The wrapper publish line at the bottom
        lines = [l.strip() for l in result.splitlines()]
        wrapper_publish = [l for l in lines if l.startswith("run:") and "npm-wrapper && npm publish" in l]
        assert any("--provenance" in l for l in wrapper_publish)

    def test_default_is_no_provenance(self):
        """Without the provenance parameter, no provenance flag is emitted."""
        result = build_npm_publish_jobs(
            npm_scope="@scope",
            bin_command="myapp",
            artifacts=[_minimal_artifact()],
        )
        assert "--provenance" not in result
