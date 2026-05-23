"""Tests for the asset size guard.

Verifies:
- The old private hook template has been removed (asset upload is built-in).
- The get_max_asset_size_mb config accessor returns correct defaults and
  respects overrides from .rlsbl/config.json.
- A bash snippet replicating the size check passes small files and
  rejects oversized files, with both default and custom limits.
"""

import json
import os
import subprocess

import pytest

from rlsbl.config import get_max_asset_size_mb


# Minimal bash snippet that replicates the size-check logic.
# Exits 0 if all files pass, 1 if any file exceeds the limit.
_SIZE_CHECK_SCRIPT = """\
set -euo pipefail
max_size_mb="$1"
dist_dir="$2"
max_size_bytes=$((max_size_mb * 1024 * 1024))
for f in "$dist_dir"/*; do
    [ -f "$f" ] || continue
    size=$(stat --format=%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null)
    if [ "$size" -gt "$max_size_bytes" ]; then
        size_mb=$((size / 1024 / 1024))
        echo "Error: $f is ${size_mb}MB, exceeds max_asset_size_mb (${max_size_mb}MB)." >&2
        exit 1
    fi
done
"""


def _run_size_check(dist_dir, max_size_mb=2):
    """Run the size-check bash snippet against a dist directory.

    Returns the CompletedProcess so callers can inspect returncode/stderr.
    """
    return subprocess.run(
        ["bash", "-c", _SIZE_CHECK_SCRIPT, "--", str(max_size_mb), str(dist_dir)],
        capture_output=True,
        text=True,
    )


def _write_config(tmp_path, payload):
    rlsbl_dir = tmp_path / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# Private hook template removed (asset upload is built-in)
# ---------------------------------------------------------------------------


class TestPrivateHookTemplateRemoved:
    """The old post-release-private.sh.tpl no longer exists."""

    def test_template_deleted(self):
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "shared", "hooks",
            "post-release-private.sh.tpl",
        )
        assert not os.path.exists(tpl_path), (
            "post-release-private.sh.tpl should be deleted; "
            "asset upload is now a built-in release step"
        )


# ---------------------------------------------------------------------------
# Config accessor
# ---------------------------------------------------------------------------


class TestGetMaxAssetSizeMb:
    """Tests for get_max_asset_size_mb config accessor."""

    def test_default_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_max_asset_size_mb() == 2

    def test_reads_custom_value(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_config(tmp_path, {"max_asset_size_mb": 100})
        assert get_max_asset_size_mb() == 100

    def test_ignores_non_numeric(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_config(tmp_path, {"max_asset_size_mb": "big"})
        assert get_max_asset_size_mb() == 2

    def test_ignores_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_config(tmp_path, {"max_asset_size_mb": 0})
        assert get_max_asset_size_mb() == 2

    def test_ignores_negative(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_config(tmp_path, {"max_asset_size_mb": -5})
        assert get_max_asset_size_mb() == 2

    def test_float_truncated(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_config(tmp_path, {"max_asset_size_mb": 10.9})
        assert get_max_asset_size_mb() == 10


# ---------------------------------------------------------------------------
# Bash size-check logic
# ---------------------------------------------------------------------------


class TestSmallFilesPass:
    """Files under the limit should not trigger the guard."""

    def test_small_files_pass(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "pkg-1.0.0.tar.gz").write_bytes(b"x" * 1024)  # 1 KB
        (dist / "pkg-1.0.0.whl").write_bytes(b"y" * 2048)  # 2 KB

        result = _run_size_check(dist, max_size_mb=2)
        assert result.returncode == 0

    def test_empty_dist_passes(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()

        result = _run_size_check(dist, max_size_mb=2)
        assert result.returncode == 0


class TestLargeFileBlocked:
    """Files exceeding the limit should abort the check."""

    def test_large_file_blocked(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        # Create a file just over 2MB
        (dist / "huge.tar.gz").write_bytes(b"\0" * (2 * 1024 * 1024 + 1))

        result = _run_size_check(dist, max_size_mb=2)
        assert result.returncode == 1
        assert "exceeds max_asset_size_mb" in result.stderr

    def test_one_large_among_small_still_blocked(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "small.whl").write_bytes(b"x" * 100)
        (dist / "huge.tar.gz").write_bytes(b"\0" * (3 * 1024 * 1024))

        result = _run_size_check(dist, max_size_mb=2)
        assert result.returncode == 1


class TestCustomLimitFromConfig:
    """Custom max_asset_size_mb should be respected by the size check."""

    def test_50mb_file_passes_with_100mb_limit(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        # Create a sparse 50MB file (fast, no real disk usage)
        big_file = dist / "medium.tar.gz"
        with open(big_file, "wb") as f:
            f.seek(50 * 1024 * 1024)
            f.write(b"\0")

        result = _run_size_check(dist, max_size_mb=100)
        assert result.returncode == 0

    def test_150mb_file_blocked_with_100mb_limit(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        big_file = dist / "huge.tar.gz"
        with open(big_file, "wb") as f:
            f.seek(150 * 1024 * 1024)
            f.write(b"\0")

        result = _run_size_check(dist, max_size_mb=100)
        assert result.returncode == 1
        assert "exceeds max_asset_size_mb (100MB)" in result.stderr

    def test_file_exactly_at_limit_passes(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        # Exactly 2MB should NOT exceed the limit (guard uses -gt, not -ge)
        (dist / "exact.tar.gz").write_bytes(b"\0" * (2 * 1024 * 1024))

        result = _run_size_check(dist, max_size_mb=2)
        assert result.returncode == 0
