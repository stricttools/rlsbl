"""Tests for the --bump / --description quick release shortcut."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import rlsbl
from rlsbl.errors import ConfigError
from rlsbl.release_file import ReleaseConfig
from rlsbl.targets import TargetEntry


def _ctx():
    return MagicMock(
        project_root=Path("/fake/project"),
        workspace_root=None,
        config={},
    )


# Common kwargs shared by all calls to cmd_release_run
_BASE_KWARGS = dict(
    dry_run=False,
    yes=True,
    quiet=False,
    allow_dirty=False,
    watch=False,
    watch_async=False,
    no_watch=False,
    preid="",
)


class TestQuickBumpHappyPath:
    """--bump patch --description 'Fix' constructs ReleaseConfig with auto-detected targets."""

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=False)  # no unreleased.toml
    @patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("pypi", ".")])
    @patch("rlsbl.commands.release.run_cmd")
    def test_constructs_release_config(self, mock_run, mock_detect, *_):
        rlsbl.cmd_release_run(
            **_BASE_KWARGS,
            bump="patch",
            description="Bug fix",
        )
        mock_run.assert_called_once()
        rc = mock_run.call_args[0][0]
        assert isinstance(rc, ReleaseConfig)
        assert rc.bump == "patch"
        assert rc.include == ["pypi"]
        assert rc.exclude == []
        assert rc.description == "Bug fix"

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=False)
    @patch("rlsbl.targets.detect_targets", return_value=[
        TargetEntry("npm", "."),
        TargetEntry("pypi", "."),
    ])
    @patch("rlsbl.commands.release.run_cmd")
    def test_multi_target(self, mock_run, mock_detect, *_):
        rlsbl.cmd_release_run(
            **_BASE_KWARGS,
            bump="minor",
            description="New feature",
        )
        rc = mock_run.call_args[0][0]
        assert rc.include == ["npm", "pypi"]
        assert rc.bump == "minor"


class TestQuickBumpErrors:
    """Error cases for --bump / --description validation."""

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    def test_bump_without_description(self, mock_ctx, *_):
        mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="",
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    def test_description_without_bump(self, mock_ctx, *_):
        mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="",
                description="Some description",
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=True)  # unreleased.toml exists
    def test_bump_with_existing_release_file(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="Fix",
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=False)
    @patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("flutter", ".")])
    def test_flutter_target_rejected(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="Fix",
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=False)
    @patch("rlsbl.targets.detect_targets", side_effect=ConfigError("no targets"))
    def test_detect_targets_config_error(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="Fix",
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    def test_invalid_bump_type(self, mock_ctx, *_):
        mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="huge",
                description="Fix",
            )
        assert exc.value.code == 1


class TestFileFallback:
    """No flags -- file-based flow unchanged (release file required)."""

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.release_file.get_release_file_path", return_value="/fake/unreleased.toml")
    @patch("os.path.exists", return_value=False)
    def test_no_flags_requires_release_file(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="",
                description="",
            )
        assert exc.value.code == 1


class TestQuickBumpErrorMessages:
    """Verify error messages contain useful information."""

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    def test_bump_without_description_message(self, _mock_ctx, _mock_ws, _mock_root, capsys):
        _mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit):
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="",
            )
        captured = capsys.readouterr()
        assert "--description is required when --bump is used" in captured.err

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    def test_description_without_bump_message(self, _mock_ctx, _mock_ws, _mock_root, capsys):
        _mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit):
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="",
                description="Fix",
            )
        captured = capsys.readouterr()
        assert "--bump is required when --description is used" in captured.err

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=True)
    def test_existing_release_file_message(self, _mock_exists, _mock_ctx, _mock_ws, _mock_root, capsys):
        _mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit):
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="Fix",
            )
        captured = capsys.readouterr()
        assert "release file exists" in captured.err.lower()

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=False)
    @patch("rlsbl.targets.detect_targets", return_value=[TargetEntry("flutter", ".")])
    def test_flutter_error_message(self, _mock_detect, _mock_exists, _mock_ctx, _mock_ws, _mock_root, capsys):
        _mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit):
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="Fix",
            )
        captured = capsys.readouterr()
        assert "flutter" in captured.err.lower()
        assert "rlsbl release init" in captured.err

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("os.path.exists", return_value=False)
    @patch("rlsbl.targets.detect_targets", side_effect=ConfigError("no targets key"))
    def test_detect_targets_error_message(self, _mock_detect, _mock_exists, _mock_ctx, _mock_ws, _mock_root, capsys):
        _mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit):
            rlsbl.cmd_release_run(
                **_BASE_KWARGS,
                bump="patch",
                description="Fix",
            )
        captured = capsys.readouterr()
        assert "cannot auto-detect targets" in captured.err.lower()
        assert "rlsbl release init" in captured.err
