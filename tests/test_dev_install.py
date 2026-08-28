"""Tests for `rlsbl dev install` -- the per-target editable installer."""

import json
import os
import subprocess

import pytest

from rlsbl.commands.dev import run_install

from conftest import workspace_toml


def _make_pypi(dir_path, name="mypkg"):
    """Create a minimal pyproject.toml so the pypi target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')


def _make_npm(dir_path, name="my-npm-pkg"):
    """Create a minimal package.json so the npm target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "version": "0.1.0"}, f)


def _make_go(dir_path):
    """Create a minimal go.mod + a main package + declared install_paths.

    dev install for Go requires install_paths on the go pipeline config
    (validated against `go list`), so the fixture provides all three.
    """
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "go.mod"), "w", encoding="utf-8") as f:
        f.write("module example.com/foo\n\ngo 1.21\n")
    with open(os.path.join(dir_path, "main.go"), "w", encoding="utf-8") as f:
        f.write("package main\n\nfunc main() {}\n")
    rlsbl_dir = os.path.join(dir_path, ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    with open(os.path.join(rlsbl_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "targets": ["go"],
                "pipelines": {
                    "go": {"type": "go", "local": True, "install_paths": ["."]}
                },
                "publish_mode": "ci",
            },
            f,
        )


def _make_hex(dir_path, app="myapp"):
    """Create a minimal mix.exs so the hex target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "mix.exs"), "w", encoding="utf-8") as f:
        f.write(
            'defmodule MyApp.MixProject do\n'
            '  use Mix.Project\n'
            '  def project do\n'
            f'    [app: :{app}, version: "0.1.0"]\n'
            '  end\n'
            'end\n'
        )


def _make_deno(dir_path, name="mydeno"):
    """Create a minimal deno.json so the deno target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "deno.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "version": "0.1.0"}, f)


def _make_zig(dir_path, name="myzig"):
    """Create a minimal build.zig.zon + VERSION so the zig target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "build.zig.zon"), "w", encoding="utf-8") as f:
        f.write(
            '.{\n'
            f'    .name = "{name}",\n'
            '    .version = "0.1.0",\n'
            '    .minimum_zig_version = "0.14.0",\n'
            '    .paths = .{""},\n'
            '}\n'
        )
    with open(os.path.join(dir_path, "VERSION"), "w", encoding="utf-8") as f:
        f.write("0.1.0\n")


def _make_swift(dir_path, name="MySwift"):
    """Create a minimal Package.swift + VERSION so the swift target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "Package.swift"), "w", encoding="utf-8") as f:
        f.write(
            '// swift-tools-version:5.7\n'
            'import PackageDescription\n'
            f'let package = Package(name: "{name}")\n'
        )
    with open(os.path.join(dir_path, "VERSION"), "w", encoding="utf-8") as f:
        f.write("0.1.0\n")


