"""Tests for publish workflow using releasable tag prefixes.

Verifies that _get_monorepo_tag_prefix returns the releasable's tag prefix
(from tag_format) instead of the project-name-based prefix when projects
belong to a releasable.
"""

from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo.sync import _get_monorepo_tag_prefix
from rlsbl.workspace import Releasable, WorkspaceProject


# ---------------------------------------------------------------------------
# _get_monorepo_tag_prefix with releasables
# ---------------------------------------------------------------------------


class TestGetMonorepoTagPrefixWithReleasables:
    """_get_monorepo_tag_prefix should use releasable tag_format when available."""

    def _make_releasables(self, name="myrel", tag_format="{name}@v{version}"):
        return [Releasable(name=name, tag_format=tag_format)]

    def test_workspace_project_with_releasable(self, tmp_path):
        """WorkspaceProject with releasable='myrel' gets the releasable prefix."""
        proj = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": "myrel"})
        releasables = self._make_releasables("myrel")

        # Mock detect_targets to avoid needing real project files
        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path), releasables=releasables)

        assert result == "myrel@v"

    def test_dict_project_with_releasable(self, tmp_path):
        """Dict-style project with releasable='myrel' gets the releasable prefix."""
        proj = {"name": "proj-a", "path": "packages/proj-a", "releasable": "myrel"}
        releasables = self._make_releasables("myrel")

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path), releasables=releasables)

        assert result == "myrel@v"

    def test_two_projects_same_releasable(self, tmp_path):
        """Two projects in the same releasable both get the releasable prefix."""
        proj_a = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": "myrel"})
        proj_b = WorkspaceProject({"name": "proj-b", "path": "packages/proj-b", "releasable": "myrel"})
        releasables = self._make_releasables("myrel")

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result_a = _get_monorepo_tag_prefix(proj_a, str(tmp_path), releasables=releasables)
            result_b = _get_monorepo_tag_prefix(proj_b, str(tmp_path), releasables=releasables)

        assert result_a == "myrel@v"
        assert result_b == "myrel@v"

    def test_custom_tag_format(self, tmp_path):
        """Custom tag_format on releasable is used for prefix derivation."""
        proj = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": "core"})
        releasables = [Releasable(name="core", tag_format="core-v{version}")]

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path), releasables=releasables)

        assert result == "core-v"

    def test_no_releasable_field_falls_back_to_target(self, tmp_path):
        """Project without releasable field falls back to target-based prefix."""
        proj = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a"})
        releasables = self._make_releasables("myrel")

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path), releasables=releasables)

        # No target detected either, so falls back to name@v
        assert result == "proj-a@v"

    def test_releasable_false_falls_back_to_target(self, tmp_path):
        """Project with releasable=false falls back to target-based prefix."""
        proj = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": False})
        releasables = self._make_releasables("myrel")

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path), releasables=releasables)

        assert result == "proj-a@v"

    def test_no_releasables_param_uses_target(self, tmp_path):
        """When releasables=None (default), always uses target-based prefix."""
        proj = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": "myrel"})

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path))

        # No releasables passed, falls through to target/default
        assert result == "proj-a@v"

    def test_empty_releasables_list_uses_target(self, tmp_path):
        """When releasables is an empty list, uses target-based prefix."""
        proj = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": "myrel"})

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path), releasables=[])

        assert result == "proj-a@v"

    def test_releasable_name_not_in_list_falls_back(self, tmp_path):
        """When project's releasable name doesn't match any Releasable, falls back."""
        proj = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": "nonexistent"})
        releasables = self._make_releasables("myrel")

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result = _get_monorepo_tag_prefix(proj, str(tmp_path), releasables=releasables)

        # No matching releasable, falls through to target/default
        assert result == "proj-a@v"

    def test_multiple_releasables_correct_match(self, tmp_path):
        """With multiple releasables, the correct one is matched per project."""
        proj_a = WorkspaceProject({"name": "proj-a", "path": "packages/proj-a", "releasable": "alpha"})
        proj_b = WorkspaceProject({"name": "proj-b", "path": "packages/proj-b", "releasable": "beta"})
        releasables = [
            Releasable(name="alpha", tag_format="{name}@v{version}"),
            Releasable(name="beta", tag_format="beta-release-v{version}"),
        ]

        with patch("rlsbl.commands.monorepo.sync.detect_targets", return_value=[]):
            result_a = _get_monorepo_tag_prefix(proj_a, str(tmp_path), releasables=releasables)
            result_b = _get_monorepo_tag_prefix(proj_b, str(tmp_path), releasables=releasables)

        assert result_a == "alpha@v"
        assert result_b == "beta-release-v"
