"""Unknown keys are refused at load on every policed configuration surface.

The policed surfaces are workspace.toml's member tables, its releasable
tables and its top level, plus a standalone repository's
``.rlsbl/releasable.toml``. On each of them a key rlsbl does not know is a
hard error naming the surface, the key and the file -- a key that was
tolerated silently was written but never read, so the file said something the
tools never did.

``.rlsbl/config.json`` is deliberately NOT policed here; see the "Policed
configuration surfaces" section of docs/configuration.md.
"""

import os

import pytest

from conftest import workspace_toml

from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    MEMBER_KEYS,
    STANDALONE_RELEASABLE_KEYS,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    WORKSPACE_TOP_LEVEL_KEYS,
    Releasable,
    WorkspaceProject,
    load_releasables,
    load_standalone_releasable,
    save_standalone_releasable,
    load_workspace,
    save_workspace,
)


def _write_workspace(root, text):
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(text)


def _write_standalone(root, text):
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "releasable.toml").write_text(text)


# ---------------------------------------------------------------------------
# The member-table known-key set is ONE declared authority
# ---------------------------------------------------------------------------


class TestMemberKeysIsTheOneAuthority:
    """MEMBER_KEYS is what the loader refuses against AND what the accessors read."""

    #: Properties that derive an answer from other keys rather than reading one
    #: of their own. Everything else on WorkspaceProject reads a member key.
    DERIVED_ACCESSORS = {"dev_node", "is_releasable"}

    def test_accessor_surface_is_exactly_the_key_set(self):
        accessors = {
            name for name, value in vars(WorkspaceProject).items()
            if isinstance(value, property)
        }
        assert accessors - self.DERIVED_ACCESSORS == set(MEMBER_KEYS)

    def test_every_declared_key_loads(self, tmp_project):
        body = (
            '[[projects]]\n'
            'path = "p"\n'
            'name = "p"\n'
            'library = true\n'
            'dev_only = true\n'
            'releasable = false\n'
            'depends_on = []\n'
            'import_name = "p_pkg"\n'
            'registry_name = "p-pkg"\n'
            'description = "a member"\n'
            'test_only = false\n'
            'lint_allow = []\n'
        )
        declared = {
            line.split(" = ")[0] for line in body.splitlines()
            if " = " in line
        }
        assert declared == set(MEMBER_KEYS), sorted(declared ^ set(MEMBER_KEYS))
        _write_workspace(tmp_project, workspace_toml(body))
        names = {p["name"] for p in load_workspace(str(tmp_project))}
        assert "p" in names


# ---------------------------------------------------------------------------
# Refusal, per policed surface
# ---------------------------------------------------------------------------


class TestMemberTableRefusal:
    def test_unknown_member_key_is_refused(self, tmp_project):
        _write_workspace(tmp_project, workspace_toml(
            '[[projects]]\npath = "p"\nname = "p"\nreleasable = false\n'
            'custom_field = "hello"\n'
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_project))
        message = str(exc.value)
        assert "custom_field" in message
        assert "member" in message
        assert WORKSPACE_FILE in message

    def test_a_misspelled_known_key_is_refused(self, tmp_project):
        _write_workspace(tmp_project, workspace_toml(
            '[[projects]]\npath = "p"\nname = "p"\nreleasable = false\n'
            'registryname = "p-pkg"\n'
        ))
        with pytest.raises(WorkspaceError, match="registryname"):
            load_workspace(str(tmp_project))


