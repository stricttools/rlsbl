"""Regression tests for the deletion of the quick-bump release shortcut.

``rlsbl release run`` used to accept ``--bump``/``--description``/``--preid``,
which constructed a :class:`ReleaseConfig` in memory and skipped
``.rlsbl/releases/unreleased.toml`` entirely. That contradicted the
file-driven-over-flag-driven rule the rest of the release flow enforces: the
release file is what makes an operator state a release's intent, in a reviewable
committed artifact, before executing it.

These tests pin the removal so the shortcut cannot silently return, and pin the
two pieces that had to survive it: the helpful "no release file" error that
points at ``rlsbl release init``, and the pre-release channel selection, which
the release file has always carried as its own ``preid`` key.
"""

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import rlsbl
from rlsbl.release_file import read_release_file
from conftest import cli_ctx

app = rlsbl.app

pytestmark = pytest.mark.repo_cwd


# Common kwargs for a direct handler call -- the whole surface that remains.
_BASE_KWARGS = dict(
    allow_dirty=False,
    watch=False,
    push_timeout=0,
    ci_timeout=0,
    check_timeout=0,
    hook_timeout=0,
    releasable=None,
)


# ---------------------------------------------------------------------------
# Flag surface: the shortcut flags no longer parse
# ---------------------------------------------------------------------------


class TestQuickBumpFlagsRejected:
    """Every removed flag must be an unknown-flag parse error."""

    @pytest.mark.parametrize("argv", [
        ["release", "run", "--no-allow-dirty", "--no-watch", "--bump", "patch"],
        ["release", "run", "--no-allow-dirty", "--no-watch", "--description", "d"],
        ["release", "run", "--no-allow-dirty", "--no-watch", "--preid", "alpha"],
    ])
    def test_removed_flag_is_unknown(self, argv):
        result = app.test(argv)
        assert result.exit_code != 0
        assert "unknown flag" in result.stderr

    def test_help_does_not_advertise_them(self):
        result = app.test(["release", "run", "--help"])
        assert result.exit_code == 0, result.stderr
        assert "--bump" not in result.stdout
        assert "--description" not in result.stdout
        assert "--preid" not in result.stdout

    def test_handler_signature_has_no_shortcut_parameters(self):
        params = inspect.signature(rlsbl.cmd_release_run).parameters
        for name in ("bump", "description", "preid"):
            assert name not in params, (
                f"{name} is quick-bump machinery and must stay deleted"
            )


# ---------------------------------------------------------------------------
# What had to survive the removal
# ---------------------------------------------------------------------------


class TestReleaseFileIsTheOnlyIntentSource:

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.release_file.get_release_file_path", return_value="/fake/unreleased.toml")
    @patch("os.path.exists", return_value=False)
    def test_missing_release_file_points_at_release_init(self, _exists, _path, _ctx, _ws, _root, capsys):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(cli_ctx(), **_BASE_KWARGS)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "No release file found" in captured.err
        assert "rlsbl release init" in captured.err

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.release_file.get_release_file_path", return_value="/fake/unreleased.toml")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.release_file.read_release_file")
    @patch("rlsbl.commands.release.run_cmd")
    def test_the_file_is_what_reaches_run_cmd(self, mock_run, mock_read, *_):
        parsed = MagicMock()
        mock_read.return_value = parsed
        rlsbl.cmd_release_run(cli_ctx(), **_BASE_KWARGS)
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] is parsed

    def test_preid_is_still_reachable_through_the_file(self, tmp_path):
        """Pre-release channel selection was never flag-only: it is a file key."""
        path = tmp_path / "unreleased.toml"
        path.write_text(
            'format_version = 1\n'
            'bump = "minor"\n'
            'description = "An alpha."\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
            'preid = "alpha"\n'
        )
        config = read_release_file(str(path))
        assert config.bump == "minor"
        assert config.preid == "alpha"