class _Capture:
    """Tiny helper to record subprocess.run calls without executing them."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=self.returncode)


@pytest.fixture
def fake_run(monkeypatch):
    """Replace subprocess.run inside rlsbl.commands.dev with a recorder."""
    cap = _Capture()
    monkeypatch.setattr("rlsbl.effects.run", cap)
    return cap


@pytest.fixture
def all_tools_present(monkeypatch):
    """Pretend every tool is on PATH."""
    monkeypatch.setattr(
        "rlsbl.commands.dev.require_tool",
        lambda name, purpose=None, fatal=True: f"/fake/bin/{name}",
    )


@pytest.fixture(autouse=True)
def stub_go_introspect(monkeypatch):
    """Stub go list introspection: fake_run patches subprocess.run globally,
    which would break the real `go list` call inside go_introspect."""
    from rlsbl.go_introspect import GoPackage

    monkeypatch.setattr(
        "rlsbl.go_introspect.list_main_packages",
        lambda project_dir: [
            GoPackage(name="main", import_path="example.com/foo", rel_dir=".")
        ],
    )


# ---------------------------------------------------------------------------
# Single-project install
# ---------------------------------------------------------------------------


def test_pypi_install_runs_uv_tool_install(tmp_project, fake_run, all_tools_present):
    _make_pypi(str(tmp_project))
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["uv", "tool", "install", "-e", "."]
    assert fake_run.calls[0]["kwargs"]["cwd"] == "."


def test_npm_install_runs_npm_link(tmp_project, fake_run, all_tools_present):
    _make_npm(str(tmp_project))
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["npm", "link"]


def test_go_install_runs_go_install(tmp_project, fake_run, all_tools_present):
    _make_go(str(tmp_project))
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    # Declared install_paths drive the install (never a blanket ./...)
    assert fake_run.calls[0]["cmd"] == ["go", "install", "."]


def test_pypi_uninstall_uses_project_name(tmp_project, fake_run, all_tools_present):
    _make_pypi(str(tmp_project), name="rlsbl-test-pkg")
    rc = run_install({"target": "global", "uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls[0]["cmd"] == ["uv", "tool", "uninstall", "rlsbl-test-pkg"]


def test_go_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_go(str(tmp_project))
    rc = run_install({"target": "global", "uninstall": True}, project_root=".")
    # Nothing actually ran -- go cannot be cleanly uninstalled.
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping go uninstall" in captured.out


def test_no_targets_returns_error(tmp_project, fake_run, all_tools_present, capsys):
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 1
    captured = capsys.readouterr()
    assert "no targets detected" in captured.err


def test_unsupported_target_skipped(tmp_project, fake_run, all_tools_present, capsys):
    # plain is a registered target with no dev_install_command override (inherits None from BaseTarget)
    os.makedirs(str(tmp_project / ".rlsbl"))
    with open(str(tmp_project / ".rlsbl" / "config.json"), "w") as f:
        json.dump({"targets": ["plain"]}, f)
    rc = run_install({"target": "global"}, project_root=".")
    # No supported targets -> nothing ran, no error.
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping plain" in captured.out
    assert "install not yet supported" in captured.out


def test_skip_reason_from_target_is_shown(
    tmp_project, fake_run, all_tools_present, capsys, monkeypatch
):
    """When a target's dev_install_command carries a 'reason' for the no-op
    spec (e.g. a Go library with no main packages), dev install prints that
    reason instead of the generic 'install not yet supported' line."""
    _make_go(str(tmp_project))
    # Turn the fixture's go project into a library: no main packages, and no
    # install_paths declared (a library can never satisfy that requirement).
    os.remove(str(tmp_project / "main.go"))
    with open(str(tmp_project / ".rlsbl" / "config.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "targets": ["go"],
                "pipelines": {"go": {"type": "go", "local": True}},
                "publish_mode": "ci",
            },
            f,
        )
    monkeypatch.setattr(
        "rlsbl.go_introspect.list_main_packages", lambda project_dir: []
    )
    # go.py binds list_main_packages at module top, so patch that name too.
    monkeypatch.setattr(
        "rlsbl.targets.go.list_main_packages", lambda project_dir: []
    )
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping go: Go library: nothing to install (no main packages)" in captured.out
    assert "not yet supported" not in captured.out


def test_missing_tool_is_skipped(tmp_project, fake_run, monkeypatch, capsys):
    _make_pypi(str(tmp_project))
    # All tools missing.
    monkeypatch.setattr(
        "rlsbl.commands.dev.require_tool",
        lambda name, purpose=None, fatal=True: None,
    )
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping pypi" in captured.out
    assert "uv not on PATH" in captured.out


def test_install_failure_returns_nonzero(tmp_project, monkeypatch, all_tools_present):
    _make_pypi(str(tmp_project))
    cap = _Capture(returncode=2)
    monkeypatch.setattr("rlsbl.effects.run", cap)
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 1
    assert len(cap.calls) == 1


# ---------------------------------------------------------------------------
# Path-scoped targets (multi-target projects like go root + npm/ + pypi/)
# ---------------------------------------------------------------------------


def _make_multi_target(root):
    """Create a project with a root go target plus path-scoped npm/ and pypi/
    targets, mirroring real multi-target layouts (go binary + npm wrapper +
    pypi wrapper)."""
    _make_npm(str(root / "npm"), name="multi-npm")
    _make_pypi(str(root / "pypi"), name="multi-pypi")
    with open(str(root / "go.mod"), "w", encoding="utf-8") as f:
        f.write("module example.com/multi\n\ngo 1.21\n")
    with open(str(root / "main.go"), "w", encoding="utf-8") as f:
        f.write("package main\n\nfunc main() {}\n")
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir()
    with open(str(rlsbl_dir / "config.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "targets": [
                    "go",
                    {"name": "npm", "path": "npm/"},
                    {"name": "pypi", "path": "pypi/"},
                ],
                "pipelines": {
                    "go": {"type": "go", "local": True, "install_paths": ["."]}
                },
                "publish_mode": "ci",
            },
            f,
        )


def test_path_scoped_targets_run_in_their_own_dirs(
    tmp_project, fake_run, all_tools_present
):
    """Each target's install command must run in that target entry's declared
    directory, not the project root: npm link needs npm/package.json, uv needs
    pypi/pyproject.toml."""
    _make_multi_target(tmp_project)
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    cwds = {c["cmd"][0]: os.path.normpath(c["kwargs"]["cwd"]) for c in fake_run.calls}
    assert cwds["go"] == "."
    assert cwds["npm"] == "npm"
    assert cwds["uv"] == "pypi"


def test_path_scoped_uninstall_reads_manifest_from_target_dir(
    tmp_project, fake_run, all_tools_present
):
    """Uninstall must resolve the package name from the target entry's own
    manifest (pypi/pyproject.toml), not fall back to the root dir basename."""
    _make_multi_target(tmp_project)
    rc = run_install({"target": "global", "uninstall": True}, project_root=".")
    assert rc == 0
    cmds = [c["cmd"] for c in fake_run.calls]
    assert ["uv", "tool", "uninstall", "multi-pypi"] in cmds
    # npm unlink also runs in its own dir.
    npm_calls = [c for c in fake_run.calls if c["cmd"][0] == "npm"]
    assert os.path.normpath(npm_calls[0]["kwargs"]["cwd"]) == "npm"


# ---------------------------------------------------------------------------
# Monorepo install
# ---------------------------------------------------------------------------


def _make_monorepo(root):
    """Create a workspace with three sub-projects (pypi, npm, go)."""
    ws_dir = root / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text(
        workspace_toml('[[projects]]\npath = "py"\nname = "pyproj"\n\n'
        '[[projects]]\npath = "node"\nname = "nodeproj"\n\n'
        '[[projects]]\npath = "gocode"\nname = "goproj"\n')
    )
    _make_pypi(str(root / "py"), name="pyproj")
    _make_npm(str(root / "node"), name="nodeproj")
    _make_go(str(root / "gocode"))


def test_monorepo_without_flags_errors(tmp_project, fake_run, all_tools_present, capsys):
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 1
    captured = capsys.readouterr()
    assert "monorepo mode" in captured.err
    assert "--all" in captured.err
    assert fake_run.calls == []


def test_monorepo_all_iterates_all_projects(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global", "all": True}, project_root=".")
    assert rc == 0
    # One install per project: uv, npm, go.
    tools = [c["cmd"][0] for c in fake_run.calls]
    assert sorted(tools) == ["go", "npm", "uv"]
    captured = capsys.readouterr()
    assert "=== pyproj ===" in captured.out
    assert "=== nodeproj ===" in captured.out
    assert "=== goproj ===" in captured.out


def test_monorepo_include_filters(tmp_project, fake_run, all_tools_present, capsys):
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global", "include": "pyproj,nodeproj"}, project_root=".")
    assert rc == 0
    tools = [c["cmd"][0] for c in fake_run.calls]
    assert sorted(tools) == ["npm", "uv"]
    captured = capsys.readouterr()
    assert "=== goproj ===" not in captured.out


def test_monorepo_exclude_filters(tmp_project, fake_run, all_tools_present, capsys):
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global", "exclude": "goproj"}, project_root=".")
    assert rc == 0
    tools = [c["cmd"][0] for c in fake_run.calls]
    assert sorted(tools) == ["npm", "uv"]
    captured = capsys.readouterr()
    assert "=== goproj ===" not in captured.out


def test_monorepo_empty_filter_errors(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global", "include": "nonexistent"}, project_root=".")
    assert rc == 1
    captured = capsys.readouterr()
    assert "No projects matched" in captured.err


# ---------------------------------------------------------------------------
# Dev nodes: skipped by a broad selection, installable when named
# ---------------------------------------------------------------------------


def _make_monorepo_with_dev_node(root):
    """A workspace with one publishable member and one dev-node member.

    Both carry a real manifest, so the only reason the dev node is not
    installed is that it is a dev node.
    """
    ws_dir = root / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text(
        workspace_toml(
            '[[projects]]\npath = "py"\nname = "pyproj"\n\n'
            '[[projects]]\npath = "tooling"\nname = "toolproj"\n'
            'dev_only = true\nreleasable = false\n'
        )
    )
    _make_pypi(str(root / "py"), name="pyproj")
    _make_npm(str(root / "tooling"), name="toolproj")


def test_monorepo_all_skips_dev_nodes(
    tmp_project, fake_run, all_tools_present, capsys
):
    """--all installs what the workspace ships, and a dev node ships nothing."""
    _make_monorepo_with_dev_node(tmp_project)
    rc = run_install({"target": "global", "all": True}, project_root=".")
    assert rc == 0
    captured = capsys.readouterr()
    assert "=== pyproj ===" in captured.out
    assert "=== toolproj ===" not in captured.out
    assert [c["cmd"][0] for c in fake_run.calls] == ["uv"]


def test_monorepo_include_installs_a_named_dev_node(
    tmp_project, fake_run, all_tools_present, capsys
):
    """Naming a dev node explicitly is a decision, and it is honoured."""
    _make_monorepo_with_dev_node(tmp_project)
    rc = run_install({"target": "global", "include": "toolproj"}, project_root=".")
    assert rc == 0
    captured = capsys.readouterr()
    assert "=== toolproj ===" in captured.out
    assert "=== pyproj ===" not in captured.out
    assert [c["cmd"][0] for c in fake_run.calls] == ["npm"]


def test_monorepo_exclude_still_skips_dev_nodes(
    tmp_project, fake_run, all_tools_present, capsys
):
    """--exclude selects broadly, like --all: the dev node stays skipped."""
    _make_monorepo_with_dev_node(tmp_project)
    rc = run_install({"target": "global", "exclude": "nobody"}, project_root=".")
    assert rc == 0
    captured = capsys.readouterr()
    assert "=== pyproj ===" in captured.out
    assert "=== toolproj ===" not in captured.out


# ---------------------------------------------------------------------------
# Monorepo --uninstall propagation
# ---------------------------------------------------------------------------


def test_monorepo_uninstall_all(tmp_project, fake_run, all_tools_present, capsys):
    """--all --uninstall runs the uninstall command for every supported project.

    pypi -> `uv tool uninstall pyproj`
    npm  -> `npm unlink`
    go   -> skipped (uninstall_args_template is None)
    """
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global", "all": True, "uninstall": True}, project_root=".")
    assert rc == 0

    cmds = [c["cmd"] for c in fake_run.calls]
    assert ["uv", "tool", "uninstall", "pyproj"] in cmds
    assert ["npm", "unlink"] in cmds
    # go has no uninstall template -> no `go` command should be invoked.
    assert not any(c[0] == "go" for c in cmds)
    # Exactly two real uninstall commands (pypi + npm).
    assert len(cmds) == 2

    captured = capsys.readouterr()
    # Each project's section header is printed.
    assert "=== pyproj ===" in captured.out
    assert "=== nodeproj ===" in captured.out
    assert "=== goproj ===" in captured.out
    # The skip message names go specifically.
    assert "Skipping go uninstall" in captured.out


def test_monorepo_uninstall_include(tmp_project, fake_run, all_tools_present, capsys):
    """--include pyproj,nodeproj --uninstall only uninstalls those two."""
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global", "include": "pyproj,nodeproj", "uninstall": True}, project_root=".")
    assert rc == 0

    cmds = [c["cmd"] for c in fake_run.calls]
    assert ["uv", "tool", "uninstall", "pyproj"] in cmds
    assert ["npm", "unlink"] in cmds
    assert len(cmds) == 2

    captured = capsys.readouterr()
    assert "=== pyproj ===" in captured.out
    assert "=== nodeproj ===" in captured.out
    # goproj was filtered out, no header and no skip message for it.
    assert "=== goproj ===" not in captured.out
    assert "Skipping go uninstall" not in captured.out


def test_monorepo_uninstall_exclude(tmp_project, fake_run, all_tools_present, capsys):
    """--exclude goproj --uninstall uninstalls pyproj and nodeproj, skipping goproj."""
    _make_monorepo(tmp_project)
    rc = run_install({"target": "global", "exclude": "goproj", "uninstall": True}, project_root=".")
    assert rc == 0

    cmds = [c["cmd"] for c in fake_run.calls]
    assert ["uv", "tool", "uninstall", "pyproj"] in cmds
    assert ["npm", "unlink"] in cmds
    assert len(cmds) == 2
    # goproj is excluded entirely -- no go command, no header for it.
    assert not any(c[0] == "go" for c in cmds)

    captured = capsys.readouterr()
    assert "=== pyproj ===" in captured.out
    assert "=== nodeproj ===" in captured.out
    assert "=== goproj ===" not in captured.out


# ---------------------------------------------------------------------------
# --target venv mode
# ---------------------------------------------------------------------------


def test_pypi_venv_runs_uv_sync(tmp_project, fake_run, all_tools_present):
    _make_pypi(str(tmp_project))
    rc = run_install({"target": "venv"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["uv", "sync", "--all-packages"]


def test_npm_venv_runs_npm_install(tmp_project, fake_run, all_tools_present):
    _make_npm(str(tmp_project))
    rc = run_install({"target": "venv"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["npm", "install"]


def test_go_venv_skipped_with_clear_message(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_go(str(tmp_project))
    rc = run_install({"target": "venv"}, project_root=".")
    # Go has no venv concept -> no command runs, clear skip message shown.
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping go" in captured.out
    assert "--target venv not supported" in captured.out


# ---------------------------------------------------------------------------
# Hex (Elixir)
# ---------------------------------------------------------------------------


def test_hex_global_runs_mix_deps_get(tmp_project, fake_run, all_tools_present):
    _make_hex(str(tmp_project))
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["mix", "deps.get"]


def test_hex_venv_also_runs_mix_deps_get(tmp_project, fake_run, all_tools_present):
    """Hex has no global/local distinction; --target venv returns the same spec."""
    _make_hex(str(tmp_project))
    rc = run_install({"target": "venv"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["mix", "deps.get"]


def test_hex_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_hex(str(tmp_project))
    rc = run_install({"target": "global", "uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping hex uninstall" in captured.out


# ---------------------------------------------------------------------------
# Deno
# ---------------------------------------------------------------------------


def test_deno_global_runs_deno_install(tmp_project, fake_run, all_tools_present):
    _make_deno(str(tmp_project))
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["deno", "install"]


def test_deno_venv_runs_deno_cache(tmp_project, fake_run, all_tools_present):
    _make_deno(str(tmp_project))
    rc = run_install({"target": "venv"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["deno", "cache", "."]


def test_deno_uninstall_uses_project_name(
    tmp_project, fake_run, all_tools_present
):
    _make_deno(str(tmp_project), name="mydeno")
    rc = run_install({"target": "global", "uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls[0]["cmd"] == ["deno", "uninstall", "mydeno"]


# ---------------------------------------------------------------------------
# Zig
# ---------------------------------------------------------------------------


def test_zig_global_runs_zig_build_install(tmp_project, fake_run, all_tools_present):
    _make_zig(str(tmp_project))
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["zig", "build", "install"]


def test_zig_venv_skipped_with_clear_message(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_zig(str(tmp_project))
    rc = run_install({"target": "venv"}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping zig" in captured.out
    assert "--target venv not supported" in captured.out


def test_zig_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_zig(str(tmp_project))
    rc = run_install({"target": "global", "uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping zig uninstall" in captured.out


# ---------------------------------------------------------------------------
# Swift
# ---------------------------------------------------------------------------


def test_swift_global_runs_swift_build(tmp_project, fake_run, all_tools_present):
    _make_swift(str(tmp_project))
    rc = run_install({"target": "global"}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["swift", "build"]


def test_swift_venv_skipped_with_clear_message(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_swift(str(tmp_project))
    rc = run_install({"target": "venv"}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping swift" in captured.out
    assert "--target venv not supported" in captured.out


def test_swift_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_swift(str(tmp_project))
    rc = run_install({"target": "global", "uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping swift uninstall" in captured.out


# ---------------------------------------------------------------------------
# CLI: --target is one required choice, not two optional bools
# ---------------------------------------------------------------------------


def _dev_install_flag_names():
    from rlsbl import app

    return {f.name for f in app._groups["dev"].commands["install"].flags}


def test_cli_install_mode_is_a_single_target_flag():
    """The install mode is one --target choice; the old bools are gone."""
    names = _dev_install_flag_names()
    assert "target" in names
    assert "global" not in names
    assert "venv" not in names


def test_cli_target_declares_exactly_the_two_modes():
    from rlsbl import app

    flag = next(
        f for f in app._groups["dev"].commands["install"].flags if f.name == "target"
    )
    assert flag.choices == ["global", "venv"]
    # Every choices entry is a value-plus-help record since strictcli 0.41, and
    # both modes carry help.
    assert [c.value for c in flag.choice_records] == ["global", "venv"]
    assert all(c.help for c in flag.choice_records)
    # Required, so the caller must state the mode explicitly -- and a required
    # declaration carries no default at all.
    assert flag.presence == "required"


def test_cli_target_is_required(tmp_path, monkeypatch):
    """Omitting --target must fail at parse time rather than assuming global."""
    from rlsbl import app

    _make_pypi(str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = app.test(["dev", "install"])
    assert result.exit_code != 0
    assert "--target" in result.stderr
    assert "required" in result.stderr


def test_cli_target_rejects_an_unknown_mode(tmp_path, monkeypatch):
    from rlsbl import app

    _make_pypi(str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = app.test(["dev", "install", "--target", "system"])
    assert result.exit_code != 0
    assert "target" in result.stderr


def _make_cli_pypi_project(root):
    """A pypi project with .rlsbl/ so the CLI resolves it as a project root."""
    _make_pypi(str(root))
    rlsbl_dir = os.path.join(str(root), ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    with open(os.path.join(rlsbl_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"targets": ["pypi"], "publish_mode": "ci"}, f)


def test_cli_target_venv_selects_the_venv_command(tmp_path, monkeypatch, fake_run, all_tools_present):
    """--target venv reaches the venv spec end to end through the CLI."""
    from rlsbl import app

    _make_cli_pypi_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = app.test(["dev", "install", "--target", "venv"])
    assert result.exit_code == 0
    assert [c["cmd"] for c in fake_run.calls] == [["uv", "sync", "--all-packages"]]


def test_cli_target_global_selects_the_global_command(tmp_path, monkeypatch, fake_run, all_tools_present):
    from rlsbl import app

    _make_cli_pypi_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = app.test(["dev", "install", "--target", "global"])
    assert result.exit_code == 0
    assert [c["cmd"] for c in fake_run.calls] == [["uv", "tool", "install", "-e", "."]]


def test_run_install_requires_an_explicit_mode(tmp_project, fake_run, all_tools_present):
    """The internal entry point has no implicit mode either."""
    _make_pypi(str(tmp_project))
    with pytest.raises(ValueError) as excinfo:
        run_install({}, project_root=".")
    assert "target" in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI: workspace-root invocation guidance
# ---------------------------------------------------------------------------


def test_cli_workspace_root_gives_cd_guidance(tmp_path, monkeypatch):
    """CLI-level: at a monorepo workspace root, `rlsbl dev install` must name
    the workspace-mode selector -- never misleadingly suggest `rlsbl monorepo
    add`, since the workspace root is a registered member's directory."""
    from rlsbl import app

    ws_dir = tmp_path / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text(workspace_toml('[[projects]]\npath = "py"\nname = "py"\n'))
    (tmp_path / "py").mkdir()
    monkeypatch.chdir(tmp_path)

    result = app.test(["dev", "install", "--target", "global"])
    assert result.exit_code == 1
    assert "--all, --include, or --exclude" in result.stderr
    assert "monorepo add" not in result.stderr


