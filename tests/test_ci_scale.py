"""Tests for CI router generation at scale (30 projects).

Validates that _generate_router and generate_inline_publish_router produce
correct, parseable YAML when given large workspaces, and that generated
routers never route via reusable-workflow calls -- GitHub rejects a
workflow file that references 20 or more reusable workflows (job-level
``uses:``), which silently breaks CI for large monorepos.

GitHub Actions has a 256-job limit per workflow, so 30 (or even 100)
projects are well within limits. The dorny/paths-filter action has no
documented limit on filter entries.
"""

import os
import textwrap
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.monorepo import (
    _generate_router,
    count_reusable_workflow_calls,
    validate_router_reusable_calls,
)
from rlsbl.commands.monorepo.sync import GITHUB_MAX_REUSABLE_CALLS
from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router
from rlsbl.errors import ConfigError


def _safe_load(text):
    return YAML(typ='safe').load(text)


PROJECT_COUNT = 30

_SIMPLE_PUBLISH_WF = textwrap.dedent("""\
    name: Publish

    on:
      release:
        types: [published]

    permissions:
      contents: read
      id-token: write

    jobs:
      publish:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v6
          - run: echo publish
""")


def _make_ci_doc():
    """Build a minimal parsed CI doc with one 'test' job."""
    return {
        "jobs": {
            "test": {
                "runs-on": "ubuntu-latest",
                "steps": [
                    {"uses": "actions/checkout@v6"},
                    {"run": "echo test"},
                ],
            },
        },
    }


def _make_projects(count, *, watch=False, ci_docs=True):
    """Build a list of synthetic project dicts."""
    projects = []
    for i in range(1, count + 1):
        proj = {"name": f"project-{i}", "path": f"packages/project-{i}"}
        if watch:
            proj["watch"] = [f"shared/lib-{i}/**"]
        if ci_docs:
            proj["_ci_docs"] = [(f"project-{i}-ci", _make_ci_doc())]
        projects.append(proj)
    return projects


def _make_projects_on_disk(root, count):
    """Build synthetic projects and create their publish workflow files on disk."""
    projects = _make_projects(count, ci_docs=False)
    for proj in projects:
        wf_dir = os.path.join(root, proj["path"], ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "publish.yml"), "w") as f:
            f.write(_SIMPLE_PUBLISH_WF)
    return projects


def _mock_tag_prefix(proj, _root, **kw):
    return f"{proj['name']}@v"


