"""Tests that pipeline _publish_command methods pass cwd=dir_path to subprocess.

Phase 6.4: subdirectory targets must publish from the correct directory.
"""

from unittest.mock import patch

from rlsbl.pipelines.npm import NpmPipeline
from rlsbl.pipelines.cargo import CargoPipeline
from rlsbl.pipelines.deno import DenoPipeline


class TestNpmPublishCwd:
    """NpmPipeline._publish_command passes cwd=dir_path."""

    def test_publish_uses_cwd(self):
        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=True, config={},
        )
        with patch("rlsbl.pipelines.npm.run") as mock_run:
            pipeline._publish_command("/some/subdir", "1.0.0", "tok")
            _kwargs = mock_run.call_args[1]
            assert _kwargs.get("cwd") == "/some/subdir"


class TestCargoPublishCwd:
    """CargoPipeline._publish_command passes cwd=dir_path."""

    def test_publish_uses_cwd(self):
        pipeline = CargoPipeline(
            name="cargo", pipeline_type="cargo", local=True, config={},
        )
        with patch("rlsbl.pipelines.cargo.run") as mock_run:
            pipeline._publish_command("/cargo/subdir", "2.0.0", "tok")
            _kwargs = mock_run.call_args[1]
            assert _kwargs.get("cwd") == "/cargo/subdir"


class TestDenoPublishCwd:
    """DenoPipeline._publish_command passes cwd=dir_path."""

    def test_publish_uses_cwd(self):
        pipeline = DenoPipeline(
            name="deno", pipeline_type="deno", local=True, config={},
        )
        with patch("rlsbl.pipelines.deno.run") as mock_run:
            pipeline._publish_command("/deno/subdir", "3.0.0", "tok")
            _kwargs = mock_run.call_args[1]
            assert _kwargs.get("cwd") == "/deno/subdir"
