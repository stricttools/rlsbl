"""Tests that workflow_dispatch trigger is present in all workflow templates and generators."""

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import _generate_merged_publish
from rlsbl.commands.monorepo import _generate_router
from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "rlsbl" / "templates"


def _read_template(relpath):
    """Read a template file and return its content."""
    path = TEMPLATES_DIR / relpath
    return path.read_text(encoding="utf-8")


class TestTemplateWorkflowDispatch:
    """All scaffold-generated workflow templates include workflow_dispatch."""

    def test_ci_template_has_workflow_dispatch(self):
        """Representative CI template (pypi/ci.yml.tpl) has workflow_dispatch."""
        content = _read_template("pypi/ci.yml.tpl")
        assert "workflow_dispatch:" in content

    def test_publish_template_has_workflow_dispatch(self):
        """Representative Publish template (npm/publish.yml.tpl) has workflow_dispatch."""
        content = _read_template("npm/publish.yml.tpl")
        assert "workflow_dispatch:" in content

    def test_deploy_template_has_workflow_dispatch(self):
        """Deploy template has workflow_dispatch."""
        content = _read_template("shared/.github/workflows/deploy.yml.tpl")
        assert "workflow_dispatch:" in content

    def test_all_workflow_templates_have_workflow_dispatch(self):
        """Every .yml.tpl file with an on: block includes workflow_dispatch."""
        missing = []
        for tpl_path in sorted(TEMPLATES_DIR.rglob("*.yml.tpl")):
            content = tpl_path.read_text(encoding="utf-8")
            if "\non:" in content or content.startswith("on:"):
                if "workflow_dispatch:" not in content:
                    missing.append(str(tpl_path.relative_to(TEMPLATES_DIR)))
        assert missing == [], f"Templates missing workflow_dispatch: {missing}"


class TestMonorepoRouterWorkflowDispatch:
    """Monorepo router generators include workflow_dispatch."""

    def test_ci_router_has_workflow_dispatch(self):
        """_generate_router output includes workflow_dispatch."""
        projects = [
            {"name": "project-a", "path": "packages/project-a"},
        ]
        content = _generate_router(projects)
        assert "workflow_dispatch:" in content

    def test_publish_router_has_workflow_dispatch(self, tmp_path):
        """generate_inline_publish_router output includes workflow_dispatch."""
        root = str(tmp_path)
        projects = [
            {"name": "project-a", "path": "packages/project-a"},
        ]
        # Create the publish workflow file on disk
        wf_dir = os.path.join(root, "packages/project-a/.github/workflows")
        os.makedirs(wf_dir, exist_ok=True)
        publish_content = textwrap.dedent("""\
            name: Publish

            on:
              release:
                types: [published]
              workflow_dispatch:

            jobs:
              publish:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v6
                  - run: echo publish
        """)
        with open(os.path.join(wf_dir, "publish.yml"), "w") as f:
            f.write(publish_content)

        def mock_tag_prefix(proj, _root, **kw):
            return f"{proj['name']}@v"

        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)
        assert "workflow_dispatch:" in content

    def test_publish_router_yaml_parses_workflow_dispatch(self, tmp_path):
        """workflow_dispatch appears as a top-level trigger key in parsed YAML."""
        root = str(tmp_path)
        projects = [
            {"name": "project-a", "path": "packages/project-a"},
        ]
        wf_dir = os.path.join(root, "packages/project-a/.github/workflows")
        os.makedirs(wf_dir, exist_ok=True)
        publish_content = textwrap.dedent("""\
            name: Publish

            on:
              release:
                types: [published]
              workflow_dispatch:

            jobs:
              publish:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v6
                  - run: echo publish
        """)
        with open(os.path.join(wf_dir, "publish.yml"), "w") as f:
            f.write(publish_content)

        def mock_tag_prefix(proj, _root, **kw):
            return f"{proj['name']}@v"

        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=mock_tag_prefix,
        ):
            content = generate_inline_publish_router(projects, root)

        # Strip the comment header before parsing
        yaml_content = "\n".join(
            line for line in content.split("\n")
            if not line.startswith("#")
        )
        yaml = YAML(typ="safe")
        parsed = yaml.load(yaml_content)
        on_block = parsed["on"]
        assert "workflow_dispatch" in on_block


class TestMultiTargetMergeWorkflowDispatch:
    """Multi-target merged publish workflow includes workflow_dispatch."""

    TEMPLATE_VARS = {
        "repoName": "user/repo",
        "name": "test",
        "version": "1.0.0",
        "npm.registryUrl": "https://registry.npmjs.org",
        "npm.name": "test",
        "npm.version": "1.0.0",
        "npm.repoName": "user/repo",
        "npm.binCommand": "test",
        "npm.author": "",
        "npm.packageManager": "npm",
        "pypi.name": "test",
        "pypi.version": "1.0.0",
        "pypi.repoName": "user/repo",
        "pypi.minRequiredPython": "3.11",
        "cargo.name": "test",
        "cargo.version": "1.0.0",
        "cargo.repoName": "user/repo",
    }

    def test_merged_publish_has_workflow_dispatch(self):
        """_generate_merged_publish output includes workflow_dispatch."""
        result = _generate_merged_publish(["npm", "pypi"], self.TEMPLATE_VARS)
        assert "workflow_dispatch:" in result

    def test_merged_publish_yaml_parses_workflow_dispatch(self):
        """workflow_dispatch appears as a top-level trigger key in parsed YAML."""
        result = _generate_merged_publish(["npm", "pypi"], self.TEMPLATE_VARS)
        yaml = YAML(typ="safe")
        parsed = yaml.load(result)
        on_block = parsed["on"]
        assert "workflow_dispatch" in on_block

    def test_merged_publish_three_targets_has_workflow_dispatch(self):
        """Three-target merge also includes workflow_dispatch."""
        result = _generate_merged_publish(
            ["npm", "pypi", "cargo"], self.TEMPLATE_VARS
        )
        assert "workflow_dispatch:" in result
        yaml = YAML(typ="safe")
        parsed = yaml.load(result)
        assert "workflow_dispatch" in parsed["on"]
