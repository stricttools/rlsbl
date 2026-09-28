"""A private repository's release publishes nothing that needs a public one.

Three publishing features need a public source repository: npm build
provenance, PyPI attestations (which record the repository's name, workflow,
and commit in a public transparency log), and the Go module proxy (which cannot
fetch a private module and caches every version it is asked about). The release
refuses them before pushing, ``rlsbl check --name private-repo-publishing``
reports them, and scaffold writes ``attestations: false`` for a private
repository. Every refusal's fix is applied here and seen to clear it.
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch


from rlsbl import app
from rlsbl.private_repo_publishing import config_uses, workflow_uses

from conftest import make_ctx

PRIVATE = '{"isPrivate": true}'
PUBLIC = '{"isPrivate": false}'

GO_LIBRARY = {"go": {"type": "go", "local": False, "artifact": "library",
                     "target": "go"}}
GO_BINARY = {"go": {"type": "go", "local": False, "artifact": "binary",
                    "target": "go"}}
GO_LOCAL = {"go": {"type": "go", "local": True, "artifact": "binary",
                   "target": "go"}}

ATTESTING = """\
name: Publish
on:
  release:
    types: [published]
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@v1
        with:
          skip-existing: true
"""


def _config(pipelines, publish_mode="ci"):
    return {"publish_mode": publish_mode, "pipelines": pipelines}


def _workflows(tmp_path, text=None):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (wf / "publish.yml").write_text(text)
    return str(wf)


class TestWhatNeedsAPublicRepository:

    def test_a_go_library_published_from_ci(self):
        (use,) = config_uses([_config(GO_LIBRARY)])
        assert "Go module proxy" in use.what
        assert '"publish_mode": "none"' in use.fix

    def test_a_local_go_pipeline(self):
        (use,) = config_uses([_config(GO_LOCAL)])
        assert "notifies the Go module proxy" in use.what

    def test_a_go_binary_published_from_ci_never_asks_the_proxy(self):
        assert config_uses([_config(GO_BINARY)]) == []

    def test_nothing_publishes_under_publish_mode_none(self):
        assert config_uses([_config(GO_LIBRARY, publish_mode="none")]) == []

    def test_a_pypi_publish_step_attests_by_default(self, tmp_path):
        (use,) = workflow_uses(_workflows(tmp_path, ATTESTING))
        assert "publish.yml" in use.what and "transparency log" in use.what

    def test_a_pypi_publish_step_with_attestations_off(self, tmp_path):
        text = ATTESTING + "          attestations: false\n"
        assert workflow_uses(_workflows(tmp_path, text)) == []


def _scaffold_pypi(root, *, private):
    from rlsbl.commands.init_cmd import run_cmd
    from rlsbl.context import ProjectContext

    (root / "pyproject.toml").write_text(
        '[project]\nname = "attest-test"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
    )
    config = {"publish_mode": "ci", "targets": ["pypi"]}
    (root / ".rlsbl").mkdir(exist_ok=True)
    (root / ".rlsbl" / "config.json").write_text(json.dumps(config) + "\n")
    with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=private), \
         patch("sys.stdout", new_callable=StringIO):
        run_cmd("pypi", [], {}, ctx=ProjectContext(
            project_root=Path("."), workspace_root=None, config=config,
        ))
    return (root / ".github" / "workflows" / "publish.yml").read_text()


class TestScaffold:

    def test_a_private_repository_gets_attestations_off(self, mock_git_repo):
        text = _scaffold_pypi(mock_git_repo, private=True)
        assert "attestations: false" in text

    def test_a_public_repository_keeps_the_default(self, mock_git_repo):
        text = _scaffold_pypi(mock_git_repo, private=False)
        assert "attestations" not in text

class TestTheRegisteredCheck:

    def _run(self, tmp_path, config):
        return app._check_defs["private-repo-publishing"].impl(
            make_ctx(tmp_path, config),
        )

    def _repo(self, tmp_path):
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        _workflows(tmp_path)

    def test_private_fails(self, tmp_path):
        self._repo(tmp_path)
        with patch("rlsbl.utils.run_gh", return_value=PRIVATE):
            result = self._run(tmp_path, _config(GO_LIBRARY))
        assert result.status == "fail"
        assert "Go module proxy" in " ".join(p.text for p in result.problems)

    def test_public_passes(self, tmp_path):
        self._repo(tmp_path)
        with patch("rlsbl.utils.run_gh", return_value=PUBLIC):
            result = self._run(tmp_path, _config(GO_LIBRARY))
        assert result.status == "pass"

    def test_nothing_in_use_skips(self, tmp_path):
        self._repo(tmp_path)
        result = self._run(tmp_path, _config(GO_BINARY))
        assert result.status == "skip"
