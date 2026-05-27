"""Tests for run_cmd_multi: dual-registry scaffold with merged publish workflow."""

import json
import os
from io import StringIO
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import (
    run_cmd_multi,
    _generate_merged_publish,
    _parse_on_triggers,
    _merge_on_triggers,
)


@pytest.fixture
def dual_registry_project(mock_git_repo):
    """Set up a project with both package.json and pyproject.toml."""
    root = mock_git_repo

    # package.json with name, version, and bin field
    pkg = {
        "name": "my-dual-pkg",
        "version": "0.2.0",
        "bin": {"my-dual-pkg": "./bin/cli.js"},
    }
    (root / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")

    # pyproject.toml with name and version
    pyproject = (
        "[project]\n"
        'name = "my-dual-pkg"\n'
        'version = "0.2.0"\n'
    )
    (root / "pyproject.toml").write_text(pyproject)

    return root


class TestRunCmdMulti:
    """Integration tests for run_cmd_multi dual-registry scaffold."""

    def test_merged_publish_workflow_created(self, dual_registry_project):
        """Merged publish.yml is generated containing both npm and pypi jobs."""
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        publish_path = os.path.join(".github", "workflows", "publish.yml")
        assert os.path.exists(publish_path)

        with open(publish_path) as f:
            content = f.read()

        # Both registry jobs must be present
        assert "npm" in content
        assert "pypi" in content
        # Verify it's actually a publish workflow
        assert "Publish" in content
        # Verify npm-specific steps
        assert "npm publish" in content
        assert "NPM_TOKEN" in content
        # Verify pypi-specific steps
        assert "pypi-publish" in content or "uv build" in content

    def test_ci_workflow_created(self, dual_registry_project):
        """CI workflow from primary registry (npm) is generated."""
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        ci_path = os.path.join(".github", "workflows", "ci.yml")
        assert os.path.exists(ci_path)

        with open(ci_path) as f:
            content = f.read()

        assert "CI" in content
        assert "npm test" in content

    def test_shared_templates_processed_once(self, dual_registry_project):
        """Shared templates (CHANGELOG.md, .gitignore) are written once, not duplicated."""
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        # Shared files exist
        assert os.path.exists("CHANGELOG.md")
        assert os.path.exists(".gitignore")

        # CHANGELOG should contain the version from package.json
        with open("CHANGELOG.md") as f:
            changelog = f.read()
        assert "0.2.0" in changelog

        # .gitignore should not be empty
        with open(".gitignore") as f:
            gitignore = f.read()
        assert len(gitignore.strip()) > 0

    def test_primary_publish_not_written(self, dual_registry_project):
        """The single-registry publish template from npm should NOT be written separately.

        Only the merged publish.yml should exist -- not a duplicate from the
        npm template mappings.
        """
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        publish_path = os.path.join(".github", "workflows", "publish.yml")
        with open(publish_path) as f:
            content = f.read()

        # The merged template has BOTH npm and pypi jobs;
        # the single npm template would only have npm.
        assert "pypi" in content

    def test_template_variables_replaced(self, dual_registry_project):
        """Template variables like {{name}} and {{version}} are replaced."""
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        # Check CHANGELOG for variable substitution
        with open("CHANGELOG.md") as f:
            content = f.read()
        assert "{{" not in content

    def test_rlsbl_version_marker_written(self, dual_registry_project):
        """The .rlsbl/version marker is written after scaffolding."""
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        marker = os.path.join(".rlsbl", "version")
        assert os.path.exists(marker)

        from rlsbl import __version__
        with open(marker) as f:
            assert f.read().strip() == __version__

    def test_hashes_saved(self, dual_registry_project):
        """File hashes are persisted after multi-registry scaffold."""
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        from rlsbl.commands.init_cmd import HASHES_FILE, load_hashes
        assert os.path.exists(HASHES_FILE)
        hashes = load_hashes()
        assert len(hashes) > 0

    def test_force_flag_overwrites(self, dual_registry_project):
        """Running with --force overwrites existing managed files."""
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {})

        # Modify CI file
        ci_path = os.path.join(".github", "workflows", "ci.yml")
        with open(ci_path, "w") as f:
            f.write("# user modified\n")

        # Re-scaffold with force
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["npm", "pypi"], [], {"force": True})

        with open(ci_path) as f:
            content = f.read()
        # Should be overwritten back to template content
        assert "# user modified" not in content
        assert "CI" in content


