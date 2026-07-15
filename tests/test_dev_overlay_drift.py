"""Tests for the dev-sync overlay sentinel, the `dev-overlay-drift` check, and
`rlsbl dev status`.

`rlsbl dev sync` overlays local editable checkouts onto a locked venv and
writes a sentinel (dev-overlays-state.toml.local-only) recording the intended
state. Any later bare `uv sync`/`uv run` silently reinstalls the locked
registry wheel over an overlay, so tests would then run against stale RELEASED
dependency code with no error. The drift check and `dev status` detect that by
comparing the sentinel against the venv's dist-info direct_url.json.

The venv layout is constructed by hand (dist-info dirs) rather than by really
running uv, so the tests are hermetic and fast.
"""

import json
import os

import pytest

from rlsbl import app
from rlsbl.context import create_context
from rlsbl.overlay_state import (
    OVERLAY_HEALTHY,
    OVERLAY_MISSING,
    OVERLAY_WIPED,
    SENTINEL_FILENAME,
    MalformedSentinelError,
    classify_overlay,
    inspect_installed,
    load_sentinel,
)
from rlsbl.commands.dev_sync import (
    _write_sentinel,
    run_status,
)


# ---------------------------------------------------------------------------
# Fake venv / sentinel builders
# ---------------------------------------------------------------------------


def _site_packages(root):
    return root / ".venv" / "lib" / "python3.13" / "site-packages"


def _make_dist_info(root, name, version, *, editable=None, url_path=None):
    """Create a ``<name>-<version>.dist-info`` under the project's fake venv.

    editable=None -> no direct_url.json (registry wheel, overlay wiped).
    editable=True/False with url_path -> a direct_url.json pointing at url_path.
    """
    site = _site_packages(root)
    di = site / f"{name.replace('-', '_')}-{version}.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
    )
    if editable is not None:
        du = {"url": f"file://{url_path}", "dir_info": {"editable": editable}}
        (di / "direct_url.json").write_text(json.dumps(du))
    return di


def _write_sentinel_entries(root, entries):
    """Write the sentinel via the production writer. *entries* is a list of
    {"package", "path", "version"} dicts."""
    _write_sentinel(str(root), entries)


def _ctx(root):
    return create_context(root)


def _run_drift_check(root):
    return app._check_defs["dev-overlay-drift"].impl(_ctx(root))


# ---------------------------------------------------------------------------
# Sentinel round-trip
# ---------------------------------------------------------------------------


def test_sentinel_write_and_load_roundtrip(tmp_project):
    overlays = [
        {"package": "depa", "path": "/abs/depa", "version": "0.3.1"},
        {"package": "depb", "path": "/abs/depb", "version": None},
    ]
    _write_sentinel(str(tmp_project), overlays)

    assert (tmp_project / SENTINEL_FILENAME).is_file()
    loaded = load_sentinel(str(tmp_project))
    assert loaded == [
        {"package": "depa", "path": "/abs/depa", "version": "0.3.1"},
        # None version round-trips as None (stored as "" in the file).
        {"package": "depb", "path": "/abs/depb", "version": None},
    ]


def test_sentinel_write_is_atomic_no_tmp_left(tmp_project):
    _write_sentinel(str(tmp_project), [{"package": "d", "path": "/x", "version": "1.0"}])
    assert not (tmp_project / (SENTINEL_FILENAME + ".tmp")).exists()


def test_load_sentinel_absent_returns_none(tmp_project):
    assert load_sentinel(str(tmp_project)) is None


def test_load_sentinel_malformed_raises(tmp_project):
    """A present-but-unparseable sentinel is a hard error, never a silent empty
    list: reading corruption as "no overlays" would make the drift check SKIP
    and `dev status` exit 0 while overlays may in fact be wiped."""
    (tmp_project / SENTINEL_FILENAME).write_text("this is ] not [ toml ==")
    with pytest.raises(MalformedSentinelError) as exc:
        load_sentinel(str(tmp_project))
    msg = str(exc.value)
    assert SENTINEL_FILENAME in msg
    assert "delete it" in msg.lower()
    assert "rlsbl dev sync" in msg


