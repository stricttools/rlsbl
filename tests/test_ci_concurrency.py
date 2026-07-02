"""Tests for per-SHA CI concurrency groups.

Every shipped CI template and the generated monorepo CI router must carry a
workflow-level ``concurrency`` block keyed on the commit SHA with
``cancel-in-progress: true``. Per-SHA keying means cancellation only dedupes
re-runs of the SAME commit -- it can never cancel an earlier release's CI run
during back-to-back batch pushes (the publish gate polls CI conclusions for a
specific SHA; a cross-SHA cancellation would poison it with ``cancelled``
conclusions).

The group is keyed on ``github.workflow_ref`` (unique per workflow file), NOT
``github.workflow`` (the workflow *name*): multi-target scaffolds generate
sibling workflows (ci-pypi.yml, ci-go.yml) that all share ``name: CI``, so a
name-keyed group would make siblings cancel each other on the same push.

Monorepo sync rewrites project CI workflows to ``workflow_call:`` (called by
the router). GitHub evaluates a called workflow's top-level concurrency in
the CALLER's github context, so all sibling called workflows would share one
group and cancel each other. Sync must therefore strip the concurrency block
from called workflows -- the router's own per-SHA concurrency covers the
whole run.
"""

import glob
import json
import os
import subprocess
from io import StringIO
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo import (
    _cmd_init,
    _cmd_sync,
    _generate_router,
    _strip_concurrency,
    emit_ci_workflow,
    parse_ci_workflow,
)
from rlsbl.workspace import save_workspace

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)

EXPECTED_GROUP = "${{ github.workflow_ref }}-${{ github.sha }}"


def _ci_templates():
    """Return sorted paths of all shipped CI templates."""
    return sorted(glob.glob(os.path.join(TEMPLATES_ROOT, "*", "ci*.yml.tpl")))


class TestTemplatesHaveConcurrency:
    """Every ci*.yml.tpl ships a workflow-level per-SHA concurrency block."""

    def test_all_ci_templates_enumerated(self):
        """Sanity guard: the glob finds the known CI templates."""
        rels = {
            os.path.relpath(p, TEMPLATES_ROOT).replace(os.sep, "/")
            for p in _ci_templates()
        }
        expected = {
            "cargo/ci.yml.tpl",
            "deno/ci.yml.tpl",
            "docker/ci.yml.tpl",
            "go/ci.yml.tpl",
            "hex/ci.yml.tpl",
            "maven/ci.yml.tpl",
            "npm/ci.yml.tpl",
            "npm/ci-pnpm.yml.tpl",
            "npm/ci-yarn.yml.tpl",
            "pypi/ci.yml.tpl",
            "spec/ci.yml.tpl",
            "swift/ci.yml.tpl",
            "swift-apple/ci.yml.tpl",
            "zig/ci.yml.tpl",
        }
        # New CI templates are allowed (they are covered by the tests below);
        # missing ones mean the enumeration or a template was deleted.
        assert expected <= rels

    @pytest.mark.parametrize(
        "tpl_path",
        _ci_templates(),
        ids=lambda p: os.path.relpath(p, TEMPLATES_ROOT).replace(os.sep, "/"),
    )
    def test_template_has_per_sha_concurrency(self, tpl_path):
        """Template contains a top-level per-SHA concurrency block after on:."""
        with open(tpl_path, encoding="utf-8") as f:
            content = f.read()

        assert "\nconcurrency:\n" in content, "missing workflow-level concurrency block"
        assert f"group: {EXPECTED_GROUP}" in content
        assert "cancel-in-progress: true" in content

        # Keyed per workflow FILE, not per workflow NAME: sibling per-target
        # workflows all share `name: CI`, so github.workflow would collide.
        assert "${{ github.workflow }}" not in content

        # Placement: workflow top level, after on:, before jobs:.
        on_idx = content.index("\non:")
        conc_idx = content.index("\nconcurrency:")
        jobs_idx = content.index("\njobs:")
        assert on_idx < conc_idx < jobs_idx


class TestRouterConcurrency:
    """The generated monorepo CI router carries the per-SHA concurrency block."""

    def test_router_has_per_sha_concurrency(self):
        projects = [{"name": "core", "path": "core"}]
        content = _generate_router(projects)
        doc = parse_ci_workflow(content)
        assert "concurrency" in doc
        assert doc["concurrency"]["group"] == EXPECTED_GROUP
        assert doc["concurrency"]["cancel-in-progress"] is True

    def test_router_concurrency_before_jobs(self):
        """Concurrency sits at workflow top level, not inside jobs."""
        projects = [{"name": "core", "path": "core"}]
        content = _generate_router(projects)
        conc_idx = content.index("\nconcurrency:")
        jobs_idx = content.index("\njobs:")
        assert conc_idx < jobs_idx