class TestPypiPrimaryNpmSecondary:
    """Integration tests for pypi+npm scaffold with pypi as primary."""

    @pytest.fixture
    def pypi_npm_project(self, mock_git_repo):
        """Set up a project with pyproject.toml (primary) and package.json (secondary)."""
        root = mock_git_repo

        pyproject = (
            "[project]\n"
            'name = "my-dual-pkg"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
        )
        (root / "pyproject.toml").write_text(pyproject)

        pkg = {
            "name": "my-dual-pkg",
            "version": "0.2.0",
            "bin": {"my-dual-pkg": "./bin/cli.js"},
        }
        (root / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")

        return root

    def test_npm_registry_url_resolved_when_secondary(self, pypi_npm_project):
        """{{registryUrl}} in npm publish template resolves when npm is secondary.

        Regression: when pypi was primary and npm secondary, {{registryUrl}}
        was left unresolved because only pypi vars were un-namespaced.
        """
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["pypi", "npm"], [], {})

        publish_path = os.path.join(".github", "workflows", "publish.yml")
        assert os.path.exists(publish_path)

        with open(publish_path) as f:
            content = f.read()

        # {{registryUrl}} must not remain as an unresolved placeholder
        assert "{{registryUrl}}" not in content
        # The actual registry URL should be present
        assert "registry.npmjs.org" in content
        # Both jobs must be present
        assert "\n  pypi:" in content
        assert "\n  npm:" in content


class TestMergedPublishCombinations:
    """Unit tests for _generate_merged_publish with diverse target combinations."""

    TEMPLATE_VARS = {"repoName": "user/repo", "name": "test", "version": "1.0.0"}

    def test_npm_cargo_merged(self):
        """Merged publish contains both an npm job and a cargo job."""
        result = _generate_merged_publish(["npm", "cargo"], self.TEMPLATE_VARS)

        # Both jobs present as top-level job keys under jobs:
        assert "\n  npm:" in result
        assert "\n  cargo:" in result

        # npm-specific content
        assert "npm publish" in result
        assert "NPM_TOKEN" in result

        # cargo-specific content
        assert "cargo publish" in result
        assert "CARGO_REGISTRY_TOKEN" in result

    def test_pypi_deno_merged(self):
        """Merged publish contains both a pypi job and a deno job."""
        result = _generate_merged_publish(["pypi", "deno"], self.TEMPLATE_VARS)

        assert "\n  pypi:" in result
        assert "\n  deno:" in result

        # pypi-specific content
        assert "uv build" in result

        # deno-specific content
        assert "deno publish" in result

    def test_single_target_merged(self):
        """Merged publish with a single target produces one job."""
        result = _generate_merged_publish(["npm"], self.TEMPLATE_VARS)

        assert "\n  npm:" in result
        assert "npm publish" in result

        # No other registry jobs
        assert "\n  pypi:" not in result
        assert "\n  cargo:" not in result
        assert "\n  go:" not in result
        assert "\n  deno:" not in result

    def test_secondary_npm_registry_url_resolved(self):
        """When npm is a secondary target, {{registryUrl}} must resolve from namespaced vars.

        Regression test: previously, only the primary target's vars were
        un-namespaced, so {{registryUrl}} in npm's template stayed unresolved
        when npm was secondary (e.g., pypi+npm project).
        """
        # Simulate a pypi-primary, npm-secondary merged vars dict:
        # pypi vars are un-namespaced, npm vars are only namespaced.
        vars_dict = {
            "name": "test-pkg",
            "version": "1.0.0",
            "repoName": "user/repo",
            "minRequiredPython": "3.11",
            "pypi.name": "test-pkg",
            "pypi.version": "1.0.0",
            "pypi.repoName": "user/repo",
            "pypi.minRequiredPython": "3.11",
            "npm.name": "test-pkg",
            "npm.version": "1.0.0",
            "npm.repoName": "user/repo",
            "npm.registryUrl": "https://registry.npmjs.org",
            "npm.binCommand": "test-pkg",
            "npm.author": "",
            "npm.packageManager": "npm",
        }
        result = _generate_merged_publish(["pypi", "npm"], vars_dict)

        # {{registryUrl}} in npm's template must be resolved
        assert "{{registryUrl}}" not in result
        assert "registry.npmjs.org" in result

    def test_permissions_merged(self):
        """Merged permissions use the most permissive value for each key.

        npm has contents: read, id-token: write.
        go has contents: write.
        The merged result should have contents: write (most permissive)
        and id-token: write (only from npm).
        """
        result = _generate_merged_publish(["npm", "go"], self.TEMPLATE_VARS)

        # contents should be escalated to write (go needs write, npm only needs read)
        assert "contents: write" in result

        # id-token: write should be preserved from npm
        assert "id-token: write" in result

    def test_workflow_dispatch_present_in_merged(self):
        """Merged publish includes workflow_dispatch from templates."""
        result = _generate_merged_publish(["npm", "pypi"], self.TEMPLATE_VARS)
        assert "workflow_dispatch:" in result

    def test_workflow_dispatch_present_single_target(self):
        """Single-target merged publish includes workflow_dispatch."""
        result = _generate_merged_publish(["npm"], self.TEMPLATE_VARS)
        assert "workflow_dispatch:" in result

    def test_on_block_preserves_release_trigger(self):
        """Merged on: block preserves release trigger with sub-keys."""
        result = _generate_merged_publish(["cargo", "deno"], self.TEMPLATE_VARS)
        assert "release:" in result
        assert "types: [published]" in result
        assert "workflow_dispatch:" in result

    def test_on_triggers_merged_from_all_targets(self):
        """All trigger keys from all templates are present in merged output."""
        result = _generate_merged_publish(["npm", "go"], self.TEMPLATE_VARS)
        assert "\non:\n" in result
        assert "  release:" in result
        assert "  workflow_dispatch:" in result


