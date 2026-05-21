"""Tests for publish_inline: workflow parser and YAML emitter."""

import copy
import os
import textwrap
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.monorepo.publish_inline import (
    compute_publish_hashes,
    emit_workflow,
    generate_inline_publish_router,
    inject_job_metadata,
    load_publish_cache,
    parse_publish_workflow,
    prefix_jobs,
    resolve_permissions,
    rewrite_action_paths,
    save_publish_cache,
    should_regenerate_router,
    transform_project_jobs,
)


def _safe_load(text):
    return YAML(typ='safe').load(text)


def _dump(data):
    from io import StringIO
    yml = YAML()
    stream = StringIO()
    yml.dump(data, stream)
    return stream.getvalue()


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
        reloaded = _safe_load(emitted)

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
        reloaded = _safe_load(output)
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


# ---------------------------------------------------------------------------
# generate_inline_publish_router tests
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
      pypi:
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

    jobs:
      npm:
        runs-on: ubuntu-latest
        permissions:
          contents: read
          id-token: write
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


def _setup_publish_project(root, project_path, workflow_content):
    """Create the publish workflow file for a project in *root*."""
    wf_dir = os.path.join(root, project_path, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    wf_file = os.path.join(wf_dir, "publish.yml")
    with open(wf_file, "w") as f:
        f.write(workflow_content)


class TestGenerateInlinePublishRouter:
    def test_single_pypi_project(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)

        projects = [{"name": "mypkg", "path": "packages/mypkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mypkg@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        assert len(parsed["jobs"]) == 2  # 1 project job + 1 no-op
        assert "mypkg-pypi" in parsed["jobs"]
        assert "no-op" in parsed["jobs"]

    def test_multi_project_workspace(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)
        _setup_publish_project(root, "packages/mylib", NPM_PUBLISH_WF)

        projects = [
            {"name": "mypkg", "path": "packages/mypkg"},
            {"name": "mylib", "path": "packages/mylib"},
        ]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=lambda proj, _root: f"{proj['name']}@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        assert len(parsed["jobs"]) == 3  # 2 project jobs + 1 no-op
        assert "mypkg-pypi" in parsed["jobs"]
        assert "mylib-npm" in parsed["jobs"]
        assert "no-op" in parsed["jobs"]

    def test_output_starts_with_header(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)

        projects = [{"name": "mypkg", "path": "packages/mypkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mypkg@v",
        ):
            result = generate_inline_publish_router(projects, root)

        assert result.startswith("# DO NOT EDIT")

    def test_output_is_valid_yaml(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)

        projects = [{"name": "mypkg", "path": "packages/mypkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mypkg@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        assert isinstance(parsed, dict)
        assert "name" in parsed
        assert "on" in parsed
        assert "jobs" in parsed

    def test_jobs_have_correct_if_condition(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)
        _setup_publish_project(root, "packages/mylib", NPM_PUBLISH_WF)

        projects = [
            {"name": "mypkg", "path": "packages/mypkg"},
            {"name": "mylib", "path": "packages/mylib"},
        ]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=lambda proj, _root: f"{proj['name']}@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        pypi_job = parsed["jobs"]["mypkg-pypi"]
        npm_job = parsed["jobs"]["mylib-npm"]

        assert pypi_job["if"] == "startsWith(github.event.release.tag_name, 'mypkg@v')"
        assert npm_job["if"] == "startsWith(github.event.release.tag_name, 'mylib@v')"

    def test_no_top_level_permissions(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)

        projects = [{"name": "mypkg", "path": "packages/mypkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mypkg@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        assert "permissions" not in parsed

    def test_workflow_structure(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)

        projects = [{"name": "mypkg", "path": "packages/mypkg"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mypkg@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        assert parsed["name"] == "Publish Router"
        assert parsed["on"] == {"release": {"types": ["published"]}}

    def test_trailing_slash_stripped_from_path(self, tmp_path):
        root = str(tmp_path)
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)

        projects = [{"name": "mypkg", "path": "packages/mypkg/"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mypkg@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        job = parsed["jobs"]["mypkg-pypi"]
        assert job["defaults"]["run"]["working-directory"] == "packages/mypkg"


# ---------------------------------------------------------------------------
# Publish hash cache tests
# ---------------------------------------------------------------------------


class TestComputePublishHashes:
    def test_hashes_for_mixed_projects(self, tmp_path):
        root = str(tmp_path)
        # Project with publish.yml
        _setup_publish_project(root, "packages/mypkg", PYPI_PUBLISH_WF)
        # Project without publish.yml (no workflow file created)
        os.makedirs(os.path.join(root, "packages/noworkflow"), exist_ok=True)

        projects = [
            {"name": "mypkg", "path": "packages/mypkg"},
            {"name": "noworkflow", "path": "packages/noworkflow"},
        ]
        result = compute_publish_hashes(projects, root)

        assert "mypkg" in result
        assert "noworkflow" in result
        # mypkg should have a hex digest string
        assert isinstance(result["mypkg"], str)
        assert len(result["mypkg"]) == 64  # SHA256 hex is 64 chars
        # noworkflow should be None
        assert result["noworkflow"] is None


class TestLoadSaveCache:
    def test_round_trip(self, tmp_path):
        monorepo_dir = str(tmp_path)
        hashes = {"mypkg": "a1b2c3d4" * 8, "other": None}

        save_publish_cache(monorepo_dir, hashes)
        loaded = load_publish_cache(monorepo_dir)

        assert loaded == hashes

    def test_load_missing_returns_none(self, tmp_path):
        assert load_publish_cache(str(tmp_path)) is None

    def test_load_invalid_json_returns_none(self, tmp_path):
        cache_file = os.path.join(str(tmp_path), "publish-cache.json")
        with open(cache_file, "w") as f:
            f.write("not valid json{{{")
        assert load_publish_cache(str(tmp_path)) is None

    def test_save_returns_path(self, tmp_path):
        monorepo_dir = str(tmp_path)
        path = save_publish_cache(monorepo_dir, {"x": "abc"})
        assert path == os.path.join(monorepo_dir, "publish-cache.json")
        assert os.path.isfile(path)


class TestShouldRegenerateRouter:
    def test_no_cache(self, tmp_path):
        router = os.path.join(str(tmp_path), "publish.yml")
        assert should_regenerate_router(None, {"a": "hash"}, router) is True

    def test_changed_hash(self, tmp_path):
        router = os.path.join(str(tmp_path), "publish.yml")
        with open(router, "w") as f:
            f.write("content")
        cached = {"mypkg": "old_hash"}
        current = {"mypkg": "new_hash"}
        assert should_regenerate_router(cached, current, router) is True

    def test_same_hashes_and_router_exists(self, tmp_path):
        router = os.path.join(str(tmp_path), "publish.yml")
        with open(router, "w") as f:
            f.write("content")
        hashes = {"mypkg": "same_hash", "other": None}
        assert should_regenerate_router(hashes, hashes, router) is False

    def test_missing_router(self, tmp_path):
        router = os.path.join(str(tmp_path), "publish.yml")
        hashes = {"mypkg": "same_hash"}
        assert should_regenerate_router(hashes, hashes, router) is True

    def test_project_added(self, tmp_path):
        router = os.path.join(str(tmp_path), "publish.yml")
        with open(router, "w") as f:
            f.write("content")
        cached = {"mypkg": "hash1"}
        current = {"mypkg": "hash1", "newproject": "hash2"}
        assert should_regenerate_router(cached, current, router) is True

    def test_project_removed(self, tmp_path):
        router = os.path.join(str(tmp_path), "publish.yml")
        with open(router, "w") as f:
            f.write("content")
        cached = {"mypkg": "hash1", "removed": "hash2"}
        current = {"mypkg": "hash1"}
        assert should_regenerate_router(cached, current, router) is True


# ---------------------------------------------------------------------------
# Integration tests with realistic multi-target workflows
# ---------------------------------------------------------------------------

# PyPI + npm dual-target workflow (like strictcli)
DUAL_TARGET_PYPI_NPM_WF = textwrap.dedent("""\
    name: Publish
    on:
      release:
        types: [published]
    permissions:
      contents: read
      id-token: write
    jobs:
      pypi:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v6
          - uses: astral-sh/setup-uv@v7
          - run: uv build
          - uses: pypa/gh-action-pypi-publish@release/v1
      npm:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v6
          - uses: actions/setup-node@v6
            with:
              node-version: 24
              registry-url: https://registry.npmjs.org
          - run: npm publish --provenance --access public
            env:
              NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
""")

# Go project with npm wrapper (goreleaser + npm-publish with needs)
GO_NPM_WRAPPER_WF = textwrap.dedent("""\
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
          - uses: goreleaser/goreleaser-action@v6
            with:
              args: release --clean
      npm-publish:
        runs-on: ubuntu-latest
        needs: [goreleaser]
        steps:
          - uses: actions/checkout@v6
          - uses: actions/setup-node@v6
            with:
              node-version: 24
              registry-url: https://registry.npmjs.org
          - run: npm publish --provenance --access public
            env:
              NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
""")

# Simple PyPI-only workflow (for multi-project test)
SIMPLE_PYPI_WF = textwrap.dedent("""\
    name: Publish
    on:
      release:
        types: [published]
    permissions:
      contents: read
      id-token: write
    jobs:
      pypi:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v6
          - run: uv build
          - uses: pypa/gh-action-pypi-publish@release/v1
""")

# Simple npm-only workflow
SIMPLE_NPM_WF = textwrap.dedent("""\
    name: Publish
    on:
      release:
        types: [published]
    jobs:
      npm:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v6
          - uses: actions/setup-node@v6
            with:
              node-version: 24
              registry-url: https://registry.npmjs.org
          - run: npm publish
            env:
              NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
""")

# Simple Go-only workflow
SIMPLE_GO_WF = textwrap.dedent("""\
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
          - uses: goreleaser/goreleaser-action@v6
""")


class TestIntegrationRealWorkflows:
    """End-to-end integration tests with realistic multi-target workspaces."""

    def test_pypi_npm_dual_target(self, tmp_path):
        """PyPI + npm dual-target project like strictcli."""
        root = str(tmp_path)
        _setup_publish_project(root, "python", DUAL_TARGET_PYPI_NPM_WF)

        projects = [{"name": "strictcli", "path": "python"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="strictcli@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        jobs = parsed["jobs"]

        # Correct job names
        assert "strictcli-pypi" in jobs
        assert "strictcli-npm" in jobs

        # Both have correct if condition
        expected_if = "startsWith(github.event.release.tag_name, 'strictcli@v')"
        assert jobs["strictcli-pypi"]["if"] == expected_if
        assert jobs["strictcli-npm"]["if"] == expected_if

        # Both have working-directory
        assert jobs["strictcli-pypi"]["defaults"]["run"]["working-directory"] == "python"
        assert jobs["strictcli-npm"]["defaults"]["run"]["working-directory"] == "python"

        # PyPI job has permissions pushed down from workflow level
        assert jobs["strictcli-pypi"]["permissions"] == {
            "contents": "read",
            "id-token": "write",
        }

        # PyPI publish step has packages-dir
        pypi_publish_step = next(
            s
            for s in jobs["strictcli-pypi"]["steps"]
            if "pypa/gh-action-pypi-publish" in s.get("uses", "")
        )
        assert pypi_publish_step["with"]["packages-dir"] == "python/dist/"

        # npm job preserves secrets (NOT filtered out -- works in non-reusable workflows)
        npm_steps_yaml = _dump(jobs["strictcli-npm"]["steps"])
        assert "${{ secrets.NPM_TOKEN }}" in npm_steps_yaml

        # No top-level permissions on the workflow
        assert "permissions" not in parsed

        # Starts with DO NOT EDIT header
        assert result.startswith("# DO NOT EDIT")

    def test_go_project_with_npm_wrapper(self, tmp_path):
        """Go project with goreleaser + npm-publish (with needs dependency)."""
        root = str(tmp_path)
        _setup_publish_project(root, "packages/golib", GO_NPM_WRAPPER_WF)

        projects = [{"name": "golib", "path": "packages/golib"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="golib@v",
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        jobs = parsed["jobs"]

        # Both jobs are prefixed
        assert "golib-goreleaser" in jobs
        assert "golib-npm-publish" in jobs

        # needs: is rewritten to prefixed name
        assert jobs["golib-npm-publish"]["needs"] == ["golib-goreleaser"]

        # go-version-file is prefixed with project path
        goreleaser_steps = jobs["golib-goreleaser"]["steps"]
        setup_go_step = next(
            s for s in goreleaser_steps if "actions/setup-go" in s.get("uses", "")
        )
        assert setup_go_step["with"]["go-version-file"] == "packages/golib/go.mod"

    def test_multi_project_router(self, tmp_path):
        """3-project workspace with pypi, npm, and go targets."""
        root = str(tmp_path)
        _setup_publish_project(root, "packages/pylib", SIMPLE_PYPI_WF)
        _setup_publish_project(root, "packages/jslib", SIMPLE_NPM_WF)
        _setup_publish_project(root, "packages/golib", GO_NPM_WRAPPER_WF)

        projects = [
            {"name": "pylib", "path": "packages/pylib"},
            {"name": "jslib", "path": "packages/jslib"},
            {"name": "golib", "path": "packages/golib"},
        ]

        def mock_tag_prefix(proj, _root):
            return f"{proj['name']}@v"

        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=mock_tag_prefix,
        ):
            result = generate_inline_publish_router(projects, root)

        parsed = _safe_load(result)
        assert isinstance(parsed, dict), "Output must be valid YAML"
        jobs = parsed["jobs"]

        # pylib has 1 job, jslib has 1 job, golib has 2 jobs + 1 no-op = 5 total
        assert len(jobs) == 5

        # All expected jobs present
        assert "pylib-pypi" in jobs
        assert "jslib-npm" in jobs
        assert "golib-goreleaser" in jobs
        assert "golib-npm-publish" in jobs

        # Each project's jobs have the correct startsWith prefix
        assert "pylib@v" in jobs["pylib-pypi"]["if"]
        assert "jslib@v" in jobs["jslib-npm"]["if"]
        assert "golib@v" in jobs["golib-goreleaser"]["if"]
        assert "golib@v" in jobs["golib-npm-publish"]["if"]

        # No job name collisions: all keys are unique (dict enforces this,
        # but verify the count matches expectations)
        all_job_names = list(jobs.keys())
        assert len(all_job_names) == len(set(all_job_names))

        # Round-trip through _safe_load confirms structural validity
        re_parsed = _safe_load(result)
        assert re_parsed == parsed

    def test_cache_skip_behavior(self, tmp_path):
        """Hash cache causes sync to skip regeneration when nothing changed."""
        root = str(tmp_path)

        # Set up 2 projects with publish workflows
        _setup_publish_project(root, "packages/alpha", SIMPLE_PYPI_WF)
        _setup_publish_project(root, "packages/beta", SIMPLE_NPM_WF)

        projects = [
            {"name": "alpha", "path": "packages/alpha"},
            {"name": "beta", "path": "packages/beta"},
        ]

        monorepo_dir = os.path.join(root, ".rlsbl-monorepo")
        os.makedirs(monorepo_dir, exist_ok=True)

        router_path = os.path.join(root, ".github", "workflows", "publish.yml")
        os.makedirs(os.path.dirname(router_path), exist_ok=True)

        def mock_tag_prefix(proj, _root):
            return f"{proj['name']}@v"

        # --- First run: no cache, generates router ---
        hashes_1 = compute_publish_hashes(projects, root)
        cached_1 = load_publish_cache(monorepo_dir)
        assert cached_1 is None  # No cache yet
        assert should_regenerate_router(cached_1, hashes_1, router_path) is True

        # Generate and save
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            side_effect=mock_tag_prefix,
        ):
            router_content = generate_inline_publish_router(
                [p for p in projects if hashes_1[p["name"]] is not None], root
            )
        with open(router_path, "w") as f:
            f.write(router_content)
        save_publish_cache(monorepo_dir, hashes_1)

        # --- Second run: same hashes, should skip ---
        hashes_2 = compute_publish_hashes(projects, root)
        cached_2 = load_publish_cache(monorepo_dir)
        assert cached_2 == hashes_1
        assert should_regenerate_router(cached_2, hashes_2, router_path) is False

        # --- Third run: modify one project's publish.yml ---
        modified_wf = SIMPLE_PYPI_WF.replace("uv build", "uv build --wheel")
        alpha_wf = os.path.join(
            root, "packages/alpha", ".github", "workflows", "publish.yml"
        )
        with open(alpha_wf, "w") as f:
            f.write(modified_wf)

        hashes_3 = compute_publish_hashes(projects, root)
        cached_3 = load_publish_cache(monorepo_dir)
        assert hashes_3["alpha"] != cached_3["alpha"]
        assert should_regenerate_router(cached_3, hashes_3, router_path) is True

        # --- Fourth run: add a new project ---
        _setup_publish_project(root, "packages/gamma", SIMPLE_GO_WF)
        projects_extended = projects + [{"name": "gamma", "path": "packages/gamma"}]

        # Save cache with current hashes first (simulate post-regen)
        save_publish_cache(monorepo_dir, hashes_3)

        hashes_4 = compute_publish_hashes(projects_extended, root)
        cached_4 = load_publish_cache(monorepo_dir)
        # New project means different keys
        assert "gamma" in hashes_4
        assert "gamma" not in cached_4
        assert should_regenerate_router(cached_4, hashes_4, router_path) is True

    def test_on_key_quoting_round_trip(self):
        """Verify emit_workflow's quoting of 'on' (YAML boolean) round-trips correctly.

        The YAML spec treats bare ``on`` as a boolean (True), so PyYAML's
        SafeDumper quotes it as ``'on'`` when used as a mapping key.  GitHub
        Actions accepts both ``on:`` and ``'on':`` so this is not a bug.
        This test documents the behavior and confirms round-trip safety.
        """
        workflow = {
            "name": "Publish Router",
            "on": {"release": {"types": ["published"]}},
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo hello"}],
                }
            },
        }

        emitted = emit_workflow(workflow)

        # The emitted YAML should contain 'on' (quoted) since PyYAML treats
        # bare on as a boolean keyword
        assert "'on'" in emitted or "on:" in emitted

        # Most importantly: round-trip preserves the key and its value
        reloaded = _safe_load(emitted)
        assert "on" in reloaded
        assert reloaded["on"] == {"release": {"types": ["published"]}}
        assert reloaded["name"] == "Publish Router"
        assert reloaded["jobs"] == workflow["jobs"]
