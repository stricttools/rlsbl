"""Tests for CI router generation at scale (30 projects).

Validates that _generate_router and generate_inline_publish_router produce
correct, parseable YAML when given large workspaces.

GitHub Actions has a 256-job limit per workflow, so 30 (or even 100)
projects are well within limits. The dorny/paths-filter action has no
documented limit on filter entries.
"""

import os
import re
import textwrap
from unittest.mock import patch

from ruamel.yaml import YAML

from rlsbl.commands.monorepo import _generate_router
from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router


def _safe_load(text):
    return YAML(typ='safe').load(text)


def _parse_workflow_yaml(content):
    """Minimal parser for rlsbl-generated GitHub Actions YAML.

    Only handles the predictable structure emitted by _generate_router
    and generate_inline_publish_router: top-level keys, jobs with 2-space
    indented names, and job properties at 4-space indentation.

    Returns a dict where:
    - Top-level keys are strings (except bare 'on' which becomes True,
      matching PyYAML's behaviour for YAML 1.1 boolean interpretation).
    - 'jobs' maps to {job_name: {property: value, ...}, ...}.
    - Other top-level keys map to a lightweight nested dict parsed from
      indentation.
    """
    result = {}
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip blank lines and comments
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        # Top-level key (no leading whitespace)
        m = re.match(r"^(\S+):\s*(.*)", line)
        if not m:
            i += 1
            continue
        raw_key = m.group(1)
        inline_val = m.group(2).strip()
        # YAML 1.1: bare 'on' is boolean True
        key = True if raw_key == "on" else raw_key
        if key == "jobs":
            jobs, i = _parse_jobs_block(lines, i + 1)
            result[key] = jobs
        elif inline_val:
            result[key] = _parse_inline(inline_val)
            i += 1
        else:
            block, i = _parse_block(lines, i + 1, indent=2)
            result[key] = block
    return result


def _parse_jobs_block(lines, start):
    """Parse the jobs block, returning {name: {prop: val}} and next index."""
    jobs = {}
    i = start
    current_job = None
    while i < len(lines):
        line = lines[i]
        # Blank line: skip
        if not line.strip():
            i += 1
            continue
        # Non-indented line means we left the jobs block
        if line[0] != " ":
            break
        # Job name at 2-space indent
        m = re.match(r"^  (\S+):\s*(.*)", line)
        if m:
            current_job = m.group(1)
            inline = m.group(2).strip()
            if inline:
                jobs[current_job] = _parse_inline(inline)
            else:
                jobs[current_job] = {}
            i += 1
            continue
        # Job property at 4-space indent
        m = re.match(r"^    (\S+):\s*(.*)", line)
        if m and current_job is not None:
            prop = m.group(1)
            val = m.group(2).strip()
            if not isinstance(jobs[current_job], dict):
                jobs[current_job] = {}
            if val:
                jobs[current_job][prop] = _parse_inline(val)
            else:
                # Collect sub-block at 6-space indent
                sub, i = _parse_sub_block(lines, i + 1, indent=6)
                jobs[current_job][prop] = sub
                continue
            i += 1
            continue
        # Deeper indentation belongs to the current property; skip
        i += 1
    return jobs, i


def _parse_sub_block(lines, start, indent):
    """Parse an indented sub-block into a dict of key: value pairs."""
    result = {}
    i = start
    prefix = " " * indent
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not line.startswith(prefix) or (len(line) > len(prefix) and line[len(prefix) - 1] != " " and not line.startswith(prefix)):
            # Only break if this line has less indentation than our block
            stripped = line.lstrip()
            actual_indent = len(line) - len(stripped)
            if actual_indent < indent:
                break
        m = re.match(rf"^{prefix}(\S+):\s*(.*)", line)
        if m:
            k = m.group(1)
            v = m.group(2).strip()
            result[k] = _parse_inline(v) if v else {}
            i += 1
        else:
            i += 1
    return result, i


