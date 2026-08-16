"""Every ``saferm delete`` rlsbl runs must state its mandatory error mode.

saferm's ``--on-error`` flag is required and has no default: an invocation
without it exits 1 before deleting anything. rlsbl called saferm from several
places without the flag, so those deletions silently never happened -- most
visibly ``release retry``, which left its ``retry.toml`` behind and dirtied the
working tree for the next release.

The guardrail used to be a source sweep over every hand-written ``saferm``
argv, which can only catch the next omission AFTER someone writes it. The
structural fix replaced the nine literals with one constructor,
:func:`rlsbl.saferm.saferm_delete`, which has no parameter for the error mode
because there is nothing to decide. The three layers here follow from that:

* the constructor's own argv, asserted directly;
* per-call-site tests that capture the argv rlsbl actually builds;
* a source sweep that now asserts the ABSENCE of any hand-written ``saferm``
  argv outside the constructor, so a new call site cannot route around it.

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
# The constructor: the one place an argv is built
# ---------------------------------------------------------------------------


def test_constructor_builds_the_mandatory_argv(tmp_path, saferm_argv):
    """The minimal call already carries the description and the error mode."""
    from rlsbl.saferm import saferm_delete

    target = tmp_path / "f.txt"
    target.write_text("x")

    saferm_delete(str(target), description="because")

    assert saferm_argv == [[
        "saferm", "delete", "--description", "because",
        "--on-error", "abort", str(target),
    ]]


def test_constructor_places_recursive_and_skip_missing_before_the_options(
    tmp_path, saferm_argv,
):
    """``-r`` and ``-f`` are short options and sit right after the subcommand."""
    from rlsbl.saferm import saferm_delete

    target = tmp_path / "d"
    target.mkdir()

    saferm_delete(str(target), description="why", recursive=True, skip_missing=True)

    assert saferm_argv[0][:4] == ["saferm", "delete", "-r", "-f"]
    assert_on_error_abort(saferm_argv[0])


def test_constructor_has_no_error_mode_parameter():
    """The error mode is not a decision, so it is not a parameter."""
    import inspect

    from rlsbl.saferm import saferm_delete

    params = inspect.signature(saferm_delete).parameters
    assert "on_error" not in params
    assert set(params) == {
        "path", "description", "recursive", "skip_missing", "install_hint",
    }
    # description is keyword-only and mandatory: it is the audit trail.
    assert params["description"].default is inspect.Parameter.empty
    assert params["description"].kind is inspect.Parameter.KEYWORD_ONLY


def test_constructor_reraises_a_missing_binary_with_the_install_hint(tmp_path):
    """``install_hint`` turns FileNotFoundError into a sentence a user can act on."""
    from rlsbl.saferm import saferm_delete

    with patch("rlsbl._effects_direct.run", side_effect=FileNotFoundError()):
        with pytest.raises(RuntimeError) as exc:
            saferm_delete(
                str(tmp_path / "f"),
                description="why",
                install_hint="Install saferm before running cleanup.",
            )
    assert "Install saferm before running cleanup." in str(exc.value)


def test_constructor_propagates_a_missing_binary_without_a_hint(tmp_path):
    """Without a hint the caller handles the absence itself (release retry does)."""
    from rlsbl.saferm import saferm_delete

    with patch("rlsbl._effects_direct.run", side_effect=FileNotFoundError()):
        with pytest.raises(FileNotFoundError):
            saferm_delete(str(tmp_path / "f"), description="why")


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
# Source sweep: the constructor is the ONLY place an argv is built
# ---------------------------------------------------------------------------

SAFERM_MODULE = RLSBL_PACKAGE / "saferm.py"


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


def test_the_sweep_really_walks_the_package():
    """The constructor's own argv is found (guards against a silent zero-match)."""
    sites = list(_saferm_argv_literals())
    assert [s[0] for s in sites] == [SAFERM_MODULE], (
        f"expected the constructor's argv and nothing else, found {sites}"
    )


def test_no_module_builds_a_saferm_argv_by_hand():
    """A new call site must go through the constructor, not spell argv again.

    This is the structural half of the guardrail. The old sweep checked that
    every hand-written argv carried ``--on-error``, which meant a new omission
    was caught only after it was written; there is now one argv in the package
    and nowhere else to write one.
    """
    offenders = [
        f"{path}:{lineno}"
        for path, lineno, _ in _saferm_argv_literals()
        if path != SAFERM_MODULE
    ]
    assert not offenders, (
        "these build a saferm delete argv by hand instead of calling "
        f"rlsbl.saferm.saferm_delete: {', '.join(offenders)}"
    )

# The constructor's own argv is asserted byte for byte at runtime, by
# test_constructor_builds_the_mandatory_argv above: it assembles the list
# across several statements, so a static read of one literal cannot see it.
