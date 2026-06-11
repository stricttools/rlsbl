"""Tests for the _get_release_branches helper in pre_push_check."""

from pathlib import Path

import pytest

from rlsbl.commands.pre_push_check import _get_release_branches
from rlsbl.context import ProjectContext


class TestGetReleaseBranches:
    """Unit tests for the _get_release_branches helper."""

    def test_default_when_no_config(self, tmp_project):
        assert _get_release_branches(ProjectContext(project_root=Path("."), workspace_root=None, config={})) == ["main", "master"]

    def test_default_when_key_missing(self, tmp_project):
        assert _get_release_branches(ProjectContext(project_root=Path("."), workspace_root=None, config={"other": 1})) == ["main", "master"]

    def test_override(self, tmp_project):
        assert _get_release_branches(ProjectContext(project_root=Path("."), workspace_root=None, config={"release_branches": ["trunk", "stable"]})) == ["trunk", "stable"]

    def test_empty_list_raises(self, tmp_project):
        """An empty list would silently disable the warning entirely.
        Treat it as a configuration error: the user should remove the key
        to opt back into the default, or list at least one branch.
        """
        with pytest.raises(ValueError) as excinfo:
            _get_release_branches(ProjectContext(project_root=Path("."), workspace_root=None, config={"release_branches": []}))
        msg = str(excinfo.value)
        assert ".rlsbl/config.json" in msg
        assert "release_branches" in msg
        assert "empty list" in msg
        # Suggests the recovery path
        assert "Remove the key" in msg or "remove the key" in msg

    def test_non_list_raises(self, tmp_project):
        """A non-list value (string, dict, int) is also a configuration error."""
        with pytest.raises(ValueError) as excinfo:
            _get_release_branches(ProjectContext(project_root=Path("."), workspace_root=None, config={"release_branches": "main"}))
        msg = str(excinfo.value)
        assert ".rlsbl/config.json" in msg
        assert "release_branches" in msg
        assert "list" in msg
