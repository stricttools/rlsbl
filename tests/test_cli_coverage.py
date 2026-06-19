"""Targeted coverage tests for rlsbl/__init__.py CLI entry points and
low-coverage modules: watch.py, yank.py, targets/protocol.py, and
pipeline modules (go, hex, npm, deno, cargo, docker, maven).

Strategy: mock the underlying implementation functions and verify that
each command handler properly parses flags and delegates.
"""

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import rlsbl


# ============================================================================
# Helpers
# ============================================================================

def _ctx(root=".", config=None, workspace_root=None):
    from rlsbl.context import ProjectContext
    if isinstance(root, str):
        root = Path(root)
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    return ProjectContext(
        project_root=root,
        workspace_root=workspace_root,
        config=config or {},
    )


# ============================================================================
# _detect_version
# ============================================================================


class TestDetectVersion:
    """Cover the tomli fallback (line 29) and importlib fallback (lines 37-39)."""

    def test_importlib_fallback(self, monkeypatch, tmp_path):
        """When pyproject.toml is not found, falls back to importlib.metadata."""
        # _detect_version is called at module load time; test the function directly
        from rlsbl import _detect_version

        # Make pyproject.toml path not exist by patching __file__
        with patch("rlsbl.os.path.isfile", return_value=False):
            with patch("rlsbl.os.path.realpath", return_value=str(tmp_path / "nope")):
                result = _detect_version()
        # Should return something (either from importlib or "unknown")
        assert isinstance(result, str)

    def test_returns_unknown_when_all_fail(self):
        """When both pyproject.toml and importlib fail, returns 'unknown'."""
        from rlsbl import _detect_version

        with patch("rlsbl.os.path.isfile", return_value=False):
            with patch("rlsbl.os.path.realpath", return_value="/nope"):
                with patch("importlib.metadata.version", side_effect=Exception("nope")):
                    result = _detect_version()
        assert result == "unknown"


# ============================================================================
# _require_project_root / _require_sub_project_root
# ============================================================================


class TestRequireProjectRoot:
    """Cover _require_project_root exit path (lines 77-78)."""

    @patch("rlsbl.utils.find_project_root", return_value=None)
    def test_exits_when_no_root_found(self, _mock):
        with pytest.raises(SystemExit) as exc:
            rlsbl._require_project_root()
        assert exc.value.code == 1


