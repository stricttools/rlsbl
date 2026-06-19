"""Tests for _strip_uv_no_sources sync transform (defense-in-depth for monorepo)."""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo import (
    _cmd_init,
    _cmd_sync,
    _strip_uv_no_sources,
    parse_ci_workflow,
    emit_ci_workflow,
)
from rlsbl.workspace import save_workspace


CI_WITH_UV_NO_SOURCES = """\
name: CI

on:
  push:
    branches: [main]

env:
  UV_NO_SOURCES: "1"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: uv run pytest
"""

CI_WITH_UV_NO_SOURCES_AND_OTHER = """\
name: CI

on:
  push:
    branches: [main]

env:
  UV_NO_SOURCES: "1"
  SOME_VAR: "hello"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: uv run pytest
"""

CI_WITHOUT_UV_NO_SOURCES = """\
name: CI

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: uv run pytest
"""

CI_WITH_OTHER_ENV_ONLY = """\
name: CI

on:
  push:
    branches: [main]

env:
  SOME_VAR: "hello"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: uv run pytest
"""


class TestStripUvNoSources:
    """Unit tests for _strip_uv_no_sources transform."""

    def test_removes_uv_no_sources(self):
        """UV_NO_SOURCES is removed from env block."""
        doc = parse_ci_workflow(CI_WITH_UV_NO_SOURCES)
        _strip_uv_no_sources(doc)
        result = emit_ci_workflow(doc)
        assert "UV_NO_SOURCES" not in result

    def test_empty_env_block_removed(self):
        """When UV_NO_SOURCES is the only env var, the entire env block is removed."""
        doc = parse_ci_workflow(CI_WITH_UV_NO_SOURCES)
        _strip_uv_no_sources(doc)
        assert "env" not in doc

    def test_noop_when_not_present(self):
        """No-op when UV_NO_SOURCES is not in the workflow."""
        doc = parse_ci_workflow(CI_WITHOUT_UV_NO_SOURCES)
        _strip_uv_no_sources(doc)
        result = emit_ci_workflow(doc)
        assert "UV_NO_SOURCES" not in result
        # Workflow should still be valid
        assert "jobs:" in result

    def test_other_env_vars_preserved(self):
        """Other env vars are preserved when UV_NO_SOURCES is removed."""
        doc = parse_ci_workflow(CI_WITH_UV_NO_SOURCES_AND_OTHER)
        _strip_uv_no_sources(doc)
        result = emit_ci_workflow(doc)
        assert "UV_NO_SOURCES" not in result
        assert "SOME_VAR" in result
        assert "env" in doc

    def test_noop_with_other_env_only(self):
        """No-op when env block exists but has no UV_NO_SOURCES."""
        doc = parse_ci_workflow(CI_WITH_OTHER_ENV_ONLY)
        _strip_uv_no_sources(doc)
        result = emit_ci_workflow(doc)
        assert "SOME_VAR" in result
        assert "env" in doc

    def test_integration_sync_strips_uv_no_sources(self, mock_git_repo, capsys):
        """Full monorepo sync strips UV_NO_SOURCES from a PyPI project workflow."""
        proj_dir = os.path.join(str(mock_git_repo), "mypylib")
        os.makedirs(proj_dir, exist_ok=True)
        with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "mypylib"\nversion = "0.1.0"\n')

        wf_dir = os.path.join(proj_dir, ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "ci.yml"), "w") as f:
            f.write(CI_WITH_UV_NO_SOURCES)

        _cmd_init({}, project_root=".")
        save_workspace(".", [{"path": "mypylib", "name": "mypylib"}])
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo),
            check=True,
        )

        _cmd_sync({}, project_root=".")
        dest = mock_git_repo / ".github" / "workflows" / "mypylib-ci.yml"
        content = dest.read_text()
        assert "UV_NO_SOURCES" not in content
        # Workflow should still have the job
        assert "test:" in content