CI_WITH_CONCURRENCY = """\
name: CI

on:
  push:
    branches: [main]

concurrency:
  group: ${{ github.workflow_ref }}-${{ github.sha }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo test
"""

CI_WITHOUT_CONCURRENCY = """\
name: CI

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo test
"""


class TestStripConcurrency:
    """Unit tests for the _strip_concurrency sync transform."""

    def test_removes_concurrency_block(self):
        doc = parse_ci_workflow(CI_WITH_CONCURRENCY)
        _strip_concurrency(doc)
        assert "concurrency" not in doc
        result = emit_ci_workflow(doc)
        assert "concurrency" not in result
        assert "cancel-in-progress" not in result

    def test_noop_when_absent(self):
        doc = parse_ci_workflow(CI_WITHOUT_CONCURRENCY)
        _strip_concurrency(doc)
        result = emit_ci_workflow(doc)
        assert "concurrency" not in result
        assert "jobs:" in result

    def test_job_level_concurrency_untouched(self):
        """Only the workflow-level block is stripped, not job-level ones."""
        doc = parse_ci_workflow(CI_WITH_CONCURRENCY)
        doc["jobs"]["test"]["concurrency"] = {"group": "job-scope"}
        _strip_concurrency(doc)
        assert "concurrency" not in doc
        assert doc["jobs"]["test"]["concurrency"] == {"group": "job-scope"}


class TestScaffoldedConcurrency:
    """Rendered scaffold output is valid YAML with the group intact."""

    def test_rendered_ci_has_concurrency_and_valid_yaml(self, mock_git_repo):
        """pypi+go scaffold: both rendered CI workflows keep the ${{ }} group."""
        from rlsbl.commands.init_cmd import run_cmd_multi
        from rlsbl.context import ProjectContext
        from pathlib import Path

        root = mock_git_repo
        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "conc-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        (root / "go.mod").write_text(
            "module github.com/test/conc-test\n\ngo 1.23\n"
        )

        ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={})
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["pypi", "go"], [], {}, ctx=ctx)

        for fname in ("ci-pypi.yml", "ci-go.yml"):
            path = os.path.join(".github", "workflows", fname)
            assert os.path.exists(path), f"{fname} not scaffolded"
            with open(path, encoding="utf-8") as f:
                content = f.read()

            # The template engine must not eat the ${{ ... }} expressions.
            assert f"group: {EXPECTED_GROUP}" in content, fname

            # Parses as valid YAML with the block at workflow top level.
            doc = parse_ci_workflow(content)
            assert doc is not None, f"{fname} is not a valid workflow"
            assert doc["concurrency"]["group"] == EXPECTED_GROUP
            assert doc["concurrency"]["cancel-in-progress"] is True


class TestSyncStripsConcurrency:
    """Full monorepo sync: called workflows lose concurrency, router keeps it."""

    def test_sync_strips_called_workflow_concurrency(self, mock_git_repo, capsys):
        proj_dir = os.path.join(str(mock_git_repo), "mypylib")
        os.makedirs(proj_dir, exist_ok=True)
        with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "mypylib"\nversion = "0.1.0"\n')

        wf_dir = os.path.join(proj_dir, ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "ci.yml"), "w") as f:
            f.write(CI_WITH_CONCURRENCY)

        _cmd_init({}, project_root=".")
        save_workspace(".", [{"path": "mypylib", "name": "mypylib"}])
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo),
            check=True,
        )

        _cmd_sync({}, project_root=".")

        # Called workflow: concurrency stripped (would be evaluated in the
        # router's context and collide across sibling projects).
        dest = mock_git_repo / ".github" / "workflows" / "mypylib-ci.yml"
        content = dest.read_text()
        assert "concurrency" not in content
        assert "workflow_call" in content

        # Router: per-SHA concurrency present.
        router = mock_git_repo / ".github" / "workflows" / "ci-router.yml"
        router_content = router.read_text()
        doc = parse_ci_workflow(router_content)
        assert doc["concurrency"]["group"] == EXPECTED_GROUP
        assert doc["concurrency"]["cancel-in-progress"] is True
