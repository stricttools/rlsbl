"""Tests for the npm launcher postinstall/bin shim templates (Phase 6.2).

Covers:
- Template content assertions (platform mapping, checksum verification,
  zero runtime deps, exec stub).
- A functional test that renders the postinstall shim and runs it under
  Node against a locally-built release asset, proving it VERIFIES a good
  asset's checksum and REJECTS a corrupted one, and that extraction yields
  the expected binary.
"""

import hashlib
import os
import shutil
import subprocess
import tarfile

import pytest

from rlsbl.commands.init_cmd import process_template


def _templates_root():
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates",
    )


def _render(template_rel, vars_dict):
    path = os.path.join(_templates_root(), template_rel)
    with open(path) as f:
        raw = f.read()
    content, _ = process_template(raw, vars_dict, template_path=path)
    return content


# ---------------------------------------------------------------------------
# Template content
# ---------------------------------------------------------------------------


class TestNpmShimContent:
    def test_postinstall_maps_platforms(self):
        content = _render("npm/shim-postinstall.cjs.tpl", {})
        # goreleaser os/arch naming.
        for token in ("linux", "darwin", "windows", "amd64", "arm64"):
            assert token in content

    def test_postinstall_verifies_checksums(self):
        content = _render("npm/shim-postinstall.cjs.tpl", {})
        assert "checksums.txt" in content
        assert "createHash" in content and "sha256" in content
        assert "verifyChecksum" in content

    def test_postinstall_zero_runtime_deps(self):
        """Only Node stdlib modules are required."""
        content = _render("npm/shim-postinstall.cjs.tpl", {})
        # No bare (non-relative, non-node:) require of a third-party package.
        import re

        requires = re.findall(r"require\(\"([^\"]+)\"\)", content)
        allowed = {"fs", "os", "path", "https", "crypto", "child_process"}
        for mod in requires:
            if mod.startswith(".") or mod.startswith("/"):
                continue
            assert mod in allowed, f"unexpected runtime dep: {mod}"

    def test_bin_stub_execs_binary(self):
        content = _render("npm/shim-bin.cjs.tpl", {"binaryName": "x", "assetProject": "x"})
        assert "spawnSync" in content
        assert "process.argv.slice(2)" in content


# ---------------------------------------------------------------------------
# Functional: verify good asset, reject corrupted
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")
_TAR = shutil.which("tar")


@pytest.mark.skipif(_NODE is None or _TAR is None, reason="node/tar not available")
class TestNpmShimFunctional:
    def _build_release(self, tmp_path, asset_project, version):
        """Build a fake goreleaser release asset + checksums.txt locally."""
        goos = "linux"
        goarch = "amd64"
        asset_name = f"{asset_project}_{version}_{goos}_{goarch}.tar.gz"
        # The archive contains a binary file named after the project.
        bin_src = tmp_path / asset_project
        bin_src.write_text("#!/bin/sh\necho hi\n")
        archive = tmp_path / asset_name
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(bin_src, arcname=asset_project)
        bin_src.unlink()
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksums = tmp_path / "checksums.txt"
        checksums.write_text(f"{digest}  {asset_name}\n")
        return asset_name, archive, checksums

    def test_verify_good_and_reject_corrupted(self, tmp_path):
        asset_project = "mytool"
        binary_name = "mycli"
        version = "1.2.3"

        # Render the shim into a package layout.
        pkg_dir = tmp_path / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True)
        (pkg_dir / "package.json").write_text(
            '{"name": "mycli", "version": "1.2.3"}\n'
        )
        shim = _render(
            "npm/shim-postinstall.cjs.tpl",
            {
                "githubRepo": "acme/mytool",
                "assetProject": asset_project,
                "binaryName": binary_name,
            },
        )
        shim_path = pkg_dir / "scripts" / "postinstall.cjs"
        shim_path.write_text(shim)

        asset_name, archive, checksums = self._build_release(
            tmp_path, asset_project, version
        )

        harness = tmp_path / "harness.cjs"
        harness.write_text(
            """
const fs = require('fs');
const path = require('path');
const shim = require(process.argv[2]);
const asset = fs.readFileSync(process.argv[3]);
const checksums = fs.readFileSync(process.argv[4], 'utf8');
const name = process.argv[5];
shim.verifyChecksum(asset, checksums, name);
console.log('GOOD_OK');
const bad = Buffer.concat([asset, Buffer.from('tampered')]);
try { shim.verifyChecksum(bad, checksums, name); console.log('BAD_ACCEPTED'); }
catch (e) { console.log('BAD_REJECTED'); }
const binPath = shim.installedBinaryPath();
shim.extractBinary(process.argv[3], path.dirname(binPath), 'tar.gz');
console.log(fs.existsSync(binPath) ? 'BINARY_PRESENT' : 'BINARY_MISSING');
"""
        )
        result = subprocess.run(
            [_NODE, str(harness), str(shim_path), str(archive),
             str(checksums), asset_name],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "GOOD_OK" in out
        assert "BAD_REJECTED" in out
        assert "BINARY_PRESENT" in out
