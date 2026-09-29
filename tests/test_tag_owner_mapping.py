"""Reconcile and scrub find a tag's owner by its scheme, never by a name prefix.

Rewriting a Release document needs the notes and the release commit of the
project the tag belongs to. The owner used to be found by a ``{name}@`` prefix,
so a Go path-style tag (``draw/cmd/v0.1.0``) matched nobody and the notes were
taken from whichever project's CHANGELOG.md happened to carry the version. A
tag is now its owner's by the owner's exact scheme, and a tag no scheme renders
is refused rather than guessed at.
"""

import pytest

from conftest import make_nested_workspace
from rlsbl.commands.release_reconcile import (
    _notes_for_tag,
    _release_record_dir_for_tag,
    tag_scheme_index,
    update_github_releases,
)
from rlsbl.errors import RlsblError


class _Ctx:
    def __init__(self, root):
        self.config = {}
        self.project_root = root
        self.workspace_root = root


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    projects = make_nested_workspace(root, "go")
    for member in ("draw", "draw/cmd", "kernel", "kernel/vulkan"):
        (root / member / "CHANGELOG.md").write_text("# Changelog\n")
    return root, projects


def _notes(root, projects, tag, version):
    return _notes_for_tag(
        tag, version, ctx=_Ctx(str(root)), project_root=str(root),
        workspace_projects=projects,
        tag_schemes=tag_scheme_index(str(root), projects),
        extract_entry=lambda path, _version: path,
    )


def test_a_go_path_tag_takes_its_own_members_notes(ws):
    root, projects = ws
    notes = _notes(root, projects, "draw/cmd/v0.1.0", "0.1.0")
    assert notes == str(root / "draw" / "cmd" / "CHANGELOG.md")


def test_a_nested_members_tag_is_not_its_parents(ws):
    root, projects = ws
    notes = _notes(root, projects, "kernel/vulkan/v0.2.0", "0.2.0")
    assert notes == str(root / "kernel" / "vulkan" / "CHANGELOG.md")


def test_a_path_tags_release_record_is_its_own_releasables(ws):
    root, projects = ws
    found = _release_record_dir_for_tag(
        "kernel/vulkan/v0.1.0", ctx=_Ctx(str(root)), project_root=str(root),
        tag_schemes=tag_scheme_index(str(root), projects),
    )
    assert found == str(
        root / ".rlsbl-monorepo" / "releasables" / "vulkan" / "releases"
    )


def test_a_tag_no_scheme_renders_is_refused_naming_the_declaration(ws):
    root, projects = ws
    with pytest.raises(RlsblError) as exc:
        _notes(root, projects, "stray/v1.0.0", "1.0.0")
    message = str(exc.value)
    assert "will not guess" in message
    assert "rlsbl transition record --non-version-tag stray/v1.0.0" in message


def test_declaring_the_tag_a_non_version_tag_clears_the_refusal(ws, monkeypatch):
    import rlsbl

    root, projects = ws
    calls = []

    def gh(args, **kwargs):
        calls.append(list(args))
        return ""

    def update():
        return update_github_releases(
            [{"refname": "refs/tags/stray/v1.0.0"}], ctx=_Ctx(str(root)),
            project_root=str(root), workspace_projects=projects,
            tag_schemes=tag_scheme_index(str(root), projects),
            gh=gh, gh_installed=lambda: True, gh_auth=lambda: True,
            extract_entry=lambda _path, _version: "notes",
        )

    with pytest.raises(RlsblError) as exc:
        update()
    assert "--non-version-tag stray/v1.0.0" in str(exc.value)

    # Apply the printed fix, as written.
    monkeypatch.chdir(root)
    result = rlsbl.app.test([
        "transition", "record", "--non-version-tag", "stray/v1.0.0",
        "--reason", "a scratch tag, never a release",
    ])
    assert result.exit_code == 0, result.stderr
    assert update() == 0
    assert not [c for c in calls if c[:2] in (["release", "create"], ["release", "edit"])]
