"""The release refuses publishing that needs a public repository from a private one.

The refusal runs before anything is pushed, names each finding's fix, and
clears once the fix is applied: ``"provenance": false`` for npm,
``"publish_mode": "none"`` for the Go module proxy, and ``rlsbl scaffold`` for
an attesting PyPI publish step.
"""

import os
from unittest.mock import patch

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    _abort_on_private_repo_publishing,
)

from test_private_repo_publishing import (
    ATTESTING,
    GO_BINARY,
    GO_LIBRARY,
    PRIVATE,
    PUBLIC,
    _config,
    _scaffold_pypi,
    _workflows,
)

RUN_GH = "rlsbl.commands.release.validate.run_gh"

from rlsbl import private_repo_publishing as _prp  # noqa: E402

_REAL_COMMITTED_WORKFLOW_USES = _prp.committed_workflow_uses


@pytest.fixture(autouse=True)
def _real_committed_workflow_read(monkeypatch):
    """The real read of the committed workflows (conftest neutralizes it)."""
    monkeypatch.setattr(
        _prp, "committed_workflow_uses", _REAL_COMMITTED_WORKFLOW_USES,
    )


class TestTheReleaseRefusesAndTheFixClearsIt:

    def test_nothing_in_use_asks_nothing(self, tmp_path):
        with patch(RUN_GH) as gh:
            _abort_on_private_repo_publishing(
                [_config(GO_BINARY)], gh_config={},
                workflows_dir=_workflows(tmp_path),
            )
        gh.assert_not_called()

    def test_a_public_repository_passes(self, tmp_path):
        with patch(RUN_GH, return_value=PUBLIC):
            _abort_on_private_repo_publishing(
                [_config(GO_LIBRARY)], gh_config={},
                workflows_dir=_workflows(tmp_path, ATTESTING),
            )

    def test_the_go_proxy_refusal_clears_under_publish_mode_none(self, tmp_path):
        wf = _workflows(tmp_path)
        with patch(RUN_GH, return_value=PRIVATE):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_private_repo_publishing(
                    [_config(GO_LIBRARY)], gh_config={}, workflows_dir=wf,
                )
            assert '"publish_mode": "none"' in str(exc.value)
            # The fix the refusal names:
            _abort_on_private_repo_publishing(
                [_config(GO_LIBRARY, publish_mode="none")], gh_config={},
                workflows_dir=wf,
            )

    def test_the_npm_provenance_refusal_clears_with_provenance_false(self, tmp_path):
        npm = {"npm": {"type": "npm", "local": False, "provenance": True}}
        wf = _workflows(tmp_path)
        with patch(RUN_GH, return_value=PRIVATE):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_private_repo_publishing(
                    [_config(npm)], gh_config={}, workflows_dir=wf,
                )
            assert '"provenance": false' in str(exc.value)
            npm["npm"]["provenance"] = False
            _abort_on_private_repo_publishing(
                [_config(npm)], gh_config={}, workflows_dir=wf,
            )

    def test_an_unknown_visibility_is_a_refusal(self, tmp_path):
        with patch(RUN_GH, side_effect=RuntimeError("no GitHub remote")):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_private_repo_publishing(
                    [_config(GO_LIBRARY)], gh_config={},
                    workflows_dir=_workflows(tmp_path),
                )
        assert "could not determine" in str(exc.value)



class TestTheAttestationFix:

    def test_the_attestation_refusal_clears_after_rlsbl_scaffold(self, mock_git_repo):
        """The refusal says to run `rlsbl scaffold`; doing so clears it."""
        wf = _workflows(mock_git_repo, ATTESTING)
        with patch(RUN_GH, return_value=PRIVATE):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_private_repo_publishing([], gh_config={}, workflows_dir=wf)
            assert "rlsbl scaffold" in str(exc.value)
            os.remove(os.path.join(wf, "publish.yml"))
            _scaffold_pypi(mock_git_repo, private=True)
            _abort_on_private_repo_publishing([], gh_config={}, workflows_dir=wf)