class TestReleasableTableRefusal:
    def test_unknown_releasable_key_is_refused_by_load_workspace(self, tmp_project):
        _write_workspace(tmp_project, (
            '[[releasables]]\nname = "core"\nfuture_key = "keep me"\n\n'
            '[[projects]]\npath = "p"\nname = "p"\nreleasable = "core"\n\n'
            '[[projects]]\npath = "."\nname = "root"\n'
            'dev_only = true\nreleasable = false\n'
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_project))
        message = str(exc.value)
        assert "future_key" in message
        assert "releasable" in message
        assert WORKSPACE_FILE in message

    def test_unknown_releasable_key_is_refused_by_load_releasables(self, tmp_project):
        _write_workspace(tmp_project, (
            '[[releasables]]\nname = "core"\nfuture_key = "keep me"\n\n'
            '[[projects]]\npath = "p"\nname = "p"\nreleasable = "core"\n\n'
            '[[projects]]\npath = "."\nname = "root"\n'
            'dev_only = true\nreleasable = false\n'
        ))
        with pytest.raises(WorkspaceError, match="future_key"):
            load_releasables(str(tmp_project))


class TestTopLevelRefusal:
    def test_unknown_top_level_key_is_refused(self, tmp_project):
        # A top-level key precedes every section header, or TOML would read it
        # as a key of the table above it.
        _write_workspace(tmp_project, 'version = "3"\n\n' + workspace_toml(
            '[[projects]]\npath = "p"\nname = "p"\nreleasable = false\n'
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_project))
        message = str(exc.value)
        assert "version" in message
        assert "top level" in message
        assert WORKSPACE_FILE in message

    def test_the_declared_top_level_keys_are_the_sections_with_readers(self):
        assert set(WORKSPACE_TOP_LEVEL_KEYS) == {
            "projects", "releasables", "layers",
        }

    def test_the_layers_section_still_loads(self, tmp_project):
        _write_workspace(tmp_project, workspace_toml(
            '[[projects]]\npath = "p"\nname = "p"\nreleasable = false\n'
        ) + '\n[layers]\norder = ["core"]\n')
        assert {p["name"] for p in load_workspace(str(tmp_project))} == {
            "p", "root",
        }


class TestStandaloneReleasableFileRefusal:
    def test_unknown_key_is_refused(self, tmp_project):
        _write_standalone(tmp_project, 'name = "solo"\nowner = "alice"\n')
        with pytest.raises(WorkspaceError) as exc:
            load_standalone_releasable(str(tmp_project))
        message = str(exc.value)
        assert "owner" in message
        assert "releasable.toml" in message

    def test_the_declared_keys_load(self, tmp_project):
        _write_standalone(
            tmp_project, 'name = "solo"\ntag_format = "v{version}"\n'
        )
        rel = load_standalone_releasable(str(tmp_project))
        assert rel.name == "solo"
        assert set(STANDALONE_RELEASABLE_KEYS) == {"name", "tag_format"}


# ---------------------------------------------------------------------------
# dev_node: deleted outright, with a dedicated remedy
# ---------------------------------------------------------------------------


class TestDevNodeKeyDeleted:
    def test_dev_node_key_is_refused_with_the_two_key_remedy(self, tmp_project):
        _write_workspace(tmp_project, workspace_toml(
            '[[projects]]\npath = "p"\nname = "p"\ndev_node = true\n'
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_project))
        message = str(exc.value)
        assert "dev_node" in message
        assert "dev_only = true" in message
        assert "releasable = false" in message

    def test_the_remedy_applied_verbatim_clears_the_error(self, tmp_project):
        _write_workspace(tmp_project, workspace_toml(
            '[[projects]]\npath = "p"\nname = "p"\n'
            'dev_only = true\nreleasable = false\n'
        ))
        member = next(
            p for p in load_workspace(str(tmp_project)) if p["name"] == "p"
        )
        assert member.dev_only is True
        assert member.dev_node is True
        assert member.is_releasable is False

    def test_dev_node_is_not_a_member_key(self):
        assert "dev_node" not in MEMBER_KEYS


# ---------------------------------------------------------------------------
# The save path can never write a key the loader would then refuse
# ---------------------------------------------------------------------------