# ---------------------------------------------------------------------------
# Help accuracy
# ---------------------------------------------------------------------------


def _install_modes():
    """Return (global, venv, uninstall) target-name sets from the live registry."""
    from rlsbl.targets import TARGETS

    supports_global, supports_venv, supports_uninstall = set(), set(), set()
    for name, target_cls in TARGETS.items():
        # A directory that exists but holds no manifest: every target returns
        # the generic shape of its command rather than introspecting a project.
        modes = target_cls.dev_install_command(os.path.dirname(__file__))
        if modes.get("global"):
            supports_global.add(name)
            if modes["global"].get("uninstall_args_template"):
                supports_uninstall.add(name)
        if modes.get("venv"):
            supports_venv.add(name)
    return supports_global, supports_venv, supports_uninstall


def test_dev_install_help_matches_the_real_supported_sets():
    """`dev install`'s help must name exactly the targets that support each mode.

    The help used to claim a flat "installs system-wide across N supported
    targets" list, which was wrong twice over: three of those targets have no
    system-wide install at all, and the venv/uninstall sets are smaller
    still. Derive the sets from the target registry so the text cannot drift.
    """
    from rlsbl import app

    help_text = app._groups["dev"].commands["install"].help
    supports_global, supports_venv, supports_uninstall = _install_modes()

    assert f"{len(supports_global)} targets" in help_text
    global_part, venv_part = help_text.split("--target venv is supported by", 1)
    for name in supports_global:
        assert name in global_part, f"{name} supports --target global but is unlisted"

    venv_sentence = venv_part.split("--uninstall", 1)[0]
    for name in supports_venv:
        assert name in venv_sentence, f"{name} supports --target venv but is unlisted"
    for name in supports_global - supports_venv:
        assert name not in venv_sentence, f"{name} does not support --target venv"

    uninstall_sentence = venv_part.split("--uninstall", 1)[1]
    for name in supports_uninstall:
        assert name in uninstall_sentence
    for name in supports_global - supports_uninstall:
        assert name not in uninstall_sentence


def test_pre_push_check_help_says_it_is_removed():
    """The stub's help must not describe a check it no longer performs."""
    from rlsbl import app

    help_text = app._commands["pre-push-check"].help
    assert help_text.startswith("Removed.")
    assert "CHANGELOG.md" not in help_text
    assert "check --tag prepush" in help_text
