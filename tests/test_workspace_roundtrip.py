"""Tests for save_workspace() preserving top-level TOML sections."""

import pytest

from conftest import with_root_member, workspace_toml, make_workspace
from rlsbl.errors import WorkspaceError
from rlsbl.workspace import load_workspace, save_workspace, WORKSPACE_DIR, WORKSPACE_FILE


TOML_WITH_LAYERS = """\
# Monorepo configuration

[layers]
[layers.core]
description = "Core libraries"
paths = ["libs/core", "libs/util"]

[layers.apps]
description = "Application layer"
paths = ["apps/web", "apps/api"]
depends_on = ["core"]

[[projects]]
path = "libs/core"
name = "core"

[[projects]]
path = "apps/web"
name = "web"
"""


class TestPreservesTopLevelSections:
    """save_workspace() must not drop non-project sections."""

    def test_layers_section_survives_roundtrip(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(TOML_WITH_LAYERS))

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)

        content = (ws_dir / WORKSPACE_FILE).read_text()
        assert "[layers.core]" in content
        assert "[layers.apps]" in content
        assert 'description = "Core libraries"' in content
        assert 'depends_on = ["core"]' in content

    def test_an_unrecognized_top_level_section_is_refused(self, tmp_project):
        """[layers] has a reader; an invented section has none, so it is refused."""
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            workspace_toml(TOML_WITH_LAYERS)
            + "\n[check]\nenabled = true\n"
        )

        with pytest.raises(WorkspaceError, match="check"):
            load_workspace(str(tmp_project))

    def test_comments_survive_roundtrip(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(TOML_WITH_LAYERS))

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)

        content = (ws_dir / WORKSPACE_FILE).read_text()
        assert "# Monorepo configuration" in content

    def test_unknown_toplevel_keys_are_refused(self, tmp_project):
        toml_text = """\
[[projects]]
path = "pkg"
name = "pkg"
releasable = false
"""
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            'custom_key = "hello"\nanother = 42\n\n' + workspace_toml(toml_text)
        )

        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_project))
        message = str(exc.value)
        assert "custom_key" in message
        assert "another" in message


class TestPreservesProjectData:
    """Project data including unknown per-project keys must be preserved."""

    def test_unknown_project_keys_are_refused(self, tmp_project):
        toml_text = """\
[[projects]]
path = "libs/foo"
name = "foo"
custom_globs = ["src/**"]
owner = "platform-team"
library = true
"""
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(toml_text))

        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_project))
        message = str(exc.value)
        assert "custom_globs" in message
        assert "owner" in message

    def test_declared_project_keys_roundtrip(self, tmp_project):
        toml_text = """\
[[projects]]
path = "libs/foo"
name = "foo"
description = "the foo library"
library = true
releasable = false
"""
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(toml_text))

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)

        reloaded = load_workspace(str(tmp_project))
        assert reloaded[0]["description"] == "the foo library"
        assert reloaded[0]["library"] is True

    def test_project_data_matches_after_roundtrip(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(TOML_WITH_LAYERS))

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)
        reloaded = load_workspace(str(tmp_project))

        assert reloaded == projects


class TestNewFileCreation:
    """When workspace.toml does not exist yet, save_workspace() creates it."""

    def test_creates_file_when_missing(self, tmp_project):
        projects = with_root_member([{"path": "pkg/a", "name": "a"}])
        save_workspace(str(tmp_project), projects)

        ws_file = tmp_project / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.exists()

        loaded = load_workspace(str(tmp_project))
        assert loaded == projects

    def test_creates_directory_when_missing(self, tmp_project):
        assert not (tmp_project / WORKSPACE_DIR).exists()
        make_workspace(str(tmp_project), [{"path": "x", "name": "x"}])
        assert (tmp_project / WORKSPACE_DIR).is_dir()

    def test_empty_projects_new_file(self, tmp_project):
        make_workspace(str(tmp_project), [])
        ws_file = tmp_project / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.exists()
        content = ws_file.read_text()
        assert "projects" in content
