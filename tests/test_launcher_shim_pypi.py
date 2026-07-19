"""Tests for the pypi launcher first-run shim template (Phase 6.3).

Covers:
- Template content assertions (platform mapping, checksum verification,
  os.exec passthrough, zero runtime deps).
- Cache-path logic per platform (monkeypatched platform/env).
- Checksum verification: accepts a good asset, rejects a corrupted one.
- Extraction yields the expected binary.
"""

import hashlib
import importlib.util
import os
import tarfile

import pytest

from rlsbl.commands.init_cmd import process_template


def _templates_root():
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates",
    )


def _render(vars_dict):
    path = os.path.join(_templates_root(), "pypi", "shim-launcher.py.tpl")
    with open(path) as f:
        raw = f.read()
    content, _ = process_template(raw, vars_dict, template_path=path)
    return content


def _load_shim(tmp_path, vars_dict):
    content = _render(vars_dict)
    mod_path = tmp_path / "shim_launcher_mod.py"
    mod_path.write_text(content)
    spec = importlib.util.spec_from_file_location("shim_launcher_mod", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_VARS = {
    "githubRepo": "acme/mytool",
    "assetProject": "mytool",
    "binaryName": "mycli",
    "distName": "mycli",
}


# ---------------------------------------------------------------------------
# Template content
# ---------------------------------------------------------------------------


class TestPypiShimContent:
    def test_maps_platforms(self):
        content = _render(_VARS)
        for token in ("linux", "darwin", "windows", "amd64", "arm64"):
            assert token in content

    def test_verifies_checksums(self):
        content = _render(_VARS)
        assert "checksums.txt" in content
        assert "sha256" in content and "verify_checksum" in content

    def test_execs_binary(self):
        content = _render(_VARS)
        assert "os.execv" in content

    def test_zero_runtime_deps(self):
        """Only Python stdlib is imported."""
        content = _render(_VARS)
        import re

        imports = set(re.findall(r"^\s*import (\w+)", content, re.MULTILINE))
        imports |= set(re.findall(r"^\s*from (\w+)", content, re.MULTILINE))
        stdlib = {
            "hashlib", "os", "platform", "sys", "tarfile", "tempfile",
            "urllib", "zipfile", "subprocess", "importlib", "pathlib",
        }
        assert imports <= stdlib, f"unexpected deps: {imports - stdlib}"


# ---------------------------------------------------------------------------
# Cache-path logic
# ---------------------------------------------------------------------------


class TestPypiCachePath:
    def test_linux_xdg(self, tmp_path, monkeypatch):
        mod = _load_shim(tmp_path, _VARS)
        monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
        monkeypatch.setenv("XDG_CACHE_HOME", "/xdg/cache")
        assert mod.cache_dir() == mod.Path("/xdg/cache") / "mycli"

    def test_linux_default(self, tmp_path, monkeypatch):
        mod = _load_shim(tmp_path, _VARS)
        monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert mod.cache_dir() == mod.Path.home() / ".cache" / "mycli"

    def test_macos(self, tmp_path, monkeypatch):
        mod = _load_shim(tmp_path, _VARS)
        monkeypatch.setattr(mod.platform, "system", lambda: "Darwin")
        assert mod.cache_dir() == mod.Path.home() / "Library" / "Caches" / "mycli"

    def test_windows(self, tmp_path, monkeypatch):
        mod = _load_shim(tmp_path, _VARS)
        monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
        assert mod.cache_dir() == mod.Path(r"C:\Users\me\AppData\Local") / "mycli"


# ---------------------------------------------------------------------------
# Checksum verification + extraction
# ---------------------------------------------------------------------------


class TestPypiVerification:
    def _build_asset(self, tmp_path, asset_project, version):
        asset_name = f"{asset_project}_{version}_linux_amd64.tar.gz"
        bin_src = tmp_path / asset_project
        bin_src.write_text("#!/bin/sh\necho hi\n")
        archive = tmp_path / asset_name
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(bin_src, arcname=asset_project)
        bin_src.unlink()
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksums = f"{digest}  {asset_name}\n"
        return asset_name, archive, checksums

    def test_verify_good(self, tmp_path):
        mod = _load_shim(tmp_path, _VARS)
        asset_name, archive, checksums = self._build_asset(tmp_path, "mytool", "1.2.3")
        assert mod.verify_checksum(archive.read_bytes(), checksums, asset_name)

    def test_reject_corrupted(self, tmp_path):
        mod = _load_shim(tmp_path, _VARS)
        asset_name, archive, checksums = self._build_asset(tmp_path, "mytool", "1.2.3")
        with pytest.raises(RuntimeError, match="[Cc]hecksum mismatch"):
            mod.verify_checksum(archive.read_bytes() + b"x", checksums, asset_name)

    def test_reject_missing_entry(self, tmp_path):
        mod = _load_shim(tmp_path, _VARS)
        with pytest.raises(RuntimeError, match="No checksum entry"):
            mod.verify_checksum(b"data", "abc  other.tar.gz\n", "mytool_1_linux_amd64.tar.gz")

    def test_extract_binary(self, tmp_path, monkeypatch):
        mod = _load_shim(tmp_path, _VARS)
        monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
        _, archive, _ = self._build_asset(tmp_path, "mytool", "1.2.3")
        dest = tmp_path / "dest"
        binary = mod._extract_binary(archive, dest, "tar.gz")
        assert binary.exists()
        assert binary.name == "mytool"
