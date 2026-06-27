"""Tests for the release asset upload step (upload_release_assets).

Verifies:
1. When no pipelines have ``assets: true``, the step is skipped.
2. When a pipeline has ``assets: true``, ``build_assets()`` is called and artifacts are uploaded.
3. When an artifact exceeds ``max_asset_size_mb``, the upload is aborted with an error.
4. When a pipeline produces no artifacts, upload is skipped.
5. Dry-run prints what would happen without building or uploading.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.commands.release import upload_release_assets
from rlsbl.commands.release.validate import ReleaseValidationError
from rlsbl.context import ProjectContext


def _write_config(tmp_dir, config):
    """Write .rlsbl/config.json in tmp_dir."""
    rlsbl_dir = os.path.join(str(tmp_dir), ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
        json.dump(config, f)


class TestNoAssetsConfigured:
    """When no pipelines have ``assets: true``, the step is a no-op."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_no_assets_config_skips_silently(self):
        """No pipelines with assets: true means nothing happens."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test", "version": "1.0.0"}, f)

        messages = []
        log = lambda msg: messages.append(msg)

        with patch("rlsbl.commands.release.run_gh", return_value=""), \
             patch("rlsbl.commands.release.run") as mock_run:
            ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={
                "targets": ["npm"],
                "pipelines": {"npm": {"type": "npm", "local": False}},
            })
            upload_release_assets("v1.0.0", "1.0.0", log, {}, ctx=ctx)
            # gh release upload should never be called
            mock_run.assert_not_called()

        # No log messages since it returns early
        assert not messages

    def test_empty_pipelines_config_skips(self):
        """No pipelines section at all still skips."""
        _write_config(self.tmp_dir, {"targets": ["npm"]})
        with open("package.json", "w") as f:
            json.dump({"name": "test", "version": "1.0.0"}, f)

        with patch("rlsbl.commands.release.run_gh", return_value=""), \
             patch("rlsbl.commands.release.run") as mock_run:
            ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={"targets": ["npm"]})
            upload_release_assets("v1.0.0", "1.0.0", lambda m: None, {}, ctx=ctx)
            mock_run.assert_not_called()

    def test_assets_false_skips(self):
        """Explicit assets: false is a no-op."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": False}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test", "version": "1.0.0"}, f)

        with patch("rlsbl.commands.release.run_gh", return_value=""), \
             patch("rlsbl.commands.release.run") as mock_run:
            ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={
                "targets": ["npm"],
                "pipelines": {"npm": {"type": "npm", "local": False, "assets": False}},
            })
            upload_release_assets("v1.0.0", "1.0.0", lambda m: None, {}, ctx=ctx)
            mock_run.assert_not_called()


class TestAssetBuildAndUpload:
    """When assets: true on a pipeline, build_assets() is called and artifacts are uploaded."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_build_and_upload(self):
        """Assets are built, size-checked, and uploaded via gh release upload."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        artifact_path = os.path.join(self.tmp_dir, "fake-artifact.tgz")

        # Mock the NpmPipeline's build_assets to produce artifacts
        mock_pipeline = MagicMock()

        def fake_build_assets(project_dir, version, dist_dir, ctx):
            os.makedirs(dist_dir, exist_ok=True)
            with open(artifact_path, "wb") as f:
                f.write(b"x" * 1024)
            return [artifact_path]

        mock_pipeline.build_assets.side_effect = fake_build_assets

        messages = []
        log = lambda msg: messages.append(msg)

        ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}},
        })
        with patch("rlsbl.commands.release.load_pipelines", return_value={"npm": mock_pipeline}):
            with patch("rlsbl.commands.release.run_gh", return_value=""), \
             patch("rlsbl.commands.release.run") as mock_run:
                upload_release_assets("v1.0.0", "1.0.0", log, {}, ctx=ctx)

                # gh release upload should be called with --clobber
                mock_run.assert_called_once()
                call_args = mock_run.call_args
                assert call_args[0][0] == "gh"
                assert "release" in call_args[0][1]
                assert "upload" in call_args[0][1]
                assert "v1.0.0" in call_args[0][1]
                assert "--clobber" in call_args[0][1]

        assert any("Uploaded" in m for m in messages)

    def test_no_artifacts_skips_upload(self):
        """When build_assets returns empty list, upload is skipped."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        mock_pipeline = MagicMock()
        mock_pipeline.build_assets.return_value = []

        messages = []
        log = lambda msg: messages.append(msg)

        ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}},
        })
        with patch("rlsbl.commands.release.load_pipelines", return_value={"npm": mock_pipeline}):
            with patch("rlsbl.commands.release.run_gh", return_value=""), \
             patch("rlsbl.commands.release.run") as mock_run:
                upload_release_assets("v1.0.0", "1.0.0", log, {}, ctx=ctx)
                mock_run.assert_not_called()

        assert any("No artifacts" in m for m in messages)


class TestAssetSizeExceeded:
    """When an artifact exceeds max_asset_size_mb, the upload is aborted."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_oversized_artifact_aborts(self):
        """An artifact exceeding the size limit raises ReleaseValidationError."""
        _write_config(self.tmp_dir, {
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False, "assets": True, "max_asset_size_mb": 1}},
        })
        with open("pyproject.toml", "w") as f:
            f.write('[project]\nname = "pkg"\nversion = "1.0.0"\n')

        oversized_path = os.path.join(self.tmp_dir, "huge.tar.gz")

        mock_pipeline = MagicMock()

        def fake_build_assets(project_dir, version, dist_dir, ctx):
            os.makedirs(dist_dir, exist_ok=True)
            with open(oversized_path, "wb") as f:
                f.write(b"\0" * (2 * 1024 * 1024))
            return [oversized_path]

        mock_pipeline.build_assets.side_effect = fake_build_assets

        ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False, "assets": True, "max_asset_size_mb": 1}},
        })
        with patch("rlsbl.commands.release.load_pipelines", return_value={"pypi": mock_pipeline}):
            with patch("rlsbl.commands.release.run_gh", return_value=""), \
             patch("rlsbl.commands.release.run"):
                with pytest.raises(ReleaseValidationError, match="exceeds max_asset_size_mb"):
                    upload_release_assets("v1.0.0", "1.0.0", lambda m: None, {}, ctx=ctx)


class TestDryRun:
    """Dry-run prints what would happen without building or uploading."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_dry_run_logs_without_action(self):
        """Dry-run mode logs intent but does not build or upload."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        messages = []
        log = lambda msg: messages.append(msg)

        ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}},
        })
        with patch("rlsbl.commands.release.load_pipelines") as mock_load:
            with patch("rlsbl.commands.release.run_gh", return_value=""), \
             patch("rlsbl.commands.release.run") as mock_run:
                upload_release_assets("v1.0.0", "1.0.0", log, {"dry-run": True}, ctx=ctx)
                # load_pipelines is called but no build or upload
                mock_run.assert_not_called()

        assert any("Would build and upload" in m for m in messages)
        assert any("npm" in m for m in messages)
