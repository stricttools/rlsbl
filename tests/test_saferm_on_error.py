"""Every ``saferm delete`` rlsbl runs must state its mandatory error mode.

saferm's ``--on-error`` flag is required and has no default: an invocation
without it exits 1 before deleting anything. rlsbl called saferm from several
places without the flag, so those deletions silently never happened -- most
visibly ``release retry``, which left its ``retry.toml`` behind and dirtied the
working tree for the next release.

Two layers here:

* per-call-site tests that capture the argv rlsbl actually builds, and
* a source sweep that walks every ``saferm`` argv literal in the package, so a
  new call site cannot reintroduce the omission.

``rlsbl._effects_direct.run`` is the primitive the conftest saferm mock also
patches; patching it again here nests over that fixture and wins.
"""

import ast
import os
import pathlib
import subprocess
from unittest.mock import patch

import pytest


RLSBL_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "rlsbl"


def assert_on_error_abort(argv):
    """Assert a saferm argv declares ``--on-error abort``."""
    assert "--on-error" in argv, f"saferm invocation lacks --on-error: {argv}"
    assert argv[argv.index("--on-error") + 1] == "abort", (
        f"saferm invocation does not request abort: {argv}"
    )


@pytest.fixture
def saferm_argv():
    """Capture every saferm argv reaching the subprocess primitive."""
    captured = []

    def _run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "saferm":
            captured.append(list(cmd))
            target = cmd[-1]
            if os.path.isdir(target):
                import shutil

                shutil.rmtree(target)
            elif os.path.exists(target):
                os.unlink(target)
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        raise AssertionError(f"unexpected non-saferm subprocess call: {cmd}")

    with patch("rlsbl._effects_direct.run", side_effect=_run):
        yield captured


# ---------------------------------------------------------------------------
# Per-call-site: the argv each helper builds
# ---------------------------------------------------------------------------


def test_release_retry_cleanup_declares_on_error(tmp_path, saferm_argv):
    """``release retry`` deletes retry.toml with an explicit error mode."""
    from rlsbl.commands.release_retry import _cleanup_retry_file

    retry_path = tmp_path / "retry.toml"
    retry_path.write_text('version = "1.0.0"\n')

    _cleanup_retry_file(str(retry_path), lambda msg: None)

    assert len(saferm_argv) == 1
    assert_on_error_abort(saferm_argv[0])
    assert not retry_path.exists()


def test_releasable_cleanup_dir_declares_on_error(tmp_path, saferm_argv):
    """``monorepo cleanup`` removes a residue directory with an error mode."""
    from rlsbl.releasable_cleanup import _saferm_dir

    target = tmp_path / "changes"
    target.mkdir()
    (target / "unreleased.jsonl").write_text("")

    _saferm_dir(str(target), "app", "changes")

    assert len(saferm_argv) == 1
    assert_on_error_abort(saferm_argv[0])
    assert "-r" in saferm_argv[0]


def test_releasable_cleanup_file_declares_on_error(tmp_path, saferm_argv):
    """``monorepo cleanup`` removes a residue file with an error mode."""
    from rlsbl.releasable_cleanup import _saferm_file

    target = tmp_path / "version"
    target.write_text("0.1.0\n")

    _saferm_file(str(target), "app", "version")

    assert len(saferm_argv) == 1
    assert_on_error_abort(saferm_argv[0])


def test_releasable_rename_cache_delete_declares_on_error(tmp_path, saferm_argv):
    """``monorepo rename-releasable`` drops the stale cache with an error mode."""
    from rlsbl.commands.monorepo.releasable_rename import _saferm_file

    target = tmp_path / ".validated"
    target.write_text("abc123\n")

    _saferm_file(str(target))

    assert len(saferm_argv) == 1
    assert_on_error_abort(saferm_argv[0])
    assert "-f" in saferm_argv[0]


def test_monorepo_sync_workflow_delete_declares_on_error(tmp_path, saferm_argv):
    """``monorepo sync`` removes a stale root workflow with an error mode."""
    from rlsbl.commands.monorepo.sync import _saferm_workflow

    target = tmp_path / "app-ci.yml"
    target.write_text("name: CI\n")

    _saferm_workflow(str(target), "stale generated workflow")

    assert len(saferm_argv) == 1
    assert_on_error_abort(saferm_argv[0])


# ---------------------------------------------------------------------------
# Source sweep: no call site anywhere may omit the flag
# ---------------------------------------------------------------------------


def _saferm_argv_literals():
    """Yield ``(file, lineno, constants)`` for every saferm argv list in rlsbl/."""
    for path in sorted(RLSBL_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            consts = [
                el.value for el in node.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
            if consts[:1] != ["saferm"]:
                continue
            if "delete" not in consts:
                continue
            yield path, node.lineno, consts


def test_source_sweep_finds_the_known_call_sites():
    """The sweep really walks the package (guards against a silent zero-match)."""
    sites = list(_saferm_argv_literals())
    assert len(sites) >= 8, f"expected the known saferm delete sites, found {sites}"


@pytest.mark.parametrize(
    "path,lineno,consts",
    [pytest.param(*s, id=f"{s[0].name}:{s[1]}") for s in _saferm_argv_literals()],
)
def test_every_saferm_delete_literal_declares_on_error(path, lineno, consts):
    """Every literal ``saferm delete`` argv in rlsbl/ carries ``--on-error abort``."""
    assert "--on-error" in consts, (
        f"{path}:{lineno} builds a saferm delete argv without --on-error; "
        "saferm requires the flag and exits 1 without it"
    )
    assert consts[consts.index("--on-error") + 1] == "abort", (
        f"{path}:{lineno} does not request --on-error abort"
    )
