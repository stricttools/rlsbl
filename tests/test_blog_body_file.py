"""Tests for blog body file validation, archival, and restoration."""

import os
import stat

import pytest

from rlsbl.commands.release import (
    _cleanup_release_artifacts,
    archive_blog_body,
    validate_blog_body,
)
from rlsbl.release_file import unfinalize_release_file


class TestBlogBodyValidation:
    """Tests for blog body file validation during release."""

    def test_blog_true_body_exists_nonempty(self, tmp_path):
        """blog=true with a non-empty body file: returns path, no warning."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        body = releases_dir / "unreleased.md"
        body.write_text("# Blog post\n\nSome content here.\n")

        body_path, warning = validate_blog_body(str(tmp_path), blog_enabled=True)
        assert body_path == str(body)
        assert warning is None

    def test_blog_true_body_missing_warns(self, tmp_path):
        """blog=true with missing body file: warning returned, no error."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        body_path, warning = validate_blog_body(str(tmp_path), blog_enabled=True)
        assert body_path is None
        assert warning is not None
        assert "changelog-only" in warning

    def test_blog_true_body_empty_errors(self, tmp_path):
        """blog=true with an empty body file: raises SystemExit."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        body = releases_dir / "unreleased.md"
        body.write_text("")

        with pytest.raises(SystemExit) as exc_info:
            validate_blog_body(str(tmp_path), blog_enabled=True)
        assert exc_info.value.code == 1

    def test_blog_true_body_whitespace_only_errors(self, tmp_path):
        """blog=true with a whitespace-only body file: treated as empty, raises SystemExit."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        body = releases_dir / "unreleased.md"
        body.write_text("   \n\n  \n")

        with pytest.raises(SystemExit) as exc_info:
            validate_blog_body(str(tmp_path), blog_enabled=True)
        assert exc_info.value.code == 1

    def test_blog_false_skips_body_check(self, tmp_path):
        """blog=false: returns (None, None) even if body file exists and is empty."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        body = releases_dir / "unreleased.md"
        body.write_text("")

        body_path, warning = validate_blog_body(str(tmp_path), blog_enabled=False)
        assert body_path is None
        assert warning is None


class TestBlogBodyArchival:
    """Tests for blog body file archival during release finalization."""

    def test_finalize_archives_body_file(self, tmp_path):
        """unreleased.md is renamed to v{version}.md and chmod 444."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        body = releases_dir / "unreleased.md"
        body.write_text("# My blog post\n\nDetails about the release.\n")

        result = archive_blog_body(str(tmp_path), "1.2.3")

        expected_dst = str(releases_dir / "v1.2.3.md")
        assert result == expected_dst
        assert not (releases_dir / "unreleased.md").exists(), "unreleased.md should be renamed away"
        assert (releases_dir / "v1.2.3.md").exists(), "v1.2.3.md should exist"
        mode = stat.S_IMODE(os.stat(expected_dst).st_mode)
        assert mode == 0o444, f"archived body file should be read-only, got {oct(mode)}"
        with open(expected_dst, "r", encoding="utf-8") as f:
            assert "My blog post" in f.read()

    def test_finalize_no_body_file(self, tmp_path):
        """No unreleased.md: returns None, no error."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        result = archive_blog_body(str(tmp_path), "1.2.3")

        assert result is None
        assert not (releases_dir / "v1.2.3.md").exists(), "no body file should be created"

    def test_finalize_body_file_included_in_commit(self, tmp_path):
        """Archived path is returned (non-None) when body file exists."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        body = releases_dir / "unreleased.md"
        body.write_text("Blog content.\n")

        result = archive_blog_body(str(tmp_path), "2.0.0")

        assert result is not None, "archived blog body path should be returned for commit"
        assert os.path.exists(result)

    def test_finalize_body_file_not_in_commit_when_missing(self, tmp_path):
        """When no body file, returns None (nothing to add to commit)."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        result = archive_blog_body(str(tmp_path), "2.0.0")

        assert result is None, "no blog body path when the file does not exist"


class TestBlogBodyCleanup:
    """Tests for blog body file cleanup during rollback."""

    def test_cleanup_removes_archived_body(self, tmp_path):
        """v{version}.md in releases/ is removed during cleanup."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        changes_dir = tmp_path / ".rlsbl" / "changes"
        releases_dir.mkdir(parents=True)
        changes_dir.mkdir(parents=True)

        version = "1.2.3"
        body_file = releases_dir / f"v{version}.md"
        body_file.write_text("Blog content.\n")
        body_file.chmod(0o444)

        assert body_file.exists()
        _cleanup_release_artifacts(str(tmp_path), version)
        assert not body_file.exists(), "archived blog body should be removed during cleanup"

    def test_cleanup_tolerates_missing_body(self, tmp_path):
        """Cleanup does not crash when blog body archive is missing."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        changes_dir = tmp_path / ".rlsbl" / "changes"
        releases_dir.mkdir(parents=True)
        changes_dir.mkdir(parents=True)

        _cleanup_release_artifacts(str(tmp_path), "1.2.3")
        # No error raised

    def test_cleanup_removes_body_alongside_other_artifacts(self, tmp_path):
        """All four artifact files are removed together."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        releases_dir = tmp_path / ".rlsbl" / "releases"
        changes_dir.mkdir(parents=True)
        releases_dir.mkdir(parents=True)

        version = "3.0.0"
        jsonl = changes_dir / f"{version}.jsonl"
        md = changes_dir / f"{version}.md"
        toml = releases_dir / f"v{version}.toml"
        body = releases_dir / f"v{version}.md"

        jsonl.write_text('{"commits":["abc"],"user_facing":false}\n')
        jsonl.chmod(0o444)
        md.write_text("## 3.0.0\n\n- stuff\n")
        toml.write_text('bump = "major"\n')
        toml.chmod(0o444)
        body.write_text("Blog body.\n")
        body.chmod(0o444)

        _cleanup_release_artifacts(str(tmp_path), version)

        assert not jsonl.exists()
        assert not md.exists()
        assert not toml.exists()
        assert not body.exists()


