"""Go modules in one workspace: requires stay current, and replace is refused.

A Go module that depends on a sibling module in the same repository names the
sibling's version in its ``require`` line. rlsbl does not bump that line when
the sibling releases -- ``go.sum`` needs the new version's hash, which exists
only once the tag reaches the proxy -- so ``go-workspace-require-current``
refuses a require below the sibling's latest release and prints the exact
``go get`` that raises it. A ``replace`` into the workspace is refused by
``go-workspace-replace``: ``go install module@version`` rejects a module that
carries one, and consumers never see it. Development across modules uses a
committed ``go.work`` instead.
"""

import os
import re
import shlex
import subprocess
import zipfile

from conftest import archive_release, make_nested_workspace, run_git

M = "github.com/example/nested"


def _check(root, name):
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_releasables, load_workspace

    projects = load_workspace(str(root))
    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=projects, releasables=load_releasables(str(root), projects),
    )
    return app._check_defs[name].impl(ctx)


def _text(result):
    return " ".join(p.text for p in result.problems)


def _released(root, releasable, version):
    releases = root / ".rlsbl-monorepo" / "releasables" / releasable / "releases"
    archive_release(releases, version, "a" * 40)


def _go_env(tmp_path, proxy):
    env = dict(os.environ)
    env.update({
        "GOPROXY": f"file://{proxy}",
        "GOSUMDB": "off",
        "GOFLAGS": "-mod=mod",
        "GOTOOLCHAIN": "local",
        "GOMODCACHE": str(tmp_path / "gomodcache"),
        "GOCACHE": str(tmp_path / "gocache"),
        "GOPATH": str(tmp_path / "gopath"),
    })
    return env


def _publish_to_proxy(proxy, module_dir, module, versions):
    """Serve *module_dir* at each of *versions* from a file:// proxy."""
    base = proxy / module / "@v"
    base.mkdir(parents=True, exist_ok=True)
    (base / "list").write_text("".join(f"{v}\n" for v in versions))
    for version in versions:
        (base / f"{version}.info").write_text(f'{{"Version":"{version}"}}\n')
        (base / f"{version}.mod").write_text((module_dir / "go.mod").read_text())
        with zipfile.ZipFile(base / f"{version}.zip", "w") as archive:
            for name in ("go.mod", "draw.go"):
                archive.write(module_dir / name, f"{module}@{version}/{name}")


# ---------------------------------------------------------------------------
# go-workspace-require-current
# ---------------------------------------------------------------------------


def test_a_require_below_the_siblings_latest_release_is_refused(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _released(root, "draw", "0.2.0")
    text = _text(_check(root, "go-workspace-require-current"))
    assert f"drawcmd: draw/cmd/go.mod requires {M}/draw v0.1.0" in text
    assert "latest release, v0.2.0" in text
    assert f"`go get {M}/draw@v0.2.0` in draw/cmd" in text


def test_a_current_require_passes(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _released(root, "draw", "0.1.0")
    assert _check(root, "go-workspace-require-current").status == "pass"


def test_a_sibling_with_no_release_is_owed_nothing(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    assert _check(root, "go-workspace-require-current").status in ("pass", "skip")


def test_running_the_printed_go_get_clears_the_refusal(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _released(root, "draw", "0.2.0")
    proxy = tmp_path / "proxy"
    _publish_to_proxy(proxy, root / "draw", f"{M}/draw", ["v0.1.0", "v0.2.0"])

    text = _text(_check(root, "go-workspace-require-current"))
    command = re.search(r"`(go get \S+)` in (\S+)", text)
    assert command, text
    ran = subprocess.run(
        shlex.split(command.group(1)), cwd=root / command.group(2).rstrip(".,"),
        env=_go_env(tmp_path, proxy), capture_output=True, text=True,
    )
    assert ran.returncode == 0, ran.stderr
    assert f"require {M}/draw v0.2.0" in (root / "draw" / "cmd" / "go.mod").read_text()
    assert _check(root, "go-workspace-require-current").status == "pass"


# ---------------------------------------------------------------------------
# go-workspace-replace
# ---------------------------------------------------------------------------


def _with_replace(root):
    go_mod = root / "draw" / "cmd" / "go.mod"
    go_mod.write_text(go_mod.read_text() + f"\nreplace {M}/draw => ../\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "replace")


def test_a_replace_into_the_workspace_is_refused(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _with_replace(root)
    text = _text(_check(root, "go-workspace-replace"))
    assert f"drawcmd: draw/cmd/go.mod replaces {M}/draw with ../" in text
    assert "go work init" in text
    assert f"go mod edit -dropreplace={M}/draw" in text


def test_a_replace_outside_the_workspace_is_not_this_checks_business(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    go_mod = root / "draw" / "cmd" / "go.mod"
    go_mod.write_text(go_mod.read_text() + "\nreplace example.com/x => example.com/y v1.0.0\n")
    assert _check(root, "go-workspace-replace").status == "pass"


def test_the_go_work_migration_clears_the_refusal(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _with_replace(root)
    text = _text(_check(root, "go-workspace-replace"))
    env = _go_env(tmp_path, tmp_path / "no-proxy")
    env["GOPROXY"] = "off"
    # Apply the named migration, command by command, as printed.
    for command in re.findall(r"`([^`]+)`", text):
        if not command.startswith(("go work", "go mod edit")):
            continue
        cwd = root
        if "dropreplace" in command:
            cwd = root / "draw" / "cmd"
        ran = subprocess.run(
            shlex.split(command), cwd=cwd, env=env, capture_output=True, text=True,
        )
        assert ran.returncode == 0, (command, ran.stderr)
    assert (root / "go.work").is_file()
    assert "replace" not in (root / "draw" / "cmd" / "go.mod").read_text()
    assert _check(root, "go-workspace-replace").status == "pass"
