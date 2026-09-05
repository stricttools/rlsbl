"""End-to-end launcher scaffold test (Phase 6.5).

Scaffolds a launcher-configured fixture project (Go binary producer + npm
launcher wrapper) end to end and proves the resulting package carries
working, verifying download logic:
- the shim source files are present,
- the launcher manifest is filled (bin/scripts.postinstall/files),
- the publish workflow has BOTH the binary-asset and checksums.txt probes,
- the shims are recorded in managed-files.json (orphan sweep won't delete
  them on the next scaffold),
- a second scaffold is a no-op for the manifest.
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch


from rlsbl.commands.init_cmd import run_cmd_multi
from rlsbl.context import ProjectContext


def _write_fixture(root: Path, download="postinstall"):
    (root / "go.mod").write_text("module github.com/acme/mytool\n\ngo 1.23\n")
    (root / "main.go").write_text(
        "package main\n\nfunc main() {}\n"
    )
    (root / "VERSION").write_text("0.1.0\n")
    # User-authored launcher manifest -- the name authority. scaffold never
    # invents this.
    (root / "package.json").write_text(
        json.dumps({"name": "mytool", "version": "0.1.0"}, indent=2) + "\n"
    )
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    config = {
        "publish_mode": "ci",
        "targets": [
            {"name": "go", "path": "."},
            {"name": "npm", "path": "."},
        ],
        "pipelines": {
            "go": {"type": "go", "local": False, "target": "go",
                   "artifact": "binary"},
            "npm": {"type": "npm", "local": False, "target": "npm",
                    "artifact": "launcher", "wraps": "go",
                    "binary_source": "github-release", "provenance": True,
                    "download": download},
        },
    }
    (rlsbl_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")


def _scaffold(root):
    ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={})
    with patch("sys.stdout", new_callable=StringIO):
        # flags: skip tagging to keep the test hermetic.
        run_cmd_multi(["go", "npm"], [], {"no-tag": True}, ctx=ctx)


class TestLauncherEndToEnd:
    def test_scaffold_produces_verifying_launcher(self, mock_git_repo):
        root = mock_git_repo
        _write_fixture(root)

        _scaffold(root)

        # 1. Shim source files present.
        postinstall = root / "scripts" / "postinstall.cjs"
        binstub = root / "bin" / "launcher.cjs"
        assert postinstall.exists(), "postinstall shim missing"
        assert binstub.exists(), "bin stub missing"

        pi_text = postinstall.read_text()
        # Producer info baked in + checksum verification.
        assert "acme/mytool" in pi_text
        assert "checksums.txt" in pi_text
        assert "verifyChecksum" in pi_text
        # No leftover template placeholders.
        assert "{{" not in pi_text

        # 2. Launcher manifest filled (name preserved).
        pkg = json.loads((root / "package.json").read_text())
        assert pkg["name"] == "mytool"
        assert pkg["bin"] == {"mytool": "bin/launcher.cjs"}
        assert pkg["scripts"]["postinstall"] == "node scripts/postinstall.cjs"
        assert "bin" in pkg["files"] and "scripts" in pkg["files"]

        # 3. Publish workflow has both probes + needs gate.
        publish = (root / ".github" / "workflows" / "publish.yml").read_text()
        assert "Verify binary asset exists" in publish
        assert "checksums.txt" in publish
        assert "gate" in publish

        # 4. Shims recorded in managed-files.json.
        managed = json.loads((root / ".rlsbl" / "managed-files.json").read_text())
        tracked = managed.get("files", managed)
        assert "scripts/postinstall.cjs" in tracked
        assert "bin/launcher.cjs" in tracked

        # 5. Second scaffold: manifest is a byte-level no-op.
        before = (root / "package.json").read_bytes()
        _scaffold(root)
        assert (root / "package.json").read_bytes() == before
        # Shims still present after the orphan sweep.
        assert postinstall.exists()
        assert binstub.exists()

    def test_scaffold_produces_first_run_launcher(self, mock_git_repo):
        root = mock_git_repo
        _write_fixture(root, download="first-run")

        _scaffold(root)

        # 1. First-run bin stub present; NO postinstall script emitted.
        binstub = root / "bin" / "launcher.cjs"
        postinstall = root / "scripts" / "postinstall.cjs"
        assert binstub.exists(), "first-run bin stub missing"
        assert not postinstall.exists(), (
            "first-run mode must not emit a postinstall script"
        )

        stub_text = binstub.read_text()
        # Producer info baked in + checksum verification + cache dir + exec.
        assert "acme/mytool" in stub_text
        assert "checksums.txt" in stub_text
        assert "verifyChecksum" in stub_text
        assert "XDG_CACHE_HOME" in stub_text
        assert "spawnSync" in stub_text
        assert "{{" not in stub_text

        # 2. Manifest filled: bin present, NO postinstall script (zero
        # network I/O at install time -- the mode's contract).
        pkg = json.loads((root / "package.json").read_text())
        assert pkg["name"] == "mytool"
        assert pkg["bin"] == {"mytool": "bin/launcher.cjs"}
        assert "postinstall" not in pkg.get("scripts", {})
        assert pkg["files"] == ["bin"]

        # 3. Bin stub recorded in managed-files.json.
        managed = json.loads((root / ".rlsbl" / "managed-files.json").read_text())
        tracked = managed.get("files", managed)
        assert "bin/launcher.cjs" in tracked
        assert "scripts/postinstall.cjs" not in tracked

        # 4. Second scaffold: manifest is a byte-level no-op.
        before = (root / "package.json").read_bytes()
        _scaffold(root)
        assert (root / "package.json").read_bytes() == before
        assert binstub.exists()