class TestBlogBodyUnfinalize:
    """Tests for blog body file restoration via unfinalize."""

    def test_unfinalize_restores_body_file(self, tmp_path):
        """v{version}.md is renamed back to unreleased.md with chmod 644."""
        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()

        version = "1.2.3"
        # Set up the toml files (required for unfinalize to proceed)
        versioned_toml = releases_dir / f"v{version}.toml"
        versioned_toml.write_text('bump = "patch"\n')
        versioned_toml.chmod(0o444)
        # No unreleased.toml (or empty one -- finalization creates an empty one)
        unreleased_toml = releases_dir / "unreleased.toml"
        unreleased_toml.write_text("")

        # Set up the blog body file
        versioned_md = releases_dir / f"v{version}.md"
        versioned_md.write_text("Blog content for 1.2.3.\n")
        versioned_md.chmod(0o444)

        changed = unfinalize_release_file(str(releases_dir), version)

        # The toml should be restored
        assert unreleased_toml.exists()
        assert not versioned_toml.exists()

        # The blog body should be restored
        unreleased_md = releases_dir / "unreleased.md"
        assert unreleased_md.exists(), "unreleased.md should be restored"
        assert not versioned_md.exists(), "v1.2.3.md should be renamed away"
        mode = stat.S_IMODE(os.stat(str(unreleased_md)).st_mode)
        assert mode == 0o644, f"restored body should be writable, got {oct(mode)}"
        assert unreleased_md.read_text() == "Blog content for 1.2.3.\n"

        # Both toml and md files should be in the changed list
        assert str(unreleased_toml) in changed
        assert str(versioned_toml) in changed
        assert str(unreleased_md) in changed
        assert str(versioned_md) in changed

    def test_unfinalize_no_body_file(self, tmp_path):
        """No v{version}.md: only toml is restored."""
        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()

        version = "1.2.3"
        versioned_toml = releases_dir / f"v{version}.toml"
        versioned_toml.write_text('bump = "patch"\n')
        versioned_toml.chmod(0o444)
        unreleased_toml = releases_dir / "unreleased.toml"
        unreleased_toml.write_text("")

        changed = unfinalize_release_file(str(releases_dir), version)

        assert unreleased_toml.exists()
        assert not versioned_toml.exists()
        # Only toml files in the changed list
        assert len(changed) == 2
        assert str(unreleased_toml) in changed
        assert str(versioned_toml) in changed

    def test_unfinalize_body_conflict_warns(self, tmp_path, capsys):
        """Both unreleased.md and v{version}.md exist: warning, body left in place."""
        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()

        version = "1.2.3"
        # Set up the toml files
        versioned_toml = releases_dir / f"v{version}.toml"
        versioned_toml.write_text('bump = "patch"\n')
        versioned_toml.chmod(0o444)
        unreleased_toml = releases_dir / "unreleased.toml"
        unreleased_toml.write_text("")

        # Both md files exist (conflict)
        versioned_md = releases_dir / f"v{version}.md"
        versioned_md.write_text("Archived blog.\n")
        versioned_md.chmod(0o444)
        unreleased_md = releases_dir / "unreleased.md"
        unreleased_md.write_text("New draft blog.\n")

        changed = unfinalize_release_file(str(releases_dir), version)

        # Toml should still be restored
        assert unreleased_toml.exists()
        assert not versioned_toml.exists()

        # Both md files should still exist (conflict -> left in place)
        assert versioned_md.exists(), "v1.2.3.md should be left in place on conflict"
        assert unreleased_md.exists(), "unreleased.md should be left in place on conflict"

        # Warning should be printed to stderr
        captured = capsys.readouterr()
        assert "warning" in captured.err
        assert "unreleased.md" in captured.err

        # Only toml files in changed list (md not touched)
        assert str(unreleased_md) not in changed
        assert str(versioned_md) not in changed

    def test_unfinalize_no_versioned_toml_noop(self, tmp_path):
        """No v{version}.toml: entire unfinalize is a no-op, including body."""
        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()

        version = "1.2.3"
        # Only a versioned md, no versioned toml
        versioned_md = releases_dir / f"v{version}.md"
        versioned_md.write_text("Orphaned blog.\n")
        versioned_md.chmod(0o444)

        changed = unfinalize_release_file(str(releases_dir), version)

        # No-op because the toml is missing
        assert changed == []
        # The md file is untouched
        assert versioned_md.exists()