def test_load_sentinel_unreadable_raises(tmp_project):
    """A present-but-unreadable sentinel (OSError) is also a hard error --
    unreadable is not the same as absent."""
    path = tmp_project / SENTINEL_FILENAME
    path.write_text('[[overlay]]\npackage = "depa"\npath = "/x"\nversion = ""\n')
    os.chmod(path, 0o000)
    try:
        # If the test runs as root, chmod 000 does not block reads; skip then.
        if os.access(path, os.R_OK):
            pytest.skip("cannot make file unreadable (running as root)")
        with pytest.raises(MalformedSentinelError) as exc:
            load_sentinel(str(tmp_project))
        assert SENTINEL_FILENAME in str(exc.value)
    finally:
        os.chmod(path, 0o644)


# ---------------------------------------------------------------------------
# inspect_installed / classify_overlay
# ---------------------------------------------------------------------------


def test_inspect_editable_install(tmp_project):
    checkout = tmp_project / "depa-src"
    _make_dist_info(tmp_project, "depa", "0.3.1", editable=True, url_path=str(checkout))
    installed = inspect_installed(str(tmp_project), "depa")
    assert installed["found"] is True
    assert installed["editable"] is True
    assert os.path.realpath(installed["path"]) == os.path.realpath(str(checkout))
    assert installed["version"] == "0.3.1"


def test_inspect_registry_wheel_not_editable(tmp_project):
    _make_dist_info(tmp_project, "depa", "0.3.1")  # no direct_url.json
    installed = inspect_installed(str(tmp_project), "depa")
    assert installed["found"] is True
    assert installed["editable"] is False


def test_inspect_missing_package(tmp_project):
    installed = inspect_installed(str(tmp_project), "depa")
    assert installed["found"] is False


def test_inspect_matches_normalized_name(tmp_project):
    # dist-info Name uses My_Pkg; lookup uses my-pkg.
    _make_dist_info(tmp_project, "My_Pkg", "1.0", editable=True, url_path="/x")
    installed = inspect_installed(str(tmp_project), "my-pkg")
    assert installed["found"] is True


def test_classify_healthy(tmp_project):
    checkout = tmp_project / "depa-src"
    _make_dist_info(tmp_project, "depa", "0.3.1", editable=True, url_path=str(checkout))
    entry = {"package": "depa", "path": str(checkout), "version": "0.3.1"}
    state, detail = classify_overlay(entry, inspect_installed(str(tmp_project), "depa"))
    assert state == OVERLAY_HEALTHY
    assert "depa" in detail


def test_classify_wiped_registry_wheel(tmp_project):
    checkout = tmp_project / "depa-src"
    _make_dist_info(tmp_project, "depa", "0.3.1")  # registry wheel
    entry = {"package": "depa", "path": str(checkout), "version": "0.3.1"}
    state, detail = classify_overlay(entry, inspect_installed(str(tmp_project), "depa"))
    assert state == OVERLAY_WIPED
    assert "rlsbl dev sync" in detail


def test_classify_wiped_editable_wrong_path(tmp_project):
    _make_dist_info(tmp_project, "depa", "0.3.1", editable=True, url_path="/somewhere/else")
    entry = {"package": "depa", "path": str(tmp_project / "depa-src"), "version": "0.3.1"}
    state, detail = classify_overlay(entry, inspect_installed(str(tmp_project), "depa"))
    assert state == OVERLAY_WIPED


def test_classify_missing(tmp_project):
    entry = {"package": "depa", "path": "/abs/depa", "version": "0.3.1"}
    state, detail = classify_overlay(entry, inspect_installed(str(tmp_project), "depa"))
    assert state == OVERLAY_MISSING
    assert "rlsbl dev sync" in detail


# ---------------------------------------------------------------------------
# dev-overlay-drift check
# ---------------------------------------------------------------------------


def test_check_skips_without_sentinel(mock_git_repo):
    result = _run_drift_check(mock_git_repo)
    assert result.status == "skip"


def test_check_skips_empty_sentinel(mock_git_repo):
    (mock_git_repo / SENTINEL_FILENAME).write_text("")
    result = _run_drift_check(mock_git_repo)
    assert result.status == "skip"


def test_check_passes_healthy_overlay(mock_git_repo):
    checkout = mock_git_repo / "depa-src"
    _make_dist_info(mock_git_repo, "depa", "0.3.1", editable=True, url_path=str(checkout))
    _write_sentinel_entries(
        mock_git_repo, [{"package": "depa", "path": str(checkout), "version": "0.3.1"}]
    )
    result = _run_drift_check(mock_git_repo)
    assert result.status == "pass"


