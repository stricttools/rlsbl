"""Tests for the pre-publish secret scan gate (gitleaks-based artifact scanning)."""

import os
import shutil
import subprocess
import zipfile

import pytest

from rlsbl.secret_scan import (
    SecretScanError,
    _find_artifacts,
    _require_gitleaks,
    _run_gitleaks,
    _unpack_artifact,
    scan_artifacts_for_secrets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FAKE_PRIVATE_KEY = """\
-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA04up8hoqzS1+APIB0RhjXyObwHQnOzhAk4Aq3lz2DOofLGz
jTCFcWR0di0PQNF7y3aAlmDaXUIE8MwXPhf3iOHL5GBDAVljHc7E5LjMFz8SFnp
djznRh3SFnowigQN6JMm4kOBlvP/wJMlWQkfIpD1M6LxCaNVheRwuS9fF/Q7qQiB
N0FEOElrxVY0Yh5DLMJR0OPHY3LkB49BuE3hyhiQIy0G0ilT0fT+JnvOTWa3E2G
hzBPDllhXFaR0E3eRCchQYIcLG5McIgPIBaRX2KZBSF0NyNDzGajTgz4JUcOGSN3
kVZe5WFnxelT3CM1Svds8L5bK0pF6dW4oanTYwIDAQABAKCAQBzCPw0JPIj2qMvJ
4dmEGGKbkmU5FHJwf1BLGhNO+d20h3LlxWEaYSqjB5wP+F5QG6dL3bVfpBLGTH9N
/bfhDhNXX3kB3s7VoEAHWnPD8C5FpF0MRoLGGKMDrWL5nD+7rON0HnM7B8ZJ4rTE
wG8Z7BchV3pQG2EOJFpR8kVKqILJjm5i6v3i9sY7Dn0vZUOG5ryGXBhx3r7h9vJh
PlkwBiQDTbKE/MlD7LQCbGdj+Y12jRhJdxqGHOCBM5F9b7xjhXaCZJ6gIGFp5tm3
BHcZkuJdNqObJfF2fkBPER5F8pJbpOmXZ2y7hIBXI+p7d5W0Zk5TIQRQzA5f8iab
-----END RSA PRIVATE KEY-----
"""


def _make_wheel_with_secret(dist_dir, filename="fakepkg-1.0.0-py3-none-any.whl"):
    """Create a minimal .whl (zip) containing a file with a private key."""
    os.makedirs(dist_dir, exist_ok=True)
    whl_path = os.path.join(dist_dir, filename)
    with zipfile.ZipFile(whl_path, "w") as zf:
        # A file with an RSA private key embedded (detected by gitleaks)
        zf.writestr("fakepkg/key.pem", _FAKE_PRIVATE_KEY)
        zf.writestr(
            "fakepkg/__init__.py",
            '__version__ = "1.0.0"\n',
        )
    return whl_path


def _make_clean_wheel(dist_dir, filename="cleanpkg-1.0.0-py3-none-any.whl"):
    """Create a minimal .whl (zip) with no secrets."""
    os.makedirs(dist_dir, exist_ok=True)
    whl_path = os.path.join(dist_dir, filename)
    with zipfile.ZipFile(whl_path, "w") as zf:
        zf.writestr(
            "cleanpkg/__init__.py",
            '__version__ = "1.0.0"\n',
        )
        zf.writestr(
            "cleanpkg/main.py",
            'def hello():\n    return "hello world"\n',
        )
    return whl_path


def _gitleaks_available():
    """Check if gitleaks is installed."""
    return shutil.which("gitleaks") is not None


skip_no_gitleaks = pytest.mark.skipif(
    not _gitleaks_available(),
    reason="gitleaks not installed",
)


# ---------------------------------------------------------------------------
# Test: missing gitleaks binary -> hard error with install instructions
# ---------------------------------------------------------------------------


class TestRequireGitleaks:
    def test_missing_gitleaks_raises_with_install_hint(self, monkeypatch):
        """When gitleaks is not on PATH, a FileNotFoundError is raised
        with install instructions."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(FileNotFoundError, match="gitleaks"):
            _require_gitleaks()

    def test_gitleaks_present_returns_path(self, monkeypatch):
        """When gitleaks is on PATH, returns the resolved path."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        result = _require_gitleaks()
        assert result == "/usr/bin/gitleaks"


# ---------------------------------------------------------------------------
# Test: no artifacts to scan -> passes (nothing to check)
# ---------------------------------------------------------------------------


class TestNoArtifacts:
    def test_no_dist_dir(self, tmp_path, monkeypatch):
        """When dist/ doesn't exist, scan passes immediately."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        messages = []
        scan_artifacts_for_secrets(str(tmp_path), log=messages.append)
        assert any("no artifacts" in m.lower() for m in messages)

    def test_empty_dist_dir(self, tmp_path, monkeypatch):
        """When dist/ exists but contains no scannable files, scan passes."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        (tmp_path / "dist").mkdir()
        messages = []
        scan_artifacts_for_secrets(str(tmp_path), log=messages.append)
        assert any("no artifacts" in m.lower() for m in messages)


# ---------------------------------------------------------------------------
# Test: artifact discovery
# ---------------------------------------------------------------------------


class TestFindArtifacts:
    def test_finds_wheels(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "pkg-1.0.0-py3-none-any.whl").write_bytes(b"")
        result = _find_artifacts(str(tmp_path))
        assert len(result) == 1
        assert result[0].endswith(".whl")

    def test_finds_tar_gz(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "pkg-1.0.0.tar.gz").write_bytes(b"")
        result = _find_artifacts(str(tmp_path))
        assert len(result) == 1
        assert result[0].endswith(".tar.gz")

    def test_finds_tgz(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "pkg-1.0.0.tgz").write_bytes(b"")
        result = _find_artifacts(str(tmp_path))
        assert len(result) == 1

    def test_finds_zip(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "pkg-1.0.0.zip").write_bytes(b"")
        result = _find_artifacts(str(tmp_path))
        assert len(result) == 1

    def test_ignores_non_archive_files(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "notes.txt").write_bytes(b"")
        (dist / "pkg-1.0.0-py3-none-any.whl").write_bytes(b"")
        result = _find_artifacts(str(tmp_path))
        assert len(result) == 1

    def test_no_dist_returns_empty(self, tmp_path):
        result = _find_artifacts(str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# Test: unpacking artifacts
# ---------------------------------------------------------------------------


class TestUnpackArtifact:
    def test_unpack_wheel(self, tmp_path):
        dist = tmp_path / "dist"
        whl = _make_clean_wheel(str(dist))
        dest = tmp_path / "unpacked"
        dest.mkdir()
        result = _unpack_artifact(whl, str(dest))
        assert result is True
        assert (dest / "cleanpkg" / "__init__.py").exists()

    def test_unpack_unknown_format_returns_false(self, tmp_path):
        fake = tmp_path / "something.dat"
        fake.write_bytes(b"data")
        dest = tmp_path / "unpacked"
        dest.mkdir()
        result = _unpack_artifact(str(fake), str(dest))
        assert result is False


# ---------------------------------------------------------------------------
# Integration tests requiring gitleaks
# ---------------------------------------------------------------------------


@skip_no_gitleaks
class TestSecretScanIntegration:
    """Integration tests that require gitleaks to be installed."""

    def test_wheel_with_secret_trips_gate(self, tmp_path):
        """A wheel containing a fake AWS key triggers SecretScanError."""
        _make_wheel_with_secret(str(tmp_path / "dist"))
        with pytest.raises(SecretScanError, match="secrets"):
            scan_artifacts_for_secrets(str(tmp_path))

    def test_clean_wheel_passes(self, tmp_path):
        """A wheel with no secrets passes the gate."""
        _make_clean_wheel(str(tmp_path / "dist"))
        messages = []
        scan_artifacts_for_secrets(str(tmp_path), log=messages.append)
        assert any("clean" in m.lower() for m in messages)

    def test_allowlist_makes_gate_pass(self, tmp_path):
        """A .gitleaks.toml allowlisting the fixture's rule lets the gate pass."""
        _make_wheel_with_secret(str(tmp_path / "dist"))

        # Write a .gitleaks.toml that allowlists the private key by regex
        gitleaks_config = tmp_path / ".gitleaks.toml"
        gitleaks_config.write_text(
            '[allowlist]\n'
            'description = "Allow test private keys"\n'
            'regexes = ["-----BEGIN RSA PRIVATE KEY-----"]\n'
        )

        messages = []
        scan_artifacts_for_secrets(str(tmp_path), log=messages.append)
        assert any("clean" in m.lower() for m in messages)

    def test_gitleaks_dir_scan_directly(self, tmp_path):
        """Directly test _run_gitleaks on a directory with a secret."""
        secret_dir = tmp_path / "scanme"
        secret_dir.mkdir()
        (secret_dir / "leaked.pem").write_text(_FAKE_PRIVATE_KEY)
        exit_code, stdout, stderr = _run_gitleaks(str(secret_dir))
        # gitleaks exit code 1 means findings
        assert exit_code == 1

    def test_gitleaks_clean_dir(self, tmp_path):
        """Directly test _run_gitleaks on a clean directory."""
        clean_dir = tmp_path / "clean"
        clean_dir.mkdir()
        (clean_dir / "hello.py").write_text('print("hello")\n')
        exit_code, stdout, stderr = _run_gitleaks(str(clean_dir))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Test: scan_artifacts_for_secrets with mocked gitleaks
# ---------------------------------------------------------------------------


class TestScanArtifactsMocked:
    """Tests using a mocked gitleaks binary (no real gitleaks needed)."""

    def test_missing_gitleaks_hard_error(self, tmp_path, monkeypatch):
        """When gitleaks is missing, scan raises FileNotFoundError."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        # Need at least one artifact to trigger the require_tool check
        _make_clean_wheel(str(tmp_path / "dist"))
        with pytest.raises(FileNotFoundError, match="gitleaks"):
            scan_artifacts_for_secrets(str(tmp_path))

    def test_gitleaks_error_exit_code_raises(self, tmp_path, monkeypatch):
        """When gitleaks exits with code > 1 (error, not findings), raise."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        _make_clean_wheel(str(tmp_path / "dist"))

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=2, stdout="", stderr="gitleaks crashed"
            )

        monkeypatch.setattr(
            "rlsbl.secret_scan.subprocess.run", fake_run
        )
        with pytest.raises(SecretScanError, match="exit code 2"):
            scan_artifacts_for_secrets(str(tmp_path))

    def test_gitleaks_findings_exit_code_1(self, tmp_path, monkeypatch):
        """When gitleaks exits with code 1 (findings), raise SecretScanError."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        _make_clean_wheel(str(tmp_path / "dist"))

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="Finding: AWS key found in config.py",
                stderr="",
            )

        monkeypatch.setattr(
            "rlsbl.secret_scan.subprocess.run", fake_run
        )
        with pytest.raises(SecretScanError, match="secrets"):
            scan_artifacts_for_secrets(str(tmp_path))

    def test_gitleaks_clean_exit_code_0(self, tmp_path, monkeypatch):
        """When gitleaks exits with code 0 (no findings), pass."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        _make_clean_wheel(str(tmp_path / "dist"))

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(
            "rlsbl.secret_scan.subprocess.run", fake_run
        )
        messages = []
        scan_artifacts_for_secrets(str(tmp_path), log=messages.append)
        assert any("clean" in m.lower() for m in messages)

    def test_config_path_passed_when_present(self, tmp_path, monkeypatch):
        """When .gitleaks.toml exists, --config is passed to gitleaks."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        _make_clean_wheel(str(tmp_path / "dist"))
        (tmp_path / ".gitleaks.toml").write_text("[allowlist]\n")

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(
            "rlsbl.secret_scan.subprocess.run", fake_run
        )
        scan_artifacts_for_secrets(str(tmp_path))
        assert len(captured_cmds) == 1
        assert "--config" in captured_cmds[0]
        # The config path should be the .gitleaks.toml file
        config_idx = captured_cmds[0].index("--config")
        assert captured_cmds[0][config_idx + 1].endswith(".gitleaks.toml")

    def test_no_config_file_no_config_flag(self, tmp_path, monkeypatch):
        """When .gitleaks.toml doesn't exist, --config is not passed."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks")
        _make_clean_wheel(str(tmp_path / "dist"))

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(
            "rlsbl.secret_scan.subprocess.run", fake_run
        )
        scan_artifacts_for_secrets(str(tmp_path))
        assert len(captured_cmds) == 1
        assert "--config" not in captured_cmds[0]


class TestCleanStaleArtifacts:
    """Temporal scoping: dist/ is cleared of matching artifacts before the
    build so the scan sees only the current release's output."""

    def test_removes_matching_artifacts(self, tmp_path, capsys):
        """All artifact-pattern files are removed; non-artifact files survive."""
        from rlsbl.secret_scan import clean_stale_artifacts

        dist = tmp_path / "dist"
        dist.mkdir()
        stale_whl = dist / "fakepkg-0.9.0-py3-none-any.whl"
        stale_sdist = dist / "fakepkg-0.9.0.tar.gz"
        stale_tgz = dist / "fakepkg-0.9.0.tgz"
        stale_zip = dist / "fakepkg-0.9.0.zip"
        for f in (stale_whl, stale_sdist, stale_tgz, stale_zip):
            f.write_bytes(b"stale")
        # Non-artifact file that must NOT be touched.
        keeper = dist / "build-notes.txt"
        keeper.write_text("keep me")

        removed = clean_stale_artifacts(str(tmp_path))

        for f in (stale_whl, stale_sdist, stale_tgz, stale_zip):
            assert not f.exists(), f"{f.name} (artifact) must be removed"
        assert keeper.exists(), "non-artifact file must be preserved"
        assert set(removed) == {
            str(stale_whl), str(stale_sdist), str(stale_tgz), str(stale_zip)
        }

        out = capsys.readouterr().out
        assert "stale artifact" in out.lower()
        assert "fakepkg-0.9.0-py3-none-any.whl" in out

    def test_no_dist_dir_is_noop(self, tmp_path):
        """Missing dist/ returns [] without error."""
        from rlsbl.secret_scan import clean_stale_artifacts

        assert clean_stale_artifacts(str(tmp_path)) == []

    def test_scan_scoped_to_fresh_after_clean(self, tmp_path):
        """After clearing stale artifacts and (simulated) rebuilding, only the
        fresh artifact is discovered by the scanner."""
        from rlsbl.secret_scan import clean_stale_artifacts, _find_artifacts

        dist = tmp_path / "dist"
        dist.mkdir()
        stale = dist / "fakepkg-0.9.0-py3-none-any.whl"
        stale.write_bytes(b"stale")

        clean_stale_artifacts(str(tmp_path))

        # Simulate the build repopulating dist/ with the current version.
        fresh = dist / "fakepkg-1.0.0-py3-none-any.whl"
        fresh.write_bytes(b"fresh")

        assert _find_artifacts(str(tmp_path)) == [str(fresh)], (
            "only the freshly built artifact should be discoverable"
        )