class TestSaveStripsRuntimeBookkeeping:
    def test_a_runtime_injected_key_is_not_persisted(self, tmp_project):
        member = WorkspaceProject({
            "path": "p", "name": "p", "releasable": False,
        })
        root = WorkspaceProject({
            "path": ".", "name": "root", "dev_only": True, "releasable": False,
        })
        # What `monorepo sync` injects into a member at runtime.
        member["_ci_files"] = ["ci-p.yml"]
        member["_ci_docs"] = [("ci-p", {})]
        root["_root_publisher"] = True

        save_workspace(str(tmp_project), [member, root], releasables=[])
        text = (tmp_project / WORKSPACE_DIR / WORKSPACE_FILE).read_text()
        assert "_ci_files" not in text
        assert "_ci_docs" not in text
        assert "_root_publisher" not in text
        # ...and the file reads back.
        assert {p["name"] for p in load_workspace(str(tmp_project))} == {"p", "root"}

    def test_saving_an_unknown_member_key_is_refused(self, tmp_project):
        member = WorkspaceProject({
            "path": "p", "name": "p", "releasable": False, "custom": 1,
        })
        with pytest.raises(WorkspaceError, match="custom"):
            save_workspace(str(tmp_project), [member], releasables=[])

    def test_load_save_reload_is_clean_on_every_workspace_surface(self, tmp_project):
        _write_workspace(tmp_project, (
            '[[releasables]]\nname = "core"\ntag_format = "v{version}"\n\n'
            '[[projects]]\npath = "p"\nname = "p"\nreleasable = "core"\n\n'
            '[[projects]]\npath = "."\nname = "root"\n'
            'dev_only = true\nreleasable = false\n'
        ))
        root = str(tmp_project)
        projects = load_workspace(root)
        releasables = load_releasables(root, projects)
        save_workspace(root, projects, releasables=releasables)
        reloaded = load_workspace(root)
        assert {p["name"] for p in reloaded} == {"p", "root"}
        assert [r.name for r in load_releasables(root, reloaded)] == ["core"]


# ---------------------------------------------------------------------------
# The standalone releasable file's tag format: explicit or absent
# ---------------------------------------------------------------------------


class TestStandaloneTagFormatIsExplicitOrAbsent:
    def test_absence_is_carried_not_invented(self, tmp_project):
        _write_standalone(tmp_project, 'name = "solo"\n')
        rel = load_standalone_releasable(str(tmp_project))
        assert rel.declares_tag_format is False

    def test_a_declared_format_is_carried(self, tmp_project):
        _write_standalone(
            tmp_project, 'name = "solo"\ntag_format = "{name}@v{version}"\n'
        )
        rel = load_standalone_releasable(str(tmp_project))
        assert rel.declares_tag_format is True
        assert rel.tag_format == "{name}@v{version}"

    def test_absence_round_trips_through_save(self, tmp_project):
        _write_standalone(tmp_project, 'name = "solo"\n')
        rel = load_standalone_releasable(str(tmp_project))
        save_standalone_releasable(str(tmp_project), rel)
        text = (tmp_project / ".rlsbl" / "releasable.toml").read_text()
        assert "tag_format" not in text
        assert load_standalone_releasable(str(tmp_project)).declares_tag_format is False

    def test_a_declared_format_round_trips_through_save(self, tmp_project):
        rel = Releasable(name="solo", tag_format="v{version}")
        os.makedirs(os.path.join(str(tmp_project), ".rlsbl"), exist_ok=True)
        save_standalone_releasable(str(tmp_project), rel)
        reloaded = load_standalone_releasable(str(tmp_project))
        assert reloaded.tag_format == "v{version}"

    def test_a_standalone_repo_still_tags_bare_versions(self, tmp_project):
        """An absent format resolves to the standalone scheme, not the workspace one."""
        from rlsbl.workspace import create_standalone_releasable

        _write_standalone(tmp_project, 'name = "solo"\n')
        rel = create_standalone_releasable(str(tmp_project))
        assert rel.effective_tag_format == "v{version}"
