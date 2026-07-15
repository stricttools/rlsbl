"""Tests for `rlsbl dev sync` -- local editable overlays driven by
dev-sources.toml.local-only.

The overlay file lists sibling checkouts to install editable on top of the
locked environment. `rlsbl dev sync` runs one `uv sync --inexact` excluding
every overlaid package, then `uv pip install -e <path>` per entry, so the
locked registry wheels never clobber the local checkouts.
"""

import os
import subprocess

import pytest

from rlsbl.commands.dev_sync import OVERRIDES_FILENAME, run_sync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Capture:
    """Record subprocess.run calls without executing them."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=self.returncode)


@pytest.fixture
def fake_run(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr("rlsbl.commands.dev_sync.subprocess.run", cap)
    return cap


@pytest.fixture
def uv_present(monkeypatch):
    monkeypatch.setattr(
        "rlsbl.commands.dev_sync.require_tool",
        lambda name, purpose=None, fatal=True: f"/fake/bin/{name}",
    )


@pytest.fixture
def no_sync_env(monkeypatch):
    """The UV_NO_SYNC=1 gate requirement, satisfied."""
    monkeypatch.setenv("UV_NO_SYNC", "1")


def _write_overlay_file(root, content):
    with open(os.path.join(str(root), OVERRIDES_FILENAME), "w", encoding="utf-8") as f:
        f.write(content)


def _make_overlay_project(root, dirname, name, version="0.3.1"):
    """Create a minimal installable project (pyproject.toml) to overlay."""
    proj = root / dirname
    proj.mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return proj


# ---------------------------------------------------------------------------
# UV_NO_SYNC gate
# ---------------------------------------------------------------------------


def test_gate_errors_when_uv_no_sync_unset(
    tmp_project, fake_run, uv_present, monkeypatch, capsys
):
    monkeypatch.delenv("UV_NO_SYNC", raising=False)
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    err = capsys.readouterr().err
    assert "UV_NO_SYNC" in err
    assert "export UV_NO_SYNC=1" in err
    # The message explains WHY the gate exists.
    assert "uv run" in err


def test_gate_errors_when_uv_no_sync_wrong_value(
    tmp_project, fake_run, uv_present, monkeypatch, capsys
):
    monkeypatch.setenv("UV_NO_SYNC", "0")
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "UV_NO_SYNC" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Overlay file parsing errors (all hard errors, never silent no-ops)
# ---------------------------------------------------------------------------


def test_missing_file_is_hard_error_with_howto(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    err = capsys.readouterr().err
    assert OVERRIDES_FILENAME in err
    # The error shows the exact file format so agents can create it.
    assert "[[overlay]]" in err
    assert "package" in err
    assert "path" in err


def test_invalid_toml_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _write_overlay_file(tmp_project, "not [ valid toml ===")
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert OVERRIDES_FILENAME in capsys.readouterr().err


def test_unknown_top_level_key_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _make_overlay_project(tmp_project, "dep", "depa")
    _write_overlay_file(
        tmp_project,
        '[[overlay]]\npackage = "depa"\npath = "dep"\n\n[extra]\nfoo = 1\n',
    )
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "extra" in capsys.readouterr().err


def test_unknown_entry_key_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _make_overlay_project(tmp_project, "dep", "depa")
    _write_overlay_file(
        tmp_project,
        '[[overlay]]\npackage = "depa"\npath = "dep"\neditable = true\n',
    )
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "editable" in capsys.readouterr().err


def test_missing_package_key_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _write_overlay_file(tmp_project, '[[overlay]]\npath = "dep"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "package" in capsys.readouterr().err


def test_missing_path_key_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "path" in capsys.readouterr().err


def test_empty_overlay_list_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _write_overlay_file(tmp_project, "")
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    err = capsys.readouterr().err
    assert "[[overlay]]" in err


def test_nonexistent_path_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _write_overlay_file(
        tmp_project, '[[overlay]]\npackage = "depa"\npath = "no-such-dir"\n'
    )
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "no-such-dir" in capsys.readouterr().err


def test_path_without_pyproject_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    (tmp_project / "dep").mkdir()
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\npath = "dep"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "pyproject.toml" in capsys.readouterr().err


def test_package_name_mismatch_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    """If the entry's `package` doesn't match the checkout's [project].name,
    --no-install-package would not match and the next sync would wipe the
    overlay -- hard error instead."""
    _make_overlay_project(tmp_project, "dep", "actual-name")
    _write_overlay_file(
        tmp_project, '[[overlay]]\npackage = "wrong-name"\npath = "dep"\n'
    )
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    err = capsys.readouterr().err
    assert "wrong-name" in err
    assert "actual-name" in err


def test_missing_project_name_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    """A checkout whose pyproject.toml has a [project] table without 'name'
    cannot be verified against the entry's 'package', so the sync-exclusion
    guard cannot be trusted -- hard error, never a silent skip."""
    dep = tmp_project / "dep"
    dep.mkdir()
    (dep / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\npath = "dep"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    err = capsys.readouterr().err
    assert "[project].name" in err
    assert "depa" in err


def test_missing_project_table_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    """Same for a pyproject.toml with no [project] table at all (e.g. a
    legacy build-system-only project): without a declared name the guard
    cannot be verified."""
    dep = tmp_project / "dep"
    dep.mkdir()
    (dep / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n'
    )
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\npath = "dep"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "[project].name" in capsys.readouterr().err


def test_package_name_match_is_normalized(
    tmp_project, fake_run, uv_present, no_sync_env
):
    """PEP 503 normalization: My_Pkg matches my-pkg."""
    _make_overlay_project(tmp_project, "dep", "My_Pkg")
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "my-pkg"\npath = "dep"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 0


def test_missing_uv_is_hard_error(
    tmp_project, fake_run, no_sync_env, monkeypatch, capsys
):
    _make_overlay_project(tmp_project, "dep", "depa")
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\npath = "dep"\n')
    monkeypatch.setattr(
        "rlsbl.commands.dev_sync.require_tool",
        lambda name, purpose=None, fatal=True: None,
    )
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    assert "uv" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Command sequence
# ---------------------------------------------------------------------------


def _two_overlays(tmp_project):
    _make_overlay_project(tmp_project, "depa-src", "depa", version="0.3.1")
    _make_overlay_project(tmp_project, "depb-src", "depb", version="1.2.0")
    _write_overlay_file(
        tmp_project,
        '[[overlay]]\npackage = "depa"\npath = "depa-src"\n\n'
        '[[overlay]]\npackage = "depb"\npath = "depb-src"\n',
    )


def test_command_sequence_one_sync_then_editable_installs(
    tmp_project, fake_run, uv_present, no_sync_env
):
    """One `uv sync --inexact` carrying ALL exclusions, then one
    `uv pip install -e <abs path>` per entry, all at the project root."""
    _two_overlays(tmp_project)
    rc = run_sync(str(tmp_project))
    assert rc == 0
    assert len(fake_run.calls) == 3

    sync_call = fake_run.calls[0]
    assert sync_call["cmd"] == [
        "uv", "sync", "--inexact",
        "--no-install-package", "depa",
        "--no-install-package", "depb",
    ]
    assert sync_call["kwargs"]["cwd"] == str(tmp_project)

    pip_calls = fake_run.calls[1:]
    expected_paths = [
        os.path.abspath(str(tmp_project / "depa-src")),
        os.path.abspath(str(tmp_project / "depb-src")),
    ]
    for call, path in zip(pip_calls, expected_paths):
        assert call["cmd"] == ["uv", "pip", "install", "-e", path]
        assert call["kwargs"]["cwd"] == str(tmp_project)

    # VIRTUAL_ENV must be stripped from every call: `uv pip` would otherwise
    # target a leaked active venv while `uv sync` targets the project's .venv,
    # silently splitting the two steps across environments.
    for call in fake_run.calls:
        env = call["kwargs"]["env"]
        assert "VIRTUAL_ENV" not in env
        assert env.get("UV_NO_SYNC") == "1"


def test_absolute_paths_are_used_verbatim(
    tmp_project, fake_run, uv_present, no_sync_env
):
    dep = _make_overlay_project(tmp_project, "dep", "depa")
    _write_overlay_file(
        tmp_project,
        f'[[overlay]]\npackage = "depa"\npath = "{dep}"\n',
    )
    rc = run_sync(str(tmp_project))
    assert rc == 0
    assert fake_run.calls[1]["cmd"] == ["uv", "pip", "install", "-e", str(dep)]


def test_sync_failure_aborts_before_installs(
    tmp_project, uv_present, no_sync_env, monkeypatch, capsys
):
    _two_overlays(tmp_project)
    cap = _Capture(returncode=3)
    monkeypatch.setattr("rlsbl.commands.dev_sync.subprocess.run", cap)
    rc = run_sync(str(tmp_project))
    assert rc == 1
    # Only the sync ran; no editable installs after the failure.
    assert len(cap.calls) == 1
    assert "uv sync" in capsys.readouterr().err


def test_editable_install_failure_is_hard_error(
    tmp_project, uv_present, no_sync_env, monkeypatch, capsys
):
    _two_overlays(tmp_project)

    class _FailSecond(_Capture):
        def __call__(self, cmd, *args, **kwargs):
            result = super().__call__(cmd, *args, **kwargs)
            if len(self.calls) == 2:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            return result

    cap = _FailSecond()
    monkeypatch.setattr("rlsbl.commands.dev_sync.subprocess.run", cap)
    rc = run_sync(str(tmp_project))
    assert rc == 1
    # Stopped at the failed install: sync + first (failing) editable install.
    assert len(cap.calls) == 2
    assert "depa" in capsys.readouterr().err


def test_idempotent_same_commands_on_rerun(
    tmp_project, fake_run, uv_present, no_sync_env
):
    _two_overlays(tmp_project)
    assert run_sync(str(tmp_project)) == 0
    first = [c["cmd"] for c in fake_run.calls]
    assert run_sync(str(tmp_project)) == 0
    second = [c["cmd"] for c in fake_run.calls[len(first):]]
    assert first == second


# ---------------------------------------------------------------------------
# Loud output
# ---------------------------------------------------------------------------


def test_output_names_every_overlay_with_version_and_path(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    _two_overlays(tmp_project)
    rc = run_sync(str(tmp_project))
    assert rc == 0
    out = capsys.readouterr().out
    assert "depa" in out
    assert "0.3.1" in out
    assert os.path.abspath(str(tmp_project / "depa-src")) in out
    assert "depb" in out
    assert "1.2.0" in out
    assert os.path.abspath(str(tmp_project / "depb-src")) in out
    # The closing note warns that a bare `uv sync` reverts the overlays.
    assert "uv sync" in out
    assert "rlsbl dev sync" in out


def test_output_marks_dynamic_versions(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    dep = tmp_project / "dep"
    dep.mkdir()
    (dep / "pyproject.toml").write_text(
        '[project]\nname = "depa"\ndynamic = ["version"]\n'
    )
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\npath = "dep"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 0
    assert "dynamic" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Monorepo behavior
# ---------------------------------------------------------------------------


def test_workspace_root_invocation_is_hard_error(
    tmp_project, fake_run, uv_present, no_sync_env, capsys
):
    """dev sync operates per sub-project (overlays live at the sub-project
    root); invoking it at the monorepo workspace root errors with guidance."""
    ws_dir = tmp_project / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text('[[projects]]\npath = "py"\nname = "py"\n')
    rc = run_sync(str(tmp_project))
    assert rc == 1
    assert fake_run.calls == []
    err = capsys.readouterr().err
    assert "sub-project" in err


def test_cli_workspace_root_gives_cd_guidance(tmp_project, monkeypatch):
    """CLI-level: at a monorepo workspace root, `rlsbl dev sync` must say to
    cd into a sub-project -- not misleadingly suggest `rlsbl monorepo add`
    (the workspace root is not an unregistered project; it is simply the
    wrong place to run dev sync from)."""
    from rlsbl import app

    ws_dir = tmp_project / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text('[[projects]]\npath = "py"\nname = "py"\n')
    (tmp_project / "py").mkdir()
    monkeypatch.setenv("UV_NO_SYNC", "1")

    result = app.test(["dev", "sync"])
    assert result.exit_code == 1
    assert "sub-project" in result.stderr
    assert OVERRIDES_FILENAME in result.stderr
    assert "monorepo add" not in result.stderr


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def test_dev_group_lists_sync_command():
    from rlsbl import app

    result = app.test(["dev", "--help"])
    assert "sync" in result.stdout
    assert "install" in result.stdout
    # Help documents the gate and the revert/restore behavior.
    assert "UV_NO_SYNC=1" in result.stdout
    assert OVERRIDES_FILENAME in result.stdout


# ---------------------------------------------------------------------------
# End-to-end with real uv (the mocked tests above pin the exact command
# sequence; this one proves the sequence works against real uv)
# ---------------------------------------------------------------------------


def test_e2e_real_uv_overlay_survives_rerun(tmp_project, no_sync_env):
    """Real uv: overlay an editable package onto a synced project, then run
    dev sync again -- the editable overlay must survive (uv sync --inexact
    --no-install-package preserves it)."""
    # Host project with a lockable environment.
    (tmp_project / "pyproject.toml").write_text(
        '[project]\nname = "hostproj"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\ndependencies = []\n'
    )
    # Sibling checkout to overlay.
    dep = tmp_project / "depa-src"
    (dep / "src" / "depa").mkdir(parents=True)
    (dep / "src" / "depa" / "__init__.py").write_text('MARK = "editable"\n')
    (dep / "pyproject.toml").write_text(
        '[project]\nname = "depa"\nversion = "0.9.9"\n'
        'requires-python = ">=3.11"\n'
        "[build-system]\n"
        'requires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    )
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\npath = "depa-src"\n')

    assert run_sync(str(tmp_project)) == 0

    def _imported_file():
        result = subprocess.run(
            ["uv", "run", "--no-sync", "python", "-c",
             "import depa; print(depa.__file__)"],
            cwd=str(tmp_project), capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    # The import resolves to the sibling checkout (editable), not site-packages.
    assert str(dep) in _imported_file()

    # Idempotent: a second run keeps the editable overlay in place.
    assert run_sync(str(tmp_project)) == 0
    assert str(dep) in _imported_file()


# ---------------------------------------------------------------------------
# Sentinel written by run_sync (drift detection input)
# ---------------------------------------------------------------------------


def test_run_sync_writes_sentinel_with_package_path_version(
    tmp_project, fake_run, uv_present, no_sync_env
):
    """After a successful sync, run_sync records the intended overlay state so
    the drift check and `dev status` can later detect a silent wipe."""
    from rlsbl.commands.dev_sync import SENTINEL_FILENAME, _load_sentinel

    _two_overlays(tmp_project)
    assert run_sync(str(tmp_project)) == 0

    assert (tmp_project / SENTINEL_FILENAME).is_file()
    sentinel = _load_sentinel(str(tmp_project))
    assert sentinel == [
        {
            "package": "depa",
            "path": os.path.abspath(str(tmp_project / "depa-src")),
            "version": "0.3.1",
        },
        {
            "package": "depb",
            "path": os.path.abspath(str(tmp_project / "depb-src")),
            "version": "1.2.0",
        },
    ]


def test_run_sync_sentinel_records_dynamic_version_as_none(
    tmp_project, fake_run, uv_present, no_sync_env
):
    """A dynamic-version checkout has no [project].version; the sentinel stores
    it and _load_sentinel round-trips it back to None."""
    from rlsbl.commands.dev_sync import _load_sentinel

    dep = tmp_project / "dep"
    dep.mkdir()
    (dep / "pyproject.toml").write_text(
        '[project]\nname = "depa"\ndynamic = ["version"]\n'
    )
    _write_overlay_file(tmp_project, '[[overlay]]\npackage = "depa"\npath = "dep"\n')
    assert run_sync(str(tmp_project)) == 0

    sentinel = _load_sentinel(str(tmp_project))
    assert sentinel == [
        {"package": "depa", "path": str(dep), "version": None}
    ]


def test_run_sync_does_not_write_sentinel_on_failure(
    tmp_project, uv_present, no_sync_env, monkeypatch
):
    """If the editable install fails, no sentinel is written -- the recorded
    state must never claim overlays that were not actually installed."""
    from rlsbl.commands.dev_sync import SENTINEL_FILENAME

    _two_overlays(tmp_project)

    class _FailInstall(_Capture):
        def __call__(self, cmd, *args, **kwargs):
            result = super().__call__(cmd, *args, **kwargs)
            if cmd[:3] == ["uv", "pip", "install"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            return result

    monkeypatch.setattr("rlsbl.commands.dev_sync.subprocess.run", _FailInstall())
    assert run_sync(str(tmp_project)) == 1
    assert not (tmp_project / SENTINEL_FILENAME).exists()
