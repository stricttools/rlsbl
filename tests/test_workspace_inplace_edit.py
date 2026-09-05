"""Tests for save_workspace() true in-place tomlkit editing.

save_workspace must edit the existing workspace.toml document surgically:
adding, removing, or field-editing a single [[projects]] or [[releasables]]
item must leave every other byte of the file untouched -- intra-table
comments and deliberate key order included.
"""

from conftest import with_root_member, workspace_toml, make_workspace
from rlsbl.workspace import (
    Releasable,
    WorkspaceProject,
    load_releasables,
    load_workspace,
    save_workspace,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)


# A workspace with deliberate (non-alphabetical) key order, inline comments,
# and standalone comment lines inside tables.
COMMENTED_TOML = """\
# Top-of-file banner comment

[[releasables]]
name = "alpha"  # the alpha group
tag_format = "{name}@v{version}"

[[releasables]]
name = "beta"

[[projects]]
# alpha-core is the shared library
path = "libs/alpha-core"
name = "alpha-core"
releasable = "alpha"
depends_on = ["beta-api"]  # a declared dependency

[[projects]]
path = "apps/alpha-web"
name = "alpha-web"
releasable = "alpha"

[[projects]]
path = "libs/beta-api"
name = "beta-api"
releasable = "beta"
"""


def _write_ws(root, text):
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(text))


def _read_ws(root):
    return (root / WORKSPACE_DIR / WORKSPACE_FILE).read_text()


class TestFieldEditPreservesRest:
    """Editing one field must change only that field's line."""

    def test_field_edit_only_intended_change(self, tmp_project):
        _write_ws(tmp_project, COMMENTED_TOML)
        projects = load_workspace(str(tmp_project))

        # Change alpha-web's library flag (add a new field to one table).
        for p in projects:
            if p["path"] == "apps/alpha-web":
                p["library"] = True

        save_workspace(str(tmp_project), projects)
        out = _read_ws(tmp_project)

        # Everything that was there is still there, verbatim.
        assert "# Top-of-file banner comment" in out
        assert 'name = "alpha"  # the alpha group' in out
        assert '# alpha-core is the shared library' in out
        assert 'depends_on = ["beta-api"]  # a declared dependency' in out
        # The intended change landed.
        assert "library = true" in out

    def test_untouched_save_is_byte_identical(self, tmp_project):
        _write_ws(tmp_project, COMMENTED_TOML)
        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects)
        # A no-op save must not perturb a single byte.
        assert _read_ws(tmp_project) == workspace_toml(COMMENTED_TOML)


class TestAddPreservesRest:
    """Adding a project appends a table and leaves the rest untouched."""

    def test_add_project_preserves_all_existing_bytes(self, tmp_project):
        _write_ws(tmp_project, COMMENTED_TOML)
        projects = load_workspace(str(tmp_project))
        projects.append(
            WorkspaceProject(
                {"path": "apps/beta-cli", "name": "beta-cli", "releasable": "beta"}
            )
        )
        save_workspace(str(tmp_project), projects)
        out = _read_ws(tmp_project)

        # The original content is a prefix-preserved subset: every original
        # comment/line survives.
        for line in COMMENTED_TOML.splitlines():
            assert line in out, f"lost line: {line!r}"
        # The new project is present.
        assert 'path = "apps/beta-cli"' in out
        assert 'name = "beta-cli"' in out


class TestRemovePreservesRest:
    """Removing a project deletes only its table."""

    def test_remove_project_preserves_comments(self, tmp_project):
        _write_ws(tmp_project, COMMENTED_TOML)
        projects = load_workspace(str(tmp_project))
        remaining = [p for p in projects if p["path"] != "apps/alpha-web"]
        save_workspace(str(tmp_project), remaining)
        out = _read_ws(tmp_project)

        # The removed table is gone.
        assert 'path = "apps/alpha-web"' not in out
        # Everything else -- comments, ordering, other tables -- survives.
        assert "# Top-of-file banner comment" in out
        assert 'name = "alpha"  # the alpha group' in out
        assert "# alpha-core is the shared library" in out
        assert 'depends_on = ["beta-api"]  # a declared dependency' in out
        assert 'path = "libs/beta-api"' in out
        assert 'path = "libs/alpha-core"' in out


class TestReleasableFieldEdit:
    """Field-editing a releasable (by name identity) preserves comments."""

    def test_releasable_tag_format_edit_in_place(self, tmp_project):
        _write_ws(tmp_project, COMMENTED_TOML)
        projects = load_workspace(str(tmp_project))
        releasables = load_releasables(str(tmp_project), projects)

        # Give beta a custom tag_format; leave alpha untouched.
        new_rels = []
        for r in releasables:
            if r.name == "beta":
                new_rels.append(Releasable(name="beta", tag_format="beta-v{version}"))
            else:
                new_rels.append(r)

        save_workspace(str(tmp_project), with_root_member(projects), releasables=new_rels)
        out = _read_ws(tmp_project)

        # alpha's inline comment and its explicit tag_format survive.
        assert 'name = "alpha"  # the alpha group' in out
        assert 'tag_format = "{name}@v{version}"' in out
        # beta gained its tag_format.
        assert 'tag_format = "beta-v{version}"' in out


class TestRoundTripStillLoads:
    """After edits the file must still parse and reload correctly."""

    def test_reload_after_edits(self, tmp_project):
        _write_ws(tmp_project, COMMENTED_TOML)
        projects = load_workspace(str(tmp_project))
        projects = [p for p in projects if p["path"] != "libs/beta-api"]
        projects.append(
            WorkspaceProject(
                {"path": "apps/new", "name": "new", "releasable": "alpha"}
            )
        )
        save_workspace(str(tmp_project), projects)

        reloaded = load_workspace(str(tmp_project))
        paths = {p["path"] for p in reloaded}
        assert "libs/beta-api" not in paths
        assert "apps/new" in paths
        # Releasables still parse.
        rels = load_releasables(str(tmp_project), reloaded)
        assert {r.name for r in rels} == {"alpha", "beta"}