def test_check_fails_wiped_overlay_names_package(mock_git_repo):
    checkout = mock_git_repo / "depa-src"
    _make_dist_info(mock_git_repo, "depa", "0.3.1")  # registry wheel = wiped
    _write_sentinel_entries(
        mock_git_repo, [{"package": "depa", "path": str(checkout), "version": "0.3.1"}]
    )
    result = _run_drift_check(mock_git_repo)
    assert result.status == "fail"
    blob = result.message + " " + " ".join(result.details or [])
    assert "depa" in blob
    assert "rlsbl dev sync" in blob


def test_check_fails_missing_overlay(mock_git_repo):
    _write_sentinel_entries(
        mock_git_repo, [{"package": "depa", "path": "/abs/depa", "version": "0.3.1"}]
    )
    result = _run_drift_check(mock_git_repo)
    assert result.status == "fail"


def test_check_fails_malformed_sentinel(mock_git_repo):
    """A corrupt sentinel must FAIL the check loudly, never SKIP: reading it as
    "no overlays" would hide overlays that may in fact be wiped."""
    (mock_git_repo / SENTINEL_FILENAME).write_text("this is ] not [ toml ==")
    result = _run_drift_check(mock_git_repo)
    assert result.status == "fail"
    assert SENTINEL_FILENAME in result.message
    assert "rlsbl dev sync" in result.message


def test_check_severity_is_error():
    assert app._check_defs["dev-overlay-drift"].severity == "error"


# ---------------------------------------------------------------------------
# dev status command
# ---------------------------------------------------------------------------


def test_status_no_sentinel_exit_zero(tmp_project, capsys):
    rc = run_status(str(tmp_project))
    assert rc == 0
    assert "No dev overlays declared" in capsys.readouterr().out


def test_status_empty_sentinel_exit_zero(tmp_project, capsys):
    (tmp_project / SENTINEL_FILENAME).write_text("")
    rc = run_status(str(tmp_project))
    assert rc == 0


def test_status_malformed_sentinel_exit_one(tmp_project, capsys):
    """A corrupt sentinel must exit nonzero with a message, never a silent 0."""
    (tmp_project / SENTINEL_FILENAME).write_text("this is ] not [ toml ==")
    rc = run_status(str(tmp_project))
    assert rc == 1
    err = capsys.readouterr().err
    assert SENTINEL_FILENAME in err
    assert "rlsbl dev sync" in err


def test_status_healthy_exit_zero(tmp_project, capsys):
    checkout = tmp_project / "depa-src"
    _make_dist_info(tmp_project, "depa", "0.3.1", editable=True, url_path=str(checkout))
    _write_sentinel_entries(
        tmp_project, [{"package": "depa", "path": str(checkout), "version": "0.3.1"}]
    )
    rc = run_status(str(tmp_project))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "depa" in out
    assert "intact" in out


def test_status_wiped_exit_one(tmp_project, capsys):
    checkout = tmp_project / "depa-src"
    _make_dist_info(tmp_project, "depa", "0.3.1")  # registry wheel = wiped
    _write_sentinel_entries(
        tmp_project, [{"package": "depa", "path": str(checkout), "version": "0.3.1"}]
    )
    rc = run_status(str(tmp_project))
    assert rc == 1
    captured = capsys.readouterr()
    assert "[WIPED]" in captured.out
    assert "drifted" in captured.err


def test_status_missing_exit_one(tmp_project, capsys):
    _write_sentinel_entries(
        tmp_project, [{"package": "depa", "path": "/abs/depa", "version": "0.3.1"}]
    )
    rc = run_status(str(tmp_project))
    assert rc == 1
    assert "[MISSING]" in capsys.readouterr().out


def test_status_mixed_exit_one(tmp_project, capsys):
    healthy = tmp_project / "depa-src"
    _make_dist_info(tmp_project, "depa", "0.3.1", editable=True, url_path=str(healthy))
    _make_dist_info(tmp_project, "depb", "1.2.0")  # wiped
    _write_sentinel_entries(
        tmp_project,
        [
            {"package": "depa", "path": str(healthy), "version": "0.3.1"},
            {"package": "depb", "path": str(tmp_project / "depb-src"), "version": "1.2.0"},
        ],
    )
    rc = run_status(str(tmp_project))
    assert rc == 1
    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "[WIPED]" in out
