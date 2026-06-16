"""Tests for the blog field in ReleaseConfig."""

import os

import pytest

from rlsbl.errors import ReleaseFileError
from rlsbl.release_file import ReleaseConfig, read_release_file


class TestBlogFieldValidation:
    """Tests for the blog field in release file parsing and validation."""

    def test_blog_true(self, tmp_path):
        """blog = true is accepted."""
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "test release"\nblog = true\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.blog is True

    def test_blog_false(self, tmp_path):
        """blog = false is accepted."""
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "test release"\nblog = false\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.blog is False

    def test_blog_non_boolean_rejected(self, tmp_path):
        """blog = "yes" (string, not bool) is rejected."""
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "test release"\nblog = "yes"\n'
        )
        with pytest.raises(ReleaseFileError, match="blog must be a boolean"):
            read_release_file(str(f))

    def test_blog_defaults_to_false(self, tmp_path):
        """Missing blog field defaults to False."""
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "test release"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.blog is False


class TestReleaseInitBlogComment:
    """Test that release init scaffold contains the blog comment."""

    def test_scaffold_contains_blog_comment(self, tmp_path, monkeypatch):
        """The scaffolded TOML contains a commented-out blog field."""
        # Create minimal project structure that detect_targets can find
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "0.1.0"\n')
        # Also need .rlsbl dir for it to be recognized
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()

        monkeypatch.chdir(tmp_path)

        from rlsbl.commands.release_init import run_cmd
        run_cmd(tmp_path)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        assert release_path.exists()
        content = release_path.read_text()
        assert "blog = false" in content or "blog" in content
