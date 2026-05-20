"""Tests for publish_inline: workflow parser and YAML emitter."""

import copy
import os
import textwrap

import pytest
import yaml

from rlsbl.commands.monorepo.publish_inline import (
    emit_workflow,
    inject_job_metadata,
    parse_publish_workflow,
    prefix_jobs,
    resolve_permissions,
    rewrite_action_paths,
    transform_project_jobs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_WORKFLOW = textwrap.dedent("""\
    name: Publish

    on:
      release:
        types: [published]

    permissions:
      contents: read
      id-token: write

    env:
      REGISTRY: ghcr.io

    jobs:
      publish:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v6
          - run: echo hello
""")

MINIMAL_WORKFLOW = textwrap.dedent("""\
    on:
      push:
        branches: [main]

    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - run: echo test
""")

NO_JOBS_WORKFLOW = textwrap.dedent("""\
    name: Broken
    on:
      push:
        branches: [main]
""")


# ---------------------------------------------------------------------------
# parse_publish_workflow tests
# ---------------------------------------------------------------------------


class TestParsePublishWorkflow:
    def test_valid_workflow(self, tmp_path):
        wf = tmp_path / "publish.yml"
        wf.write_text(VALID_WORKFLOW)

        result = parse_publish_workflow(str(wf))

        assert "publish" in result["jobs"]
        assert result["jobs"]["publish"]["runs-on"] == "ubuntu-latest"
        assert result["name"] == "Publish"

    def test_missing_jobs_raises(self, tmp_path):
        wf = tmp_path / "bad.yml"
        wf.write_text(NO_JOBS_WORKFLOW)

        with pytest.raises(ValueError, match="missing a 'jobs' key"):
            parse_publish_workflow(str(wf))

    def test_permissions_and_env_extracted(self, tmp_path):
        wf = tmp_path / "publish.yml"
        wf.write_text(VALID_WORKFLOW)

        result = parse_publish_workflow(str(wf))

        assert result["permissions"] == {"contents": "read", "id-token": "write"}
        assert result["env"] == {"REGISTRY": "ghcr.io"}

    def test_no_permissions_returns_none(self, tmp_path):
        wf = tmp_path / "minimal.yml"
        wf.write_text(MINIMAL_WORKFLOW)

        result = parse_publish_workflow(str(wf))

        assert result["permissions"] is None
        assert result["env"] is None
        assert result["name"] is None
        assert "test" in result["jobs"]


# ---------------------------------------------------------------------------
# emit_workflow tests
# ---------------------------------------------------------------------------


class TestEmitWorkflow:
    def test_single_line_strings(self):
        data = {"name": "CI", "on": "push"}
        output = emit_workflow(data)

        assert "name: CI" in output
        # Single-line values must NOT use literal block style
        assert "|" not in output

    def test_multiline_literal_block(self):
        data = {"jobs": {"build": {"steps": [{"run": "echo hello\necho world\n"}]}}}
        output = emit_workflow(data)

        assert "|\n" in output
        assert "echo hello" in output
        assert "echo world" in output

    def test_sort_keys_false(self):
        # Plain dicts are insertion-ordered in Python 3.7+
        data = {"zebra": 1, "alpha": 2, "middle": 3}
        output = emit_workflow(data)
        lines = output.strip().splitlines()

        assert lines[0].startswith("zebra")
        assert lines[1].startswith("alpha")
        assert lines[2].startswith("middle")

    def test_round_trip(self):
        original = {
            "name": "Publish",
            "on": {"release": {"types": ["published"]}},
            "permissions": {"contents": "read", "id-token": "write"},
            "jobs": {
                "publish": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"uses": "actions/checkout@v6"},
                        {"run": "echo done"},
                    ],
                }
            },
        }
        emitted = emit_workflow(original)
        reloaded = yaml.safe_load(emitted)

        assert reloaded == original

    def test_github_expressions_not_mangled(self):
        data = {
            "jobs": {
                "publish": {
                    "steps": [
                        {
                            "run": "npm publish",
                            "env": {"NODE_AUTH_TOKEN": "${{ secrets.NPM_TOKEN }}"},
                        }
                    ]
                }
            }
        }
        output = emit_workflow(data)

        assert "${{ secrets.NPM_TOKEN }}" in output

        # Verify round-trip preserves the expression
        reloaded = yaml.safe_load(output)
        token = reloaded["jobs"]["publish"]["steps"][0]["env"]["NODE_AUTH_TOKEN"]
        assert token == "${{ secrets.NPM_TOKEN }}"


# ---------------------------------------------------------------------------
# prefix_jobs tests
# ---------------------------------------------------------------------------


