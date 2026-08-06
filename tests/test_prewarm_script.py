"""Executable tests for ``scripts/test-prewarm.sh``'s module-cache gate.

The pre-warm runs OUTSIDE the sandbox and decides one thing: whether the
sandbox will have a real safegit binary at all. Two ways it can get that
wrong, both silent (the suite still passes -- it just SKIPS the 18
real-binary safegit e2e tests):

1. **Partial module-cache entry.** An interrupted ``go install`` leaves
   ``<pin>.info`` in the download cache without the ``.mod``/``.zip``. Gating
   on the ``.info`` alone reports "already cached", skips the fetch, and the
   sandbox's offline ``file://`` GOPROXY then cannot serve the module.

2. **Deleting the fallback before verifying the replacement.** The staged
   locally-built binary is the only stand-in when the module route fails.
   Removing it on the strength of a cache-directory listing -- rather than a
   verified install -- can leave the sandbox with neither.

These tests run the real script with a stubbed ``go`` on PATH and a staged
fake module cache, so they exercise the shipped gate rather than a copy of it.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PREWARM = Path(__file__).resolve().parent.parent / "scripts" / "test-prewarm.sh"

requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("grep") is None,
    reason="requires bash and grep on PATH",
)

# The pin the staged fake repo declares; must round-trip through the script's
# SAFEGIT_MIN_VERSION grep.
PIN = "v9.9.9"

# A `go` stub covering every invocation the script makes. Behaviour is driven
# by FAKE_GO_INSTALL_EXIT / FAKE_GO_BUILD_EXIT so a test can stage a failing
# install without a network.
FAKE_GO = """#!/usr/bin/env bash
case "$1 $2" in
  "env GOVERSION") echo "go1.99.0"; exit 0 ;;
  "env GOMODCACHE") echo "${FAKE_GOMODCACHE:-/nonexistent}"; exit 0 ;;
