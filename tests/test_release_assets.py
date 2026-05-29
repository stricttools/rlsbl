"""Tests for the release asset upload step (upload_release_assets).

Verifies:
1. When no targets have ``assets: true``, the step is skipped.
2. When a target has ``assets: true``, ``build_assets()`` is called and artifacts are uploaded.
3. When an artifact exceeds ``max_asset_size_mb``, the upload is aborted with an error.
4. When ``build_assets()`` raises ``NotImplementedError``, the target is skipped with a warning.
5. Dry-run prints what would happen without building or uploading.
"""

import json
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest

from rlsbl.commands.release import upload_release_assets


def _write_config(tmp_dir, config):
    """Write .rlsbl/config.json in tmp_dir."""
    rlsbl_dir = os.path.join(str(tmp_dir), ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
        json.dump(config, f)


class TestNoAssetsConfigured:
    """When no targets have ``assets: true``, the step is a no-op."""

    def setup_method(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def teardown_method(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_no_assets_config_skips_silently(self):
        """No targets with assets: true means nothing happens."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "publish": {"npm": {"local": False}},
        })
        # Create package.json so npm target is detected
        with open("package.json", "w") as f:
            json.dump({"name": "test", "version": "1.0.0"}, f)

        messages = []
        log = lambda msg: messages.append(msg)

        with patch("rlsbl.commands.release.run") as mock_run:
            upload_release_assets("v1.0.0", ".", "1.0.0", log, {}, project_root=".")
            # gh release upload should never be called
            mock_run.assert_not_called()

        # No log messages since it returns early
        assert not messages

    def test_empty_publish_config_skips(self):
        """No publish section at all still skips."""
        _write_config(self.tmp_dir, {"targets": ["npm"]})
        with open("package.json", "w") as f:
            json.dump({"name": "test", "version": "1.0.0"}, f)

        with patch("rlsbl.commands.release.run") as mock_run:
            upload_release_assets("v1.0.0", ".", "1.0.0", lambda m: None, {}, project_root=".")
            mock_run.assert_not_called()

    def test_assets_false_skips(self):
        """Explicit assets: false is a no-op."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "publish": {"npm": {"assets": False}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test", "version": "1.0.0"}, f)

        with patch("rlsbl.commands.release.run") as mock_run:
            upload_release_assets("v1.0.0", ".", "1.0.0", lambda m: None, {}, project_root=".")
            mock_run.assert_not_called()


class TestAssetBuildAndUpload:
    """When assets: true, build_assets() is called and artifacts are uploaded."""

    def setup_method(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def teardown_method(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_build_and_upload(self):
        """Assets are built, size-checked, and uploaded via gh release upload."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "publish": {"npm": {"assets": True, "max_asset_size_mb": 50}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        # Create a fake target that produces artifacts
        mock_target = MagicMock()
        artifact_path = os.path.join(self.tmp_dir, "fake-artifact.tgz")

        def fake_build_assets(project_dir, version, dist_dir):
            os.makedirs(dist_dir, exist_ok=True)
            # Create a small artifact
            with open(artifact_path, "wb") as f:
                f.write(b"x" * 1024)
            return [artifact_path]

        mock_target.build_assets.side_effect = fake_build_assets

        messages = []
        log = lambda msg: messages.append(msg)

        with patch("rlsbl.commands.release.TARGETS", {"npm": mock_target}):
            with patch("rlsbl.commands.release.run") as mock_run:
                upload_release_assets("v1.0.0", ".", "1.0.0", log, {}, project_root=".")

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
            "publish": {"npm": {"assets": True, "max_asset_size_mb": 50}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        mock_target = MagicMock()
        mock_target.build_assets.return_value = []

        messages = []
        log = lambda msg: messages.append(msg)

        with patch("rlsbl.commands.release.TARGETS", {"npm": mock_target}):
            with patch("rlsbl.commands.release.run") as mock_run:
                upload_release_assets("v1.0.0", ".", "1.0.0", log, {}, project_root=".")
                mock_run.assert_not_called()

        assert any("No artifacts" in m for m in messages)


class TestAssetSizeExceeded:
    """When an artifact exceeds max_asset_size_mb, the upload is aborted."""

    def setup_method(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def teardown_method(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_oversized_artifact_aborts(self, capsys):
        """An artifact exceeding the size limit causes sys.exit(1)."""
        _write_config(self.tmp_dir, {
            "targets": ["pypi"],
            "publish": {"pypi": {"assets": True, "max_asset_size_mb": 1}},
        })
        with open("pyproject.toml", "w") as f:
            f.write('[project]\nname = "pkg"\nversion = "1.0.0"\n')

        oversized_path = os.path.join(self.tmp_dir, "huge.tar.gz")

        mock_target = MagicMock()

        def fake_build_assets(project_dir, version, dist_dir):
            os.makedirs(dist_dir, exist_ok=True)
            # Create a file that exceeds 1MB
            with open(oversized_path, "wb") as f:
                f.write(b"\0" * (2 * 1024 * 1024))
            return [oversized_path]

        mock_target.build_assets.side_effect = fake_build_assets

        with patch("rlsbl.commands.release.TARGETS", {"pypi": mock_target}):
            with patch("rlsbl.commands.release.run"):
                with pytest.raises(SystemExit) as exc_info:
                    upload_release_assets("v1.0.0", ".", "1.0.0", lambda m: None, {}, project_root=".")
                assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "exceeds max_asset_size_mb" in captured.err
        assert "huge.tar.gz" in captured.err
        assert "1MB" in captured.err


class TestBuildAssetsNotImplemented:
    """When build_assets() raises NotImplementedError, the target is skipped."""

    def setup_method(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def teardown_method(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_not_implemented_warns_and_skips(self, capsys):
        """NotImplementedError prints a warning and skips the target."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "publish": {"npm": {"assets": True, "max_asset_size_mb": 50}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        mock_target = MagicMock()
        mock_target.build_assets.side_effect = NotImplementedError("not supported")

        with patch("rlsbl.commands.release.TARGETS", {"npm": mock_target}):
            with patch("rlsbl.commands.release.run") as mock_run:
                # Should not raise
                upload_release_assets("v1.0.0", ".", "1.0.0", lambda m: None, {}, project_root=".")
                # No upload call
                mock_run.assert_not_called()

        captured = capsys.readouterr()
        assert "does not support asset builds" in captured.err


class TestDryRun:
    """Dry-run prints what would happen without building or uploading."""

    def setup_method(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def teardown_method(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_dry_run_logs_without_action(self):
        """Dry-run mode logs intent but does not build or upload."""
        _write_config(self.tmp_dir, {
            "targets": ["npm"],
            "publish": {"npm": {"assets": True, "max_asset_size_mb": 50}},
        })
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        mock_target = MagicMock()

        messages = []
        log = lambda msg: messages.append(msg)

        with patch("rlsbl.commands.release.TARGETS", {"npm": mock_target}):
            with patch("rlsbl.commands.release.run") as mock_run:
                upload_release_assets("v1.0.0", ".", "1.0.0", log, {"dry-run": True}, project_root=".")
                # No build_assets or upload calls
                mock_target.build_assets.assert_not_called()
                mock_run.assert_not_called()

        assert any("Would build and upload" in m for m in messages)
        assert any("npm" in m for m in messages)
