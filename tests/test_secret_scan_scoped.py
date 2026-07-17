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


class TestCleanStaleArtifactsTargetPaths:
    """clean_stale_artifacts cleans per-target dist/ when target_paths given."""

    def test_subdir_stale_artifact_removed(self, tmp_path):
        from rlsbl.secret_scan import clean_stale_artifacts

        subdir = tmp_path / "packages" / "core"
        sub_dist = subdir / "dist"
        sub_dist.mkdir(parents=True)
        stale = sub_dist / "core-1.0.0.whl"
        stale.write_bytes(b"")

        removed = clean_stale_artifacts(
            str(tmp_path), target_paths={"pypi": str(subdir)},
        )

        assert not stale.exists()
        assert any(r.endswith("core-1.0.0.whl") for r in removed)

    def test_root_and_subdir_both_cleaned(self, tmp_path):
        from rlsbl.secret_scan import clean_stale_artifacts

        root_dist = tmp_path / "dist"
        root_dist.mkdir()
        root_stale = root_dist / "root-1.0.0.tar.gz"
        root_stale.write_bytes(b"")

        subdir = tmp_path / "npmpkg"
        sub_dist = subdir / "dist"
        sub_dist.mkdir(parents=True)
        sub_stale = sub_dist / "sub-1.0.0.tgz"
        sub_stale.write_bytes(b"")

        removed = clean_stale_artifacts(
            str(tmp_path), target_paths={"npm": str(subdir)},
        )

        assert not root_stale.exists()
        assert not sub_stale.exists()
        assert len(removed) == 2

    def test_subdir_not_cleaned_without_target_paths(self, tmp_path):
        # Default behaviour unchanged: only the root dist/ is cleaned.
        from rlsbl.secret_scan import clean_stale_artifacts

        subdir = tmp_path / "packages" / "core"
        sub_dist = subdir / "dist"
        sub_dist.mkdir(parents=True)
        stale = sub_dist / "core-1.0.0.whl"
        stale.write_bytes(b"")

        removed = clean_stale_artifacts(str(tmp_path))

        assert stale.exists()
        assert removed == []

    def test_scan_scoped_after_clean_with_target_paths(self, tmp_path):
        # Integration: stale subdir artifact removed before the scan discovers it.
        from rlsbl.secret_scan import clean_stale_artifacts, _find_artifacts

        subdir = tmp_path / "py"
        sub_dist = subdir / "dist"
        sub_dist.mkdir(parents=True)
        (sub_dist / "old-0.1.0.whl").write_bytes(b"")

        tpaths = {"pypi": str(subdir)}
        clean_stale_artifacts(str(tmp_path), target_paths=tpaths)
        assert _find_artifacts(str(tmp_path), target_paths=tpaths) == []
