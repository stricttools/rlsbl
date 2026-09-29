"""A Go preflight that cannot run or fails names the fix for its cause.

The release's ``go mod tidy -diff`` guard and its ``go work sync`` guard run
real go commands before the release mutates anything. When one of them cannot
run or fails, the refusal names the fix for the cause go's own output shows,
and each test here applies that fix and watches the guard pass.

Real Go toolchain against a ``file://`` module proxy; the unreachable proxy is
a closed local port, so nothing leaves the machine.
"""

import json
import os
import shutil
import subprocess
import zipfile

import pytest

from rlsbl.commands.release.execute import (
    GO_TIDY,
    GO_WORK_SYNC,
    UntidyGoModuleError,
    refuse_go_work_sync_changes,
    refuse_untidy_go_modules,
)

pytestmark = pytest.mark.skipif(
    shutil.which("go") is None
    or subprocess.run(["go", "version"], capture_output=True).returncode != 0,
    reason="needs a Go toolchain",
)

UNREACHABLE = "http://127.0.0.1:9"
GUARDS = ("tidy", "work")


def _publish(proxy, version):
    """Put example.com/x at *version* on the file proxy: its release."""
    d = proxy / "example.com" / "x" / "@v"
    d.mkdir(parents=True, exist_ok=True)
    mod = "module example.com/x\n\ngo 1.22\n"
    (d / f"{version}.mod").write_text(mod)
    (d / f"{version}.info").write_text(
        json.dumps({"Version": version, "Time": "2026-01-01T00:00:00Z"})
    )
    with zipfile.ZipFile(d / f"{version}.zip", "w") as z:
        z.writestr(f"example.com/x@{version}/go.mod", mod)
        z.writestr(f"example.com/x@{version}/x.go", "package x\n\nfunc X() int { return 1 }\n")
    listed = sorted(p.stem for p in d.glob("*.info"))
    (d / "list").write_text("".join(f"{v}\n" for v in listed))


def _unpublish(proxy, version):
    d = proxy / "example.com" / "x" / "@v"
    for ext in ("mod", "info", "zip"):
        (d / f"{version}.{ext}").unlink()
    listed = sorted(p.stem for p in d.glob("*.info"))
    (d / "list").write_text("".join(f"{v}\n" for v in listed))


def _go(cwd, *args, **env):
    return subprocess.run(
        ["go", *args], cwd=cwd, check=True, capture_output=True, text=True,
        timeout=120, env={**os.environ, **env},
    )


class Setup:
    def __init__(self, tmp_path, monkeypatch, guard, version="v0.1.0"):
        self.tmp = tmp_path
        self.guard = guard
        self.proxy = tmp_path / "proxy"
        self.good_proxy = f"file://{self.proxy}"
        _publish(self.proxy, version)
        monkeypatch.setenv("GOPROXY", self.good_proxy)
        monkeypatch.setenv("GOSUMDB", "off")
        monkeypatch.setenv("GOFLAGS", "")
        monkeypatch.setenv("GOTOOLCHAIN", "local")
        monkeypatch.setenv("GOCACHE", str(tmp_path / "gocache"))
        monkeypatch.setenv("GOMODCACHE", str(tmp_path / "warm-cache"))
        monkeypatch.setenv("GOWORK", "off")
        self.root = tmp_path / "ws"
        self.module = self.root / "a"
        self.module.mkdir(parents=True)
        (self.module / "go.mod").write_text(
            f"module example.com/a\n\ngo 1.22\n\nrequire example.com/x {version}\n"
        )
        (self.module / "a.go").write_text(
            'package a\n\nimport "example.com/x"\n\nfunc F() int { return x.X() }\n'
        )
        _go(self.module, "mod", "tidy")
        monkeypatch.delenv("GOWORK")
        if guard == "work":
            (self.root / "go.work").write_text("go 1.22\n\nuse ./a\n")
            (self.root / "go.work.sum").write_text("")
            _go(self.root, "work", "sync")
        # Every test starts from a module cache holding nothing.
        self.cache = tmp_path / "cold-cache"
        monkeypatch.setenv("GOMODCACHE", str(self.cache))

    @property
    def where(self):
        return str(self.root if self.guard == "work" else self.module)

    def syncs(self, timeout=60):
        if self.guard == "work":
            return [{"cwd": str(self.root), "cmd": list(GO_WORK_SYNC), "timeout": timeout}]
        return [{"cwd": str(self.module), "cmd": list(GO_TIDY), "timeout": timeout}]

    def check(self, timeout=60):
        if self.guard == "work":
            refuse_go_work_sync_changes(self.syncs(timeout))
        else:
            refuse_untidy_go_modules(self.syncs(timeout))

    def refusal(self, timeout=60):
        with pytest.raises(UntidyGoModuleError) as exc:
            self.check(timeout)
        return str(exc.value)

    def warm(self, go="go"):
        """The named warm-up: `go mod download all` where the guard runs."""
        subprocess.run(
            [go, "mod", "download", "all"], cwd=self.where, check=True,
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "GOPROXY": self.good_proxy},
        )