def _parse_block(lines, start, indent):
    """Parse a generic indented block into a dict."""
    result = {}
    i = start
    prefix = " " * indent
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not line.startswith(prefix):
            break
        m = re.match(rf"^{prefix}(\S+):\s*(.*)", line)
        if m:
            k = m.group(1)
            v = m.group(2).strip()
            if v:
                result[k] = _parse_inline(v)
                i += 1
            else:
                sub, i = _parse_block(lines, i + 1, indent + 2)
                result[k] = sub
        else:
            i += 1
    return result, i


def _parse_inline(val):
    """Parse an inline YAML value (scalars, lists in [] notation)."""
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",")]
    if val.startswith("'") and val.endswith("'"):
        return val[1:-1]
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val

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


def _make_projects(count, *, watch=False):
    """Build a list of synthetic project dicts."""
    projects = []
    for i in range(1, count + 1):
        proj = {"name": f"project-{i}", "path": f"packages/project-{i}"}
        if watch:
            proj["watch"] = [f"shared/lib-{i}/**"]
        projects.append(proj)
    return projects


def _make_projects_on_disk(root, count):
    """Build synthetic projects and create their publish workflow files on disk."""
    projects = _make_projects(count)
    for proj in projects:
        wf_dir = os.path.join(root, proj["path"], ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "publish.yml"), "w") as f:
            f.write(_SIMPLE_PUBLISH_WF)
    return projects


def _mock_tag_prefix(proj, _root):
    return f"{proj['name']}@v"


class TestCIRouterScale:
    """CI router generation with 30 projects."""

    def test_syntactic_validity(self):
        """Generated ci-router.yml parses as valid YAML."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _parse_workflow_yaml(content)
        assert isinstance(parsed, dict)

    def test_job_count(self):
        """ci-router has exactly 30 project jobs + 1 detect job = 31 total."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _parse_workflow_yaml(content)
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
        parsed = _parse_workflow_yaml(content)
        job_names = list(parsed["jobs"].keys())
        assert len(job_names) == len(set(job_names))

    def test_workflow_call_references(self):
        """Each project job calls ./.github/workflows/{name}-ci.yml."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _parse_workflow_yaml(content)
        for proj in projects:
            name = proj["name"]
            job = parsed["jobs"][name]
            assert job["uses"] == f"./.github/workflows/{name}-ci.yml"

    def test_detect_outputs(self):
        """The detect job declares an output for every project."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _parse_workflow_yaml(content)
        detect = parsed["jobs"]["detect"]
        outputs = detect["outputs"]
        for proj in projects:
            assert proj["name"] in outputs

    def test_conditional_expressions(self):
        """Each project job has the correct if-condition on detect output."""
        projects = _make_projects(PROJECT_COUNT)
        content = _generate_router(projects)
        parsed = _parse_workflow_yaml(content)
        for proj in projects:
            name = proj["name"]
            job = parsed["jobs"][name]
            assert job["if"] == f"needs.detect.outputs.{name} == 'true'"

    def test_with_watch_paths(self):
        """Router with watch paths uses multi-line filter format for all 30 projects."""
        projects = _make_projects(PROJECT_COUNT, watch=True)
        content = _generate_router(projects)
        parsed = _parse_workflow_yaml(content)
        # Still valid YAML and correct job count
        assert len(parsed["jobs"]) == PROJECT_COUNT + 1
        # Each project's path and watch glob appear in the content
        for proj in projects:
            assert f"- '{proj['path']}/**'" in content
            for w in proj["watch"]:
                assert f"- '{w}'" in content


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
        """Inline publish router has 30 project jobs + 1 no-op job."""
        root = str(tmp_path)
        projects = _make_projects_on_disk(root, PROJECT_COUNT)
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert len(parsed["jobs"]) == PROJECT_COUNT + 1  # +1 for no-op job

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
            expected = f"startsWith(github.event.release.tag_name, '{name}@v')"
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
        assert parsed["on"] == {"release": {"types": ["published"]}}
