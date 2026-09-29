"""A Go program on strictcli's former module path is told to migrate the path.

Go strictcli moved from ``github.com/smm-h/strictcli/go`` to
``github.com/stricttools/strictcli/go``. A program still requiring the former
path is on a strictcli without ``help --json``, so the release's schema step
refuses it. ``go get`` on the new path does not move the program's imports, so
the refusal names the path migration first: ``rlsbl rewrite go-module-path``,
then ``go get`` and ``go mod tidy`` on the new path. The test applies exactly
that to a scratch module and the schema step goes through.

Real Go toolchain against a ``file://`` module proxy serving stand-ins for both
module paths: the former one has no ``help`` command, the new one prints a
help document.
"""

import json
import os
import shutil
import subprocess
import zipfile

import pytest

from githarness import git, init_repo

from rlsbl import app
from rlsbl.commands.release import _run_strictcli_schema_dump
from rlsbl.commands.release.validate import ReleaseValidationError

pytestmark = pytest.mark.skipif(
    shutil.which("go") is None
    or subprocess.run(["go", "version"], capture_output=True).returncode != 0,
    reason="needs a Go toolchain",
)

OLD = "github.com/smm-h/strictcli/go"
NEW = "github.com/stricttools/strictcli/go"

_OLD_SRC = """package strictcli

import (
\t"fmt"
\t"os"
)

// Run is a strictcli from before `help --json`.
func Run() {
\tif len(os.Args) > 1 && os.Args[1] == "help" {
\t\tfmt.Fprintln(os.Stderr, "error: unknown command 'help'")
\t\tos.Exit(1)
\t}
}
"""

_NEW_SRC = """package strictcli

import (
\t"fmt"
\t"os"
)

// Run prints the help document for `help --json`.
func Run() {
\tif len(os.Args) > 2 && os.Args[1] == "help" && os.Args[2] == "--json" {
\t\tfmt.Print("{\\n  \\"schema_version\\": 2,\\n  \\"version\\": \\"0.1.0\\"\\n}\\n")
\t\tos.Exit(0)
\t}
}
"""


def _publish(proxy, module, version, src):
    d = proxy / module / "@v"
    d.mkdir(parents=True)
    mod = f"module {module}\n\ngo 1.22\n"
    (d / f"{version}.mod").write_text(mod)
    (d / f"{version}.info").write_text(
        json.dumps({"Version": version, "Time": "2026-01-01T00:00:00Z"})
    )
    with zipfile.ZipFile(d / f"{version}.zip", "w") as z:
        z.writestr(f"{module}@{version}/go.mod", mod)
        z.writestr(f"{module}@{version}/strictcli.go", src)
    (d / "list").write_text(f"{version}\n")


def _go(cwd, *args):
    r = subprocess.run(
        ["go", *args], cwd=cwd, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"go {' '.join(args)}: {r.stderr}"


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    proxy = tmp_path / "proxy"
    _publish(proxy, OLD, "v0.35.0", _OLD_SRC)
    _publish(proxy, NEW, "v0.37.0", _NEW_SRC)
    monkeypatch.setenv("GOPROXY", f"file://{proxy}")
    monkeypatch.setenv("GOSUMDB", "off")
    monkeypatch.setenv("GOFLAGS", "")
    monkeypatch.setenv("GOTOOLCHAIN", "local")
    monkeypatch.setenv("GOWORK", "off")
    monkeypatch.setenv("GOMODCACHE", str(tmp_path / "modcache"))
    monkeypatch.setenv("GOCACHE", str(tmp_path / "gocache"))

    repo = tmp_path / "app"
    init_repo(repo)
    (repo / "go.mod").write_text(
        f"module example.com/app\n\ngo 1.22\n\nrequire {OLD} v0.35.0\n"
    )
    (repo / "main.go").write_text(
        f'package main\n\nimport strictcli "{OLD}"\n\nfunc main() {{ strictcli.Run() }}\n'
    )
    (repo / ".rlsbl").mkdir()
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["go"]}) + "\n"
    )
    _go(repo, "mod", "tidy")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "app on the former strictcli path")
    return repo


def test_the_former_module_path_names_the_migration_and_applying_it_clears(
    consumer, monkeypatch,
):
    with pytest.raises(ReleaseValidationError) as exc:
        _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(consumer))
    message = str(exc.value)
    assert "predates `help --json`" in message
    assert f"former module path {OLD}" in message
    rewrite = f"rlsbl rewrite go-module-path --from-module {OLD} --to-module {NEW}"
    assert rewrite in message
    assert f"`go mod edit -droprequire={NEW}`" in message
    assert f"`go get {NEW}@latest`" in message
    assert not (consumer / ".strictcli").exists()

    # The named fix, in order: rewrite the module path, drop the carried-over
    # requirement, go get, go mod tidy.
    monkeypatch.chdir(consumer)
    result = app.test(rewrite.split()[1:])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert f'"{NEW}"' in (consumer / "main.go").read_text()
    _go(consumer, "mod", "edit", f"-droprequire={NEW}")
    _go(consumer, "get", f"{NEW}@latest")
    _go(consumer, "mod", "tidy")
    assert OLD not in (consumer / "go.mod").read_text()

    _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(consumer))
    schema = json.loads((consumer / ".strictcli" / "schema.json").read_text())
    assert schema["schema_version"] == 2


def test_the_new_module_path_names_only_the_upgrade(consumer, monkeypatch):
    """A program already on the new path at a version without `help --json`
    needs no migration: the refusal names go get and go mod tidy alone."""
    monkeypatch.chdir(consumer)
    (consumer / "main.go").write_text(
        (consumer / "main.go").read_text().replace(OLD, NEW)
    )
    (consumer / "go.mod").write_text(
        f"module example.com/app\n\ngo 1.22\n\nrequire {NEW} v0.36.0\n"
    )
    proxy = consumer.parent / "proxy"
    _publish_extra = proxy / NEW / "@v"
    mod = f"module {NEW}\n\ngo 1.22\n"
    (_publish_extra / "v0.36.0.mod").write_text(mod)
    (_publish_extra / "v0.36.0.info").write_text(
        json.dumps({"Version": "v0.36.0", "Time": "2025-12-01T00:00:00Z"})
    )
    with zipfile.ZipFile(_publish_extra / "v0.36.0.zip", "w") as z:
        z.writestr(f"{NEW}@v0.36.0/go.mod", mod)
        z.writestr(f"{NEW}@v0.36.0/strictcli.go", _OLD_SRC)
    (_publish_extra / "list").write_text("v0.36.0\nv0.37.0\n")
    os.remove(consumer / "go.sum")
    _go(consumer, "mod", "tidy")

    with pytest.raises(ReleaseValidationError) as exc:
        _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(consumer))
    message = str(exc.value)
    assert "rewrite go-module-path" not in message
    assert f"`go get {NEW}@latest`" in message

    _go(consumer, "get", f"{NEW}@latest")
    _go(consumer, "mod", "tidy")
    _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(consumer))
    assert (consumer / ".strictcli" / "schema.json").exists()
