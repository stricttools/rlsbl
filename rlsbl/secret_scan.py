"""Pre-publish secret scan gate using gitleaks.

Scans built artifacts (wheels, tarballs, archives) for leaked secrets
before any push or publish step. This is a hard, non-bypassable gate
in the release flow.
"""

import glob
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from .utils import require_tool


_GITLEAKS_INSTALL_HINT = (
    "Install gitleaks:\n"
    "  go install github.com/zricethezav/gitleaks/v8@latest\n"
    "  brew install gitleaks          (macOS)\n"
    "  sudo apt install gitleaks      (Debian/Ubuntu)\n"
    "  https://github.com/gitleaks/gitleaks#installing"
)


class SecretScanError(Exception):
    """Raised when gitleaks finds secrets in built artifacts."""


def _require_gitleaks():
    """Ensure gitleaks is available. Hard error with install instructions if missing."""
    try:
        return require_tool(
            "gitleaks",
            purpose="for pre-publish secret scanning of built artifacts",
        )
    except FileNotFoundError:
        print(
            "Error: gitleaks is required for pre-publish secret scanning "
            "but was not found on PATH.\n\n" + _GITLEAKS_INSTALL_HINT,
            file=sys.stderr,
        )
        raise


# Glob patterns for the archive types the secret scan understands. Both the
# discovery and the pre-build clean use this single source of truth.
_ARTIFACT_PATTERNS = ["*.whl", "*.tar.gz", "*.tgz", "*.zip"]


def _find_artifacts(project_dir):
    """Discover built artifacts in dist/ under the project directory.

    Returns a list of absolute paths to scannable archive files
    (.whl, .tar.gz, .tgz, .zip).
    """
    dist_dir = os.path.join(project_dir, "dist")
    if not os.path.isdir(dist_dir):
        return []

    artifacts = []
    for pattern in _ARTIFACT_PATTERNS:
        artifacts.extend(glob.glob(os.path.join(dist_dir, pattern)))
    return sorted(set(artifacts))


def clean_stale_artifacts(project_dir, log=None):
    """Remove pre-existing build artifacts from dist/ before a fresh build.

    Build tools (e.g. ``uv build``, ``npm pack``) write new artifacts into
    dist/ without removing older ones, so artifacts from previous versions
    accumulate. The secret scan would then scan artifacts that are not part of
    the current release -- stale files that could carry old secrets or slow the
    scan. Clearing matching artifact files *before* the build scopes the
    subsequent scan to exactly what this release produces.

    This uses a temporal clean (option (a)) rather than version-string
    filtering because version-in-filename conventions are unreliable across
    ecosystems: wheels normalize versions per PEP 440 (``1.0.0-rc1`` ->
    ``1.0.0rc1``) and package names (``my-pkg`` -> ``my_pkg``), npm scoped
    packages embed the scope, etc. Matching a raw version string against these
    filenames would miss or mis-match. Removing all artifacts before the build
    and letting the build repopulate dist/ is robust for every ecosystem.

    Only files matching the artifact glob patterns are removed -- never other
    dist/ contents. Each removed file is reported via ``log``. This is normal
    runtime tool behavior (not user data), so plain ``os.remove`` is used
    rather than saferm.

    Note: this scoping logic is intentionally a cohesive, standalone function
    so a later phase can relocate it to per-target build boundaries cleanly.

    Returns the list of absolute paths that were removed.
    """
    if log is None:
        def log(msg):
            print(msg)

    dist_dir = os.path.join(project_dir, "dist")
    if not os.path.isdir(dist_dir):
        return []

    removed = []
    for pattern in _ARTIFACT_PATTERNS:
        for path in glob.glob(os.path.join(dist_dir, pattern)):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass  # Best-effort: a file we cannot remove is not fatal.

    if removed:
        log(f"Cleared {len(removed)} stale artifact(s) from dist/ before build:")
        for path in sorted(removed):
            log(f"  removed {os.path.basename(path)}")
    return removed