@pytest.fixture(params=GUARDS)
def setup(request, tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch, request.param)


def test_goproxy_off_with_a_cold_cache_names_the_warm_up(setup, monkeypatch):
    monkeypatch.setenv("GOPROXY", "off")
    message = setup.refusal()
    assert "GOPROXY=off forbids downloads" in message
    assert f"`go mod download all` in {setup.where}" in message

    setup.warm()
    setup.check()


def test_goproxy_off_names_a_reachable_proxy_as_the_other_fix(setup, monkeypatch):
    monkeypatch.setenv("GOPROXY", "off")
    assert "re-run the release with GOPROXY naming such a proxy" in setup.refusal()

    monkeypatch.setenv("GOPROXY", setup.good_proxy)
    setup.check()


def test_an_unreachable_proxy_names_the_warm_up(setup, monkeypatch):
    monkeypatch.setenv("GOPROXY", UNREACHABLE)
    message = setup.refusal()
    assert "The module proxy (GOPROXY) could not be reached" in message
    assert f"`go mod download all` in {setup.where}" in message

    setup.warm()
    setup.check()


def test_an_unreachable_proxy_names_a_reachable_proxy_as_the_other_fix(setup, monkeypatch):
    monkeypatch.setenv("GOPROXY", UNREACHABLE)
    assert "set GOPROXY to a proxy this machine reaches" in setup.refusal()

    monkeypatch.setenv("GOPROXY", setup.good_proxy)
    setup.check()


@pytest.mark.parametrize("guard", GUARDS)
def test_an_unpublished_sibling_version_names_its_release(tmp_path, monkeypatch, guard):
    # Tidy against v0.3.0 while it exists, then take it off the proxy: the
    # module requires a sibling version that was never released.
    setup = Setup(tmp_path, monkeypatch, guard, version="v0.3.0")
    _unpublish(setup.proxy, "v0.3.0")
    message = setup.refusal()
    assert "example.com/x v0.3.0 is required but the module proxy does not have it" in message
    assert "release it first" in message

    _publish(setup.proxy, "v0.3.0")
    setup.check()


def test_a_timeout_names_the_warm_up(setup, tmp_path, monkeypatch):
    # A `go` that stalls while the module cache is cold, as a slow download does.
    real_go = shutil.which("go")
    shim = tmp_path / "shim"
    shim.mkdir()
    marker = setup.cache / "cache" / "download" / "example.com" / "x" / "@v" / "v0.1.0.mod"
    (shim / "go").write_text(
        f'#!/bin/sh\n[ -e "{marker}" ] || exec sleep 30\nexec "{real_go}" "$@"\n'
    )
    (shim / "go").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    message = setup.refusal(timeout=2)
    assert "did not finish within 2s" in message
    assert f"`go mod download all` in {setup.where}" in message

    setup.warm(go=real_go)
    setup.check(timeout=60)


def test_an_unparseable_goflags_names_its_correction(setup, monkeypatch):
    monkeypatch.setenv("GOFLAGS", "-bogus")
    assert "correct or unset GOFLAGS" in setup.refusal()

    monkeypatch.setenv("GOFLAGS", "")
    setup.check()


def test_a_go_that_cannot_start_names_the_toolchain(setup, tmp_path, monkeypatch):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "go").write_bytes(b"\x7fELF not a real binary")
    (broken / "go").chmod(0o755)
    path = os.environ["PATH"]
    # Alone on PATH: a later working `go` would be exec'd in its place.
    monkeypatch.setenv("PATH", str(broken))
    assert "Put a working Go toolchain first on PATH" in setup.refusal()

    monkeypatch.setenv("PATH", path)
    setup.warm()
    setup.check()


def test_any_other_failure_names_the_command_that_reproduces_it(setup):
    setup.warm()
    gomod = setup.module / "go.mod"
    good = gomod.read_text()
    gomod.write_text(good + "require (\n")
    message = setup.refusal()
    assert "to reproduce it, fix the error it prints" in message
    assert f"in {setup.where}" in message

    gomod.write_text(good)
    setup.check()


def test_a_private_modules_not_found_is_not_called_unpublished():
    from rlsbl.commands.release.execute import go_preflight_failure

    stderr = (
        "go: example.com/a imports\n\tgithub.com/o/priv: github.com/o/priv@v1.0.0: "
        "reading https://proxy.golang.org/github.com/o/priv/@v/v1.0.0.zip: 404 Not Found\n"
        "\tserver response: not found: fatal: could not read Username for "
        "'https://github.com': terminal prompts disabled"
    )
    message = go_preflight_failure(
        ["go", "mod", "tidy", "-diff"], "/w", "guard", returncode=1, stderr=stderr,
    )
    assert "never published" not in message
    assert "to reproduce it" in message
