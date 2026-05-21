"""Tests for description and test_only per-project fields in workspace.toml."""

from rlsbl.workspace import load_workspace, save_workspace, WORKSPACE_DIR, WORKSPACE_FILE


TOML_WITH_DESCRIPTION = """\
[[projects]]
path = "libs/core"
name = "core"
description = "Core models and shared utilities"

[[projects]]
path = "apps/web"
name = "web"
"""

TOML_WITH_TEST_ONLY = """\
[[projects]]
path = "libs/core"
name = "core"

[[projects]]
path = "tests/integration"
name = "integration"
test_only = true
"""

TOML_WITH_BOTH = """\
[[projects]]
path = "libs/core"
name = "core"
description = "Core models and shared utilities"
test_only = false

[[projects]]
path = "tests/integration"
name = "integration"
description = "End-to-end integration tests"
test_only = true
"""


class TestDescriptionField:
    """The description field is loaded and preserved on save."""

    def test_description_loaded(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_DESCRIPTION)

        projects = load_workspace(str(tmp_project))
        assert projects[0]["description"] == "Core models and shared utilities"

    def test_description_absent_when_not_set(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_DESCRIPTION)

        projects = load_workspace(str(tmp_project))
        assert "description" not in projects[1]

    def test_description_survives_roundtrip(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_DESCRIPTION)

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)
        reloaded = load_workspace(str(tmp_project))

        assert reloaded[0]["description"] == "Core models and shared utilities"
        assert "description" not in reloaded[1]

    def test_description_in_written_toml(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_DESCRIPTION)

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)

        content = (ws_dir / WORKSPACE_FILE).read_text()
        assert 'description = "Core models and shared utilities"' in content


class TestTestOnlyField:
    """The test_only field is loaded and preserved on save."""

    def test_test_only_loaded(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_TEST_ONLY)

        projects = load_workspace(str(tmp_project))
        assert projects[1]["test_only"] is True

    def test_test_only_absent_when_not_set(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_TEST_ONLY)

        projects = load_workspace(str(tmp_project))
        assert "test_only" not in projects[0]

    def test_test_only_survives_roundtrip(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_TEST_ONLY)

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)
        reloaded = load_workspace(str(tmp_project))

        assert reloaded[1]["test_only"] is True
        assert "test_only" not in reloaded[0]

    def test_test_only_in_written_toml(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_TEST_ONLY)

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)

        content = (ws_dir / WORKSPACE_FILE).read_text()
        assert "test_only = true" in content


class TestBothFieldsTogether:
    """Both fields work together on the same project entry."""

    def test_both_fields_loaded(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_BOTH)

        projects = load_workspace(str(tmp_project))

        assert projects[0]["description"] == "Core models and shared utilities"
        assert projects[0]["test_only"] is False
        assert projects[1]["description"] == "End-to-end integration tests"
        assert projects[1]["test_only"] is True

    def test_both_fields_survive_roundtrip(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_BOTH)

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)
        reloaded = load_workspace(str(tmp_project))

        assert reloaded == projects

    def test_both_fields_in_written_toml(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_BOTH)

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)

        content = (ws_dir / WORKSPACE_FILE).read_text()
        assert 'description = "Core models and shared utilities"' in content
        assert 'description = "End-to-end integration tests"' in content
        assert "test_only = false" in content
        assert "test_only = true" in content

    def test_field_order_in_output(self, tmp_project):
        """Extra keys are written in sorted order after path and name."""
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(TOML_WITH_BOTH)

        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)

        content = (ws_dir / WORKSPACE_FILE).read_text()
        # description comes before test_only alphabetically
        desc_pos = content.index("description")
        test_pos = content.index("test_only")
        assert desc_pos < test_pos