class TestOnTriggerParsing:
    """Unit tests for _parse_on_triggers and _merge_on_triggers."""

    def test_parse_simple_on_block(self):
        """Parse on: block with release + workflow_dispatch."""
        block = [
            "on:\n",
            "  release:\n",
            "    types: [published]\n",
            "  workflow_dispatch:\n",
        ]
        result = _parse_on_triggers(block)
        assert "release" in result
        assert "workflow_dispatch" in result
        assert len(result["release"]) == 1
        assert "types: [published]" in result["release"][0]
        assert result["workflow_dispatch"] == []

    def test_parse_only_release(self):
        """Parse on: block with only release trigger."""
        block = [
            "on:\n",
            "  release:\n",
            "    types: [published]\n",
        ]
        result = _parse_on_triggers(block)
        assert "release" in result
        assert "workflow_dispatch" not in result

    def test_merge_adds_workflow_dispatch(self):
        """Merge guarantees workflow_dispatch even if no template has it."""
        triggers = [{"release": ["    types: [published]\n"]}]
        result = _merge_on_triggers(triggers)
        assert "workflow_dispatch" in result
        assert "release" in result

    def test_merge_unions_keys(self):
        """Merge unions trigger keys from all dicts."""
        t1 = {"release": ["    types: [published]\n"], "workflow_dispatch": []}
        t2 = {"release": ["    types: [published]\n"], "push": ["    branches: [main]\n"]}
        result = _merge_on_triggers([t1, t2])
        assert "release" in result
        assert "workflow_dispatch" in result
        assert "push" in result

    def test_merge_keeps_first_nonempty_subblock(self):
        """When both dicts have sub-lines for same trigger, first non-empty wins."""
        t1 = {"release": ["    types: [published]\n"]}
        t2 = {"release": ["    types: [created]\n"]}
        result = _merge_on_triggers([t1, t2])
        assert "published" in result["release"][0]
