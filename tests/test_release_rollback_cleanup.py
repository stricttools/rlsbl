"""Tests for _cleanup_release_artifacts: orphaned generated files are removed
after a release rollback so the working tree stays clean for the next attempt.
"""

import os

from rlsbl.commands.release import _cleanup_release_artifacts


class TestCleanupReleaseArtifacts:
    """Unit tests for the _cleanup_release_artifacts helper."""

    def test_rollback_cleans_orphaned_files(self, tmp_path):
        """All three generated artifact files are removed when present."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        releases_dir = tmp_path / ".rlsbl" / "releases"
        changes_dir.mkdir(parents=True)
        releases_dir.mkdir(parents=True)

        version = "1.2.3"
        jsonl_file = changes_dir / f"{version}.jsonl"
        md_file = changes_dir / f"{version}.md"
        toml_file = releases_dir / f"v{version}.toml"

        # Simulate finalization artifacts (jsonl is chmod 444 in real flow)
        jsonl_file.write_text('{"commits":["abc"],"user_facing":false}\n')
        jsonl_file.chmod(0o444)
        md_file.write_text("## 1.2.3\n\n- No user-facing changes.\n")
        toml_file.write_text("bump = \"patch\"\n")
        toml_file.chmod(0o444)

        _cleanup_release_artifacts(str(tmp_path), version)

        assert not jsonl_file.exists(), "finalized JSONL should be removed"
        assert not md_file.exists(), "per-version .md should be removed"
        assert not toml_file.exists(), "versioned release TOML should be removed"

    def test_rollback_tolerates_missing_files(self, tmp_path):
        """Cleanup does not crash when none of the artifact files exist."""
        # Directories don't even exist
        _cleanup_release_artifacts(str(tmp_path), "0.5.0")

        # Directories exist but files don't
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "releases").mkdir(parents=True)
        _cleanup_release_artifacts(str(tmp_path), "0.5.0")

    def test_rollback_preserves_unrelated_files(self, tmp_path):
        """Files that do not match the version pattern are left untouched."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        releases_dir = tmp_path / ".rlsbl" / "releases"
        changes_dir.mkdir(parents=True)
        releases_dir.mkdir(parents=True)

        # Unrelated files that should survive cleanup
        unreleased = changes_dir / "unreleased.jsonl"
        unreleased.write_text("")
        other_version = changes_dir / "1.0.0.jsonl"
        other_version.write_text('{"commits":["xyz"],"user_facing":false}\n')
        other_toml = releases_dir / "v1.0.0.toml"
        other_toml.write_text("bump = \"minor\"\n")

        # Target version is different
        _cleanup_release_artifacts(str(tmp_path), "1.2.3")

        assert unreleased.exists(), "unreleased.jsonl must not be deleted"
        assert other_version.exists(), "other version JSONL must not be deleted"
        assert other_toml.exists(), "other version TOML must not be deleted"

    def test_rollback_handles_read_only_jsonl(self, tmp_path):
        """Read-only finalized JSONL (chmod 444) is still cleaned up."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)

        version = "2.0.0"
        jsonl_file = changes_dir / f"{version}.jsonl"
        jsonl_file.write_text('{"commits":["def"],"user_facing":false}\n')
        jsonl_file.chmod(0o444)

        _cleanup_release_artifacts(str(tmp_path), version)

        assert not jsonl_file.exists(), "read-only JSONL should be removed"
