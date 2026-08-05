"""Post-merge YAML rewrites must not manufacture conflicts, nor crash on one.

Two defects, one shape. Scaffold used to run its CI-workflow YAML rewrites
(service-container injection, subdirectory working-directory injection) AFTER
``plan_mappings`` had already computed the three-way merge:

- the rewritten, re-emitted YAML was stored as the merge BASE while "theirs"
  stayed the raw template text, so base and theirs differed by the whole
  rewrite on every run -- any local edit then collided with that phantom diff
  and conflicted (half B);
- the rewrite then parsed the conflict-marked merge output as YAML and died
  with a bare ``while scanning a simple key`` ScannerError naming no file
  (half A).

The fix applies the rewrites to "theirs" BEFORE the merge (so base and theirs
come out of the same pipeline), and makes conflict-marked text a reported,
file-named error rather than a YAML crash.
"""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import run_cmd, plan_mappings, _save_base
from rlsbl.context import create_context
from rlsbl.errors import ConfigError


SERVICES_CONFIG = {
    "postgres": {
        "targets": ["go"],
        "image": "postgres:17",
        "env": {"POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test"},
        "ports": ["5432:5432"],
        "health": {
            "cmd": "pg_isready -U test",
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
        },
    }
}


def _go_project(root, *, services):
    """Create a minimal single-target Go project with an rlsbl config."""
    (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n")
    (root / "main.go").write_text(
        "package main\n\nfunc main() {}\n"
    )
    (root / "VERSION").write_text("0.1.0\n")
    cfg = {
        "targets": ["go"],
        "publish_mode": "ci",
        "pipelines": {
            "go": {
                "type": "go",
                "local": False,
                "target": "go",
                "artifact": "binary",
            }
        },
    }
    if services:
        cfg["services"] = SERVICES_CONFIG
        cfg["test_env"] = {"DEMO_DB": "postgres://test:test@localhost:5432/x"}
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def _scaffold(root):
    with patch("sys.stdout", new_callable=StringIO):
        run_cmd(
            "go", [],
            {"auto-commit": False, "auto-tag": False, "skip-shared": True},
            ctx=create_context(root),
        )


class TestServicesRewriteSymmetry:
    """Half B: a local edit to a services-injected CI workflow re-scaffolds cleanly."""

    def test_local_edit_survives_rescaffold(self, mock_git_repo):
        _go_project(mock_git_repo, services=True)
        _scaffold(mock_git_repo)

        ci = mock_git_repo / ".github" / "workflows" / "ci.yml"
        assert "services:" in ci.read_text()

        # A hand edit on the line immediately above the injected block --
        # exactly what a maintainer pinning a runner image would do, and the
        # position that collides with the phantom base->theirs diff.
        edited = ci.read_text().replace(
            "runs-on: ubuntu-latest", "runs-on: ubuntu-24.04  # pinned"
        )
        assert edited != ci.read_text()
        ci.write_text(edited)

        # Re-scaffold: must not crash, must not conflict, must keep the edit.
        _scaffold(mock_git_repo)

        after = ci.read_text()
        assert "<<<<<<<" not in after, "re-scaffold produced merge conflict markers"
        assert "# pinned" in after, "the local edit was lost"
        assert "services:" in after, "the injected services block was lost"

    def test_stored_base_matches_generated_output(self, mock_git_repo):
        """The stored merge base must equal what scaffold now generates.

        This is the invariant that makes the merge symmetric: base and theirs
        are produced by the same pipeline, so an untouched file re-scaffolds to
        a no-op instead of a phantom diff.
        """
        _go_project(mock_git_repo, services=True)
        _scaffold(mock_git_repo)

        ci = mock_git_repo / ".github" / "workflows" / "ci.yml"
        base = mock_git_repo / ".rlsbl" / "bases" / ".github" / "workflows" / "ci.yml"
        assert base.exists()
        assert base.read_text() == ci.read_text()

    def test_rescaffold_untouched_is_noop(self, mock_git_repo):
        _go_project(mock_git_repo, services=True)
        _scaffold(mock_git_repo)
        ci = mock_git_repo / ".github" / "workflows" / "ci.yml"
        first = ci.read_text()

        _scaffold(mock_git_repo)
        assert ci.read_text() == first


class TestConflictIsReportedNotCrashed:
    """Half A: conflict-marked YAML never reaches a parser as a bare crash."""

    CONFLICTED = (
        "name: CI\n"
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "<<<<<<< .ours\n"
        "      - run: echo mine\n"
        "=======\n"
        "      - run: echo theirs\n"
        ">>>>>>> .theirs\n"
    )

    def test_parse_ci_workflow_reports_conflict(self):
        from rlsbl.ci_yaml import parse_ci_workflow

        with pytest.raises(ConfigError) as exc:
            parse_ci_workflow(
                self.CONFLICTED, source=".github/workflows/ci.yml"
            )
        msg = str(exc.value)
        assert ".github/workflows/ci.yml" in msg
        # The conflicting region is located, not just "somewhere in the file".
        assert "8" in msg and "12" in msg

    def test_parse_ci_workflow_names_source_on_yaml_error(self):
        from rlsbl.ci_yaml import parse_ci_workflow

        with pytest.raises(ConfigError) as exc:
            parse_ci_workflow(
                "jobs:\n  test:\n   - a\n  - b\n", source="broken.yml"
            )
        assert "broken.yml" in str(exc.value)

    def test_plan_warning_names_file_and_regions(self, mock_git_repo, tmp_path):
        """A genuine three-way conflict is reported with file + line regions."""
        template_dir = tmp_path / "tpl"
        template_dir.mkdir()
        (template_dir / "thing.tpl").write_text(
            "alpha\nTHEIRS-LINE\nomega\n"
        )
        target = "thing.txt"
        (mock_git_repo / target).write_text("alpha\nOURS-LINE\nomega\n")
        _save_base(target, "alpha\nBASE-LINE\nomega\n")

        plans = plan_mappings(str(template_dir), [
            {"template": "thing.tpl", "target": target},
        ], {})

        plan = plans[0]
        assert plan["status"].startswith("CONFLICTS")
        warning = plan["warning"]
        assert target in warning
        assert "line" in warning.lower()
        # The conflicting region is the middle line.
        assert "2" in warning


def _subdir_pypi_project(root):
    """npm at the root + a pypi target living in ``py/`` (subdirectory target)."""
    (root / "package.json").write_text(json.dumps({
        "name": "demo", "version": "0.1.0", "bin": {"demo": "./bin/cli.js"},
    }, indent=2) + "\n")
    py = root / "py"
    py.mkdir(exist_ok=True)
    (py / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
    )
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps({
        "targets": ["npm", {"name": "pypi", "path": "py"}],
        "publish_mode": "ci",
    }, indent=2) + "\n")