class TestCIRouterScale:
    """CI router generation with 30 projects (all jobs inlined)."""

    def test_syntactic_validity(self):
        """Generated ci-router.yml parses as valid YAML."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        assert isinstance(parsed, dict)

    def test_job_count(self):
        """ci-router has exactly 30 inlined project jobs + 1 detect job = 31 total."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        jobs = parsed["jobs"]
        assert len(jobs) == PROJECT_COUNT + 1  # 30 projects + detect
        assert "detect" in jobs

    def test_path_filter_entries(self):
        """The dorny/paths-filter filters block has 30 entries."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        for i in range(1, PROJECT_COUNT + 1):
            name = f"project-{i}"
            path = f"packages/project-{i}"
            assert f"{name}: '{path}/**'" in content

    def test_no_duplicate_job_names(self):
        """All job names in ci-router are unique."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        job_names = list(parsed["jobs"].keys())
        assert len(job_names) == len(set(job_names))

    def test_jobs_inlined_not_reusable(self):
        """No job carries a reusable-workflow 'uses:' -- all jobs are inlined."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        assert count_reusable_workflow_calls(parsed["jobs"]) == 0
        for proj in projects:
            name = proj["name"]
            job = parsed["jobs"][f"{name}-ci-test"]
            assert "uses" not in job
            assert job["name"] == f"{name}-ci / test"
            assert job["needs"] == ["detect"]

    def test_detect_outputs(self):
        """The detect job declares an output for every project."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        detect = parsed["jobs"]["detect"]
        outputs = detect["outputs"]
        for proj in projects:
            assert proj["name"] in outputs

    def test_conditional_expressions(self):
        """Each inlined project job has the correct if-condition on detect output."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        for proj in projects:
            name = proj["name"]
            job = parsed["jobs"][f"{name}-ci-test"]
            assert job["if"] == f"needs.detect.outputs.{name} == 'true'"

    def test_with_watch_paths(self):
        """Router with watch paths uses multi-line filter format for all 30 projects."""
        projects = _make_projects(PROJECT_COUNT, watch=True)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        # Still valid YAML and correct job count
        assert len(parsed["jobs"]) == PROJECT_COUNT + 1
        # Each project's path and watch glob appear in the content
        for proj in projects:
            assert f"- '{proj['path']}/**'" in content
            for w in proj["watch"]:
                assert f"- '{w}'" in content


class TestReusableCallGuard:
    """Hard-error guard: generated routers must never reach GitHub's
    20-reusable-workflow limit (job-level 'uses:' calls)."""

    @staticmethod
    def _jobs_with_uses(count):
        jobs = {"detect": {"runs-on": "ubuntu-latest", "steps": []}}
        for i in range(count):
            jobs[f"job-{i}"] = {"uses": f"./.github/workflows/wf-{i}.yml"}
        return jobs

    def test_count_only_counts_job_level_uses(self):
        """Step-level uses (actions) are not reusable-workflow calls."""
        jobs = {
            "inline": {
                "runs-on": "ubuntu-latest",
                "steps": [{"uses": "actions/checkout@v6"}],
            },
            "reusable": {"uses": "./.github/workflows/x.yml"},
        }
        assert count_reusable_workflow_calls(jobs) == 1

    def test_validate_passes_below_limit(self):
        jobs = self._jobs_with_uses(GITHUB_MAX_REUSABLE_CALLS - 1)
        validate_router_reusable_calls(jobs, "test-router.yml")  # no raise

    def test_validate_errors_at_limit(self):
        jobs = self._jobs_with_uses(GITHUB_MAX_REUSABLE_CALLS)
        with pytest.raises(ConfigError, match="reusable-workflow calls"):
            validate_router_reusable_calls(jobs, "test-router.yml")

    def test_validate_errors_above_limit(self):
        jobs = self._jobs_with_uses(51)
        with pytest.raises(ConfigError, match="51"):
            validate_router_reusable_calls(jobs, "test-router.yml")

    def test_generated_ci_router_has_zero_reusable_calls(self):
        """Even a 100-project workspace produces a router with zero calls."""
        projects = _make_projects(100)
        content = _generate_router(projects)
        parsed = _safe_load(content)
        assert count_reusable_workflow_calls(parsed["jobs"]) == 0

    def test_generated_publish_router_has_zero_reusable_calls(self, tmp_path):
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert count_reusable_workflow_calls(parsed["jobs"]) == 0


class TestPublishRouterScale:
    """Inline publish router generation with 30 projects."""

    def test_syntactic_validity(self, tmp_path):
        """Generated inline publish router parses as valid YAML."""
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert isinstance(parsed, dict)

    def test_job_count(self, tmp_path):
        """Inline publish router has 30 project jobs + no-op + shared gate."""
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert len(parsed["jobs"]) == PROJECT_COUNT + 2  # +1 no-op, +1 shared gate

    def test_no_duplicate_job_names(self, tmp_path):
        """All job names in inline publish router are unique."""
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job_names = list(parsed["jobs"].keys())
        assert len(job_names) == len(set(job_names))

    def test_tag_matching_conditions(self, tmp_path):
        """Each inlined job has the correct startsWith tag condition."""
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        for proj in projects:
            name = proj["name"]
            job_key = f"{name}-publish"
            assert job_key in parsed["jobs"], f"Missing job {job_key}"
            job = parsed["jobs"][job_key]
            expected = f"startsWith(github.ref_name, '{name}@v')"
            assert job["if"] == expected

    def test_all_jobs_have_permissions(self, tmp_path):
        """Each inlined job has its own permissions block."""
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        for job_name, job in parsed["jobs"].items():
            assert "permissions" in job, f"Job {job_name} missing permissions"

    def test_release_trigger(self, tmp_path):
        """Publish router triggers on release published events."""
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert parsed["on"] == {"release": {"types": ["published"]}, "workflow_dispatch": None}
