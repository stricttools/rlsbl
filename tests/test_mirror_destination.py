"""The mirror destination is a RELEASABLE-level key, not a per-project one.

A mirror mirrors one subtree, and the unit that owns a version, a changelog and
a tag scheme is the releasable -- so the binding that says "this unit is
mirrored at <url>" belongs on the releasable. The per-project spelling is a
loader error carrying the migration edit, and a releasable with more than one
member cannot declare one at all.
"""

import pytest

from conftest import workspace_toml

from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    Releasable,
    load_releasables,
    load_workspace,
    mirror_remote_for,
    save_workspace,
)


def _write(root, body, **kwargs):
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(body, **kwargs))


class TestReleasableCarriesTheBinding:
    def test_loaded_from_the_releasables_section(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"
subtree_remote = "https://example.com/o/mylib.git"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
""")
        [rel] = [
            r for r in load_releasables(str(tmp_path)) if r.name == "mylib"
        ]
        assert rel.subtree_remote == "https://example.com/o/mylib.git"
        assert rel.is_mirrored

    def test_absent_binding_is_the_empty_string(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
""")
        [rel] = [
            r for r in load_releasables(str(tmp_path)) if r.name == "mylib"
        ]
        assert rel.subtree_remote == ""
        assert not rel.is_mirrored

    def test_non_string_binding_is_an_error(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"
subtree_remote = 7

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
""")
        with pytest.raises(WorkspaceError, match="subtree_remote must be a string"):
            load_releasables(str(tmp_path))

    def test_roundtrips_through_save_workspace(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"
subtree_remote = "https://example.com/o/mylib.git"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
""")
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        save_workspace(str(tmp_path), projects, releasables=releasables)

        reloaded = load_releasables(str(tmp_path))
        assert {r.name: r.subtree_remote for r in reloaded}["mylib"] == (
            "https://example.com/o/mylib.git"
        )

    def test_absent_binding_is_not_written_back(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
""")
        projects = load_workspace(str(tmp_path))
        save_workspace(
            str(tmp_path), projects,
            releasables=load_releasables(str(tmp_path), projects),
        )
        text = (tmp_path / WORKSPACE_DIR / WORKSPACE_FILE).read_text()
        assert "subtree_remote" not in text


class TestPerProjectKeyIsRefused:
    def test_project_level_binding_is_a_loader_error(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
subtree_remote = "https://example.com/o/mylib.git"
""")
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "subtree_remote" in message
        # It names where the key moved and how to make the edit.
        assert "[[releasables]]" in message
        assert "migration script" in message

    def test_the_remedy_is_pasteable_toml(self, tmp_path):
        """The suggested line must be TOML, not a Python repr.

        The remedy is meant to be copied into workspace.toml as it stands, and
        TOML strings are double-quoted.
        """
        _write(tmp_path, """\
[[releasables]]
name = "mylib"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
subtree_remote = "https://example.com/o/mylib.git"
""")
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        assert (
            'subtree_remote = "https://example.com/o/mylib.git"'
            in str(exc.value)
        ), str(exc.value)

    def test_the_error_names_the_offending_member(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
subtree_remote = "https://example.com/o/mylib.git"
""")
        with pytest.raises(WorkspaceError, match="mylib"):
            load_workspace(str(tmp_path))


class TestOneSubtreePerMirror:
    def test_multi_member_releasable_with_a_mirror_is_an_error(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "core"
subtree_remote = "https://example.com/o/core.git"

[[projects]]
path = "packages/a"
name = "a"
releasable = "core"

[[projects]]
path = "packages/b"
name = "b"
releasable = "core"
""")
        with pytest.raises(WorkspaceError) as exc:
            load_releasables(str(tmp_path))
        message = str(exc.value)
        assert "ONE subtree" in message
        assert "a" in message and "b" in message

    def test_single_member_releasable_with_a_mirror_loads(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "core"
subtree_remote = "https://example.com/o/core.git"

[[projects]]
path = "packages/a"
name = "a"
releasable = "core"
""")
        assert any(r.is_mirrored for r in load_releasables(str(tmp_path)))


class TestMirrorRemoteFor:
    def test_resolves_through_the_members_releasable(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"
subtree_remote = "https://example.com/o/mylib.git"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"
""")
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        member = next(p for p in projects if p.name == "mylib")
        assert mirror_remote_for(member, releasables) == (
            "https://example.com/o/mylib.git"
        )

    def test_a_member_outside_every_releasable_has_no_mirror(self, tmp_path):
        _write(tmp_path, """\
[[releasables]]
name = "mylib"
subtree_remote = "https://example.com/o/mylib.git"

[[projects]]
path = "packages/mylib"
name = "mylib"
releasable = "mylib"

[[projects]]
path = "tools/dev"
name = "dev"
releasable = false
""")
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        member = next(p for p in projects if p.name == "dev")
        assert mirror_remote_for(member, releasables) == ""


def test_releasable_declares_binding_is_independent_of_tag_format():
    """The two optional releasable keys do not interfere."""
    rel = Releasable(name="x", subtree_remote="https://example.com/o/x.git")
    assert rel.is_mirrored
    assert not rel.declares_tag_format
