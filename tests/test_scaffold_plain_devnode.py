"""Scaffold root resolution for dev-node members, and a bare scaffold.

When `--target` is passed, scaffold uses the current directory as its root,
so a dev-node member is judged non-releasable from its own directory. A bare
`rlsbl scaffold` with no `.rlsbl/config.json` and no manifest still errors.
"""

import json



from rlsbl.commands.init_cmd import run_cmd, _is_non_releasable_project
from rlsbl.context import create_context
from conftest import make_workspace


class TestScaffoldDevNodeRoot:
    """A dev_node member is judged from its own directory."""

    def _setup_monorepo_with_dev_node(self, mock_git_repo, subdir="infra"):
        """Create a monorepo with a dev_node sub-project.

        Returns the sub-project directory path.
        """
        proj_dir = mock_git_repo / subdir
        proj_dir.mkdir()

        # Set up workspace.toml with the project marked as dev_node
        make_workspace(mock_git_repo, [
            {"path": subdir, "name": subdir, "dev_only": True, "releasable": False},
        ])

        return proj_dir

    def test_is_non_releasable_with_correct_root(self, mock_git_repo, monkeypatch):
        """_is_non_releasable_project returns True when project_root points to
        the sub-project directory (the fixed behavior)."""
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        # With project_root = sub-project dir (correct), non-releasable is detected
        assert _is_non_releasable_project(proj_dir) is True

    def test_is_non_releasable_at_the_workspace_root(self, mock_git_repo, monkeypatch):
        """At the workspace root, the answer is about the root member.

        The workspace root used to match no member at all, so the answer was
        False whatever the workspace said. It is the root member's directory,
        and the default root member is a dev node -- so the answer is True.
        """
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        assert _is_non_releasable_project(mock_git_repo) is True

class TestBareScaffold:
    """Bare `rlsbl scaffold` (no --target)."""

    def test_bare_scaffold_without_config_or_manifest_errors(self, tmp_project):
        """Without .rlsbl/config.json and no manifest files, scaffold errors."""
        import subprocess
        result = subprocess.run(
            # No confirm-skip flag: `scaffold` is `mutating` but not
            # `consequential`, so the framework never prompts for it.
            ["python", "-m", "rlsbl", "scaffold"],
            capture_output=True, text=True,
            cwd=str(tmp_project),
        )
        assert result.returncode != 0
        assert "no package.json" in result.stderr or "not in an rlsbl project" in result.stderr


class TestDevNodeGetsNoPublishWorkflow:
    """Dev nodes cannot be released, so they get no publish workflow.

    Regression: publish scaffolding was gated only on ``publish_mode`` and the
    workspace-root check, so a dev node carrying the ordinary scaffold default
    ``publish_mode: "ci"`` got a ``publish.yml`` that can never legitimately
    run -- ``rlsbl release run`` hard-errors on a dev node.
    """

    def _dev_node_npm_project(self, mock_git_repo, *, dev_node=True,
                              subdir="conformance"):
        proj_dir = mock_git_repo / subdir
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text(json.dumps({"engines": {"node": ">=20"}, 
            "name": "conformance", "version": "0.1.0",
            "scripts": {"test": "node t.js"},
        }, indent=2) + "\n")
        rlsbl_dir = proj_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({
            "targets": ["npm"], "publish_mode": "ci",
        }, indent=2) + "\n")
        make_workspace(mock_git_repo, [
            dict({"path": subdir, "name": subdir},
                 **({"dev_only": True, "releasable": False} if dev_node else {})),
        ])
        return proj_dir

    def _scaffold(self, proj_dir):
        run_cmd("npm", [], {
            "auto-commit": False, "auto-tag": False, "skip-shared": True,
        }, ctx=create_context(proj_dir))

    def test_no_publish_workflow_for_dev_node(self, mock_git_repo, monkeypatch,
                                              capsys):
        proj_dir = self._dev_node_npm_project(mock_git_repo)
        monkeypatch.chdir(proj_dir)
        self._scaffold(proj_dir)

        assert not (proj_dir / ".github" / "workflows" / "publish.yml").exists()
        # CI is still scaffolded -- a dev node is tested, just never released.
        assert (proj_dir / ".github" / "workflows" / "ci.yml").exists()

    def test_releasable_project_still_gets_one(self, mock_git_repo, monkeypatch,
                                               capsys):
        proj_dir = self._dev_node_npm_project(mock_git_repo, dev_node=False)
        monkeypatch.chdir(proj_dir)
        self._scaffold(proj_dir)

        assert (proj_dir / ".github" / "workflows" / "publish.yml").exists()

    def test_existing_publish_workflow_is_swept(self, mock_git_repo, monkeypatch,
                                                capsys):
        """A dev node scaffolded before the fix loses its publish.yml on re-scaffold."""
        proj_dir = self._dev_node_npm_project(mock_git_repo, dev_node=False)
        monkeypatch.chdir(proj_dir)
        self._scaffold(proj_dir)
        publish = proj_dir / ".github" / "workflows" / "publish.yml"
        assert publish.exists()

        # The project is (re)declared a dev node.
        make_workspace(mock_git_repo, [
            {"path": "conformance", "name": "conformance", "dev_only": True, "releasable": False},
        ])
        # As a releasable member its merge bases lived at the releasable; a
        # member that releases nothing keeps its own, so the scaffold asks for
        # the directory before it heals the bases (the refusal's own remedy).
        (proj_dir / ".rlsbl" / "bases").mkdir()
        capsys.readouterr()
        self._scaffold(proj_dir)
        assert not publish.exists()
        # The removal says why: a bare "orphan" reads as a scaffold bug, and
        # was once undone by hand for exactly that reason.
        out = capsys.readouterr().out
        assert (
            ".github/workflows/publish.yml" in out
            and "removed (this member releases nothing (releasable = false), "
                "so it gets no publish workflow)" in out
        ), out
