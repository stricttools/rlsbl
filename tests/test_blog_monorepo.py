"""Tests for blog post generation in monorepo batch releases.

Verifies that the blog wiring (selfblog post generate) fires per-project
when run_cmd is invoked from the batch release flow. Since _cmd_batch_release
delegates to run_cmd with the full ReleaseConfig (including the blog field),
the blog wiring is already handled by the single-project release flow.
These tests verify that the delegation preserves the blog field correctly.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.release_file import BatchReleaseConfig, ReleaseConfig, read_batch_release_file
from rlsbl.commands.monorepo.batch_release import _cmd_batch_release
from rlsbl.workspace import save_workspace, WORKSPACE_DIR


def _write_toml(path, content):
    """Write a TOML string to a file, creating directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_pypi_project(base_path, subdir, version="0.1.0"):
    """Create a minimal pypi project."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    content = f'[project]\nname = "{subdir}"\nversion = "{version}"\n'
    with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
        f.write(content)


def _init_workspace(base_path, projects):
    """Initialize a workspace with the given project list."""
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    save_workspace(str(base_path), projects)


class TestBatchReleaseBlogWiring:
    """Verify that blog field is preserved and passed through in batch releases."""

    def test_batch_release_preserves_blog_field(self, tmp_path, monkeypatch, bypass_upfront_validation):
        """Each per-project run_cmd call receives the ReleaseConfig with its blog field."""
        monkeypatch.chdir(tmp_path)

        # Set up workspace with two projects
        projects = [
            {"path": "alpha", "name": "alpha"},
            {"path": "beta", "name": "beta"},
        ]
        _init_workspace(tmp_path, projects)
        _make_pypi_project(tmp_path, "alpha")
        _make_pypi_project(tmp_path, "beta")

        # Write batch release file: alpha has blog=true, beta has blog=false
        batch_toml = os.path.join(
            str(tmp_path), ".rlsbl-monorepo", "releases", "unreleased.toml"
        )
        _write_toml(
            batch_toml,
            """
[packages.alpha]
bump = "patch"
include = ["pypi"]
exclude = []
description = "Alpha release with blog"
blog = true

[packages.beta]
bump = "minor"
include = ["pypi"]
exclude = []
description = "Beta release without blog"
blog = false
""",
        )

        # Capture run_cmd calls to verify ReleaseConfig is passed correctly
        captured_configs = []

        def mock_run_cmd(release_config, flags, **kwargs):
            captured_configs.append({
                "name": os.path.basename(str(kwargs["ctx"].project_root)),
                "blog": release_config.blog,
                "description": release_config.description,
                "bump": release_config.bump,
            })

        with patch("rlsbl.commands.release.run_cmd", mock_run_cmd), \
             patch("rlsbl.commands.monorepo.batch_release.run", return_value="abc123"), \
             patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            _cmd_batch_release(
                {"dry-run": False, "yes": True, "quiet": True},
                tmp_path,
            )

        # Both projects should have been released
        assert len(captured_configs) == 2

        # Find each project's config (order is topological, but both are independent)
        alpha_cfg = next(c for c in captured_configs if c["name"] == "alpha")
        beta_cfg = next(c for c in captured_configs if c["name"] == "beta")

        assert alpha_cfg["blog"] is True, "alpha should have blog=true"
        assert alpha_cfg["description"] == "Alpha release with blog"
        assert beta_cfg["blog"] is False, "beta should have blog=false"
        assert beta_cfg["description"] == "Beta release without blog"

    def test_batch_release_blog_default_false(self, tmp_path, monkeypatch, bypass_upfront_validation):
        """When blog is not specified in batch TOML, it defaults to False."""
        monkeypatch.chdir(tmp_path)

        projects = [{"path": "solo", "name": "solo"}]
        _init_workspace(tmp_path, projects)
        _make_pypi_project(tmp_path, "solo")

        batch_toml = os.path.join(
            str(tmp_path), ".rlsbl-monorepo", "releases", "unreleased.toml"
        )
        _write_toml(
            batch_toml,
            """
[packages.solo]
bump = "patch"
include = ["pypi"]
exclude = []
description = "Solo release"
""",
        )

        captured_configs = []

        def mock_run_cmd(release_config, flags, **kwargs):
            captured_configs.append({
                "blog": release_config.blog,
            })

        with patch("rlsbl.commands.release.run_cmd", mock_run_cmd), \
             patch("rlsbl.commands.monorepo.batch_release.run", return_value="abc123"), \
             patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            _cmd_batch_release(
                {"dry-run": False, "yes": True, "quiet": True},
                tmp_path,
            )

        assert len(captured_configs) == 1
        assert captured_configs[0]["blog"] is False, "blog should default to False"

    def test_batch_release_blog_field_parsed_from_toml(self, tmp_path):
        """read_batch_release_file correctly parses blog field per package."""
        batch_toml = tmp_path / "batch.toml"
        batch_toml.write_text("""
[packages.pkg_a]
bump = "patch"
include = ["pypi"]
exclude = []
description = "Package A"
blog = true

[packages.pkg_b]
bump = "minor"
include = ["npm"]
exclude = []
description = "Package B"
blog = false
""")

        config = read_batch_release_file(str(batch_toml))

        assert config.packages["pkg_a"].blog is True
        assert config.packages["pkg_b"].blog is False

    def test_batch_release_mixed_blog_per_project(self, tmp_path, monkeypatch, bypass_upfront_validation):
        """In a batch with mixed blog settings, each project gets its own blog value."""
        monkeypatch.chdir(tmp_path)

        projects = [
            {"path": "lib1", "name": "lib1"},
            {"path": "lib2", "name": "lib2"},
            {"path": "lib3", "name": "lib3"},
        ]
        _init_workspace(tmp_path, projects)
        for p in projects:
            _make_pypi_project(tmp_path, p["name"])

        batch_toml = os.path.join(
            str(tmp_path), ".rlsbl-monorepo", "releases", "unreleased.toml"
        )
        _write_toml(
            batch_toml,
            """
[packages.lib1]
bump = "patch"
include = ["pypi"]
exclude = []
description = "lib1 release"
blog = true

[packages.lib2]
bump = "patch"
include = ["pypi"]
exclude = []
description = "lib2 release"

[packages.lib3]
bump = "minor"
include = ["pypi"]
exclude = []
description = "lib3 release"
blog = true
""",
        )

        captured_blogs = {}

        def mock_run_cmd(release_config, flags, **kwargs):
            name = os.path.basename(str(kwargs["ctx"].project_root))
            captured_blogs[name] = release_config.blog

        with patch("rlsbl.commands.release.run_cmd", mock_run_cmd), \
             patch("rlsbl.commands.monorepo.batch_release.run", return_value="abc123"), \
             patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            _cmd_batch_release(
                {"dry-run": False, "yes": True, "quiet": True},
                tmp_path,
            )

        assert captured_blogs["lib1"] is True
        assert captured_blogs["lib2"] is False  # default
        assert captured_blogs["lib3"] is True
