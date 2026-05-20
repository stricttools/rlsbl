"""Tests for publish_inline: workflow parser and YAML emitter."""

import os
import textwrap

import pytest
import yaml

from rlsbl.commands.monorepo.publish_inline import (
    emit_workflow,
    parse_publish_workflow,
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
