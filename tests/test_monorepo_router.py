"""Tests for the monorepo inline publish router.

The router inlines each sub-project's publish jobs directly into a single
``publish.yml``.  Each job gets an ``if: startsWith(...)`` condition, its
own ``permissions:`` block (resolved from workflow-level permissions), and
a ``defaults.run.working-directory`` pointing to the sub-project.

These tests pin the generated YAML contract so the contract cannot regress
silently.
"""

import os
import textwrap
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router


def _safe_load(text):
    return YAML(typ='safe').load(text)


# ---------------------------------------------------------------------------
# Workflow fixtures
# ---------------------------------------------------------------------------

PYPI_PUBLISH_WF = textwrap.dedent("""\
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
        environment:
          name: pypi
          url: https://pypi.org/p/mypkg
        steps:
          - uses: actions/checkout@v6
          - uses: actions/setup-python@v5
            with:
              python-version-file: .python-version
          - run: uv build
          - uses: pypa/gh-action-pypi-publish@release/v1
""")

NPM_PUBLISH_WF = textwrap.dedent("""\
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
          - uses: actions/setup-node@v4
            with:
              node-version-file: .nvmrc
              registry-url: https://registry.npmjs.org
          - run: npm publish
            env:
              NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
""")

GO_PUBLISH_WF = textwrap.dedent("""\
    name: Publish

    on:
      release:
        types: [published]

    permissions:
      contents: write

    jobs:
      goreleaser:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v6
          - uses: actions/setup-go@v5
            with:
              go-version-file: go.mod
          - run: goreleaser release
""")


def _setup_project(root, path, workflow_content):
    """Create a project directory with a publish workflow."""
    wf_dir = os.path.join(root, path, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    with open(os.path.join(wf_dir, "publish.yml"), "w") as f:
        f.write(workflow_content)


def _mock_tag_prefix(proj, _root, **kw):
    return f"{proj['name']}@v"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPublishRouterTopLevel:
    """Router triggers + structure remain stable."""

    def test_has_release_trigger(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "pkg", PYPI_PUBLISH_WF)
        projects = [{"name": "pkg", "path": "pkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert parsed["on"] == {"release": {"types": ["published"]}, "workflow_dispatch": None}

    def test_has_router_name(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "pkg", PYPI_PUBLISH_WF)
        projects = [{"name": "pkg", "path": "pkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert parsed["name"] == "Publish Router"

    def test_no_top_level_permissions(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "pkg", PYPI_PUBLISH_WF)
        projects = [{"name": "pkg", "path": "pkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert "permissions" not in parsed


class TestPublishRouterPyPIOidc:
    """PyPI uses OIDC -- workflow permissions are pushed down to jobs."""

    def test_pypi_router_includes_id_token_permission(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "python", PYPI_PUBLISH_WF)
        projects = [{"name": "mypkg", "path": "python"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job = parsed["jobs"]["mypkg-publish"]
        assert job["permissions"]["id-token"] == "write"
        assert job["permissions"]["contents"] == "read"


class TestPublishRouterNpmSecrets:
    """npm publish needs NPM_TOKEN -- secrets are embedded in the inlined steps."""

    def test_npm_router_includes_id_token_for_provenance(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "node", NPM_PUBLISH_WF)
        projects = [{"name": "mylib", "path": "node"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job = parsed["jobs"]["mylib-publish"]
        assert job["permissions"]["id-token"] == "write"

    def test_npm_secrets_in_inlined_steps(self, tmp_path):
        """NPM_TOKEN reference is preserved in inlined steps."""
        root = str(tmp_path)
        _setup_project(root, "node", NPM_PUBLISH_WF)
        projects = [{"name": "mylib", "path": "node"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        assert "${{ secrets.NPM_TOKEN }}" in content


class TestPublishRouterGoContents:
    """Go publish (goreleaser) needs contents: write to push assets."""

    def test_go_router_includes_contents_write(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "go", GO_PUBLISH_WF)
        projects = [{"name": "mymod", "path": "go"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job = parsed["jobs"]["mymod-goreleaser"]
        assert job["permissions"]["contents"] == "write"


class TestRouterJobStructure:
    """Each project gets prefixed inlined jobs with if/permissions/working-directory."""

    def test_job_has_tag_prefix_condition(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "node", NPM_PUBLISH_WF)
        projects = [{"name": "mylib", "path": "node"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job = parsed["jobs"]["mylib-publish"]
        assert job["if"] == "startsWith(github.ref_name, 'mylib@v')"

    def test_job_has_working_directory(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "node", NPM_PUBLISH_WF)
        projects = [{"name": "mylib", "path": "node"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job = parsed["jobs"]["mylib-publish"]
        assert job["defaults"]["run"]["working-directory"] == "node"

    def test_pypi_publish_has_packages_dir(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "python", PYPI_PUBLISH_WF)
        projects = [{"name": "mypkg", "path": "python"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job = parsed["jobs"]["mypkg-publish"]
        pypi_step = next(
            s for s in job["steps"]
            if "pypa/gh-action-pypi-publish" in s.get("uses", "")
        )
        assert pypi_step["with"]["packages-dir"] == "python/dist/"

    def test_permissions_block_on_each_job(self, tmp_path):
        """Each inlined job has its own permissions (no top-level permissions)."""
        root = str(tmp_path)
        _setup_project(root, "node", NPM_PUBLISH_WF)
        _setup_project(root, "python", PYPI_PUBLISH_WF)
        projects = [
            {"name": "mylib", "path": "node"},
            {"name": "mypkg", "path": "python"},
        ]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        assert "permissions" not in parsed
        for job_name, job in parsed["jobs"].items():
            assert "permissions" in job, f"Job {job_name} is missing permissions"

    def test_multi_project_jobs_are_prefixed(self, tmp_path):
        root = str(tmp_path)
        _setup_project(root, "python", PYPI_PUBLISH_WF)
        _setup_project(root, "node", NPM_PUBLISH_WF)
        projects = [
            {"name": "mypkg", "path": "python"},
            {"name": "mylib", "path": "node"},
        ]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=_mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        parsed = _safe_load(content)
        job_names = list(parsed["jobs"].keys())
        assert "mypkg-publish" in job_names
        assert "mylib-publish" in job_names