class TestPrefixJobs:
    def test_single_job(self):
        jobs = {"publish": {"runs-on": "ubuntu-latest", "steps": []}}
        result = prefix_jobs("myproject", jobs)

        assert "myproject-publish" in result
        assert "publish" not in result

    def test_multi_job_with_needs_list(self):
        jobs = {
            "goreleaser": {"runs-on": "ubuntu-latest", "steps": []},
            "npm-publish": {
                "runs-on": "ubuntu-latest",
                "needs": ["goreleaser"],
                "steps": [],
            },
        }
        result = prefix_jobs("myproject", jobs)

        assert "myproject-goreleaser" in result
        assert "myproject-npm-publish" in result
        assert result["myproject-npm-publish"]["needs"] == ["myproject-goreleaser"]

    def test_needs_as_string(self):
        jobs = {
            "build": {"runs-on": "ubuntu-latest", "steps": []},
            "deploy": {
                "runs-on": "ubuntu-latest",
                "needs": "build",
                "steps": [],
            },
        }
        result = prefix_jobs("proj", jobs)

        assert result["proj-deploy"]["needs"] == "proj-build"

    def test_does_not_mutate_input(self):
        jobs = {"publish": {"runs-on": "ubuntu-latest", "needs": "build"}}
        original = copy.deepcopy(jobs)
        prefix_jobs("x", jobs)
        assert jobs == original


# ---------------------------------------------------------------------------
# inject_job_metadata tests
# ---------------------------------------------------------------------------


class TestInjectJobMetadata:
    def test_adds_if_condition(self):
        jobs = {"publish": {"runs-on": "ubuntu-latest", "steps": []}}
        result = inject_job_metadata(jobs, "strictcli/v", "packages/strictcli")

        assert result["publish"]["if"] == "startsWith(github.event.release.tag_name, 'strictcli/v')"

    def test_adds_working_directory(self):
        jobs = {"publish": {"runs-on": "ubuntu-latest", "steps": []}}
        result = inject_job_metadata(jobs, "strictcli/v", "packages/strictcli")

        assert result["publish"]["defaults"]["run"]["working-directory"] == "packages/strictcli"

    def test_preserves_existing_keys(self):
        jobs = {
            "publish": {
                "runs-on": "ubuntu-latest",
                "timeout-minutes": 30,
                "steps": [{"run": "echo hello"}],
            }
        }
        result = inject_job_metadata(jobs, "prefix/v", "path/to/pkg")

        assert result["publish"]["runs-on"] == "ubuntu-latest"
        assert result["publish"]["timeout-minutes"] == 30
        assert result["publish"]["steps"] == [{"run": "echo hello"}]

    def test_merges_existing_defaults_run(self):
        jobs = {
            "publish": {
                "runs-on": "ubuntu-latest",
                "defaults": {"run": {"shell": "bash"}},
                "steps": [],
            }
        }
        result = inject_job_metadata(jobs, "p/v", "pkg")

        assert result["publish"]["defaults"]["run"]["shell"] == "bash"
        assert result["publish"]["defaults"]["run"]["working-directory"] == "pkg"

    def test_does_not_mutate_input(self):
        jobs = {"publish": {"runs-on": "ubuntu-latest", "steps": []}}
        original = copy.deepcopy(jobs)
        inject_job_metadata(jobs, "prefix/v", "path")
        assert jobs == original


# ---------------------------------------------------------------------------
# rewrite_action_paths tests
# ---------------------------------------------------------------------------


class TestRewriteActionPaths:
    def test_pypi_publish_gets_packages_dir(self):
        jobs = {
            "publish": {
                "steps": [
                    {"uses": "pypa/gh-action-pypi-publish@release/v1"},
                ]
            }
        }
        result = rewrite_action_paths(jobs, "packages/mylib")

        step = result["publish"]["steps"][0]
        assert step["with"]["packages-dir"] == "packages/mylib/dist/"

    def test_pypi_publish_with_existing_with_block(self):
        jobs = {
            "publish": {
                "steps": [
                    {
                        "uses": "pypa/gh-action-pypi-publish@release/v1",
                        "with": {"verbose": True},
                    },
                ]
            }
        }
        result = rewrite_action_paths(jobs, "pkg/lib")

        step = result["publish"]["steps"][0]
        assert step["with"]["packages-dir"] == "pkg/lib/dist/"
        assert step["with"]["verbose"] is True

    def test_version_file_paths_get_prefixed(self):
        jobs = {
            "build": {
                "steps": [
                    {
                        "uses": "actions/setup-python@v5",
                        "with": {"python-version-file": ".python-version"},
                    },
                    {
                        "uses": "actions/setup-go@v5",
                        "with": {"go-version-file": "go.mod"},
                    },
                    {
                        "uses": "actions/setup-node@v4",
                        "with": {"node-version-file": ".nvmrc"},
                    },
                ]
            }
        }
        result = rewrite_action_paths(jobs, "packages/mylib")

        steps = result["build"]["steps"]
        assert steps[0]["with"]["python-version-file"] == "packages/mylib/.python-version"
        assert steps[1]["with"]["go-version-file"] == "packages/mylib/go.mod"
        assert steps[2]["with"]["node-version-file"] == "packages/mylib/.nvmrc"

    def test_already_prefixed_paths_not_doubled(self):
        jobs = {
            "build": {
                "steps": [
                    {
                        "uses": "actions/setup-python@v5",
                        "with": {"python-version-file": "packages/mylib/.python-version"},
                    },
                ]
            }
        }
        result = rewrite_action_paths(jobs, "packages/mylib")

        step = result["build"]["steps"][0]
        assert step["with"]["python-version-file"] == "packages/mylib/.python-version"

    def test_does_not_mutate_input(self):
        jobs = {
            "publish": {
                "steps": [
                    {"uses": "pypa/gh-action-pypi-publish@v1"},
                ]
            }
        }
        original = copy.deepcopy(jobs)
        rewrite_action_paths(jobs, "pkg")
        assert jobs == original