esac
case "$1" in
  version) echo "go version go1.99.0 linux/amd64"; exit 0 ;;
  env) echo ""; exit 0 ;;
  install)
    echo "install $*" >> "$FAKE_GO_LOG"
    if [ "${FAKE_GO_INSTALL_EXIT:-0}" = "0" ] && [ -n "${GOBIN:-}" ]; then
      printf 'fake safegit\\n' > "${GOBIN}/safegit"
      chmod +x "${GOBIN}/safegit"
    fi
    exit "${FAKE_GO_INSTALL_EXIT:-0}"
    ;;
  build)
    echo "build $*" >> "$FAKE_GO_LOG"
    out=""
    while [ $# -gt 0 ]; do
      [ "$1" = "-o" ] && out="$2"
      shift
    done
    # A compiler opens (and so truncates) its -o target before it can know
    # whether the build succeeds. Reproduce that faithfully.
    [ -n "$out" ] && : > "$out"
    if [ "${FAKE_GO_BUILD_EXIT:-0}" = "0" ] && [ -n "$out" ]; then
      printf 'locally built safegit\\n' > "$out"
      chmod +x "$out"
    fi
    exit "${FAKE_GO_BUILD_EXIT:-0}"
    ;;
esac
echo "fake go: unhandled invocation: $*" >&2
exit 2
"""


def _stage(tmp_path, *, cache_files, staged_binary=True, safegit_src=True):
    """Build a fake repo root + module cache and return the script's env.

    *cache_files* is the list of module-cache extensions to create for the pin
    (``["info"]`` is the interrupted-fetch shape; all three is a complete
    entry). *staged_binary* creates the locally-built fallback the script may
    delete. *safegit_src* creates a local safegit checkout for the rebuild.
    """
    repo = tmp_path / "repo"
    (repo / "rlsbl" / "commands").mkdir(parents=True)
    (repo / "rlsbl" / "commands" / "release_scrub.py").write_text(
        "SAFEGIT_MIN_VERSION = (9, 9, 9)\n"
    )

    cache = tmp_path / "download"
    vdir = cache / "github.com" / "smm-h" / "safegit" / "@v"
    vdir.mkdir(parents=True)
    for ext in cache_files:
        (vdir / f"{PIN}.{ext}").write_text("cached\n")

    stage_dir = repo / ".rlsbl-test-tools"
    staged_bin = stage_dir / f"safegit-{PIN}"
    if staged_binary:
        stage_dir.mkdir(parents=True)
        staged_bin.write_text("previously built fallback\n")
        staged_bin.chmod(0o755)

    src = tmp_path / "safegit"
    if safegit_src:
        src.mkdir()
        (src / "go.mod").write_text("module github.com/smm-h/safegit\n\ngo 1.25\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    go = bindir / "go"
    go.write_text(FAKE_GO)
    go.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "PATH": f"{bindir}:/usr/bin:/bin",
        "REPO_ROOT": str(repo),
        "GO_DOWNLOAD_CACHE": str(cache),
        "RLSBL_SAFEGIT_SRC": str(src),
        "FAKE_GO_LOG": str(tmp_path / "go.log"),
        "TMPDIR": str(tmp_path / "tmp"),
    })
    (tmp_path / "tmp").mkdir()
    return env, staged_bin


def _run(env):
    return subprocess.run(
        ["bash", str(PREWARM)], capture_output=True, text=True, env=env,
    )


def _go_log(env):
    log = Path(env["FAKE_GO_LOG"])
    return log.read_text() if log.exists() else ""


@requires_bash
class TestModuleCacheGate:
    def test_partial_entry_is_not_treated_as_cached(self, tmp_path):
        """An interrupted fetch (.info only) must not short-circuit the gate.

        The script must attempt the fetch rather than declaring a cache hit,
        and must not destroy the fallback the sandbox would fall back on.
        """
        env, staged_bin = _stage(tmp_path, cache_files=["info"])
        env["FAKE_GO_INSTALL_EXIT"] = "1"  # proxy has no such version

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert "already in the module cache" not in result.stderr
        assert "install" in _go_log(env), "the fetch was skipped on a partial entry"
        assert staged_bin.is_file(), "the fallback binary was destroyed"

    def test_partial_entry_with_zero_byte_zip_is_not_cached(self, tmp_path):
        """A truncated .zip is as unusable as a missing one."""
        env, staged_bin = _stage(tmp_path, cache_files=["info", "mod"])
        (tmp_path / "download" / "github.com" / "smm-h" / "safegit" / "@v"
         / f"{PIN}.zip").write_text("")
        env["FAKE_GO_INSTALL_EXIT"] = "1"

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert "already in the module cache" not in result.stderr
        assert staged_bin.is_file()

    def test_complete_entry_reports_a_cache_hit(self, tmp_path):
        env, _ = _stage(tmp_path, cache_files=["info", "mod", "zip"])

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert "already in the module cache" in result.stderr


@requires_bash
class TestFallbackDeletionIsGatedOnAVerifiedInstall:
    def test_failed_install_keeps_the_fallback(self, tmp_path):
        """A complete-looking cache entry whose install fails keeps the stand-in.

        The cache directory listing is evidence, not proof: the module can be
        corrupt, the checksum can mismatch, the build can fail. Nothing may be
        deleted until a real binary for the pin has materialized.
        """
        env, staged_bin = _stage(tmp_path, cache_files=["info", "mod", "zip"])
        env["FAKE_GO_INSTALL_EXIT"] = "1"

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert staged_bin.is_file(), "the fallback was deleted without a verified install"
        assert "build" in _go_log(env), "the fallback was not rebuilt"
        assert staged_bin.read_text() == "locally built safegit\n"

    def test_successful_install_drops_the_stale_fallback(self, tmp_path):
        """Once the pin really installs, a binary named after it is a lie."""
        env, staged_bin = _stage(tmp_path, cache_files=["info", "mod", "zip"])

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert not staged_bin.exists()

    def test_install_that_exits_zero_without_a_binary_keeps_the_fallback(self, tmp_path):
        """Exit status alone is not proof; the binary has to exist."""
        env, staged_bin = _stage(tmp_path, cache_files=["info", "mod", "zip"])
        # Succeed but produce nothing: GOBIN unset from the stub's point of
        # view is simulated by making the stub skip the write.
        env["FAKE_GO_INSTALL_EXIT"] = "0"
        go = tmp_path / "bin" / "go"
        go.write_text(FAKE_GO.replace('printf \'fake safegit\\n\' > "${GOBIN}/safegit"',
                                      'true'))
        go.chmod(0o755)

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert staged_bin.is_file(), "no binary materialized, yet the fallback was dropped"

    def test_failed_rebuild_does_not_destroy_the_previous_fallback(self, tmp_path):
        """The rebuild must land beside the staged binary, then rename over it.

        Compiling straight onto the staged path truncates the only stand-in
        the moment the output file is opened, so a broken local checkout used
        to leave the sandbox with a corpse instead of a binary.
        """
        env, staged_bin = _stage(tmp_path, cache_files=["info"])
        env["FAKE_GO_INSTALL_EXIT"] = "1"
        env["FAKE_GO_BUILD_EXIT"] = "1"

        _run(env)

        assert staged_bin.is_file()
        assert staged_bin.read_text() == "previously built fallback\n"
        assert not (staged_bin.parent / f"{staged_bin.name}.new").exists()

    def test_no_fallback_and_no_source_warns_without_failing(self, tmp_path):
        """The suite must not abort: the fixture's skip reason is louder."""
        env, _ = _stage(
            tmp_path, cache_files=["info"], staged_binary=False, safegit_src=False,
        )
        env["FAKE_GO_INSTALL_EXIT"] = "1"

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stderr
        assert "SKIP" in result.stderr


@requires_bash
class TestScriptHygiene:
    def test_script_is_valid_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(PREWARM)], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_probe_gobin_is_not_leaked(self, tmp_path):
        """The install probe's throwaway GOBIN must be cleaned up."""
        env, _ = _stage(tmp_path, cache_files=["info", "mod", "zip"])

        result = _run(env)

        assert result.returncode == 0, result.stderr
        leftovers = list((tmp_path / "tmp").iterdir())
        assert leftovers == [], f"pre-warm leaked temp dirs: {leftovers}"
