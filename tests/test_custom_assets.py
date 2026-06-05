"""Tests for BasePipeline.build_custom_assets()."""

import os

import pytest

from rlsbl.pipelines.base import BasePipeline


def _pipeline(custom_assets, max_asset_size_mb=100):
    """Create a BasePipeline with custom_assets config."""
    config = {}
    if custom_assets is not None:
        config["custom_assets"] = custom_assets
    if max_asset_size_mb is not None:
        config["max_asset_size_mb"] = max_asset_size_mb
    return BasePipeline(name="test", pipeline_type="base", local=True, config=config)


class TestBuildCustomAssetsSuccess:
    """Successful custom asset builds."""

    def test_simple_build_creates_file(self, tmp_path):
        dist = str(tmp_path / "dist")
        p = _pipeline([
            {"name": "output.txt", "build": f"echo hello > {tmp_path}/dist/output.txt"},
        ])
        result = p.build_custom_assets(dist)
        assert len(result) == 1
        assert result[0] == os.path.join(dist, "output.txt")
        assert os.path.isfile(result[0])

    def test_multiple_assets(self, tmp_path):
        dist = str(tmp_path / "dist")
        p = _pipeline([
            {"name": "a.txt", "build": f"echo a > {tmp_path}/dist/a.txt"},
            {"name": "b.txt", "build": f"echo b > {tmp_path}/dist/b.txt"},
        ])
        result = p.build_custom_assets(dist)
        assert len(result) == 2
        assert result[0] == os.path.join(dist, "a.txt")
        assert result[1] == os.path.join(dist, "b.txt")

    def test_creates_dist_dir(self, tmp_path):
        dist = str(tmp_path / "nonexistent" / "dist")
        p = _pipeline([
            {"name": "out.txt", "build": f"echo x > {dist}/out.txt"},
        ])
        result = p.build_custom_assets(dist)
        assert os.path.isdir(dist)
        assert len(result) == 1


class TestBuildCustomAssetsEmpty:
    """Empty or absent custom_assets returns empty list."""

    def test_empty_list_returns_empty(self, tmp_path):
        dist = str(tmp_path / "dist")
        p = _pipeline([])
        result = p.build_custom_assets(dist)
        assert result == []

    def test_no_custom_assets_key_returns_empty(self, tmp_path):
        dist = str(tmp_path / "dist")
        p = _pipeline(None)
        result = p.build_custom_assets(dist)
        assert result == []


class TestBuildCustomAssetsFailingCommand:
    """Build command with non-zero exit code is a hard error."""

    def test_failing_command_exits(self, tmp_path):
        dist = str(tmp_path / "dist")
        p = _pipeline([
            {"name": "out.txt", "build": "false"},
        ])
        with pytest.raises(SystemExit) as exc_info:
            p.build_custom_assets(dist)
        assert exc_info.value.code == 1

    def test_failing_command_error_message(self, tmp_path, capsys):
        dist = str(tmp_path / "dist")
        p = _pipeline([
            {"name": "out.txt", "build": "exit 42"},
        ])
        with pytest.raises(SystemExit):
            p.build_custom_assets(dist)
        err = capsys.readouterr().err
        assert "custom asset 'out.txt' build command failed" in err
        assert "exit code 42" in err


class TestBuildCustomAssetsMissingOutput:
    """Build command succeeds but output file is missing -- hard error."""

    def test_missing_output_exits(self, tmp_path):
        dist = str(tmp_path / "dist")
        os.makedirs(dist, exist_ok=True)
        p = _pipeline([
            {"name": "missing.bin", "build": "true"},
        ])
        with pytest.raises(SystemExit) as exc_info:
            p.build_custom_assets(dist)
        assert exc_info.value.code == 1

    def test_missing_output_error_message(self, tmp_path, capsys):
        dist = str(tmp_path / "dist")
        os.makedirs(dist, exist_ok=True)
        p = _pipeline([
            {"name": "missing.bin", "build": "true"},
        ])
        with pytest.raises(SystemExit):
            p.build_custom_assets(dist)
        err = capsys.readouterr().err
        assert "custom asset 'missing.bin'" in err
        assert "output file not found" in err


class TestBuildCustomAssetsOversized:
    """Output file exceeds max_asset_size_mb -- hard error."""

    def test_oversized_file_exits(self, tmp_path):
        dist = str(tmp_path / "dist")
        os.makedirs(dist, exist_ok=True)
        # Create a file that exceeds 1MB limit
        p = _pipeline(
            [{"name": "big.bin", "build": f"dd if=/dev/zero of={dist}/big.bin bs=1024 count=1100 2>/dev/null"}],
            max_asset_size_mb=1,
        )
        with pytest.raises(SystemExit) as exc_info:
            p.build_custom_assets(dist)
        assert exc_info.value.code == 1

    def test_oversized_file_error_message(self, tmp_path, capsys):
        dist = str(tmp_path / "dist")
        os.makedirs(dist, exist_ok=True)
        p = _pipeline(
            [{"name": "big.bin", "build": f"dd if=/dev/zero of={dist}/big.bin bs=1024 count=1100 2>/dev/null"}],
            max_asset_size_mb=1,
        )
        with pytest.raises(SystemExit):
            p.build_custom_assets(dist)
        err = capsys.readouterr().err
        assert "custom asset 'big.bin'" in err
        assert "exceeds max_asset_size_mb (1MB)" in err

    def test_file_at_limit_passes(self, tmp_path):
        dist = str(tmp_path / "dist")
        os.makedirs(dist, exist_ok=True)
        # Create a file exactly at 1MB (1048576 bytes) -- should pass (> not >=)
        p = _pipeline(
            [{"name": "exact.bin", "build": f"dd if=/dev/zero of={dist}/exact.bin bs=1048576 count=1 2>/dev/null"}],
            max_asset_size_mb=1,
        )
        result = p.build_custom_assets(dist)
        assert len(result) == 1
