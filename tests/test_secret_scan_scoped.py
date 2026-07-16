"""Tests that secret scan discovers artifacts in per-target dist/ directories.

Phase 6.4: scan_artifacts_for_secrets must cover subdirectory targets'
dist/ directories when target_paths is provided.
"""

import os
import zipfile

import pytest

from rlsbl.secret_scan import _find_artifacts


class TestFindArtifactsTargetPaths:
    """_find_artifacts discovers subdirectory dist/ when target_paths given."""

    def test_root_only_without_target_paths(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "pkg-1.0.0.whl").write_bytes(b"")
        result = _find_artifacts(str(tmp_path))
        assert len(result) == 1
        assert result[0].endswith("pkg-1.0.0.whl")

    def test_subdir_target_found(self, tmp_path):
        subdir = tmp_path / "packages" / "core"
        subdir.mkdir(parents=True)
        sub_dist = subdir / "dist"
        sub_dist.mkdir()
        (sub_dist / "core-2.0.0.tar.gz").write_bytes(b"")

        result = _find_artifacts(
            str(tmp_path),
            target_paths={"pypi": str(subdir)},
        )
        assert len(result) == 1
        assert result[0].endswith("core-2.0.0.tar.gz")

    def test_root_and_subdir_combined(self, tmp_path):
        # Root dist
        root_dist = tmp_path / "dist"
        root_dist.mkdir()
        (root_dist / "root-1.0.0.whl").write_bytes(b"")
        # Subdir dist
        subdir = tmp_path / "packages" / "sub"
        subdir.mkdir(parents=True)
        sub_dist = subdir / "dist"
        sub_dist.mkdir()
        (sub_dist / "sub-1.0.0.zip").write_bytes(b"")

        result = _find_artifacts(
            str(tmp_path),
            target_paths={"npm": str(subdir)},
        )
        assert len(result) == 2

    def test_dedup_same_path(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "pkg-1.0.0.whl").write_bytes(b"")

        # target_paths pointing to same root
        result = _find_artifacts(
            str(tmp_path),
            target_paths={"pypi": str(tmp_path)},
        )
        assert len(result) == 1

    def test_nonexistent_subdir_dist_ignored(self, tmp_path):
        result = _find_artifacts(
            str(tmp_path),
            target_paths={"go": str(tmp_path / "nonexistent")},
        )
        assert result == []
