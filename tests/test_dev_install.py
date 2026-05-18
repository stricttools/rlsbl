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
    rc = run_install({})
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["uv", "tool", "install", "-e", "."]
    assert fake_run.calls[0]["kwargs"]["cwd"] == "."


def test_npm_install_runs_npm_link(tmp_project, fake_run, all_tools_present):
    _make_npm(str(tmp_project))
    rc = run_install({})
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["npm", "link"]


def test_go_install_runs_go_install(tmp_project, fake_run, all_tools_present):
    _make_go(str(tmp_project))
    rc = run_install({})
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["cmd"] == ["go", "install", "./..."]


def test_pypi_uninstall_uses_project_name(tmp_project, fake_run, all_tools_present):
    _make_pypi(str(tmp_project), name="rlsbl-test-pkg")
    rc = run_install({"uninstall": True})
    assert rc == 0
    assert fake_run.calls[0]["cmd"] == ["uv", "tool", "uninstall", "rlsbl-test-pkg"]


def test_go_uninstall_is_skipped(tmp_project, fake_run, all_tools_present, capsys):
    _make_go(str(tmp_project))
    rc = run_install({"uninstall": True})
    # Nothing actually ran -- go cannot be cleanly uninstalled.
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping go uninstall" in captured.out


def test_no_targets_returns_error(tmp_project, fake_run, all_tools_present, capsys):
    rc = run_install({})
    assert rc == 1
    captured = capsys.readouterr()
    assert "no targets detected" in captured.err


def test_unsupported_target_skipped(tmp_project, fake_run, all_tools_present, capsys):
    # `docs` is a registered target but not in INSTALL_COMMANDS
    os.makedirs(str(tmp_project / ".rlsbl"))
    with open(str(tmp_project / ".rlsbl" / "config.json"), "w") as f:
        json.dump({"targets": ["docs"]}, f)
    rc = run_install({})
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
    rc = run_install({})
    assert rc == 0
    assert fake_run.calls == []
    captured = capsys.readouterr()
    assert "Skipping pypi" in captured.out
    assert "uv not on PATH" in captured.out


def test_install_failure_returns_nonzero(tmp_project, monkeypatch, all_tools_present):
    _make_pypi(str(tmp_project))
    cap = _Capture(returncode=2)
    monkeypatch.setattr("rlsbl.commands.dev.subprocess.run", cap)
    rc = run_install({})
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
    rc = run_install({})
    assert rc == 1
    captured = capsys.readouterr()
    assert "monorepo mode" in captured.err
    assert "--all" in captured.err
    assert fake_run.calls == []


def test_monorepo_all_iterates_all_projects(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_monorepo(tmp_project)
    rc = run_install({"all": True})
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
    rc = run_install({"include": "pyproj,nodeproj"})
    assert rc == 0
    tools = [c["cmd"][0] for c in fake_run.calls]
    assert sorted(tools) == ["npm", "uv"]
    captured = capsys.readouterr()
    assert "=== goproj ===" not in captured.out


def test_monorepo_exclude_filters(tmp_project, fake_run, all_tools_present, capsys):
    _make_monorepo(tmp_project)
    rc = run_install({"exclude": "goproj"})
    assert rc == 0
    tools = [c["cmd"][0] for c in fake_run.calls]
    assert sorted(tools) == ["npm", "uv"]
    captured = capsys.readouterr()
    assert "=== goproj ===" not in captured.out


def test_monorepo_empty_filter_errors(
    tmp_project, fake_run, all_tools_present, capsys
):
    _make_monorepo(tmp_project)
    rc = run_install({"include": "nonexistent"})
    assert rc == 1
    captured = capsys.readouterr()
    assert "No projects matched" in captured.err