class TestTheReleaseFlow:
    """`rlsbl release run` refuses before pushing, and the fix clears it."""

    def _release(self, repo, gh, pushed):
        from pathlib import Path
        from unittest.mock import patch as _patch

        from rlsbl.commands.release import run_cmd
        from rlsbl.context import ProjectContext
        from rlsbl.release_file import ReleaseConfig

        with (
            _patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            _patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            _patch("rlsbl.commands.release.validate_gh_push_access"),
            _patch("rlsbl.commands.release.validate_branch_and_remote",
                   return_value="main"),
            _patch("rlsbl.commands.release.push_if_needed",
                   side_effect=lambda *a, **k: pushed.append(a)),
            _patch("rlsbl.commands.release.resolve_tag_push_plan",
                   return_value=False),
            _patch("rlsbl.commands.release.run_gh", side_effect=gh),
            _patch("rlsbl.commands.release.validate.run_gh", side_effect=gh),
        ):
            run_cmd(
                ReleaseConfig(bump="patch", include=["npm"], exclude=[],
                              description="test release"),
                {"quiet": True, "watch": False},
                ctx=ProjectContext(
                    project_root=Path(repo), workspace_root=None,
                    config={"publish_mode": "ci", "pipelines": {}},
                ),
            )

    def test_an_attesting_workflow_in_a_private_repository(self, mock_git_repo, capsys):
        import subprocess

        from test_post_push_failure_state import _setup_npm_project

        def git(*args):
            subprocess.run(["git", *args], cwd=mock_git_repo, check=True,
                           capture_output=True)

        _setup_npm_project(mock_git_repo)
        _workflows(mock_git_repo, ATTESTING)
        git("add", ".github/workflows/publish.yml")
        git("commit", "-q", "-m", "publish workflow",
            "--trailer", "Autogenerated: true")

        def gh(args, *_config, **kwargs):
            args = list(args)
            if args[:2] == ["repo", "view"]:
                return PRIVATE
            if args[:2] == ["release", "view"]:
                raise subprocess.CalledProcessError(1, "gh release view")
            return ""

        pushed = []
        with pytest.raises(SystemExit) as exc:
            self._release(mock_git_repo, gh, pushed)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "this repository is PRIVATE" in err
        assert "transparency log" in err
        assert pushed == [], "refused before anything was pushed"

    def test_an_uncommitted_fix_does_not_clear_the_committed_workflow(
        self, mock_git_repo, capsys,
    ):
        """The release tags the COMMITTED tree, so its workflow is the one judged.

        Regenerating the workflow with ``attestations: false`` and leaving it
        uncommitted changes nothing the release publishes: the release checkout
        runs the committed commit, whose publish step still attests.
        """
        import subprocess

        from test_post_push_failure_state import _setup_npm_project

        def git(*args):
            subprocess.run(["git", *args], cwd=mock_git_repo, check=True,
                           capture_output=True)

        _setup_npm_project(mock_git_repo)
        _workflows(mock_git_repo, ATTESTING)
        git("add", ".github/workflows/publish.yml")
        git("commit", "-q", "-m", "publish workflow",
            "--trailer", "Autogenerated: true")
        # The fix, applied to the working tree only.
        _workflows(mock_git_repo, ATTESTING + "          attestations: false\n")

        def gh(args, *_config, **kwargs):
            args = list(args)
            if args[:2] == ["repo", "view"]:
                return PRIVATE
            if args[:2] == ["release", "view"]:
                raise subprocess.CalledProcessError(1, "gh release view")
            return ""

        pushed = []
        with pytest.raises(SystemExit) as exc:
            self._release(mock_git_repo, gh, pushed)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "this repository is PRIVATE" in err
        assert "transparency log" in err
        assert pushed == [], "refused before anything was pushed"


GO_VERIFYING = """\
name: Publish
on:
  release:
    types: [published]
jobs:
  verify-module:
    runs-on: ubuntu-latest
    steps:
      - name: Verify module is available on proxy
        run: |
          MODULE="example.com/lib"
          GOPROXY=proxy.golang.org go list -m "${MODULE}@v${VERSION}"
"""

NPM_PROVENANCE = """\
name: Publish
on:
  release:
    types: [published]
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: npm publish --provenance --access public ${{ steps.dist-tag.outputs.tag }}
"""


class TestTheConfigFixAloneLeavesTheCommittedWorkflow:
    """The config fix changes what the NEXT scaffold renders, not the committed
    publish workflow a Release starts. A private repository whose committed
    workflow still asks the Go module proxy for the module, or still publishes
    with --provenance, uses exactly what the refusal named -- after the tag is
    public. So the workflow is judged too, and the fix names the scaffold that
    regenerates (or removes) it."""

    def test_the_go_refusal_names_the_scaffold_that_removes_the_workflow(self):
        (use,) = config_uses_of(GO_LIBRARY)
        assert '"publish_mode": "none"' in use.fix
        assert "rlsbl scaffold" in use.fix

    def test_the_npm_refusal_names_the_scaffold_that_regenerates_the_workflow(self):
        (use,) = config_uses_of(
            {"npm": {"type": "npm", "local": False, "provenance": True}},
        )
        assert '"provenance": false' in use.fix
        assert "rlsbl scaffold" in use.fix

    def test_a_committed_proxy_verification_is_refused_until_removed(self, tmp_path):
        wf = _workflows(tmp_path, GO_VERIFYING)
        fixed_config = [_config(GO_LIBRARY, publish_mode="none")]
        with patch(RUN_GH, return_value=PRIVATE):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_private_repo_publishing(
                    fixed_config, gh_config={}, workflows_dir=wf,
                )
            assert "Go module proxy" in str(exc.value)
            assert "rlsbl scaffold" in str(exc.value)
            os.remove(os.path.join(wf, "publish.yml"))
            _abort_on_private_repo_publishing(
                fixed_config, gh_config={}, workflows_dir=wf,
            )

    def test_a_committed_provenance_flag_is_refused_until_regenerated(self, tmp_path):
        npm = {"npm": {"type": "npm", "local": False, "provenance": False}}
        wf = _workflows(tmp_path, NPM_PROVENANCE)
        with patch(RUN_GH, return_value=PRIVATE):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_private_repo_publishing(
                    [_config(npm)], gh_config={}, workflows_dir=wf,
                )
            assert "--provenance" in str(exc.value)
            assert "rlsbl scaffold" in str(exc.value)
            _workflows(tmp_path, NPM_PROVENANCE.replace("--provenance ", ""))
            _abort_on_private_repo_publishing(
                [_config(npm)], gh_config={}, workflows_dir=wf,
            )

    def test_a_public_repository_is_not_asked_about_either(self, tmp_path):
        wf = _workflows(tmp_path, GO_VERIFYING)
        with patch(RUN_GH, return_value=PUBLIC):
            _abort_on_private_repo_publishing(
                [_config(GO_LIBRARY)], gh_config={}, workflows_dir=wf,
            )


def config_uses_of(pipelines):
    from rlsbl.private_repo_publishing import config_uses

    return config_uses([_config(pipelines)])