def _unpack_artifact(artifact_path, dest_dir):
    """Unpack a single artifact into dest_dir.

    Handles .whl/.zip (zip archives) and .tar.gz/.tgz (tar archives).
    """
    if artifact_path.endswith(".whl") or artifact_path.endswith(".zip"):
        with zipfile.ZipFile(artifact_path, "r") as zf:
            zf.extractall(dest_dir)
    elif artifact_path.endswith(".tar.gz") or artifact_path.endswith(".tgz"):
        with tarfile.open(artifact_path, "r:gz") as tf:
            tf.extractall(dest_dir)
    else:
        # Unknown format -- skip silently
        return False
    return True


def _run_gitleaks(scan_dir, config_path=None):
    """Run gitleaks on a directory. Returns (exit_code, stdout, stderr).

    Uses ``gitleaks dir`` which scans files directly (no git history).
    """
    cmd = ["gitleaks", "dir", scan_dir]
    if config_path and os.path.isfile(config_path):
        cmd.extend(["--config", config_path])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def scan_artifacts_for_secrets(project_dir, log=None):
    """Scan all built artifacts in dist/ for leaked secrets.

    This is a hard gate: if gitleaks finds any secrets, the release
    is aborted. There is no bypass flag.

    Args:
        project_dir: path to the project root (dist/ is expected here).
        log: optional callable for status messages.

    Raises:
        FileNotFoundError: if gitleaks is not installed.
        SecretScanError: if secrets are found in any artifact.
    """
    if log is None:
        def log(msg):
            print(msg)

    _require_gitleaks()

    artifacts = _find_artifacts(project_dir)
    if not artifacts:
        log("Secret scan: no artifacts found in dist/, skipping")
        return

    # Check for user-owned .gitleaks.toml config
    config_path = os.path.join(project_dir, ".gitleaks.toml")
    if not os.path.isfile(config_path):
        config_path = None

    log(f"Secret scan: scanning {len(artifacts)} artifact(s)...")

    all_findings = []
    for artifact_path in artifacts:
        artifact_name = os.path.basename(artifact_path)
        tmp_dir = tempfile.mkdtemp(prefix="rlsbl-secret-scan-")
        try:
            if not _unpack_artifact(artifact_path, tmp_dir):
                continue

            exit_code, stdout, stderr = _run_gitleaks(tmp_dir, config_path)
            if exit_code != 0:
                # exit code 1 means findings, other codes mean errors
                if exit_code == 1:
                    all_findings.append((artifact_name, stdout, stderr))
                else:
                    # gitleaks error (not findings) -- treat as hard error
                    print(
                        f"Error: gitleaks failed on {artifact_name} "
                        f"(exit code {exit_code}):\n{stderr}",
                        file=sys.stderr,
                    )
                    raise SecretScanError(
                        f"gitleaks failed on {artifact_name} with exit code {exit_code}"
                    )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if all_findings:
        print(
            "\nSecret scan FAILED: secrets detected in built artifacts.\n",
            file=sys.stderr,
        )
        for artifact_name, stdout, stderr in all_findings:
            print(f"  Artifact: {artifact_name}", file=sys.stderr)
            if stdout.strip():
                for line in stdout.strip().splitlines():
                    print(f"    {line}", file=sys.stderr)
            if stderr.strip():
                for line in stderr.strip().splitlines():
                    print(f"    {line}", file=sys.stderr)
        print(
            "\nTo allowlist false positives, create a .gitleaks.toml at the "
            "project root with rule-id or regex-based allowlist entries.\n"
            "See: https://github.com/gitleaks/gitleaks#configuration\n"
            "\nNote: use rule-id or regex-based allowlisting, not path-based "
            "(paths inside archives differ from source paths).",
            file=sys.stderr,
        )
        raise SecretScanError(
            f"gitleaks found secrets in {len(all_findings)} artifact(s)"
        )

    log("Secret scan: all artifacts clean")