# ---------------------------------------------------------------------------
# resolve_permissions tests
# ---------------------------------------------------------------------------


class TestResolvePermissions:
    def test_job_with_own_permissions_kept(self):
        jobs = {
            "publish": {
                "runs-on": "ubuntu-latest",
                "permissions": {"contents": "write"},
                "steps": [],
            }
        }
        wf_perms = {"contents": "read", "id-token": "write"}
        result = resolve_permissions(jobs, wf_perms)

        assert result["publish"]["permissions"] == {"contents": "write"}

    def test_job_inherits_workflow_permissions(self):
        jobs = {
            "publish": {"runs-on": "ubuntu-latest", "steps": []},
        }
        wf_perms = {"contents": "read", "id-token": "write"}
        result = resolve_permissions(jobs, wf_perms)

        assert result["publish"]["permissions"] == {"contents": "read", "id-token": "write"}

    def test_no_workflow_permissions_no_job_permissions(self):
        jobs = {
            "publish": {"runs-on": "ubuntu-latest", "steps": []},
        }
        result = resolve_permissions(jobs, None)

        assert "permissions" not in result["publish"]

    def test_does_not_mutate_input(self):
        jobs = {"publish": {"runs-on": "ubuntu-latest", "steps": []}}
        wf_perms = {"id-token": "write"}
        original_jobs = copy.deepcopy(jobs)
        original_perms = copy.deepcopy(wf_perms)
        resolve_permissions(jobs, wf_perms)
        assert jobs == original_jobs
        assert wf_perms == original_perms


# ---------------------------------------------------------------------------
# transform_project_jobs (end-to-end) tests
# ---------------------------------------------------------------------------


REALISTIC_WORKFLOW = textwrap.dedent("""\
    name: Publish strictcli

    on:
      release:
        types: [published]

    permissions:
      contents: read
      id-token: write

    jobs:
      pypi:
        runs-on: ubuntu-latest
        environment:
          name: pypi
          url: https://pypi.org/p/strictcli
        steps:
          - uses: actions/checkout@v6
          - uses: actions/setup-python@v5
            with:
              python-version-file: .python-version
          - run: uv build
          - uses: pypa/gh-action-pypi-publish@release/v1

      npm:
        runs-on: ubuntu-latest
        needs: pypi
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


class TestTransformProjectJobs:
    def test_end_to_end(self, tmp_path):
        wf = tmp_path / "publish.yml"
        wf.write_text(REALISTIC_WORKFLOW)

        result = transform_project_jobs(
            project_name="strictcli",
            project_path="packages/strictcli",
            tag_prefix="strictcli/v",
            workflow_path=str(wf),
        )

        # Job keys are prefixed
        assert "strictcli-pypi" in result
        assert "strictcli-npm" in result
        assert "pypi" not in result
        assert "npm" not in result

        pypi_job = result["strictcli-pypi"]
        npm_job = result["strictcli-npm"]

        # needs: rewritten
        assert npm_job["needs"] == "strictcli-pypi"

        # if: condition added
        assert pypi_job["if"] == "startsWith(github.event.release.tag_name, 'strictcli/v')"
        assert npm_job["if"] == "startsWith(github.event.release.tag_name, 'strictcli/v')"

        # working-directory injected
        assert pypi_job["defaults"]["run"]["working-directory"] == "packages/strictcli"
        assert npm_job["defaults"]["run"]["working-directory"] == "packages/strictcli"

        # Permissions pushed down from workflow level
        assert pypi_job["permissions"] == {"contents": "read", "id-token": "write"}
        assert npm_job["permissions"] == {"contents": "read", "id-token": "write"}

        # PyPI action has packages-dir
        pypi_publish_step = next(
            s for s in pypi_job["steps"]
            if "pypa/gh-action-pypi-publish" in s.get("uses", "")
        )
        assert pypi_publish_step["with"]["packages-dir"] == "packages/strictcli/dist/"

        # Version file paths are prefixed
        setup_python_step = next(
            s for s in pypi_job["steps"]
            if "actions/setup-python" in s.get("uses", "")
        )
        assert setup_python_step["with"]["python-version-file"] == "packages/strictcli/.python-version"

        setup_node_step = next(
            s for s in npm_job["steps"]
            if "actions/setup-node" in s.get("uses", "")
        )
        assert setup_node_step["with"]["node-version-file"] == "packages/strictcli/.nvmrc"