def _scaffold_multi(root):
    from rlsbl.commands.init_cmd import run_cmd_multi

    with patch("sys.stdout", new_callable=StringIO):
        run_cmd_multi(
            ["npm", "pypi"], [],
            {"auto-commit": False, "auto-tag": False, "skip-shared": True},
            ctx=create_context(root),
        )


class TestWorkingDirectoryRewriteSymmetry:
    """Half B, second site: the subdirectory working-directory injection."""

    def test_stored_base_matches_generated_output(self, mock_git_repo):
        _subdir_pypi_project(mock_git_repo)
        _scaffold_multi(mock_git_repo)

        ci = mock_git_repo / ".github" / "workflows" / "ci-pypi.yml"
        assert "working-directory: ./py" in ci.read_text()
        base = (
            mock_git_repo / ".rlsbl" / "bases"
            / ".github" / "workflows" / "ci-pypi.yml"
        )
        assert base.exists()
        assert base.read_text() == ci.read_text()

    def test_local_edit_survives_rescaffold(self, mock_git_repo):
        _subdir_pypi_project(mock_git_repo)
        _scaffold_multi(mock_git_repo)

        ci = mock_git_repo / ".github" / "workflows" / "ci-pypi.yml"
        # An edit inside the steps block -- the region the emitter reindents,
        # and therefore the region the phantom base->theirs diff overlaps.
        edited = ci.read_text().replace(
            "- run: uv sync --locked",
            "- run: uv sync --locked --all-extras  # pinned",
            1,
        )
        assert edited != ci.read_text()
        ci.write_text(edited)

        _scaffold_multi(mock_git_repo)

        after = ci.read_text()
        assert "<<<<<<<" not in after, "re-scaffold produced merge conflict markers"
        assert "# pinned" in after, "the local edit was lost"
        assert "working-directory: ./py" in after
