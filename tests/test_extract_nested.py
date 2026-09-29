"""Extract and mirror refuse a member with a nested member that is not leaving.

Extracting ``tools`` filtered and then deleted its whole directory, which
contains ``tools/lint``, a member of another releasable that stays: its files
were carried into the new repository and deleted from the source, with no
mention in the plan. A mirror of such a member publishes the nested member's
tree the same way.
"""

import pytest

from conftest import make_nested_workspace, make_state_for_every_releasable, run_git
from rlsbl.commands.monorepo.extract_cmd import ExtractError, resolve_departure


@pytest.fixture(autouse=True)
def _filter_repo_on_path(monkeypatch):
    """Stand in for git-filter-repo's PATH lookup.

    These tests stop at resolution -- nothing is filtered -- and the sandbox
    runner cannot resolve the real executable, which resolution asks for.
    """
    monkeypatch.setattr(
        "rlsbl.commands.monorepo.extract_cmd.require_filter_repo",
        lambda: "/usr/bin/git-filter-repo",
    )


def _go_workspace(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    make_state_for_every_releasable(root)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "release state")
    return root


def _set_releasable(root, member_name, releasable):
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    lines = toml.read_text().splitlines()
    out, current = [], None
    for line in lines:
        if line.startswith("name = "):
            current = line.split('"')[1]
        if current == member_name and line.startswith("releasable = "):
            line = f'releasable = "{releasable}"'
        out.append(line)
    toml.write_text("\n".join(out) + "\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", f"{member_name} joins {releasable}")


def test_extracting_a_parent_whose_nested_member_stays_is_refused(tmp_path):
    root = _go_workspace(tmp_path)
    with pytest.raises(ExtractError) as exc:
        resolve_departure(str(root), "draw", str(tmp_path / "out"), delete_with_rm=True)
    message = str(exc.value)
    assert "member 'drawcmd' (draw/cmd) lies inside departing member 'draw'" in message
    assert "stays behind (releasable 'drawcmd')" in message
    assert "Extract 'drawcmd' first" in message
    assert "move it into releasable 'draw'" in message


def test_moving_the_nested_member_into_the_releasable_clears_the_refusal(tmp_path):
    root = _go_workspace(tmp_path)
    _set_releasable(root, "drawcmd", "draw")
    departure = resolve_departure(
        str(root), "draw", str(tmp_path / "out"), delete_with_rm=True,
    )
    assert {m.name for m in departure.members} == {"draw", "drawcmd"}


def test_extracting_the_nested_member_alone_is_allowed(tmp_path):
    root = _go_workspace(tmp_path)
    departure = resolve_departure(
        str(root), "drawcmd", str(tmp_path / "out"), delete_with_rm=True,
    )
    assert [m.name for m in departure.members] == ["drawcmd"]


def test_a_nested_member_outside_every_releasable_is_refused_too(tmp_path):
    root = _go_workspace(tmp_path)
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    toml.write_text(toml.read_text().replace('releasable = "drawcmd"', "releasable = false"))
    toml.write_text(toml.read_text().replace(
        '[[releasables]]\nname = "drawcmd"\ntag_format = "draw/cmd/v{version}"\n', "",
    ))
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "drawcmd leaves versioning")
    with pytest.raises(ExtractError) as exc:
        resolve_departure(str(root), "draw", str(tmp_path / "out"), delete_with_rm=True)
    assert "stays behind (outside every releasable)" in str(exc.value)
    # Apply the named fix: move it into the departing releasable.
    _set_releasable(root, "drawcmd", "draw")
    departure = resolve_departure(
        str(root), "draw", str(tmp_path / "out"), delete_with_rm=True,
    )
    assert {m.name for m in departure.members} == {"draw", "drawcmd"}


# ---------------------------------------------------------------------------
# Mirrors
# ---------------------------------------------------------------------------


def _mirrored(tmp_path):
    root = _go_workspace(tmp_path)
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    toml.write_text(toml.read_text().replace(
        'name = "draw"\ntag_format = "draw/v{version}"',
        'name = "draw"\ntag_format = "draw/v{version}"\n'
        'subtree_remote = "git@github.com:example/draw.git"',
    ))
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "mirror draw")
    return root


def test_mirroring_a_member_that_encloses_another_is_refused(tmp_path, monkeypatch):
    import rlsbl

    root = _mirrored(tmp_path)
    monkeypatch.chdir(root)
    result = rlsbl.app.test(["monorepo", "mirror", "draw", "--dry-run"])
    assert result.exit_code == 1
    assert "encloses the workspace member(s) 'draw/cmd'" in result.stderr
    assert "drop subtree_remote" in result.stderr


def test_a_release_of_that_member_is_refused_before_anything_is_written(tmp_path):
    from rlsbl.commands.release.validate import (
        ReleaseValidationError,
        resolve_monorepo_context,
    )

    root = _mirrored(tmp_path)
    with pytest.raises(ReleaseValidationError) as exc:
        resolve_monorepo_context(str(root), str(root / "draw"), lambda _msg: None)
    assert "encloses the workspace member(s) 'draw/cmd'" in str(exc.value)


def test_moving_the_nested_member_out_clears_the_mirror_refusal(tmp_path):
    from rlsbl.commands.release.validate import resolve_monorepo_context

    root = _mirrored(tmp_path)
    run_git(root, "mv", "draw/cmd", "drawcmd")
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    toml.write_text(toml.read_text().replace('path = "draw/cmd"', 'path = "drawcmd"'))
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "move drawcmd out of draw")
    names = resolve_monorepo_context(str(root), str(root / "draw"), lambda _msg: None)
    assert names[0] == "draw"
