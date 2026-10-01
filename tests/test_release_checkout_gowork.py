"""The release checkout never inherits the live repository's local go.work.

The checkout lives at ``<repo>/.git/rlsbl/release-checkout``, inside the live
repository, and the Go toolchain looks for a ``go.work`` in every parent
directory of its working directory. A gitignored, uncommitted ``go.work`` at
the live root (``use .``) is therefore found from inside the checkout: every
``go`` command there resolves against the live root's workspace, which does not
contain the checkout's directories, so ``go list`` finds no packages and
strictcli entry-point detection refuses with "main packages detected: (none)".

A release builds committed state only. These tests drive the Go toolchain for
real inside a real release checkout.
"""

import os
import shutil
from pathlib import Path

import pytest

from githarness import git, init_repo
from rlsbl import effects, release_checkout
from rlsbl.go_introspect import list_main_packages
from rlsbl.strictcli_detect import detect_strictcli

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="needs the go toolchain")

MODULE = "example.com/proj"
GO_MOD = (
    f"module {MODULE}\n\ngo 1.21\n\n"
    "require github.com/stricttools/strictcli v0.1.0\n"
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture(autouse=True)
def _offline_go(monkeypatch, tmp_path):
    """Keep the toolchain offline and away from the operator's caches."""
    monkeypatch.setenv("GOPROXY", "off")
    monkeypatch.setenv("GOFLAGS", "-mod=mod")
    monkeypatch.setenv("GOTOOLCHAIN", "local")
    monkeypatch.delenv("GOWORK", raising=False)
    monkeypatch.setenv("GOPATH", str(tmp_path / "gopath"))
    monkeypatch.setenv("GOCACHE", str(tmp_path / "gocache"))
    monkeypatch.setenv("GOMODCACHE", str(tmp_path / "gopath" / "mod"))


def _repo(tmp_path, *, committed_work=None):
    """A Go binary requiring strictcli, with a local, gitignored go.work.

    *committed_work* is a repo-relative directory that gets a committed
    ``go.work`` of its own (``use .``) next to a module of its own.
    """
    repo = tmp_path / "repo"
    init_repo(repo)
    _write(repo / "go.mod", GO_MOD)
    _write(repo / "main.go", "package main\n\nfunc main() {}\n")
    _write(repo / ".gitignore", "go.work\ngo.work.sum\n")
    if committed_work is not None:
        sub = repo / committed_work
        _write(sub / "go.mod", "module example.com/sub\n\ngo 1.21\n")
        _write(sub / "sub.go", "package sub\n")
        _write(sub / "go.work", "go 1.21\n\nuse .\n")
        git(repo, "add", "-f", f"{committed_work}/go.work")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    # The operator's local workspace: uncommitted, ignored, and -- seen from
    # the checkout -- a workspace that does not contain the checkout.
    _write(repo / "go.work", "go 1.21\n\nuse .\n")
    assert git(repo, "status", "--porcelain") == ""
    return repo


def _enter(repo):
    head = git(repo, "rev-parse", "refs/heads/main")
    return release_checkout.entered(str(repo), branch="main", sha=head, cwd=str(repo))


def _go(args, cwd):
    return effects.run(["go", *args], cwd=cwd, capture_output=True, text=True)


def test_the_fixture_reproduces_the_inherited_workspace(tmp_path):
    """Without the fix's environment, go resolves the live root's go.work."""
    repo = _repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    path = release_checkout.prepare_checkout(str(repo), head)
    import subprocess

    env = os.environ.copy()
    out = subprocess.run(["go", "env", "GOWORK"], cwd=path, env=env,
                         capture_output=True, text=True)
    assert out.stdout.strip() == str(repo / "go.work")
    listed = subprocess.run(["go", "list", "."], cwd=path, env=env,
                            capture_output=True, text=True)
    assert listed.returncode != 0


def test_go_commands_in_the_checkout_ignore_the_live_go_work(tmp_path):
    repo = _repo(tmp_path)
    with _enter(repo) as co:
        env = _go(["env", "GOWORK"], co.path)
        assert env.returncode == 0, env.stderr
        assert env.stdout.strip() in ("", "off")
        listed = _go(["list", "."], co.path)
        assert listed.returncode == 0, listed.stderr
        assert listed.stdout.strip() == MODULE
        # The cwd a caller leaves implicit is the checkout too.
        implicit = effects.run(["go", "list", "."], capture_output=True, text=True)
        assert implicit.returncode == 0, implicit.stderr


def test_strictcli_detection_finds_the_main_package_in_the_checkout(tmp_path):
    repo = _repo(tmp_path)
    with _enter(repo) as co:
        assert [p.rel_dir for p in list_main_packages(co.path)] == ["."]
        assert detect_strictcli(co.path) == (".", "go")


def test_a_child_process_inherits_the_setting(tmp_path):
    """A hook's own go commands see the same workspace resolution."""
    repo = _repo(tmp_path)
    with _enter(repo) as co:
        out = effects.run("go list .", shell=True, cwd=co.path,
                          capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == MODULE


def _repo_with_committed_work(tmp_path):
    """A repository whose root tracks its own go.work (``use . ./sub``)."""
    repo = tmp_path / "repo"
    init_repo(repo)
    _write(repo / "go.mod", f"module {MODULE}\n\ngo 1.21\n")
    _write(repo / "main.go", "package main\n\nfunc main() {}\n")
    _write(repo / "sub" / "go.mod", "module example.com/sub\n\ngo 1.21\n")
    _write(repo / "sub" / "sub.go", "package sub\n")
    _write(repo / "go.work", "go 1.21\n\nuse (\n\t.\n\t./sub\n)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_a_committed_root_go_work_in_the_checkout_is_honored(tmp_path, monkeypatch):
    """A root that tracks a go.work releases against the checkout's own copy
    (rlsbl's go work sync and go.work.sum support read it), never the live
    tree's."""
    monkeypatch.delenv("GOFLAGS")  # -mod=mod is refused in workspace mode
    repo = _repo_with_committed_work(tmp_path)
    with _enter(repo) as co:
        want = os.path.join(co.path, "go.work")
        assert os.environ["GOWORK"] == want
        for where in (co.path, os.path.join(co.path, "sub")):
            env = _go(["env", "GOWORK"], where)
            assert env.stdout.strip() == want, env.stderr
        listed = _go(["list", "example.com/sub"], co.path)
        assert listed.returncode == 0, listed.stderr


def test_a_hook_runs_with_the_checkouts_workspace(tmp_path):
    """The release hooks (pre-checks.sh and the rest) build in the checkout."""
    from rlsbl.commands.release.hooks import build_hook_env, run_release_hook

    repo = _repo(tmp_path)
    with _enter(repo) as co:
        out = Path(co.path) / "hook-out"
        hook = Path(co.path) / "hook.sh"
        hook.write_text(f'go env GOWORK > "{out}"\ngo list . >> "{out}"\n')
        run_release_hook(
            "pre-checks", str(hook), co.path,
            build_hook_env(os.environ.copy(), "0.1.1"), 60,
        )
        assert out.read_text().split() == ["off", MODULE]


def test_an_operators_gowork_is_restored(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("GOWORK", str(repo / "go.work"))
    with _enter(repo):
        assert os.environ["GOWORK"] == "off"
    assert os.environ["GOWORK"] == str(repo / "go.work")


def test_the_environment_is_restored_on_exit(tmp_path):
    """Once the release leaves the checkout, Go in the live tree sees the
    operator's workspace again."""
    repo = _repo(tmp_path)
    with _enter(repo):
        assert os.environ["GOWORK"] == "off"
    assert "GOWORK" not in os.environ
    after = _go(["env", "GOWORK"], str(repo))
    assert after.stdout.strip() == str(repo / "go.work")
