"""A release refuses a Go workspace whose `go work sync` would edit a module's go.mod.

The release runs ``go work sync`` at a workspace root to refresh go.work.sum.
When two of the workspace's modules require one dependency at different
versions, it also raises the lower requirement in that module's go.mod, the
way an untidy module's ``go mod tidy`` rewrote go.mod: a requirement change
nobody committed, which the release stopped on late as unexpected modified
files. The guard reads the build list and each module's requirements without
writing, refuses before anything mutates, and names ``go work sync`` and a
commit as the fix, which the test applies.

Real Go toolchain; the dependency is served from a file:// module proxy built
in the test, so nothing reaches the network.
"""

import json
import subprocess
import zipfile

import pytest

from rlsbl.commands.release.execute import (
    UntidyGoModuleError,
    _target_lockfile_syncs,
    refuse_go_work_sync_changes,
    release_lock_targets,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["go", "version"], capture_output=True).returncode != 0,
    reason="needs a Go toolchain",
)


def _proxy(root):
    d = root / "proxy" / "example.com" / "x" / "@v"
    d.mkdir(parents=True)
    for v in ("v0.1.0", "v0.2.0"):
        mod = "module example.com/x\n\ngo 1.22\n"
        (d / f"{v}.mod").write_text(mod)
        (d / f"{v}.info").write_text(json.dumps({"Version": v, "Time": "2026-01-01T00:00:00Z"}))
        with zipfile.ZipFile(d / f"{v}.zip", "w") as z:
            z.writestr(f"example.com/x@{v}/go.mod", mod)
            z.writestr(f"example.com/x@{v}/x.go", "package x\n\nfunc X() int { return 1 }\n")
    (d / "list").write_text("v0.1.0\nv0.2.0\n")
    return root / "proxy"


def _go(cwd, *args, env=None):
    return subprocess.run(
        ["go", *args], cwd=cwd, check=True, capture_output=True, text=True,
        timeout=120, env=env,
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    proxy = _proxy(tmp_path)
    monkeypatch.setenv("GOPROXY", f"file://{proxy}")
    monkeypatch.setenv("GOSUMDB", "off")
    monkeypatch.setenv("GOFLAGS", "")
    monkeypatch.setenv("GOMODCACHE", str(tmp_path / "modcache"))
    monkeypatch.setenv("GOCACHE", str(tmp_path / "gocache"))
    monkeypatch.setenv("GOTOOLCHAIN", "local")
    ws = tmp_path / "ws"
    for name, version in (("a", "v0.1.0"), ("b", "v0.2.0")):
        (ws / name).mkdir(parents=True)
        (ws / name / "go.mod").write_text(
            f"module example.com/{name}\n\ngo 1.22\n\nrequire example.com/x {version}\n"
        )
        (ws / name / f"{name}.go").write_text(
            f'package {name}\n\nimport "example.com/x"\n\nfunc F() int {{ return x.X() }}\n'
        )
        monkeypatch.setenv("GOWORK", "off")
        _go(ws / name, "mod", "tidy")
    monkeypatch.delenv("GOWORK")
    (ws / "go.work").write_text("go 1.22\n\nuse (\n\t./a\n\t./b\n)\n")
    (ws / "go.work.sum").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, timeout=30)
    return ws


def _owed(ws):
    owed = []
    for paths in release_lock_targets(
        {}, member_package_paths=None, monorepo_root=str(ws), releasable_cfg_dir=None,
    ):
        owed.extend(_target_lockfile_syncs(paths, lambda _m: None))
    return owed


def test_a_sync_that_raises_a_requirement_is_refused_and_the_fix_clears_it(workspace):
    before = (workspace / "a" / "go.mod").read_text()
    owed = _owed(workspace)
    assert any(s["cmd"] == ["go", "work", "sync"] for s in owed)

    with pytest.raises(UntidyGoModuleError) as exc:
        refuse_go_work_sync_changes(owed)
    message = str(exc.value)
    assert "example.com/x v0.1.0 -> v0.2.0" in message
    assert f"{workspace}/a/go.mod" in message
    assert "Run `go work sync`" in message
    # The guard itself wrote nothing.
    assert (workspace / "a" / "go.mod").read_text() == before

    # The named fix: go work sync in the workspace, then the release goes on.
    _go(workspace, "work", "sync")
    refuse_go_work_sync_changes(_owed(workspace))


def test_a_workspace_in_sync_is_not_refused(workspace):
    _go(workspace, "work", "sync")
    refuse_go_work_sync_changes(_owed(workspace))


@pytest.fixture
def unpublished_sibling(tmp_path, monkeypatch):
    """A workspace whose module requires a sibling at a version no proxy has.

    It builds under go.work, which resolves the sibling from its directory,
    while ``go mod tidy`` and the build list want the sibling's published
    go.mod: with GOPROXY=off neither can answer. The release's own tidy and
    sync fail the same way, and their failure only warns.
    """
    monkeypatch.setenv("GOPROXY", "off")
    monkeypatch.setenv("GOSUMDB", "off")
    monkeypatch.setenv("GOFLAGS", "")
    monkeypatch.setenv("GOMODCACHE", str(tmp_path / "modcache"))
    monkeypatch.setenv("GOCACHE", str(tmp_path / "gocache"))
    monkeypatch.setenv("GOTOOLCHAIN", "local")
    ws = tmp_path / "ws"
    (ws / "a").mkdir(parents=True)
    (ws / "b").mkdir()
    (ws / "a" / "go.mod").write_text(
        "module example.com/a\n\ngo 1.22\n\nrequire example.com/b v0.1.0\n"
    )
    (ws / "a" / "go.sum").write_text("")
    (ws / "a" / "a.go").write_text(
        'package a\n\nimport "example.com/b"\n\nvar X = b.Y\n'
    )
    (ws / "b" / "go.mod").write_text("module example.com/b\n\ngo 1.22\n")
    (ws / "b" / "b.go").write_text("package b\n\nvar Y = 1\n")
    (ws / "go.work").write_text("go 1.22\n\nuse (\n\t./a\n\t./b\n)\n")
    (ws / "go.work.sum").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, timeout=30)
    _go(ws, "build", "./a/...")
    return ws


def test_an_unanswerable_guard_warns_where_the_release_sync_only_warns(
    unpublished_sibling, capsys,
):
    from rlsbl.commands.release.execute import refuse_untidy_go_modules

    ws = unpublished_sibling
    owed = _owed(ws)
    owed += _target_lockfile_syncs({"go": str(ws / "a")}, lambda _m: None)
    assert {tuple(s["cmd"]) for s in owed} == {
        ("go", "work", "sync"), ("go", "mod", "tidy"),
    }

    refuse_untidy_go_modules(owed)
    refuse_go_work_sync_changes(owed)
    err = capsys.readouterr().err
    assert "module lookup disabled by GOPROXY=off" in err
    assert err.count("Warning:") == 2
