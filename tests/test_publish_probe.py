"""Tests for Phase 7.1: idempotent publish with probe-first pattern.

Tests that each pipeline's publish() calls probe_before_publish() and
handles already-published errors as success. Also tests per-pipeline
resume via published_targets in release state.
"""

import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from rlsbl.pipelines.base import BasePipeline, _ALREADY_EXISTS_SIGNATURES
from rlsbl.publication_probe import PublicationProbeResult, PublicationStatus


class TestProbeBeforePublish(unittest.TestCase):
    """Tests for BasePipeline.probe_before_publish()."""

    def _make_pipeline(self, target_name=None):
        """Create a BasePipeline instance with optional target link."""
        pl = BasePipeline("test", "test", local=True, config={})
        pl.target = target_name
        return pl

    def test_no_target_link_proceeds(self):
        """When pipeline has no target link, probe returns True (proceed)."""
        pl = self._make_pipeline(target_name=None)
        self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_unknown_target_proceeds(self):
        """When target name not in TARGETS registry, probe returns True."""
        pl = self._make_pipeline(target_name="nonexistent")
        self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_target_without_probe_capability_proceeds(self):
        """When target lacks publication_probe capability, proceed."""
        target = MagicMock()
        target.capabilities = frozenset({"read_name"})
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")
        with patch("rlsbl.targets.TARGETS", mock_targets):
            self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_published_skips(self):
        """When probe returns PUBLISHED, probe_before_publish returns False."""
        target = MagicMock()
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            status=PublicationStatus.PUBLISHED,
            registry="npm",
            version="1.0.0",
            message="pkg@1.0.0 found on npm",
        )
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")

        with patch("rlsbl.targets.TARGETS", mock_targets), \
             patch("sys.stdout", new_callable=StringIO) as out:
            result = pl.probe_before_publish("/dir", "1.0.0", ctx=None)

        self.assertFalse(result)
        self.assertIn("already published", out.getvalue())

    def test_unpublished_proceeds(self):
        """When probe returns UNPUBLISHED, probe_before_publish returns True."""
        target = MagicMock()
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            status=PublicationStatus.UNPUBLISHED,
            registry="npm",
            version="1.0.0",
            message="not found",
        )
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")
        with patch("rlsbl.targets.TARGETS", mock_targets):
            self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_unprobeable_proceeds(self):
        """When probe returns UNPROBEABLE, probe_before_publish returns True."""
        target = MagicMock()
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            status=PublicationStatus.UNPROBEABLE,
            registry="npm",
            version="1.0.0",
            message="API error",
        )
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")
        with patch("rlsbl.targets.TARGETS", mock_targets):
            self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))


class TestIsAlreadyPublishedError(unittest.TestCase):
    """Tests for BasePipeline.is_already_published_error()."""

    def test_npm_e403_signature(self):
        exc = RuntimeError("npm ERR! previously published version 1.0.0")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_npm_epublishconflict(self):
        exc = RuntimeError("EPUBLISHCONFLICT")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_pypi_file_already_exists(self):
        exc = RuntimeError("File already exists: pkg-1.0.0.tar.gz")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_crates_already_uploaded(self):
        exc = RuntimeError("crate version 1.0.0 already uploaded")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_unrelated_error(self):
        exc = RuntimeError("network timeout")
        self.assertFalse(BasePipeline.is_already_published_error(exc))


class TestPipelineProbeIntegration(unittest.TestCase):
    """Tests that pipeline publish() methods call probe and handle errors."""

    @patch("rlsbl.pipelines.pypi.run")
    def test_pypi_publish_calls_probe(self, mock_run):
        """PypiPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.pypi import PypiPipeline
        import os

        pl = PypiPipeline("pypi", "pypi", local=True, config={})
        pl.target = "pypi"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        # publish should NOT have called run since probe said skip
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.npm.run")
    def test_npm_publish_calls_probe(self, mock_run):
        """NpmPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.npm import NpmPipeline

        pl = NpmPipeline("npm", "npm", local=True, config={})
        pl.target = "npm"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.cargo.run")
    def test_cargo_publish_calls_probe(self, mock_run):
        """CargoPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.cargo import CargoPipeline

        pl = CargoPipeline("cargo", "cargo", local=True, config={})
        pl.target = "cargo"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.go.read_go_module_path", return_value="example.com/mod")
    @patch("rlsbl.pipelines.go.run")
    def test_go_publish_calls_probe(self, mock_run, mock_modpath):
        """GoPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.go import GoPipeline

        pl = GoPipeline("go", "go", local=True, config={"install_paths": ["./cmd/x"]})
        pl.target = "go"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.pypi.run")
    def test_pypi_already_exists_treated_as_success(self, mock_run):
        """PypiPipeline treats 'File already exists' error as success."""
        import subprocess
        from rlsbl.pipelines.pypi import PypiPipeline

        pl = PypiPipeline("pypi", "pypi", local=True, config={})
        pl.target = "pypi"

        # First call (build) succeeds, second call (publish) raises
        call_count = {"n": 0}
        def run_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                exc = subprocess.CalledProcessError(
                    1, "uv publish",
                )
                exc.stderr = "File already exists: pkg-1.0.0.tar.gz"
                raise exc

        mock_run.side_effect = run_effect

        with patch.object(pl, "probe_before_publish", return_value=True), \
             patch.dict("os.environ", {"PYPI_TOKEN": "tok"}), \
             patch("sys.stdout", new_callable=StringIO) as out:
            # Should not raise
            pl.publish("/dir", "1.0.0", ctx=None)

        self.assertIn("already exists", out.getvalue())


class TestPublishedTargetsResume(unittest.TestCase):
    """Tests for published_targets tracking in release state."""

    def test_published_targets_skips_done_pipeline(self):
        """When a pipeline name is in published_targets, it is skipped."""
        from rlsbl.commands.release.release_state import (
            load_release_state, save_release_state,
        )
        import tempfile, os, json

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "in-progress.json")
            state = {
                "completed_steps": ["TAGGED", "PUSHED", "GITHUB_RELEASE"],
                "published_targets": ["pypi-pl"],
            }
            save_release_state(state_path, state)

            loaded = load_release_state(state_path)
            self.assertIn("pypi-pl", loaded["published_targets"])


class TestPypiCheckUrl(unittest.TestCase):
    """Test that PyPI pipeline passes --check-url to uv publish."""

    @patch("rlsbl.pipelines.pypi.run")
    def test_publish_passes_check_url(self, mock_run):
        from rlsbl.pipelines.pypi import PypiPipeline

        pl = PypiPipeline("pypi", "pypi", local=True, config={})
        pl.target = "pypi"

        with patch.object(pl, "probe_before_publish", return_value=True), \
             patch.dict("os.environ", {"PYPI_TOKEN": "tok"}):
            pl.publish("/dir", "1.0.0", ctx=None)

        # Find the publish call (second call, after build)
        publish_call = mock_run.call_args_list[1]
        args = publish_call[0]
        self.assertEqual(args[0], "uv")
        self.assertIn("--check-url", args[1])
        self.assertIn("https://pypi.org/simple/", args[1])


if __name__ == "__main__":
    unittest.main()
