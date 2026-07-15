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


class TestCleanupTrackedGuard:
    """Real-git tests for the tracked-file guard: finalize artifacts that are
    TRACKED at the post-reset HEAD must be preserved (deleting them would leave
    ` D` index entries that block a retry with --no-allow-dirty)."""

    def test_tracked_finalized_files_are_preserved(self, tmp_path):
        """Re-release case: version files committed by an earlier attempt are
        tracked at HEAD; cleanup must leave them alone and keep the tree clean.
        """
        from githarness import init_repo, git

        repo = tmp_path / "repo"
        init_repo(repo)
        changes = repo / ".rlsbl" / "changes"
        releases = repo / ".rlsbl" / "releases"
        changes.mkdir(parents=True)
        releases.mkdir(parents=True)

        version = "1.2.3"
        jsonl = changes / f"{version}.jsonl"
        md = changes / f"{version}.md"
        toml = releases / f"v{version}.toml"
        jsonl.write_text('{"commits":["abc"],"user_facing":false}\n')
        md.write_text("## 1.2.3\n\n- No user-facing changes.\n")
        toml.write_text('bump = "patch"\n')

        # Commit them -- they are now TRACKED at HEAD (as after a reset --hard
        # that restores an earlier partial attempt's finalize commit).
        git(repo, "add", ".rlsbl")
        git(repo, "commit", "-q", "-m", "finalize files from earlier attempt")

        _cleanup_release_artifacts(str(repo), version)

        assert jsonl.exists(), "tracked finalized JSONL must be preserved"
        assert md.exists(), "tracked finalized .md must be preserved"
        assert toml.exists(), "tracked finalized TOML must be preserved"
        # Preserving tracked files must leave the working tree clean.
        assert git(repo, "status", "--porcelain") == "", (
            "cleanup must not dirty the tree by deleting tracked files"
        )

    def test_untracked_finalized_files_still_removed_in_repo(self, tmp_path):
        """Guard is narrow: genuinely orphaned (untracked) finalize files are
        still removed even inside a git repo."""
        from githarness import init_repo, commit_file, git

        repo = tmp_path / "repo"
        init_repo(repo)
        commit_file(repo, "README.md", "# hi\n", "initial")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        version = "1.2.3"
        jsonl = changes / f"{version}.jsonl"
        md = changes / f"{version}.md"
        jsonl.write_text('{"commits":["abc"],"user_facing":false}\n')
        jsonl.chmod(0o444)
        md.write_text("## 1.2.3\n")
        # NOT committed -- these are untracked orphans.

        _cleanup_release_artifacts(str(repo), version)

        assert not jsonl.exists(), "untracked finalized JSONL must be removed"
        assert not md.exists(), "untracked finalized .md must be removed"