class TestRequireSubProjectRoot:
    """Cover _require_sub_project_root monorepo paths (lines 98-108)."""

    @patch("rlsbl.utils.find_project_root", return_value="/repo")
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.workspace.resolve_project", return_value=None)
    def test_exits_when_not_in_registered_project(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl._require_sub_project_root()
        assert exc.value.code == 1

    @patch("rlsbl.utils.find_project_root", return_value="/repo")
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.workspace.resolve_project")
    def test_resolves_sub_project_path(self, mock_resolve, *_):
        project = {"path": "packages/mylib", "name": "mylib"}
        mock_resolve.return_value = project
        result = rlsbl._require_sub_project_root()
        assert str(result).endswith("packages/mylib")
        assert rlsbl._resolved_project is project


# ============================================================================
# _resolve_target
# ============================================================================


class TestResolveTarget:
    """Cover _resolve_target validation and auto-detect paths."""

    def test_unknown_target_exits(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl._resolve_target("nonexistent_target")
        assert exc.value.code == 1

    @patch("rlsbl.detect_registries", return_value=[])
    def test_no_auto_detect_exits(self, _):
        with pytest.raises(SystemExit) as exc:
            rlsbl._resolve_target(None)
        assert exc.value.code == 1

    @patch("rlsbl.detect_registries", return_value=["npm"])
    def test_auto_detect_returns_first(self, _):
        result = rlsbl._resolve_target(None)
        assert result == "npm"


# ============================================================================
# cmd_release_run
# ============================================================================


class TestCmdReleaseRun:
    """Cover cmd_release_run handler paths (lines 230-263)."""

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/fake/mono")
    @patch("rlsbl.workspace.resolve_project", return_value=None)
    @patch("rlsbl.context.create_context")
    def test_exits_from_monorepo_root(self, mock_ctx, *_):
        mock_ctx.return_value = _ctx()
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                dry_run=False, yes=True, quiet=False,
                allow_dirty=False, watch=False, no_watch=False,
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.release_file.get_release_file_path", return_value="/fake/unreleased.toml")
    @patch("os.path.exists", return_value=False)
    def test_exits_when_no_release_file(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                dry_run=False, yes=True, quiet=False,
                allow_dirty=False, watch=False, no_watch=False,
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.release_file.get_release_file_path", return_value="/fake/unreleased.toml")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.release_file.read_release_file", side_effect=rlsbl.ReleaseFileError("bad"))
    def test_exits_on_release_file_error(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_run(
                dry_run=False, yes=True, quiet=False,
                allow_dirty=False, watch=False, no_watch=False,
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.release_file.get_release_file_path", return_value="/fake/unreleased.toml")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.release_file.read_release_file")
    @patch("rlsbl.commands.release.run_cmd")
    def test_delegates_to_release_run_cmd(self, mock_run, mock_read, *_):
        mock_read.return_value = MagicMock()
        rlsbl.cmd_release_run(
            dry_run=True, yes=True, quiet=False,
            allow_dirty=True, watch=True, no_watch=False,
        )
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        flags = call_args[0][1]
        assert flags["dry-run"] is True
        assert flags["allow-dirty"] is True
        assert flags["watch"] is True


# ============================================================================
# cmd_release_init
# ============================================================================


class TestCmdReleaseInit:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.release_init.run_cmd")
    def test_delegates(self, mock_run, _):
        rlsbl.cmd_release_init()
        mock_run.assert_called_once_with(project_root=Path("/fake"))


# ============================================================================
# cmd_release_retry
# ============================================================================


class TestCmdReleaseRetry:
    """Cover retry handler paths (lines 283-314)."""

    @patch("os.remove")
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.release_file.get_retry_file_path", return_value="/fake/retry.toml")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.release_file.read_retry_file", side_effect=rlsbl.ReleaseFileError("bad"))
    def test_exits_on_retry_file_error(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_release_retry(
                dry_run=False, yes=True, quiet=False,
                watch=False, no_watch=False,
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.release_file.get_retry_file_path", return_value="/fake/retry.toml")
    @patch("os.path.exists", return_value=False)
    @patch("rlsbl.commands.release_retry.run_cmd")
    def test_passes_none_config_when_no_file(self, mock_run, *_):
        rlsbl.cmd_release_retry(
            dry_run=True, yes=False, quiet=False,
            watch=True, no_watch=False,
        )
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] is None
        flags = mock_run.call_args[0][1]
        assert flags["watch"] is True


# ============================================================================
# cmd_status
# ============================================================================


class TestCmdStatus:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl._resolve_target", return_value="npm")
    @patch("rlsbl.commands.status.run_cmd")
    def test_delegates(self, mock_run, *_):
        rlsbl.cmd_status(target="npm", json=True)
        mock_run.assert_called_once()
        assert mock_run.call_args[0][2] == {"json": True}


# ============================================================================
# cmd_check_name
# ============================================================================


class TestCmdCheckName:
    """Cover cmd_check_name handler paths (lines 429-454)."""

    def test_exits_when_no_target(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_check_name(target=[], delay="200")
        assert exc.value.code == 1

    def test_exits_on_invalid_targets(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_check_name(target=["bogus"], delay="200")
        assert exc.value.code == 1

    @patch("rlsbl.commands.check.run_cmd")
    def test_delegates_to_check_run_cmd(self, mock_run):
        rlsbl._variadic_args = ["my-package"]
        rlsbl.cmd_check_name(target=["npm", "pypi"], delay="500")
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0] == "npm"
        assert mock_run.call_args_list[1][0][0] == "pypi"


# ============================================================================
# cmd_claim_name
# ============================================================================


class TestCmdClaimName:
    """Cover cmd_claim_name handler (lines 462-488)."""

    def test_exits_when_no_target(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_claim_name(target="", yes=False)
        assert exc.value.code == 1

    def test_exits_on_invalid_target(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_claim_name(target="bogus", yes=False)
        assert exc.value.code == 1

    def test_exits_when_multiple_names(self):
        rlsbl._variadic_args = ["name1", "name2"]
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_claim_name(target="npm", yes=False)
        assert exc.value.code == 1

    @patch("rlsbl.commands.claim_name.run_cmd")
    def test_delegates(self, mock_run):
        rlsbl._variadic_args = ["my-package"]
        rlsbl.cmd_claim_name(target="npm", yes=True)
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == "npm"


# ============================================================================
# cmd_release_edit
# ============================================================================


class TestCmdReleaseEdit:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.edit_release.run_cmd")
    def test_with_version(self, mock_run, _):
        rlsbl.cmd_release_edit(dry_run=True, version="1.2.3")
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["1.2.3"]
        assert mock_run.call_args[0][1] == {"dry-run": True}

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.edit_release.run_cmd")
    def test_without_version(self, mock_run, _):
        rlsbl.cmd_release_edit(dry_run=False)
        assert mock_run.call_args[0][0] == []


# ============================================================================
# cmd_release_undo
# ============================================================================


class TestCmdReleaseUndo:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.commands.undo.run_cmd")
    def test_delegates(self, mock_run, *_):
        rlsbl.cmd_release_undo(target="npm", yes=True)
        mock_run.assert_called_once()
        assert mock_run.call_args[1]["ctx"] is not None


# ============================================================================
# cmd_release_yank
# ============================================================================


class TestCmdReleaseYank:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.yank.run_cmd")
    def test_delegates(self, mock_run, _):
        rlsbl.cmd_release_yank(
            reason="security", use="1.2.4", hard=True,
            dry_run=True, yes=True, version="1.2.3",
        )
        mock_run.assert_called_once()
        flags = mock_run.call_args[0][1]
        assert flags["reason"] == "security"
        assert flags["use"] == "1.2.4"
        assert flags["hard"] is True


# ============================================================================
# cmd_release_scrub
# ============================================================================


class TestCmdReleaseScrub:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.commands.release_scrub.run_cmd")
    def test_delegates(self, mock_run, *_):
        rlsbl.cmd_release_scrub(
            pattern="secret", file=None, replace="XXX", mangle=False,
            from_commit="abc123", entire_history=False, reason="test",
            dry_run=True, yes=True,
        )
        mock_run.assert_called_once()
        flags = mock_run.call_args[0][0]
        assert flags["pattern"] == "secret"
        assert flags["from-commit"] == "abc123"


# ============================================================================
# cmd_discover
# ============================================================================


class TestCmdDiscover:
    @patch("rlsbl.commands.discover.run_cmd")
    def test_delegates(self, mock_run):
        rlsbl.cmd_discover(mine=True)
        mock_run.assert_called_once()
        assert mock_run.call_args[0][2] == {"mine": True}


# ============================================================================
# cmd_watch
# ============================================================================


class TestCmdWatch:
    def test_sha_and_run_id_mutual_exclusion(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_watch(target="", run_id=["123"], sha="abc")
        assert exc.value.code == 1

    @patch("rlsbl.commands.watch.run_cmd")
    def test_sha_only(self, mock_run):
        rlsbl.cmd_watch(target="npm", run_id=[], sha="abc123")
        mock_run.assert_called_once()
        assert mock_run.call_args[0][1] == ["abc123"]

    @patch("rlsbl.commands.watch.run_cmd")
    def test_no_args_uses_head(self, mock_run):
        rlsbl.cmd_watch(target="", run_id=[])
        mock_run.assert_called_once()
        assert mock_run.call_args[0][1] == []


# ============================================================================
# cmd_pre_push_check
# ============================================================================


class TestCmdPrePushCheck:
    def test_exits_with_removed_message(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_pre_push_check()
        assert exc.value.code == 1


# ============================================================================
# cmd_prs
# ============================================================================


class TestCmdPrs:
    @patch("rlsbl.commands.prs.run_cmd")
    def test_delegates(self, mock_run):
        rlsbl.cmd_prs()
        mock_run.assert_called_once_with(None, [], {})


# ============================================================================
# cmd_unreleased
# ============================================================================


class TestCmdUnreleased:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.unreleased.run_cmd")
    def test_delegates(self, mock_run, _):
        rlsbl.cmd_unreleased(json=True)
        mock_run.assert_called_once()
        assert mock_run.call_args[0][2] == {"json": True}


# ============================================================================
# cmd_targets
# ============================================================================


class TestCmdTargets:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.targets_cmd.run_cmd")
    def test_delegates(self, mock_run, _):
        rlsbl.cmd_targets()
        mock_run.assert_called_once()


# ============================================================================
# cmd_record_gif
# ============================================================================


class TestCmdRecordGif:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.commands.record_gif.run_cmd")
    def test_delegates(self, mock_run, *_):
        rlsbl.cmd_record_gif(width="800", height="400", font_size="20", duration="5")
        mock_run.assert_called_once()
        flags = mock_run.call_args[0][2]
        assert flags["width"] == "800"
        assert flags["font-size"] == "20"


# ============================================================================
# cmd_migrate
# ============================================================================


class TestCmdMigrate:
    @patch("rlsbl.commands.migrate.run_cmd")
    def test_delegates(self, mock_run):
        rlsbl.cmd_migrate(dry_run=True, status=True)
        mock_run.assert_called_once()
        flags = mock_run.call_args[0][2]
        assert flags["dry-run"] is True
        assert flags["status"] is True


# ============================================================================
# cmd_migrate_publish_config
# ============================================================================


class TestCmdMigratePublishConfig:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.config.migrate_publish_config", return_value=({}, {}))
    def test_no_fields(self, mock_migrate, _, capsys):
        rlsbl.cmd_migrate_publish_config()
        assert "Nothing to migrate" in capsys.readouterr().out

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.config.migrate_publish_config", return_value=({"targets": {}}, {"private": True}))
    def test_with_fields(self, mock_migrate, _, capsys):
        rlsbl.cmd_migrate_publish_config()
        out = capsys.readouterr().out
        assert "Migrated 1 field(s)" in out
        assert "targets" in out

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.config.migrate_publish_config")
    def test_config_error_exits(self, mock_migrate, _):
        from rlsbl.errors import ConfigError
        mock_migrate.side_effect = ConfigError("bad")
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_migrate_publish_config()
        assert exc.value.code == 1


# ============================================================================
# cmd_deploy
# ============================================================================


class TestCmdDeploy:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.context.create_context")
    @patch("rlsbl.commands.deploy_cmd.run_cmd")
    def test_delegates(self, mock_run, *_):
        rlsbl.cmd_deploy(target="npm", dry_run=True, force=True, target_name="staging")
        mock_run.assert_called_once()
        assert mock_run.call_args[0][1] == ["staging"]
        assert mock_run.call_args[0][2]["force"] is True


# ============================================================================
# cmd_commit
# ============================================================================


class TestCmdCommit:
    def test_exits_when_no_files(self):
        rlsbl._variadic_args = []
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_commit(message="test")
        assert exc.value.code == 1

    @patch("rlsbl.commands.commit_cmd.run_cmd")
    def test_delegates(self, mock_run):
        rlsbl._variadic_args = ["file1.txt", "file2.txt"]
        rlsbl.cmd_commit(message="test commit")
        mock_run.assert_called_once_with("test commit", ["file1.txt", "file2.txt"])


# ============================================================================
# Changelog group commands
# ============================================================================


class TestCmdChlogAdd:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.changelog_cmd.cmd_add")
    def test_delegates(self, mock_add, _):
        rlsbl.cmd_chlog_add(
            commits="abc123", description="New feature", type="feature",
            no_user_facing=False, no_commit=True, allow_batch=False,
        )
        mock_add.assert_called_once()
        flags = mock_add.call_args[0][0]
        assert flags["commits"] == "abc123"
        assert flags["no-commit"] is True


class TestCmdChlogGenerate:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.changelog_cmd.cmd_generate")
    def test_delegates(self, mock_gen, _):
        rlsbl.cmd_chlog_generate(dry_run=True, no_commit=False)
        mock_gen.assert_called_once()
        flags = mock_gen.call_args[0][0]
        assert flags["dry-run"] is True


class TestCmdChlogAmend:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.changelog_cmd.cmd_amend")
    def test_delegates(self, mock_amend, _):
        rlsbl.cmd_chlog_amend(
            version="1.0.0", commits="abc", description="fix",
            type="fix", no_user_facing=False, no_resolve=True,
        )
        mock_amend.assert_called_once()
        flags = mock_amend.call_args[0][0]
        assert flags["version"] == "1.0.0"
        assert flags["no-resolve"] is True


class TestCmdChlogEdit:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.changelog_cmd.cmd_edit")
    def test_delegates(self, mock_edit, _):
        rlsbl.cmd_chlog_edit(
            commits="abc", type="fix", description="updated",
            no_user_facing=False, user_facing=True, no_commit=False,
        )
        mock_edit.assert_called_once()
        flags = mock_edit.call_args[0][0]
        assert flags["user-facing"] is True


# ============================================================================
# Monorepo group commands
# ============================================================================


class TestCmdMonoInit:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_init")
    def test_delegates(self, mock_init, _):
        rlsbl.cmd_mono_init(no_commit=True)
        mock_init.assert_called_once()
        assert mock_init.call_args[0][0]["no-commit"] is True


class TestCmdMonoAdd:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_add")
    def test_delegates_with_all_flags(self, mock_add, _):
        rlsbl.cmd_mono_add(
            name="mylib", target="npm", watch="*.ts,*.js",
            subtree_remote="git@github.com:user/mylib.git",
            depends_on="core,utils", library="true", dev_only="true",
            releasable="core", no_commit=True, path="packages/mylib",
        )
        mock_add.assert_called_once()
        flags = mock_add.call_args[0][1]
        assert flags["name"] == "mylib"
        assert flags["target"] == "npm"
        assert flags["watch"] == "*.ts,*.js"


class TestCmdMonoRemove:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_remove")
    def test_delegates(self, mock_remove, _):
        rlsbl.cmd_mono_remove(path="packages/old")
        mock_remove.assert_called_once_with(["packages/old"], {}, project_root=Path("/fake"))


class TestCmdMonoList:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_list")
    def test_delegates(self, mock_list, _):
        rlsbl.cmd_mono_list()
        mock_list.assert_called_once()


class TestCmdMonoSync:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_sync")
    def test_delegates(self, mock_sync, _):
        rlsbl.cmd_mono_sync(no_commit=True)
        mock_sync.assert_called_once()


class TestCmdMonoStatus:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_status")
    def test_delegates(self, mock_status, _):
        rlsbl.cmd_mono_status()
        mock_status.assert_called_once()


class TestCmdMonoCheckNames:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_check_names")
    def test_delegates(self, mock_check, _):
        rlsbl._variadic_args = ["pkg-a"]
        rlsbl.cmd_mono_check_names(
            target="npm", prefix="@scope/", suffix="", delay="500",
        )
        mock_check.assert_called_once()
        flags = mock_check.call_args[0][1]
        assert flags["prefix"] == "@scope/"


class TestCmdMonoReleaseOrder:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_release_order")
    def test_delegates(self, mock_order, _):
        rlsbl.cmd_mono_release_order()
        mock_order.assert_called_once()


class TestCmdMonoOutdated:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_outdated")
    def test_delegates(self, mock_out, _):
        rlsbl.cmd_mono_outdated()
        mock_out.assert_called_once()


class TestCmdMonoSnapshot:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_snapshot")
    def test_delegates(self, mock_snap, _):
        rlsbl.cmd_mono_snapshot(check=True)
        mock_snap.assert_called_once()
        assert mock_snap.call_args[0][0]["check"] is True


class TestCmdMonoMirror:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_mirror")
    def test_delegates(self, mock_mirror, _):
        rlsbl.cmd_mono_mirror(project="mylib")
        mock_mirror.assert_called_once()
        assert mock_mirror.call_args[0][0]["project"] == "mylib"


class TestCmdMonoGraph:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_graph")
    def test_delegates_with_options(self, mock_graph, _):
        rlsbl.cmd_mono_graph(
            format="dot", output="graph.dot", root="core",
            reverse="", depth=3,
        )
        mock_graph.assert_called_once()
        flags = mock_graph.call_args[0][0]
        assert flags["format"] == "dot"
        assert flags["output"] == "graph.dot"
        assert flags["root"] == "core"
        assert flags["depth"] == 3
        assert "reverse" not in flags  # empty string not added


class TestCmdMonoImpact:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_impact")
    def test_delegates(self, mock_impact, _):
        rlsbl._variadic_args = ["packages/core"]
        rlsbl.cmd_mono_impact(format="json", depth=2, since="HEAD~3")
        mock_impact.assert_called_once()
        flags = mock_impact.call_args[0][1]
        assert flags["since"] == "HEAD~3"
        assert flags["depth"] == 2


class TestCmdMonoRelease:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_batch_release")
    def test_delegates(self, mock_release, _):
        rlsbl.cmd_mono_release(dry_run=True, yes=True, quiet=False, allow_dirty=True)
        mock_release.assert_called_once()
        flags = mock_release.call_args[0][0]
        assert flags["allow-dirty"] is True


class TestCmdMonoReleaseInit:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.monorepo._cmd_batch_release_init")
    def test_delegates(self, mock_init, _):
        rlsbl.cmd_mono_release_init(packages="core,web")
        mock_init.assert_called_once()
        assert mock_init.call_args[1]["packages"] == "core,web"


class TestCmdMonoExtract:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    def test_exits_when_no_workspace(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_mono_extract(dry_run=False, package_name="pkg", target_path="/out")
        assert exc.value.code == 1

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_extract")
    def test_dry_run(self, mock_extract, *_):
        mock_extract.return_value = {
            "package_name": "pkg", "package_path": "packages/pkg",
            "target_path": "/out",
        }
        rlsbl.cmd_mono_extract(dry_run=True, package_name="pkg", target_path="/out")
        mock_extract.assert_called_once()

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_extract")
    def test_real_run(self, mock_extract, *_):
        mock_extract.return_value = {
            "package_name": "pkg", "package_path": "packages/pkg",
            "target_path": "/out", "entries_migrated": 5, "files_written": 2,
        }
        rlsbl.cmd_mono_extract(dry_run=False, package_name="pkg", target_path="/out")

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_extract", side_effect=ValueError("bad"))
    def test_error_exits(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_mono_extract(dry_run=False, package_name="pkg", target_path="/out")
        assert exc.value.code == 1


class TestCmdMonoAbsorb:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    def test_exits_when_no_workspace(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_mono_absorb(
                dry_run=False, releasable="", source_path="/src", package_name="pkg",
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_absorb")
    def test_dry_run(self, mock_absorb, *_):
        mock_absorb.return_value = {
            "package_name": "pkg", "source_path": "/src", "source_branch": "main",
        }
        rlsbl.cmd_mono_absorb(
            dry_run=True, releasable="core", source_path="/src", package_name="pkg",
        )

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_absorb")
    def test_real_run(self, mock_absorb, *_):
        mock_absorb.return_value = {
            "package_name": "pkg", "source_path": "/src", "source_branch": "main",
            "entries_migrated": 3, "files_written": 1,
        }
        rlsbl.cmd_mono_absorb(
            dry_run=False, releasable="", source_path="/src", package_name="pkg",
        )

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_absorb", side_effect=ValueError("bad"))
    def test_error_exits(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_mono_absorb(
                dry_run=False, releasable="", source_path="/src", package_name="pkg",
            )
        assert exc.value.code == 1


class TestCmdMonoExtractReleasable:
    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value=None)
    def test_exits_when_no_workspace(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_mono_extract_releasable(
                dry_run=False, releasable_name="core", target_path="/out",
            )
        assert exc.value.code == 1

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_extract_releasable")
    def test_dry_run(self, mock_extract, *_):
        mock_extract.return_value = {
            "releasable_name": "core", "is_monorepo": True,
            "target_path": "/out", "member_packages": ["pkg-a", "pkg-b"],
        }
        rlsbl.cmd_mono_extract_releasable(
            dry_run=True, releasable_name="core", target_path="/out",
        )

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_extract_releasable")
    def test_real_run(self, mock_extract, *_):
        mock_extract.return_value = {
            "releasable_name": "core", "is_monorepo": False,
            "target_path": "/out", "member_packages": ["pkg-a"],
            "entries_migrated": 5, "files_written": 2,
        }
        rlsbl.cmd_mono_extract_releasable(
            dry_run=False, releasable_name="core", target_path="/out",
        )

    @patch("rlsbl._require_project_root", return_value=Path("/fake"))
    @patch("rlsbl.workspace.find_workspace_root", return_value="/repo")
    @patch("rlsbl.commands.monorepo.cmd_extract_releasable", side_effect=ValueError("bad"))
    def test_error_exits(self, *_):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_mono_extract_releasable(
                dry_run=False, releasable_name="core", target_path="/out",
            )
        assert exc.value.code == 1


# ============================================================================
# dev group commands
# ============================================================================


class TestCmdDevInstall:
    def test_global_and_venv_mutual_exclusion(self):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_dev_install(
                all=False, include="", exclude="", uninstall=False,
                global_=True, venv=True,
            )
        assert exc.value.code == 2

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.dev.run_install", return_value=0)
    def test_delegates_global_mode(self, mock_run, _):
        rlsbl.cmd_dev_install(
            all=False, include="core", exclude="", uninstall=False,
            global_=False, venv=False,
        )
        mock_run.assert_called_once()
        flags = mock_run.call_args[0][0]
        assert flags["global"] is True
        assert flags["venv"] is False

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.dev.run_install", return_value=1)
    def test_exits_on_nonzero_return(self, mock_run, _):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_dev_install(
                all=False, include="", exclude="", uninstall=False,
                global_=False, venv=True,
            )
        assert exc.value.code == 1


# ============================================================================
# _extract_variadic_args
# ============================================================================


class TestExtractVariadicArgs:
    """Cover the variadic arg extraction for commit, check-name, etc."""

    def test_commit_with_separator(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "commit", "-m", "test msg", "--", "file1.txt", "file2.txt"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["file1.txt", "file2.txt"]

    def test_commit_without_separator(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "commit", "-m", "test msg", "file1.txt"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["file1.txt"]

    def test_commit_with_long_flag_equals(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "commit", "--message=test msg", "--", "file.txt"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["file.txt"]

    def test_check_name_extracts_positionals(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "check-name", "my-pkg", "other-pkg", "--target", "npm"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["my-pkg", "other-pkg"]

    def test_claim_name_extracts_positional(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "claim-name", "my-pkg", "--target", "npm"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["my-pkg"]

    def test_monorepo_check_names(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "monorepo", "check-names", "pkg-a", "--target", "npm", "--delay", "100"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["pkg-a"]

    def test_monorepo_impact(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "monorepo", "impact", "packages/core", "--format", "json"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["packages/core"]

    def test_unrecognized_command_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["rlsbl", "status"])
        result = rlsbl._extract_variadic_args()
        assert result == []

    def test_empty_argv_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["rlsbl"])
        result = rlsbl._extract_variadic_args()
        assert result == []

    def test_check_name_with_flag_equals(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "check-name", "my-pkg", "--target=npm", "--delay=100"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["my-pkg"]

    def test_claim_name_with_flag_equals(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "claim-name", "my-pkg", "--target=pypi"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["my-pkg"]

    def test_monorepo_check_names_with_flag_equals(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "monorepo", "check-names", "pkg-a", "--target=npm", "--prefix=@scope/"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["pkg-a"]

    def test_monorepo_impact_with_flag_equals(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "monorepo", "impact", "packages/core", "--format=json", "--depth=2"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["packages/core"]

    def test_commit_bool_flag(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "commit", "--dry-run", "-m", "msg", "--", "f.txt"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["f.txt"]

    def test_check_name_short_flag(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["rlsbl", "check-name", "my-pkg", "-v"],
        )
        result = rlsbl._extract_variadic_args()
        assert result == ["my-pkg"]


# ============================================================================
# main()
# ============================================================================


class TestMain:
    """Cover main() exception handling (lines 1303-1310)."""

    @patch("rlsbl.app.run", side_effect=subprocess.CalledProcessError(
        1, ["git", "push"], stderr="rejected\n",
    ))
    def test_called_process_error_with_stderr(self, _, capsys):
        with pytest.raises(SystemExit) as exc:
            rlsbl.main()
        assert exc.value.code == 1

    @patch("rlsbl.app.run", side_effect=subprocess.CalledProcessError(
        1, ["git", "push"], stderr="",
    ))
    def test_called_process_error_without_stderr(self, _, capsys):
        with pytest.raises(SystemExit) as exc:
            rlsbl.main()
        assert exc.value.code == 1

    @patch("rlsbl.app.run", side_effect=RuntimeError("unexpected"))
    def test_generic_exception(self, _, capsys):
        with pytest.raises(SystemExit) as exc:
            rlsbl.main()
        assert exc.value.code == 1


# ============================================================================
# cmd_scaffold paths
# ============================================================================


class TestCmdScaffold:
    """Cover cmd_scaffold handler paths."""

    def test_exits_when_no_registry_no_root(self, tmp_project):
        # tmp_project chdir's to a clean temp dir -- no .rlsbl/config.json, no manifests
        with patch("rlsbl.detect_registries", return_value=[]):
            with patch("rlsbl.utils.find_project_root", return_value=None):
                with pytest.raises(SystemExit) as exc:
                    rlsbl.cmd_scaffold(
                        target="", force=False, private=False, no_commit=False,
                        skip_shared=False, no_tag=False, dry_run=False,
                    )
                assert exc.value.code == 1

    def test_single_target_auto_detected(self, tmp_project):
        with patch("rlsbl.detect_registries", return_value=["npm"]):
            with patch("rlsbl.context.create_context") as mock_ctx:
                mock_ctx.return_value = _ctx(config={})
                with patch("rlsbl.commands.init_cmd.run_cmd") as mock_run:
                    rlsbl.cmd_scaffold(
                        target="", force=False, private=False, no_commit=False,
                        skip_shared=False, no_tag=False, dry_run=False,
                    )
                    mock_run.assert_called_once()

    def test_multi_target_auto_detected(self, tmp_project):
        with patch("rlsbl.detect_registries", return_value=["npm", "pypi"]):
            with patch("rlsbl.context.create_context") as mock_ctx:
                mock_ctx.return_value = _ctx(config={})
                with patch("rlsbl.commands.init_cmd.run_cmd_multi") as mock_run_multi:
                    rlsbl.cmd_scaffold(
                        target="", force=False, private=False, no_commit=False,
                        skip_shared=False, no_tag=False, dry_run=False,
                    )
                    mock_run_multi.assert_called_once()

    def test_explicit_unknown_target_exits(self, tmp_project):
        with pytest.raises(SystemExit) as exc:
            rlsbl.cmd_scaffold(
                target="nonexistent", force=False, private=False, no_commit=False,
                skip_shared=False, no_tag=False, dry_run=False,
            )
        assert exc.value.code == 1

    def test_explicit_valid_target(self, tmp_project):
        with patch("rlsbl.context.create_context") as mock_ctx:
            mock_ctx.return_value = _ctx()
            with patch("rlsbl.commands.init_cmd.run_cmd") as mock_run:
                rlsbl.cmd_scaffold(
                    target="npm", force=True, private=True, no_commit=True,
                    skip_shared=True, no_tag=True, dry_run=True,
                )
                mock_run.assert_called_once()
                flags = mock_run.call_args[0][2]
                assert flags["force"] is True
                assert flags["private"] is True


# ============================================================================
# yank.py -- cover uncovered lines
# ============================================================================

MOD_YANK = "rlsbl.commands.yank"


class TestYankNoArgs:
    def test_exits_without_version(self):
        from rlsbl.commands.yank import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd([], {}, project_root=Path("/fake"))
        assert exc.value.code == 1


class TestYankMonorepoContext:
    """Cover monorepo detection and tag formatting in yank (lines 47-74)."""

    @patch(f"{MOD_YANK}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_YANK}.resolve_project")
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=True)
    @patch(f"{MOD_YANK}.run")
    def test_monorepo_plain_tag(self, mock_run, _auth, _inst, _targets, mock_resolve, _ws):
        """Monorepo without releasable uses target.monorepo_tag_format."""
        proj = {"name": "mylib", "path": "packages/mylib"}
        mock_resolve.return_value = proj
        mock_run.side_effect = [
            "",  # gh release view
            Exception("latest check fails"),  # gh release list
        ]
        with pytest.raises(SystemExit):
            from rlsbl.commands.yank import run_cmd
            run_cmd(["1.0.0"], {"hard": False, "yes": True}, project_root=Path("/ws/packages/mylib"))

    @patch(f"{MOD_YANK}.find_workspace_root", return_value="/ws")
    @patch(f"{MOD_YANK}.resolve_project")
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=False)
    def test_gh_not_installed(self, *_):
        from rlsbl.commands.yank import run_cmd
        proj = {"name": "mylib", "path": "packages/mylib"}
        with patch(f"{MOD_YANK}.resolve_project", return_value=proj):
            with pytest.raises(SystemExit) as exc:
                run_cmd(["1.0.0"], {}, project_root=Path("/ws/packages/mylib"))
            assert exc.value.code == 1

    @patch(f"{MOD_YANK}.find_workspace_root", return_value=None)
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=False)
    def test_gh_not_authed(self, *_):
        from rlsbl.commands.yank import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(["1.0.0"], {}, project_root=Path("/fake"))
        assert exc.value.code == 1


class TestYankLatestRefused:
    """Cover the 'latest release' guard (lines 93-104)."""

    @patch(f"{MOD_YANK}.find_workspace_root", return_value=None)
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=True)
    @patch(f"{MOD_YANK}.run")
    def test_refuses_latest(self, mock_run, *_):
        mock_run.side_effect = [
            "",  # gh release view (exists)
            "v1.0.0",  # gh release list --limit 1 (latest matches)
        ]
        from rlsbl.commands.yank import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(["1.0.0"], {"yes": True}, project_root=Path("/fake"))
        assert exc.value.code == 1

    @patch(f"{MOD_YANK}.find_workspace_root", return_value=None)
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=True)
    @patch(f"{MOD_YANK}.run")
    def test_latest_check_failure_exits(self, mock_run, *_):
        mock_run.side_effect = [
            "",  # gh release view (exists)
            Exception("API error"),  # gh release list fails
        ]
        from rlsbl.commands.yank import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(["1.0.0"], {"yes": True}, project_root=Path("/fake"))
        assert exc.value.code == 1


class TestYankConfirmation:
    """Cover confirmation prompt (lines 108-119)."""

    @patch(f"{MOD_YANK}.find_workspace_root", return_value=None)
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=True)
    @patch(f"{MOD_YANK}.run")
    def test_hard_yank_prompt_declined(self, mock_run, *_):
        mock_run.side_effect = [
            "",  # gh release view
            "v2.0.0",  # latest is different
        ]
        from rlsbl.commands.yank import run_cmd
        with patch("builtins.input", return_value="n"):
            with pytest.raises(SystemExit) as exc:
                run_cmd(["1.0.0"], {"hard": True}, project_root=Path("/fake"))
            assert exc.value.code == 0

    @patch(f"{MOD_YANK}.find_workspace_root", return_value=None)
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=True)
    @patch(f"{MOD_YANK}.run")
    def test_soft_yank_prompt_eof(self, mock_run, *_):
        mock_run.side_effect = [
            "",  # gh release view
            "v2.0.0",  # latest is different
        ]
        from rlsbl.commands.yank import run_cmd
        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc:
                run_cmd(["1.0.0"], {"hard": False}, project_root=Path("/fake"))
            assert exc.value.code == 1


class TestYankSoftYank:
    """Cover _soft_yank (lines 137-168)."""

    @patch(f"{MOD_YANK}.find_workspace_root", return_value=None)
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=True)
    @patch(f"{MOD_YANK}.run")
    def test_soft_yank_succeeds(self, mock_run, _auth, _inst, _targets, _ws, capsys):
        mock_run.side_effect = [
            "",  # gh release view
            "v2.0.0",  # latest is different
            "existing body",  # gh release view --json body
            "",  # gh release edit
        ]
        from rlsbl.commands.yank import run_cmd
        run_cmd(["1.0.0"], {"hard": False, "yes": True}, project_root=Path("/fake"))
        assert "Yanked v1.0.0" in capsys.readouterr().out


class TestYankHardYank:
    """Cover _hard_yank (lines 127-134)."""

    @patch(f"{MOD_YANK}.find_workspace_root", return_value=None)
    @patch(f"{MOD_YANK}.detect_targets", return_value=[])
    @patch(f"{MOD_YANK}.check_gh_installed", return_value=True)
    @patch(f"{MOD_YANK}.check_gh_auth", return_value=True)
    @patch(f"{MOD_YANK}.run")
    def test_hard_yank_dry_run(self, mock_run, _auth, _inst, _targets, _ws, capsys):
        mock_run.side_effect = [
            "",  # gh release view
            "v2.0.0",  # latest is different
        ]
        from rlsbl.commands.yank import run_cmd
        run_cmd(["1.0.0"], {"hard": True, "dry-run": True, "yes": True}, project_root=Path("/fake"))
        assert "Would delete" in capsys.readouterr().out


class TestYankBuildNotice:
    """Cover _build_notice (lines 171-182)."""

    def test_notice_with_reason_and_use(self):
        from rlsbl.commands.yank import _build_notice
        result = _build_notice("security fix", "1.2.4")
        assert "security fix" in result
        assert "v1.2.4" in result

    def test_notice_no_reason_no_use(self):
        from rlsbl.commands.yank import _build_notice
        result = _build_notice(None, None)
        assert result == "> **Deprecated.**"


# ============================================================================
# watch.py -- cover remaining uncovered lines
# ============================================================================

MOD_WATCH = "rlsbl.commands.watch"


class TestWatchNotifyDarwin:
    """Cover macOS notification path (lines 42-44)."""

    @patch(f"{MOD_WATCH}.subprocess.run")
    def test_notify_darwin(self, mock_run):
        with patch(f"{MOD_WATCH}.sys") as mock_sys:
            mock_sys.platform = "darwin"
            from rlsbl.commands.watch import _notify
            _notify("Title", "Body")
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "osascript"


class TestWatchNotifyException:
    """Cover the exception handler in _notify (line 57-58)."""

    @patch(f"{MOD_WATCH}.subprocess.run", side_effect=Exception("notif error"))
    def test_notify_exception_silent(self, _):
        with patch(f"{MOD_WATCH}.sys") as mock_sys:
            mock_sys.platform = "darwin"
            from rlsbl.commands.watch import _notify
            # Should not raise
            _notify("Title", "Body")


class TestWatchRetryTimeout:
    """Cover retry timeout path (lines 114-116)."""

    @patch(f"{MOD_WATCH}.run")
    @patch(f"{MOD_WATCH}.time")
    @patch(f"{MOD_WATCH}.subprocess.run")
    def test_retry_watch_timeout(self, mock_subproc, mock_time, mock_run, capsys):
        mock_subproc.side_effect = [
            MagicMock(returncode=0),  # gh workflow run trigger
            subprocess.TimeoutExpired("gh", 3600),  # retry watch times out
        ]
        mock_run.return_value = json.dumps(
            [{"databaseId": 300, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]
        )
        from rlsbl.commands.watch import _retry_workflow
        result = _retry_workflow("CI", "main", "user/repo", "test")
        assert result is not None
        assert result["passed"] is False
        assert "timed out" in capsys.readouterr().err


class TestWatchRetryGenericException:
    """Cover retry generic exception path (lines 117-119)."""

    @patch(f"{MOD_WATCH}.run")
    @patch(f"{MOD_WATCH}.time")
    @patch(f"{MOD_WATCH}.subprocess.run")
    def test_retry_watch_generic_error(self, mock_subproc, mock_time, mock_run, capsys):
        mock_subproc.side_effect = [
            MagicMock(returncode=0),  # gh workflow run trigger
            RuntimeError("unexpected"),  # retry watch fails
        ]
        mock_run.return_value = json.dumps(
            [{"databaseId": 300, "name": "CI", "status": "queued", "createdAt": "2026-01-01"}]
        )
        from rlsbl.commands.watch import _retry_workflow
        result = _retry_workflow("CI", "main", "user/repo", "test")
        assert result is not None
        assert result["passed"] is False
        assert "retry error" in capsys.readouterr().err


class TestWatchSingleRunTimeout:
    """Cover _watch_single_run timeout path (lines 164-167)."""

    @patch(f"{MOD_WATCH}.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 3600))
    def test_timeout(self, _, capsys):
        from rlsbl.commands.watch import _watch_single_run
        ci_run = {"databaseId": 100, "name": "CI"}
        result = _watch_single_run(ci_run, "test", "user/repo")
        assert result["passed"] is False
        assert "timed out" in capsys.readouterr().err


class TestWatchSingleRunGenericException:
    """Cover _watch_single_run generic exception path (lines 168-171)."""

    @patch(f"{MOD_WATCH}.subprocess.run", side_effect=RuntimeError("unexpected"))
    def test_generic_error(self, _, capsys):
        from rlsbl.commands.watch import _watch_single_run
        ci_run = {"databaseId": 100, "name": "CI"}
        result = _watch_single_run(ci_run, "test", "user/repo")
        assert result["passed"] is False
        assert "error: unexpected" in capsys.readouterr().err


class TestWatchSingleRunPassed:
    """Cover _watch_single_run pass path (lines 140-142)."""

    @patch(f"{MOD_WATCH}.subprocess.run")
    def test_passed(self, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        from rlsbl.commands.watch import _watch_single_run
        ci_run = {"databaseId": 100, "name": "CI"}
        result = _watch_single_run(ci_run, "test", "user/repo")
        assert result["passed"] is True
        assert "passed" in capsys.readouterr().err


class TestWatchRunsThreadError:
    """Cover _watch_runs future exception handling (lines 192-199)."""

    @patch(f"{MOD_WATCH}._watch_single_run", side_effect=RuntimeError("thread boom"))
    def test_thread_error(self, _, capsys):
        from rlsbl.commands.watch import _watch_runs
        runs = [
            {"databaseId": 100, "name": "CI"},
            {"databaseId": 200, "name": "Publish"},
        ]
        results = _watch_runs(runs, "test", "user/repo")
        assert len(results) == 2
        assert all(not r["passed"] for r in results)
        assert "thread error" in capsys.readouterr().err


class TestWatchRunsSingleRun:
    """Cover _watch_runs single-run path (line 178)."""

    @patch(f"{MOD_WATCH}._watch_single_run")
    def test_single_run_no_threading(self, mock_single):
        mock_single.return_value = {"name": "CI", "passed": True}
        from rlsbl.commands.watch import _watch_runs
        runs = [{"databaseId": 100, "name": "CI"}]
        results = _watch_runs(runs, "test", "user/repo")
        assert len(results) == 1
        # Single-run path only passes 3 args (no retried_lock/retried_workflows)
        assert len(mock_single.call_args[0]) == 3


class TestWatchRunCmdRunIdRepoError:
    """Cover run_cmd --run-id repo info error (lines 303-305)."""

    @patch(f"{MOD_WATCH}._resolve_run_ids")
    @patch(f"{MOD_WATCH}.run", side_effect=Exception("no repo"))
    def test_repo_info_failure(self, _, mock_resolve, capsys):
        mock_resolve.return_value = [{"databaseId": 100, "name": "CI"}]
        from rlsbl.commands.watch import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(None, [], {"run-id": ["100"]})
        assert exc.value.code == 1

    @patch(f"{MOD_WATCH}._resolve_run_ids")
    @patch(f"{MOD_WATCH}.run")
    @patch(f"{MOD_WATCH}._watch_runs")
    @patch(f"{MOD_WATCH}._print_workflow_audit")
    @patch(f"{MOD_WATCH}._notify")
    def test_run_id_keyboard_interrupt(self, _notify, _audit, _watch, _run, mock_resolve):
        mock_resolve.side_effect = KeyboardInterrupt()
        from rlsbl.commands.watch import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(None, [], {"run-id": ["100"]})
        assert exc.value.code == 130


class TestWatchRunCmdShaRepoError:
    """Cover run_cmd SHA path repo info error (lines 353-355)."""

    @patch(f"{MOD_WATCH}.run")
    def test_sha_path_repo_error(self, mock_run, capsys):
        mock_run.side_effect = [
            "abc123full",  # git rev-parse
            Exception("no repo"),  # gh repo view fails
        ]
        from rlsbl.commands.watch import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(None, ["abc123"], {})
        assert exc.value.code == 1


class TestWatchRunCmdNoGitRepo:
    """Cover run_cmd when not in a git repo (lines 338-345)."""

    @patch(f"{MOD_WATCH}.run", side_effect=Exception("not a git repo"))
    def test_no_git_repo_exits(self, _, capsys):
        from rlsbl.commands.watch import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(None, [], {})
        assert exc.value.code == 1


class TestWatchRunCmdFallbackReleaseUrl:
    """Cover the _release_url fallback for success URL (lines 431-432)."""

    @patch(f"{MOD_WATCH}._notify")
    @patch(f"{MOD_WATCH}._print_workflow_audit", return_value=False)
    @patch(f"{MOD_WATCH}._watch_runs")
    @patch(f"{MOD_WATCH}.poll_runs")
    @patch(f"{MOD_WATCH}.time")
    @patch(f"{MOD_WATCH}.run")
    def test_success_url_fallback_to_release_url(
        self, mock_run, mock_time, mock_poll, mock_watch, mock_audit, mock_notify,
    ):
        """When tag is a truncated SHA, falls back to _release_url."""
        ci_run = {"databaseId": 100, "name": "CI", "status": "in_progress"}
        mock_poll.side_effect = [
            [ci_run],
            [ci_run],
        ]
        mock_run.side_effect = [
            "abc123fullhash" + "0" * 26,  # git rev-parse
            json.dumps({"nameWithOwner": "user/repo", "name": "repo"}),  # gh repo view
            Exception("no tag"),  # git describe fails -> tag = abc123fullha
        ]
        mock_watch.return_value = [{"name": "CI", "passed": True, "run_id": "100"}]

        with pytest.raises(SystemExit) as exc:
            from rlsbl.commands.watch import run_cmd
            run_cmd(None, ["abc123"], {})
        assert exc.value.code == 0


class TestWatchRunCmdKeyboardInterrupt:
    """Cover keyboard interrupt in SHA path (lines 437-438)."""

    @patch(f"{MOD_WATCH}.run", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt(self, _):
        from rlsbl.commands.watch import run_cmd
        with pytest.raises(SystemExit) as exc:
            run_cmd(None, ["abc"], {})
        assert exc.value.code == 130


# ============================================================================
# targets/protocol.py -- cover default method implementations
# ============================================================================


class TestReleaseTargetProtocol:
    """Cover default implementations in targets/protocol.py."""

    def test_protocol_defaults(self):
        """Test the default method implementations defined on the Protocol."""
        from rlsbl.targets.base import BaseTarget

        # BaseTarget is the concrete base that inherits Protocol defaults.
        # Use a real target to test protocol-level defaults.
        t = BaseTarget.__new__(BaseTarget)

        # Call the Protocol's default methods explicitly via the protocol class
        from rlsbl.targets.protocol import ReleaseTarget
        assert ReleaseTarget.read_name(t, ".", None) is None
        assert ReleaseTarget.read_metadata(t, ".") == {}
        assert ReleaseTarget.template_dir(t) is None
        assert ReleaseTarget.shared_template_dir(t) is None
        assert ReleaseTarget.template_vars(t, ".", None) == {}
        assert ReleaseTarget.template_mappings(t, None) == []
        assert ReleaseTarget.shared_template_mappings(t, None) == []
        assert ReleaseTarget.get_project_init_hint(t) == ""
        ReleaseTarget.build(t, ".", "1.0.0")  # no-op, should not raise
        result = ReleaseTarget.dev_install_command(t, ".")
        assert result == {"global": None, "venv": None}


# ============================================================================
# Pipeline modules -- cover template_dir, template_mappings, publish, etc.
# ============================================================================


class TestGoPipeline:
    """Cover GoPipeline uncovered lines."""

    def test_template_dir(self):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", True, {})
        assert p.template_dir() is not None
        assert "go" in p.template_dir()

    def test_template_mappings(self):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", True, {})
        mappings = p.template_mappings(None)
        assert len(mappings) == 1
        assert "publish.yml" in mappings[0]["target"]

    def test_publish_local_false(self, capsys):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", False, {})
        p.publish("/fake", "1.0.0", None)
        assert "Skipping" in capsys.readouterr().out

    def test_publish_no_go_mod(self, capsys, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", True, {})
        p.publish(str(tmp_path), "1.0.0", None)
        assert "could not read module path" in capsys.readouterr().out

    def test_publish_no_go_tool(self, capsys, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        (tmp_path / "go.mod").write_text("module github.com/user/repo\n")
        p = GoPipeline("go", "go", True, {})
        with patch("rlsbl.pipelines.go.require_tool", return_value=False):
            p.publish(str(tmp_path), "1.0.0", None)
        assert "'go' not found" in capsys.readouterr().out

    def test_publish_proxy_notification_failure(self, capsys, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        (tmp_path / "go.mod").write_text("module github.com/user/repo\n")
        p = GoPipeline("go", "go", True, {})
        with patch("rlsbl.pipelines.go.require_tool", return_value=True):
            with patch("rlsbl.pipelines.go.run", side_effect=subprocess.CalledProcessError(1, "go")):
                p.publish(str(tmp_path), "1.0.0", None)
        assert "proxy notification failed" in capsys.readouterr().out

    def test_publish_proxy_success_no_install_path(self, capsys, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        (tmp_path / "go.mod").write_text("module github.com/user/repo\n")
        p = GoPipeline("go", "go", True, {})
        with patch("rlsbl.pipelines.go.require_tool", return_value=True):
            with patch("rlsbl.pipelines.go.run"):
                p.publish(str(tmp_path), "1.0.0", None)
        assert "Notified Go module proxy" in capsys.readouterr().out

    def test_detect_install_path_cmd_layout(self, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", True, {})
        cmd_dir = tmp_path / "cmd" / "myapp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text("package main\n")
        result = p._detect_install_path(str(tmp_path))
        assert result == "./cmd/myapp"

    def test_detect_install_path_root_main(self, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", True, {})
        (tmp_path / "main.go").write_text("package main\n")
        result = p._detect_install_path(str(tmp_path))
        assert result == "."

    def test_detect_install_path_none(self, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", True, {})
        result = p._detect_install_path(str(tmp_path))
        assert result is None

    def test_publish_with_install_path_success(self, capsys, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        (tmp_path / "go.mod").write_text("module github.com/user/repo\n")
        (tmp_path / "main.go").write_text("package main\n")
        p = GoPipeline("go", "go", True, {})
        with patch("rlsbl.pipelines.go.require_tool", return_value=True):
            with patch("rlsbl.pipelines.go.run"):
                with patch("subprocess.run"):
                    p.publish(str(tmp_path), "1.0.0", None)
        out = capsys.readouterr().out
        assert "Notified" in out
        assert "Installed" in out

    def test_publish_with_install_failure(self, capsys, tmp_path):
        from rlsbl.pipelines.go import GoPipeline
        (tmp_path / "go.mod").write_text("module github.com/user/repo\n")
        (tmp_path / "main.go").write_text("package main\n")
        p = GoPipeline("go", "go", True, {})
        with patch("rlsbl.pipelines.go.require_tool", return_value=True):
            with patch("rlsbl.pipelines.go.run"):
                with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "go")):
                    p.publish(str(tmp_path), "1.0.0", None)
        out = capsys.readouterr().out
        assert "go install failed" in out

    def test_required_env_vars(self):
        from rlsbl.pipelines.go import GoPipeline
        p = GoPipeline("go", "go", True, {})
        assert p.required_env_vars() == []


class TestHexPipeline:
    """Cover HexPipeline uncovered lines."""

    def test_template_dir(self):
        from rlsbl.pipelines.hex import HexPipeline
        p = HexPipeline("hex", "hex", True, {})
        assert "hex" in p.template_dir()

    def test_template_mappings(self):
        from rlsbl.pipelines.hex import HexPipeline
        p = HexPipeline("hex", "hex", True, {})
        assert len(p.template_mappings(None)) == 1

    def test_publish_success(self, monkeypatch, capsys):
        from rlsbl.pipelines.hex import HexPipeline
        monkeypatch.setenv("HEX_API_KEY", "fake-key")
        p = HexPipeline("hex", "hex", True, {})
        with patch("rlsbl.pipelines.hex.run"):
            p.publish(".", "1.0.0", None)
        assert "Published to Hex" in capsys.readouterr().out

    def test_publish_failure(self, monkeypatch):
        from rlsbl.pipelines.hex import HexPipeline
        monkeypatch.setenv("HEX_API_KEY", "fake-key")
        p = HexPipeline("hex", "hex", True, {})
        with patch("rlsbl.pipelines.hex.run", side_effect=subprocess.CalledProcessError(1, "mix")):
            with pytest.raises(RuntimeError, match="hex.publish failed"):
                p.publish(".", "1.0.0", None)


class TestNpmPipeline:
    """Cover NpmPipeline uncovered lines."""

    def test_template_dir(self):
        from rlsbl.pipelines.npm import NpmPipeline
        p = NpmPipeline("npm", "npm", True, {})
        assert "npm" in p.template_dir()

    def test_template_mappings_npm(self):
        from rlsbl.pipelines.npm import NpmPipeline
        p = NpmPipeline("npm", "npm", True, {})
        mappings = p.template_mappings(None)
        assert len(mappings) == 1
        assert "publish.yml" in mappings[0]["target"]

    def test_template_mappings_pnpm(self, tmp_path, monkeypatch):
        from rlsbl.pipelines.npm import NpmPipeline
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pnpm-lock.yaml").write_text("")
        (tmp_path / ".git").mkdir()
        p = NpmPipeline("npm", "npm", True, {})
        mappings = p.template_mappings(_ctx(root=tmp_path))
        assert "pnpm" in mappings[0]["template"]

    def test_template_mappings_yarn(self, tmp_path, monkeypatch):
        from rlsbl.pipelines.npm import NpmPipeline
        monkeypatch.chdir(tmp_path)
        (tmp_path / "yarn.lock").write_text("")
        (tmp_path / ".git").mkdir()
        p = NpmPipeline("npm", "npm", True, {})
        mappings = p.template_mappings(_ctx(root=tmp_path))
        assert "yarn" in mappings[0]["template"]

    def test_publish_success(self, monkeypatch, capsys):
        from rlsbl.pipelines.npm import NpmPipeline
        monkeypatch.setenv("NPM_TOKEN", "fake-token")
        p = NpmPipeline("npm", "npm", True, {})
        with patch("rlsbl.pipelines.npm.run"):
            p.publish(".", "1.0.0", None)
        assert "Published to npm" in capsys.readouterr().out

    def test_publish_failure(self, monkeypatch):
        from rlsbl.pipelines.npm import NpmPipeline
        monkeypatch.setenv("NPM_TOKEN", "fake-token")
        p = NpmPipeline("npm", "npm", True, {})
        with patch("rlsbl.pipelines.npm.run", side_effect=subprocess.CalledProcessError(1, "npm")):
            with pytest.raises(RuntimeError, match="npm publish failed"):
                p.publish(".", "1.0.0", None)


class TestDenoPipeline:
    """Cover DenoPipeline uncovered lines."""

    def test_template_dir(self):
        from rlsbl.pipelines.deno import DenoPipeline
        p = DenoPipeline("deno", "deno", True, {})
        assert "deno" in p.template_dir()

    def test_template_mappings(self):
        from rlsbl.pipelines.deno import DenoPipeline
        p = DenoPipeline("deno", "deno", True, {})
        assert len(p.template_mappings(None)) == 1

    def test_publish_local_false(self, capsys):
        from rlsbl.pipelines.deno import DenoPipeline
        p = DenoPipeline("deno", "deno", False, {})
        p.publish(".", "1.0.0", None)
        assert "Skipping" in capsys.readouterr().out

    def test_publish_explicit_token_var(self, monkeypatch, capsys):
        from rlsbl.pipelines.deno import DenoPipeline
        monkeypatch.setenv("MY_TOKEN", "fake")
        p = DenoPipeline("deno", "deno", True, {"token_var": "MY_TOKEN"})
        with patch("rlsbl.pipelines.deno.run"):
            p.publish(".", "1.0.0", None)
        assert "Published to JSR" in capsys.readouterr().out

    def test_publish_explicit_token_missing(self, monkeypatch):
        from rlsbl.pipelines.deno import DenoPipeline
        monkeypatch.delenv("MY_TOKEN", raising=False)
        p = DenoPipeline("deno", "deno", True, {"token_var": "MY_TOKEN"})
        with pytest.raises(SystemExit):
            p.publish(".", "1.0.0", None)

    def test_publish_dual_token_deno(self, monkeypatch, capsys):
        from rlsbl.pipelines.deno import DenoPipeline
        monkeypatch.setenv("DENO_TOKEN", "fake")
        p = DenoPipeline("deno", "deno", True, {})
        with patch("rlsbl.pipelines.deno.run"):
            p.publish(".", "1.0.0", None)
        assert "Published to JSR" in capsys.readouterr().out

    def test_publish_dual_token_jsr_fallback(self, monkeypatch, capsys):
        from rlsbl.pipelines.deno import DenoPipeline
        monkeypatch.delenv("DENO_TOKEN", raising=False)
        monkeypatch.setenv("JSR_TOKEN", "fake")
        p = DenoPipeline("deno", "deno", True, {})
        with patch("rlsbl.pipelines.deno.run"):
            p.publish(".", "1.0.0", None)
        assert "Published to JSR" in capsys.readouterr().out

    def test_publish_no_token_exits(self, monkeypatch):
        from rlsbl.pipelines.deno import DenoPipeline
        monkeypatch.delenv("DENO_TOKEN", raising=False)
        monkeypatch.delenv("JSR_TOKEN", raising=False)
        p = DenoPipeline("deno", "deno", True, {})
        with pytest.raises(SystemExit):
            p.publish(".", "1.0.0", None)

    def test_publish_failure(self, monkeypatch):
        from rlsbl.pipelines.deno import DenoPipeline
        monkeypatch.setenv("DENO_TOKEN", "fake")
        p = DenoPipeline("deno", "deno", True, {})
        with patch("rlsbl.pipelines.deno.run", side_effect=subprocess.CalledProcessError(1, "deno")):
            with pytest.raises(RuntimeError, match="deno publish failed"):
                p.publish(".", "1.0.0", None)

    def test_required_env_vars_local_false(self):
        from rlsbl.pipelines.deno import DenoPipeline
        p = DenoPipeline("deno", "deno", False, {})
        assert p.required_env_vars() == []

    def test_required_env_vars_explicit_token(self):
        from rlsbl.pipelines.deno import DenoPipeline
        p = DenoPipeline("deno", "deno", True, {"token_var": "MY_TOKEN"})
        assert p.required_env_vars() == ["MY_TOKEN"]

    def test_required_env_vars_default(self):
        from rlsbl.pipelines.deno import DenoPipeline
        p = DenoPipeline("deno", "deno", True, {})
        assert p.required_env_vars() == ["DENO_TOKEN"]


class TestCargoPipeline:
    """Cover CargoPipeline uncovered lines."""

    def test_template_dir(self):
        from rlsbl.pipelines.cargo import CargoPipeline
        p = CargoPipeline("cargo", "cargo", True, {})
        assert "cargo" in p.template_dir()

    def test_template_mappings(self):
        from rlsbl.pipelines.cargo import CargoPipeline
        p = CargoPipeline("cargo", "cargo", True, {})
        assert len(p.template_mappings(None)) == 1

    def test_publish_success(self, monkeypatch, capsys):
        from rlsbl.pipelines.cargo import CargoPipeline
        monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "fake")
        p = CargoPipeline("cargo", "cargo", True, {})
        with patch("rlsbl.pipelines.cargo.run"):
            p.publish(".", "1.0.0", None)
        assert "Published to crates.io" in capsys.readouterr().out

    def test_publish_failure(self, monkeypatch):
        from rlsbl.pipelines.cargo import CargoPipeline
        monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "fake")
        p = CargoPipeline("cargo", "cargo", True, {})
        with patch("rlsbl.pipelines.cargo.run", side_effect=subprocess.CalledProcessError(1, "cargo")):
            with pytest.raises(RuntimeError, match="cargo publish failed"):
                p.publish(".", "1.0.0", None)


class TestDockerPipeline:
    """Cover DockerPipeline uncovered lines."""

    def test_template_dir(self):
        from rlsbl.pipelines.docker import DockerPipeline
        p = DockerPipeline("docker", "docker", True, {})
        assert "docker" in p.template_dir()

    def test_template_mappings(self):
        from rlsbl.pipelines.docker import DockerPipeline
        p = DockerPipeline("docker", "docker", True, {})
        assert len(p.template_mappings(None)) == 1

    def test_publish_no_image_config(self, monkeypatch):
        from rlsbl.pipelines.docker import DockerPipeline
        p = DockerPipeline("docker", "docker", True, {})
        with pytest.raises(RuntimeError, match="requires 'image' and 'registry'"):
            p._publish_command(".", "1.0.0", "user", "pass")

    def test_publish_no_docker(self, monkeypatch):
        from rlsbl.pipelines.docker import DockerPipeline
        p = DockerPipeline("docker", "docker", True, {"image": "myapp", "registry": "ghcr.io"})
        with patch("rlsbl.pipelines.docker.require_tool", return_value=False):
            with pytest.raises(RuntimeError, match="docker.*not found"):
                p._publish_command(".", "1.0.0", "user", "pass")

    def test_publish_success(self, monkeypatch, capsys):
        from rlsbl.pipelines.docker import DockerPipeline
        p = DockerPipeline("docker", "docker", True, {"image": "myapp", "registry": "ghcr.io"})
        with patch("rlsbl.pipelines.docker.require_tool", return_value=True):
            with patch("rlsbl.pipelines.docker.run"):
                p._publish_command(".", "1.0.0", "user", "pass")
        assert "Published Docker image" in capsys.readouterr().out

    def test_publish_failure(self, monkeypatch):
        from rlsbl.pipelines.docker import DockerPipeline
        p = DockerPipeline("docker", "docker", True, {"image": "myapp", "registry": "ghcr.io"})
        with patch("rlsbl.pipelines.docker.require_tool", return_value=True):
            with patch("rlsbl.pipelines.docker.run", side_effect=subprocess.CalledProcessError(1, "docker")):
                with pytest.raises(RuntimeError, match="Docker publish failed"):
                    p._publish_command(".", "1.0.0", "user", "pass")


class TestMavenPipeline:
    """Cover MavenPipeline uncovered lines."""

    def test_template_dir(self):
        from rlsbl.pipelines.maven import MavenPipeline
        p = MavenPipeline("maven", "maven", True, {})
        assert "maven" in p.template_dir()

    def test_template_mappings(self):
        from rlsbl.pipelines.maven import MavenPipeline
        p = MavenPipeline("maven", "maven", True, {})
        assert len(p.template_mappings(None)) == 1

    def test_publish_local_false(self, capsys):
        from rlsbl.pipelines.maven import MavenPipeline
        p = MavenPipeline("maven", "maven", False, {})
        p.publish(".", "1.0.0", None)
        assert "Skipping" in capsys.readouterr().out

    def test_publish_no_token(self, monkeypatch):
        from rlsbl.pipelines.maven import MavenPipeline
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        p = MavenPipeline("maven", "maven", True, {})
        with pytest.raises(SystemExit):
            p.publish(".", "1.0.0", None)

    def test_publish_gradle(self, monkeypatch, tmp_path, capsys):
        from rlsbl.pipelines.maven import MavenPipeline
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        (tmp_path / "gradlew").write_text("#!/bin/bash\n")
        p = MavenPipeline("maven", "maven", True, {})
        with patch("rlsbl.pipelines.maven.run"):
            p.publish(str(tmp_path), "1.0.0", None)
        assert "Gradle" in capsys.readouterr().out

    def test_publish_maven(self, monkeypatch, tmp_path, capsys):
        from rlsbl.pipelines.maven import MavenPipeline
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        (tmp_path / "pom.xml").write_text("<project/>")
        p = MavenPipeline("maven", "maven", True, {})
        with patch("rlsbl.pipelines.maven.run"):
            p.publish(str(tmp_path), "1.0.0", None)
        assert "Maven" in capsys.readouterr().out

    def test_publish_no_build_tool(self, monkeypatch, tmp_path):
        from rlsbl.pipelines.maven import MavenPipeline
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        p = MavenPipeline("maven", "maven", True, {})
        with pytest.raises(RuntimeError, match="no gradlew or pom.xml"):
            p.publish(str(tmp_path), "1.0.0", None)

    def test_publish_gradle_failure(self, monkeypatch, tmp_path):
        from rlsbl.pipelines.maven import MavenPipeline
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        (tmp_path / "gradlew").write_text("#!/bin/bash\n")
        p = MavenPipeline("maven", "maven", True, {})
        with patch("rlsbl.pipelines.maven.run", side_effect=subprocess.CalledProcessError(1, "gradlew")):
            with pytest.raises(RuntimeError, match="Gradle publish failed"):
                p.publish(str(tmp_path), "1.0.0", None)

    def test_publish_maven_failure(self, monkeypatch, tmp_path):
        from rlsbl.pipelines.maven import MavenPipeline
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        (tmp_path / "pom.xml").write_text("<project/>")
        p = MavenPipeline("maven", "maven", True, {})
        with patch("rlsbl.pipelines.maven.run", side_effect=subprocess.CalledProcessError(1, "mvn")):
            with pytest.raises(RuntimeError, match="Maven deploy failed"):
                p.publish(str(tmp_path), "1.0.0", None)

    def test_required_env_vars_local(self):
        from rlsbl.pipelines.maven import MavenPipeline
        p = MavenPipeline("maven", "maven", True, {})
        assert p.required_env_vars() == ["GITHUB_TOKEN"]

    def test_required_env_vars_not_local(self):
        from rlsbl.pipelines.maven import MavenPipeline
        p = MavenPipeline("maven", "maven", False, {})
        assert p.required_env_vars() == []
