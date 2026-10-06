"""Tests that pipeline _publish_command methods pass cwd=dir_path to subprocess.

Subdirectory targets must publish from the correct directory.
"""

from unittest.mock import patch

from rlsbl.pipelines.npm import NpmPipeline


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
