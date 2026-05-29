"""Tests for `rlsbl dev install` -- the per-target editable installer."""

import json
import os
import subprocess

import pytest

from rlsbl.commands.dev import run_install


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
    """Create a minimal go.mod so the go target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "go.mod"), "w", encoding="utf-8") as f:
        f.write("module example.com/foo\n\ngo 1.21\n")


def _make_cargo(dir_path, name="mycrate"):
    """Create a minimal Cargo.toml so the cargo target is detected."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "Cargo.toml"), "w", encoding="utf-8") as f:
        f.write(f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n')


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
    monkeypatch.setattr("rlsbl.commands.dev.subprocess.run", cap)
    return cap


@pytest.fixture
def all_tools_present(monkeypatch):
    """Pretend every tool is on PATH."""
    monkeypatch.setattr(
        "rlsbl.commands.dev.require_tool",
        lambda name, purpose=None, fatal=True: f"/fake/bin/{name}",
    )


# ---------------------------------------------------------------------------
# Single-project install
# ---------------------------------------------------------------------------


def test_pypi_install_runs_uv_tool_install(tmp_project, fake_run, all_tools_present):
    _make_pypi(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["uv", "tool", "install", "-e", "."]
    assert fake_run.calls[0]["kwargs"]["cwd"] == "."


def test_npm_install_runs_npm_link(tmp_project, fake_run, all_tools_present):
    _make_npm(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["npm", "link"]


def test_go_install_runs_go_install(tmp_project, fake_run, all_tools_present):
    _make_go(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["go", "install", "./..."]


def test_pypi_uninstall_uses_project_name(tmp_project, fake_run, all_tools_present):
    _make_pypi(str(tmp_project), name="rlsbl-test-pkg")
    rc = run_install({"uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls[0]["cmd"] == ["uv", "tool", "uninstall", "rlsbl-test-pkg"]


def test_go_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_go(str(tmp_project))
    rc = run_install({"uninstall": True}, project_root=".")
    # Nothing actually ran -- go cannot be cleanly uninstalled.
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping go uninstall" in captured.out


def test_no_targets_returns_error(tmp_project, fake_run, all_tools_present, capsys):
    rc = run_install({}, project_root=".")
    assert rc == 1
    captured = capsys.readouterr()
    assert "no targets detected" in captured.err


def test_unsupported_target_skipped(tmp_project, fake_run, all_tools_present, capsys):
    # docs is a registered target with no dev_install_command override (inherits None from BaseTarget)
    os.makedirs(str(tmp_project / ".rlsbl"))
    with open(str(tmp_project / ".rlsbl" / "config.json"), "w") as f:
        json.dump({"targets": ["docs"]}, f)
    rc = run_install({}, project_root=".")
    # No supported targets -> nothing ran, no error.
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping docs" in captured.out
    assert "install not yet supported" in captured.out


def test_missing_tool_is_skipped(tmp_project, fake_run, monkeypatch, capsys):
    _make_pypi(str(tmp_project))
    # All tools missing.
    monkeypatch.setattr(
        "rlsbl.commands.dev.require_tool",
        lambda name, purpose=None, fatal=True: None,
    )
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping pypi" in captured.out
    assert "uv not on PATH" in captured.out


def test_install_failure_returns_nonzero(tmp_project, monkeypatch, all_tools_present):
    _make_pypi(str(tmp_project))
    cap = _Capture(returncode=2)
    monkeypatch.setattr("rlsbl.commands.dev.subprocess.run", cap)
    rc = run_install({}, project_root=".")
    assert rc == 1
    assert len(cap.calls) == 1


# ---------------------------------------------------------------------------
# Monorepo install
# ---------------------------------------------------------------------------


def _make_monorepo(root):
    """Create a workspace with three sub-projects (pypi, npm, go)."""
    ws_dir = root / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text(
        '[[projects]]\npath = "py"\nname = "pyproj"\n\n'
        '[[projects]]\npath = "node"\nname = "nodeproj"\n\n'
        '[[projects]]\npath = "gocode"\nname = "goproj"\n'
    )
    _make_pypi(str(root / "py"), name="pyproj")
    _make_npm(str(root / "node"), name="nodeproj")
    _make_go(str(root / "gocode"))


def test_monorepo_without_flags_errors(tmp_project, fake_run, all_tools_present, capsys):
    _make_monorepo(tmp_project)
    rc = run_install({}, project_root=".")
    assert rc == 1
    captured = capsys.readouterr()
    assert "monorepo mode" in captured.err
    assert "--all" in captured.err
    assert fake_run.calls == []


def test_monorepo_all_iterates_all_projects(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_monorepo(tmp_project)
    rc = run_install({"all": True}, project_root=".")
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
    rc = run_install({"include": "pyproj,nodeproj"}, project_root=".")
    assert rc == 0
    tools = [c["cmd"][0] for c in fake_run.calls]
    assert sorted(tools) == ["npm", "uv"]
    captured = capsys.readouterr()
    assert "=== goproj ===" not in captured.out


def test_monorepo_exclude_filters(tmp_project, fake_run, all_tools_present, capsys):
    _make_monorepo(tmp_project)
    rc = run_install({"exclude": "goproj"}, project_root=".")
    assert rc == 0
    tools = [c["cmd"][0] for c in fake_run.calls]
    assert sorted(tools) == ["npm", "uv"]
    captured = capsys.readouterr()
    assert "=== goproj ===" not in captured.out


def test_monorepo_empty_filter_errors(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_monorepo(tmp_project)
    rc = run_install({"include": "nonexistent"}, project_root=".")
    assert rc == 1
    captured = capsys.readouterr()
    assert "No projects matched" in captured.err


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
    rc = run_install({"all": True, "uninstall": True}, project_root=".")
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
    rc = run_install({"include": "pyproj,nodeproj", "uninstall": True}, project_root=".")
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
    rc = run_install({"exclude": "goproj", "uninstall": True}, project_root=".")
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
# --venv mode
# ---------------------------------------------------------------------------


def test_pypi_venv_runs_uv_sync(tmp_project, fake_run, all_tools_present):
    _make_pypi(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["uv", "sync"]


def test_npm_venv_runs_npm_install(tmp_project, fake_run, all_tools_present):
    _make_npm(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["npm", "install"]


def test_go_venv_skipped_with_clear_message(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_go(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    # Go has no venv concept -> no command runs, clear skip message shown.
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping go" in captured.out
    assert "--venv not supported" in captured.out


def test_cargo_venv_skipped_with_clear_message(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_cargo(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping cargo" in captured.out
    assert "--venv not supported" in captured.out


def test_cargo_global_runs_cargo_install(tmp_project, fake_run, all_tools_present):
    _make_cargo(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["cargo", "install", "--path", "."]


# ---------------------------------------------------------------------------
# Hex (Elixir)
# ---------------------------------------------------------------------------


def test_hex_global_runs_mix_deps_get(tmp_project, fake_run, all_tools_present):
    _make_hex(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["mix", "deps.get"]


def test_hex_venv_also_runs_mix_deps_get(tmp_project, fake_run, all_tools_present):
    """Hex has no global/local distinction; --venv returns the same spec."""
    _make_hex(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["mix", "deps.get"]


def test_hex_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_hex(str(tmp_project))
    rc = run_install({"uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping hex uninstall" in captured.out


# ---------------------------------------------------------------------------
# Deno
# ---------------------------------------------------------------------------


def test_deno_global_runs_deno_install(tmp_project, fake_run, all_tools_present):
    _make_deno(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["deno", "install"]


def test_deno_venv_runs_deno_cache(tmp_project, fake_run, all_tools_present):
    _make_deno(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["deno", "cache", "."]


def test_deno_uninstall_uses_project_name(
    tmp_project, fake_run, all_tools_present
):
    _make_deno(str(tmp_project), name="mydeno")
    rc = run_install({"uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls[0]["cmd"] == ["deno", "uninstall", "mydeno"]


# ---------------------------------------------------------------------------
# Zig
# ---------------------------------------------------------------------------


def test_zig_global_runs_zig_build_install(tmp_project, fake_run, all_tools_present):
    _make_zig(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["zig", "build", "install"]


def test_zig_venv_skipped_with_clear_message(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_zig(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping zig" in captured.out
    assert "--venv not supported" in captured.out


def test_zig_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_zig(str(tmp_project))
    rc = run_install({"uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping zig uninstall" in captured.out


# ---------------------------------------------------------------------------
# Swift
# ---------------------------------------------------------------------------


def test_swift_global_runs_swift_build(tmp_project, fake_run, all_tools_present):
    _make_swift(str(tmp_project))
    rc = run_install({}, project_root=".")
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["swift", "build"]


def test_swift_venv_skipped_with_clear_message(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_swift(str(tmp_project))
    rc = run_install({"venv": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping swift" in captured.out
    assert "--venv not supported" in captured.out


def test_swift_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_swift(str(tmp_project))
    rc = run_install({"uninstall": True}, project_root=".")
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping swift uninstall" in captured.out


# ---------------------------------------------------------------------------
# CLI: --global / --venv mutual exclusion
# ---------------------------------------------------------------------------


def test_cli_global_and_venv_are_mutually_exclusive():
    """`rlsbl dev install --global --venv` must exit non-zero with a clear error.

    We exercise the strictcli-wired handler directly to verify the guard
    rather than spawning a subprocess.
    """
    import pytest as _pytest

    from rlsbl import cmd_dev_install

    with _pytest.raises(SystemExit) as excinfo:
        # We expect the handler to bail before touching the filesystem, so
        # passing no project setup is fine.
        cmd_dev_install(
            all=False,
            include="",
            exclude="",
            uninstall=False,
            global_=True,
            venv=True,
        )
    assert excinfo.value.code == 2
