"""Tests for Phase 9.2: asset naming for multi-artifact releasables.

Covers:
- Asset filenames are prefixed with member name in releasable mode
"""

import os

import pytest

from rlsbl.commands.release.publish import _prefix_artifact


class TestAssetNamingPrefix:
    """Asset filenames get member-name prefix in releasable mode."""

    def test_prefix_artifact_renames_file(self, tmp_path):
        """_prefix_artifact renames the file with member prefix."""
        artifact = tmp_path / "mylib-0.1.0.tar.gz"
        artifact.write_bytes(b"fake artifact")

        result = _prefix_artifact(str(artifact), "core")

        assert os.path.basename(result) == "core--mylib-0.1.0.tar.gz"
        assert os.path.exists(result)
        assert not os.path.exists(str(artifact))

    def test_prefix_artifact_preserves_directory(self, tmp_path):
        """Prefixed artifact stays in the same directory."""
        sub = tmp_path / "dist"
        sub.mkdir()
        artifact = sub / "pkg-1.0.0.whl"
        artifact.write_bytes(b"fake")

        result = _prefix_artifact(str(artifact), "web")

        assert os.path.dirname(result) == str(sub)
        assert os.path.basename(result) == "web--pkg-1.0.0.whl"

    def test_different_members_produce_unique_names(self, tmp_path):
        """Two members with same base name get unique prefixed names."""
        art1 = tmp_path / "lib-0.1.0.tar.gz"
        art1.write_bytes(b"a")
        art2 = tmp_path / "lib-0.1.0.tar.gz.copy"
        art2.write_bytes(b"b")
        # Simulate: rename copy to same base name in separate dir
        dir1 = tmp_path / "d1"
        dir1.mkdir()
        dir2 = tmp_path / "d2"
        dir2.mkdir()
        a1 = dir1 / "lib-0.1.0.tar.gz"
        a1.write_bytes(b"a")
        a2 = dir2 / "lib-0.1.0.tar.gz"
        a2.write_bytes(b"b")

        r1 = _prefix_artifact(str(a1), "core")
        r2 = _prefix_artifact(str(a2), "web")

        assert os.path.basename(r1) == "core--lib-0.1.0.tar.gz"
        assert os.path.basename(r2) == "web--lib-0.1.0.tar.gz"
        assert r1 != r2
