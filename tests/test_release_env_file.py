"""Tests for the shared ``env_file`` entry helper.

``env_file`` names a file of KEY=VALUE pairs (typically the shared
``~/Projects/.env``) that the release machinery loads into ``os.environ`` so
deploy steps, post-release hooks and local publish pipelines find their
credentials. It used to be loaded in exactly ONE place -- deep inside
``_run_cmd_inner`` -- so:

- ``rlsbl release resume`` re-ran the deploy and post-release hooks WITHOUT it
  (the failure a resume exists to fix reappeared, now as a missing-credential
  error),
- ``rlsbl deploy`` ran the same steps without it,
- the batch orchestrator's own root-level work never saw it,

and a configured-but-missing file printed a warning and continued, so the
release proceeded to publish and deploy with an environment nobody supplied.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.config import load_env_file
from rlsbl.context import ProjectContext
from rlsbl.errors import ConfigError

from conftest import make_releasable_state, make_workspace, with_root_member


_PROBE = "RLSBL_ENV_FILE_PROBE"


@pytest.fixture(autouse=True)
def _clear_probe():
    """The helper writes into the real os.environ; undo it per test."""
    os.environ.pop(_PROBE, None)
    yield
    os.environ.pop(_PROBE, None)


def _env_file(tmp_path, value="loaded"):
    path = tmp_path / "shared.env"
    path.write_text(f"# a comment\n{_PROBE}={value}\n")
    return str(path)


class TestLoadEnvFile:
    """The loader itself: a configured file that is not there is an error."""

    def test_missing_file_is_a_hard_error(self, tmp_path):
        missing = str(tmp_path / "nope.env")
        with pytest.raises(ConfigError) as exc:
            load_env_file(missing)
        assert missing in str(exc.value)
        assert "env_file" in str(exc.value)

    def test_present_file_is_loaded(self, tmp_path):
        load_env_file(_env_file(tmp_path))
        assert os.environ[_PROBE] == "loaded"

    def test_tilde_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _env_file(tmp_path)
        load_env_file("~/shared.env")
        assert os.environ[_PROBE] == "loaded"


class TestSharedHelper:
    """``load_release_env`` is the one entry point every command shares."""

    def test_no_env_file_configured_is_a_no_op(self):
        from rlsbl.commands.release.shared import load_release_env

        assert load_release_env({}) is None
        assert load_release_env(None) is None

    def test_cloudflare_alias_is_applied(self, tmp_path):
        from rlsbl.commands.release.shared import load_release_env

        path = tmp_path / "cf.env"
        path.write_text("CF_ACCOUNT_ID=acct-123\n")
        os.environ.pop("CF_ACCOUNT_ID", None)
        os.environ.pop("CLOUDFLARE_ACCOUNT_ID", None)
        try:
            load_release_env({"env_file": str(path)})
            assert os.environ["CLOUDFLARE_ACCOUNT_ID"] == "acct-123"
        finally:
            os.environ.pop("CF_ACCOUNT_ID", None)
            os.environ.pop("CLOUDFLARE_ACCOUNT_ID", None)


class TestResumeLoadsTheEnvFile:
    """A resume re-runs deploy and the post-release hooks: it needs the env."""

    def test_resume_loads_it(self, mock_git_repo, tmp_path):
        from test_release_resume import (
            _fake_run_factory,
            _make_in_progress_state,
            _setup_releasable_npm_project,
        )
        from rlsbl.commands.release import resume_cmd
        from rlsbl.commands.release.release_state import load_release_state

        _setup_releasable_npm_project(mock_git_repo)
        state_path = _make_in_progress_state(mock_git_repo)

        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)),
            workspace_root=None,
            config={
                "publish_mode": "ci",
                "pipelines": {},
                "env_file": _env_file(tmp_path),
            },
        )
        with (
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run_gh", return_value=""),
            patch("rlsbl.commands.release.run",
                  side_effect=_fake_run_factory()),
        ):
            resume_cmd(load_release_state(state_path), {"quiet": True}, ctx=ctx)

        assert os.environ.get(_PROBE) == "loaded", (
            "a resume runs the deploy and post-release steps that need it"
        )

    def test_resume_hard_errors_on_a_missing_env_file(self, mock_git_repo, tmp_path):
        from test_release_resume import (
            _make_in_progress_state,
            _setup_releasable_npm_project,
        )
        from rlsbl.commands.release import resume_cmd
        from rlsbl.commands.release.release_state import load_release_state

        _setup_releasable_npm_project(mock_git_repo)
        state_path = _make_in_progress_state(mock_git_repo)

        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)),
            workspace_root=None,
            config={
                "publish_mode": "ci",
                "pipelines": {},
                "env_file": str(tmp_path / "absent.env"),
            },
        )
        with pytest.raises(SystemExit) as exc:
            resume_cmd(load_release_state(state_path), {"quiet": True}, ctx=ctx)
        assert exc.value.code == 1


class TestRunHardErrorsOnAMissingEnvFile:

    def test_release_run_refuses(self, mock_git_repo, tmp_path, capsys):
        from test_release_resume import _setup_releasable_npm_project
        from rlsbl.commands.release import run_cmd
        from rlsbl.release_file import ReleaseConfig

        _setup_releasable_npm_project(mock_git_repo)
        missing = str(tmp_path / "absent.env")
        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)),
            workspace_root=None,
            config={
                "publish_mode": "ci", "pipelines": {}, "env_file": missing,
            },
        )
        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.validate_gh_push_access"),
        ):
            with pytest.raises(SystemExit) as exc:
                run_cmd(
                    ReleaseConfig(bump="patch", include=["npm"], exclude=[]),
                    {"quiet": True}, ctx=ctx,
                )
        assert exc.value.code == 1
        assert missing in capsys.readouterr().err


class TestDeployLoadsTheEnvFile:
    """`rlsbl deploy` runs the same steps the release deploy phase runs."""

    def _ctx(self, tmp_path, env_file):
        return ProjectContext(
            project_root=Path(str(tmp_path)),
            workspace_root=None,
            config={
                "env_file": env_file,
                "deploy": [{
                    "name": "site",
                    "host": "example.invalid",
                    "only_on": ["main"],
                    "steps": ["echo hi"],
                }],
            },
        )

    def test_deploy_loads_it(self, tmp_path):
        from rlsbl.commands import deploy_cmd
        from rlsbl.deploy import DeployResult

        seen = {}

        def fake_deploy(target_config, branch):
            seen["probe"] = os.environ.get(_PROBE)
            return DeployResult(
                target_name=target_config["name"], success=True, message="ok",
            )

        with (
            patch.object(deploy_cmd, "deploy_target", side_effect=fake_deploy),
            patch.object(deploy_cmd, "get_current_branch", return_value="main"),
        ):
            deploy_cmd.run_cmd(
                None, [], {}, ctx=self._ctx(tmp_path, _env_file(tmp_path)),
            )

        assert seen["probe"] == "loaded"

    def test_deploy_hard_errors_on_a_missing_env_file(self, tmp_path):
        from rlsbl.commands import deploy_cmd

        ctx = self._ctx(tmp_path, str(tmp_path / "absent.env"))
        with patch.object(deploy_cmd, "deploy_target") as deployed:
            with pytest.raises(ConfigError):
                deploy_cmd.run_cmd(None, [], {}, ctx=ctx)
        deployed.assert_not_called()


class TestBatchOrchestratorLoadsTheEnvFile:
    """The orchestrator's own root-level work (selfdoc, hooks, push) needs it."""

    def _workspace(self, ws, env_file):
        from rlsbl.release_file import get_batch_release_file_path
        from rlsbl.workspace import save_workspace

        make_workspace(str(ws), [{"path": "alpha", "name": "alpha"}])
        # The releasable has shipped 0.1.0 below (the tag), so its ledger
        # carries the matching archive -- an empty ledger next to a real tag
        # is refused.
        make_releasable_state(ws, "alpha", version="0.1.0")
        (ws / "alpha").mkdir()
        (ws / "alpha" / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )
        (ws / ".rlsbl").mkdir(exist_ok=True)
        (ws / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "none", "env_file": env_file}) + "\n"
        )
        batch_path = get_batch_release_file_path(str(ws))
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)
        with open(batch_path, "w", encoding="utf-8") as f:
            f.write(
                '[releasables.alpha]\nbump = "patch"\n'
                'description = "release alpha"\n'
                'include = ["pypi"]\nexclude = []\n'
            )

    def test_batch_loads_it_before_releasing(
        self, mock_git_repo, tmp_path, bypass_upfront_validation,
    ):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        ws = mock_git_repo
        self._workspace(ws, _env_file(tmp_path))
        seen = {}

        def mock_run(release_config, flags, **kwargs):
            seen["probe"] = os.environ.get(_PROBE)
            raise SystemExit(1)

        with (
            patch("rlsbl.commands.release.run_cmd", mock_run),
            patch("rlsbl.commands.monorepo.batch_release.commit_files"),
        ):
            with pytest.raises(SystemExit):
                _cmd_batch_release({"dry-run": False, "quiet": True},
                                   project_root=ws)

        assert seen["probe"] == "loaded"

    def test_batch_hard_errors_on_a_missing_env_file(
        self, mock_git_repo, tmp_path, bypass_upfront_validation, capsys,
    ):
        from rlsbl.commands.monorepo.batch_release import _cmd_batch_release

        ws = mock_git_repo
        missing = str(tmp_path / "absent.env")
        self._workspace(ws, missing)

        def mock_run(release_config, flags, **kwargs):
            raise AssertionError("must not release without the declared env")

        with (
            patch("rlsbl.commands.release.run_cmd", mock_run),
            patch("rlsbl.commands.monorepo.batch_release.commit_files"),
        ):
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release({"dry-run": False, "quiet": True},
                                   project_root=ws)
        assert exc.value.code == 1
        assert missing in capsys.readouterr().err
