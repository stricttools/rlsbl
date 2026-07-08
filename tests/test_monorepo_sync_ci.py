"""Tests for per-target CI file support in monorepo sync (inline router)."""

import json
import os
import stat
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo import (
    _cmd_init,
    _cmd_add,
    _cmd_sync,
    _generate_router,
    parse_ci_workflow,
)


CI_WORKFLOW = """\
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo test
"""

CI_PYPI_WORKFLOW = """\
name: CI PyPI

on:
  push:
    branches: [main]

jobs:
  test-pypi:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: uv run pytest
"""

CI_GO_WORKFLOW = """\
name: CI Go

on:
  push:
    branches: [main]

jobs:
  test-go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: go test ./...
"""


def _make_project_with_ci_files(base_path, subdir, ci_files, name=None):
    """Create a minimal npm project with specific CI workflow files.

    ci_files: dict of filename -> content, e.g. {"ci.yml": ..., "ci-pypi.yml": ...}
    """
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg_name = name or os.path.basename(subdir)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": pkg_name, "version": "0.1.0"}, f)

    wf_dir = os.path.join(proj_dir, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    for filename, content in ci_files.items():
        with open(os.path.join(wf_dir, filename), "w") as f:
            f.write(content)

    return subdir


def _router_doc(repo):
    """Parse the generated CI router in *repo*."""
    router = repo / ".github" / "workflows" / "ci-router.yml"
    assert router.exists(), "ci-router.yml not generated"
    return parse_ci_workflow(router.read_text())


class TestPerTargetCIFiles:
    """Per-target CI files (ci-pypi.yml, ci-go.yml, ...) are inlined into
    the router with per-file job prefixes; no root copies are written."""

    def test_sync_per_target_ci_files(self, mock_git_repo, capsys):
        """Sub-project with ci-pypi.yml and ci-go.yml (no ci.yml) inlines both."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW, "ci-go.yml": CI_GO_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        # Remove the scaffold-generated ci.yml so only per-target files remain
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        # No root-level copies are written
        wf_dir = mock_git_repo / ".github" / "workflows"
        assert not (wf_dir / "tooling-ci-pypi.yml").exists()
        assert not (wf_dir / "tooling-ci-go.yml").exists()
        assert not (wf_dir / "tooling-ci.yml").exists()

        # Both per-target files' jobs are inlined with per-file prefixes
        doc = _router_doc(mock_git_repo)
        assert "tooling-ci-pypi-test-pypi" in doc["jobs"]
        assert "tooling-ci-go-test-go" in doc["jobs"]

    def test_per_target_jobs_gated_on_detect(self, mock_git_repo, capsys):
        """Inlined per-target jobs are gated on the project's detect output."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        # Remove scaffold-generated ci.yml
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        doc = _router_doc(mock_git_repo)
        job = doc["jobs"]["tooling-ci-pypi-test-pypi"]
        assert job["if"] == "needs.detect.outputs.tooling == 'true'"
        assert list(job["needs"]) == ["detect"]

    def test_per_target_check_run_names(self, mock_git_repo, capsys):
        """Inlined jobs keep the reusable-workflow-era check-run naming
        ('{prefix} / {job}') so publish gate regexes keep matching."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        doc = _router_doc(mock_git_repo)
        job = doc["jobs"]["tooling-ci-pypi-test-pypi"]
        assert job["name"] == "tooling-ci-pypi / test-pypi"

    def test_router_read_only(self, mock_git_repo, capsys):
        """The generated router is written as read-only."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-go.yml": CI_GO_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        router = mock_git_repo / ".github" / "workflows" / "ci-router.yml"
        mode = stat.S_IMODE(os.stat(str(router)).st_mode)
        assert mode == 0o444

    def test_per_target_working_directory_injected(self, mock_git_repo, capsys):
        """Inlined per-target jobs get working-directory injected."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        doc = _router_doc(mock_git_repo)
        job = doc["jobs"]["tooling-ci-pypi-test-pypi"]
        assert job["defaults"]["run"]["working-directory"] == "tooling"


class TestSingleCIInline:
    """The single ci.yml pattern inlines with the {name}-ci prefix."""

    def test_single_ci_yml_inlined(self, mock_git_repo, capsys):
        """Sub-project with only ci.yml produces {name}-ci-{job} router jobs."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "core",
            {"ci.yml": CI_WORKFLOW},
        )
        _cmd_add(["core"], {}, project_root=".")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        wf_dir = mock_git_repo / ".github" / "workflows"
        assert not (wf_dir / "core-ci.yml").exists()
        doc = _router_doc(mock_git_repo)
        assert "core-ci-test" in doc["jobs"]
        assert doc["jobs"]["core-ci-test"]["name"] == "core-ci / test"

    def test_mixed_single_and_per_target(self, mock_git_repo, capsys):
        """Project with ci.yml AND ci-pypi.yml inlines jobs from both files."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "hybrid",
            {"ci.yml": CI_WORKFLOW, "ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["hybrid"], {}, project_root=".")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        doc = _router_doc(mock_git_repo)
        assert "hybrid-ci-test" in doc["jobs"]
        assert "hybrid-ci-pypi-test-pypi" in doc["jobs"]


class TestRouterMultipleCIFiles:
    """The CI router inlines jobs from multiple CI docs per project."""

    @staticmethod
    def _doc(job_key):
        return {
            "jobs": {
                job_key: {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo test"}],
                },
            },
        }

    def test_router_single_ci_doc(self):
        """Project with one _ci_docs entry produces one prefixed job."""
        projects = [
            {"name": "core", "path": "core",
             "_ci_docs": [("core-ci", self._doc("test"))]},
        ]
        content = _generate_router(projects)
        doc = parse_ci_workflow(content)
        assert "core-ci-test" in doc["jobs"]
        assert "uses" not in doc["jobs"]["core-ci-test"]

    def test_router_multiple_ci_docs(self):
        """Project with multiple _ci_docs generates jobs per CI file."""
        projects = [
            {
                "name": "tooling",
                "path": "tooling",
                "_ci_docs": [
                    ("tooling-ci-pypi", self._doc("test")),
                    ("tooling-ci-go", self._doc("test")),
                ],
            },
        ]
        content = _generate_router(projects)
        doc = parse_ci_workflow(content)
        assert "tooling-ci-pypi-test" in doc["jobs"]
        assert "tooling-ci-go-test" in doc["jobs"]
        # Both jobs check the same project output
        assert content.count("needs.detect.outputs.tooling == 'true'") == 2

    def test_router_mixed_single_and_multi(self):
        """Mix of single-CI and multi-CI projects in same router."""
        projects = [
            {"name": "core", "path": "core",
             "_ci_docs": [("core-ci", self._doc("test"))]},
            {
                "name": "tooling",
                "path": "tooling",
                "_ci_docs": [
                    ("tooling-ci-pypi", self._doc("test")),
                    ("tooling-ci-go", self._doc("test")),
                ],
            },
        ]
        content = _generate_router(projects)
        doc = parse_ci_workflow(content)
        assert "core-ci-test" in doc["jobs"]
        assert "tooling-ci-pypi-test" in doc["jobs"]
        assert "tooling-ci-go-test" in doc["jobs"]

    def test_router_rewrites_intra_workflow_needs(self):
        """Intra-workflow needs are rewritten to prefixed keys + detect."""
        ci_doc = {
            "jobs": {
                "build": {"runs-on": "ubuntu-latest", "steps": []},
                "test": {
                    "runs-on": "ubuntu-latest",
                    "needs": "build",
                    "steps": [],
                },
            },
        }
        projects = [
            {"name": "core", "path": "core", "_ci_docs": [("core-ci", ci_doc)]},
        ]
        content = _generate_router(projects)
        doc = parse_ci_workflow(content)
        assert list(doc["jobs"]["core-ci-test"]["needs"]) == ["detect", "core-ci-build"]
        assert list(doc["jobs"]["core-ci-build"]["needs"]) == ["detect"]

    def test_router_job_key_collision_errors(self):
        """Two CI files producing the same prefixed job key hard-error."""
        from rlsbl.errors import ConfigError
        projects = [
            {
                "name": "core",
                "path": "core",
                "_ci_docs": [
                    ("core-ci", self._doc("test")),
                    ("core-ci", self._doc("test")),
                ],
            },
        ]
        with pytest.raises(ConfigError, match="collision"):
            _generate_router(projects)


class TestCleanupPerTargetCI:
    """Stale root-level per-target CI copies (pre-inline era) are removed."""

    def test_cleanup_removes_legacy_per_target_ci(self, mock_git_repo, capsys):
        """Legacy {name}-ci-{target}.yml root copies are removed on sync."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "core",
            {"ci.yml": CI_WORKFLOW},
        )
        _cmd_add(["core"], {}, project_root=".")
        # Plant legacy root copies (as older rlsbl versions wrote them)
        wf_dir = mock_git_repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        legacy = [wf_dir / "tooling-ci-pypi.yml", wf_dir / "tooling-ci-go.yml"]
        for f in legacy:
            f.write_text("# legacy generated copy\non: workflow_call\njobs: {}\n")
            os.chmod(str(f), 0o444)
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        for f in legacy:
            assert not f.exists(), f"stale workflow {f.name} not removed"
        # Router untouched by cleanup
        assert (wf_dir / "ci-router.yml").exists()

    def test_sync_idempotent(self, mock_git_repo, capsys):
        """A second sync leaves the router intact and adds no stray files."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW, "ci-go.yml": CI_GO_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
            capsys.readouterr()
            _cmd_sync({}, project_root=".")

        wf_dir = mock_git_repo / ".github" / "workflows"
        doc = _router_doc(mock_git_repo)
        assert "tooling-ci-pypi-test-pypi" in doc["jobs"]
        assert "tooling-ci-go-test-go" in doc["jobs"]
        # No per-project copies appeared
        stray = [f for f in os.listdir(str(wf_dir)) if "-ci" in f and f.endswith(".yml")]
        assert stray == []
