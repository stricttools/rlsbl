"""Workspace member paths are accepted in exactly one spelling.

A member path is repository-relative and '/'-separated, with no '.', '..' or
empty segment, no leading or trailing '/', and no backslash; the repository
root is '.'. Any other spelling is refused at load, naming the spelling to
write, rather than tidied into one: a tidied spelling is a guess about what
the operator meant, and `draw/./cmd` loaded unnormalized used to own nothing.
"""

import pytest

from conftest import workspace_toml
from rlsbl.errors import WorkspaceError
from rlsbl.ownership import canonical_member_path, member_path_problem
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE, load_workspace, save_workspace


def write_raw(root, body):
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(body)


def member_body(path, name="drawcmd"):
    return (
        '[[projects]]\npath = "draw"\nname = "draw"\nreleasable = false\n\n'
        f'[[projects]]\npath = "{path}"\nname = "{name}"\nreleasable = false\n'
    )


#: Non-canonical spellings of a member at draw/cmd, with the spelling to write.
FIXABLE = [
    ("draw/./cmd", "draw/cmd"),
    ("draw//cmd", "draw/cmd"),
    ("./draw/cmd", "draw/cmd"),
    ("draw/cmd/", "draw/cmd"),
    ("draw/cmd/x/..", "draw/cmd"),
    ("draw\\\\cmd", "draw/cmd"),
    (" draw/cmd", "draw/cmd"),
]

#: Spellings that name no directory inside the repository.
OUTSIDE = ["../x", "/abs/draw/cmd", "draw/../../x", ".."]


@pytest.mark.parametrize("spelling, canonical", FIXABLE)
def test_a_non_canonical_member_path_is_refused_naming_the_spelling(
    tmp_path, spelling, canonical,
):
    write_raw(tmp_path, workspace_toml(member_body(spelling)))
    with pytest.raises(WorkspaceError) as exc:
        load_workspace(str(tmp_path))
    message = str(exc.value)
    assert "projects[1] ('drawcmd')" in message
    assert "not a canonical member path" in message
    assert f"Write it as '{canonical}'" in message


@pytest.mark.parametrize("spelling, canonical", FIXABLE)
def test_writing_the_named_spelling_clears_the_refusal(
    tmp_path, spelling, canonical,
):
    write_raw(tmp_path, workspace_toml(member_body(spelling)))
    with pytest.raises(WorkspaceError):
        load_workspace(str(tmp_path))
    # Apply the printed fix: write the spelling the message names.
    write_raw(tmp_path, workspace_toml(member_body(canonical)))
    projects = load_workspace(str(tmp_path))
    assert "draw/cmd" in [p["path"] for p in projects]


@pytest.mark.parametrize("spelling", OUTSIDE)
def test_a_path_outside_the_repository_is_refused_with_no_spelling(
    tmp_path, spelling,
):
    write_raw(tmp_path, workspace_toml(member_body(spelling)))
    with pytest.raises(WorkspaceError) as exc:
        load_workspace(str(tmp_path))
    message = str(exc.value)
    assert "not a canonical member path" in message
    assert "Write it as" not in message
    assert "inside the repository" in message


@pytest.mark.parametrize("spelling", ["", "./", "./."])
def test_a_root_member_spelled_otherwise_is_refused_naming_dot(
    tmp_path, spelling,
):
    write_raw(tmp_path, workspace_toml(
        f'[[projects]]\npath = "{spelling}"\nname = "root"\n'
        'dev_only = true\nreleasable = false\n',
        root_member="",
    ))
    with pytest.raises(WorkspaceError) as exc:
        load_workspace(str(tmp_path))
    assert "Write it as '.'" in str(exc.value)


def test_canonical_paths_load_unchanged(tmp_path):
    write_raw(tmp_path, workspace_toml(member_body("draw/cmd")))
    projects = load_workspace(str(tmp_path))
    assert sorted(p["path"] for p in projects) == [".", "draw", "draw/cmd"]


def test_the_validator_answers_the_canonical_spelling():
    assert member_path_problem("draw/cmd") is None
    assert member_path_problem(".") is None
    assert canonical_member_path("draw//cmd/") == "draw/cmd"
    assert canonical_member_path("") == "."
    assert canonical_member_path("../x") is None


def test_save_workspace_refuses_to_write_a_non_canonical_path(tmp_path):
    with pytest.raises(WorkspaceError) as exc:
        save_workspace(str(tmp_path), [
            {"path": ".", "name": "root", "dev_only": True, "releasable": False},
            {"path": "draw/", "name": "draw", "releasable": False},
        ], releasables=[])
    assert "Write it as 'draw'" in str(exc.value)


def _npm_package(base, subdir):
    import json
    import os

    os.makedirs(os.path.join(str(base), subdir), exist_ok=True)
    with open(os.path.join(str(base), subdir, "package.json"), "w") as f:
        json.dump({"name": "test-" + subdir.replace("/", "-"), "version": "0.1.0"}, f)


@pytest.mark.parametrize("spelling, canonical", [
    ("pkg/", "pkg"), ("./pkg", "pkg"), ("pkg//inner", "pkg/inner"),
])
def test_monorepo_add_refuses_the_same_spellings(
    mock_git_repo, capsys, spelling, canonical,
):
    from rlsbl.commands.monorepo import _cmd_add, _cmd_init

    _cmd_init({"root-dev-node": True}, project_root=".")
    _npm_package(mock_git_repo, canonical)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        _cmd_add([spelling], {"releasable": "false"}, project_root=".",
                 dry_run=True)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not a canonical member path" in err
    assert f"Write it as '{canonical}'" in err
    # Apply the printed fix: pass the spelling the message names.
    _cmd_add([canonical], {"releasable": "false"}, project_root=".",
             dry_run=True)
    assert f"Would add project" in capsys.readouterr().out


def test_monorepo_absorb_refuses_a_non_canonical_destination(tmp_path):
    from rlsbl.commands.monorepo.absorb_cmd import AbsorbError, resolve_arrival

    with pytest.raises(AbsorbError) as exc:
        resolve_arrival(
            str(tmp_path), str(tmp_path / "src"), "vendor/widget/",
            name=None, registry_name=None, releasable_name=None,
            tag_format=None, delete_with_rm=False,
        )
    assert "Write it as 'vendor/widget'" in str(exc.value)
