"""`rlsbl rewrite go-module-path` renames one module, never the modules nested under it.

A module path that begins with the renamed one is a DIFFERENT module when it
is declared by its own go.mod: renaming ``M/draw`` used to rename
``M/draw/cmd`` too, silently moving a module the caller never named. The
longest declared module path owns every token and import, so only what belongs
to the renamed module itself is rewritten; a nested module is renamed by an
invocation of its own.
"""

import re
import shlex

from conftest import make_nested_workspace, make_state_for_every_releasable, run_git
from rlsbl.commands.rewrite.go_module_path import apply_item, observe

M = "github.com/example/nested"


def _rename(root, old, new):
    preview = observe(root, old, new)
    for item in preview.items:
        apply_item(item, old, new)


def test_renaming_a_parent_module_leaves_a_nested_modules_path_alone(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _rename(root, f"{M}/draw", f"{M}/drawing")

    assert (root / "draw" / "go.mod").read_text().startswith(f"module {M}/drawing\n")
    child = (root / "draw" / "cmd" / "go.mod").read_text()
    assert child.startswith(f"module {M}/draw/cmd\n"), child
    # ...while the nested module's references to the renamed one move with it.
    assert f"require {M}/drawing v0.1.0" in child
    assert f'"{M}/drawing"' in (root / "draw" / "cmd" / "main.go").read_text()


def test_an_import_of_a_nested_modules_package_is_left_alone(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    (root / "kernel" / "uses.go").write_text(
        f'package kernel\n\nimport "{M}/kernel/vulkan"\n\nfunc G() int {{ return vulkan.F() }}\n'
    )
    _rename(root, f"{M}/kernel", f"{M}/kern")
    assert f'"{M}/kernel/vulkan"' in (root / "kernel" / "uses.go").read_text()


def test_the_nested_module_is_renamed_by_its_own_invocation(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _rename(root, f"{M}/draw/cmd", f"{M}/draw/cli")
    assert (root / "draw" / "cmd" / "go.mod").read_text().startswith(
        f"module {M}/draw/cli\n"
    )
    assert (root / "draw" / "go.mod").read_text().startswith(f"module {M}/draw\n")


def test_extracts_printed_rewrite_severs_the_edge_and_spares_nested_modules(
    tmp_path, monkeypatch,
):
    """The fix extract prints for an inbound Go edge, executed as printed."""
    import rlsbl
    from rlsbl.commands.monorepo.extract_cmd import ExtractError, resolve_departure

    import pytest

    # Resolution asks for git-filter-repo on PATH; nothing is filtered here.
    monkeypatch.setattr(
        "rlsbl.commands.monorepo.extract_cmd.require_filter_repo",
        lambda: "/usr/bin/git-filter-repo",
    )
    root = tmp_path / "ws"
    make_nested_workspace(root, "go", commit=False)
    # vulkan leaves with kernel: one releasable. app stays and requires kernel.
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    text = toml.read_text().replace('releasable = "vulkan"', 'releasable = "kernel"')
    text = text.replace('[[releasables]]\nname = "vulkan"\ntag_format = "kernel/vulkan/v{version}"\n', "")
    toml.write_text(text)
    (root / "app").mkdir()
    (root / "app" / "go.mod").write_text(
        f"module {M}/app\n\ngo 1.22\n\nrequire {M}/kernel v0.1.0\n"
    )
    (root / "app" / "app.go").write_text(
        f'package app\n\nimport "{M}/kernel"\n\nfunc A() int {{ return kernel.F() }}\n'
    )
    from conftest import make_workspace
    from rlsbl.workspace import load_releasables, load_workspace

    members = [
        {"path": p["path"], "name": p["name"], "releasable": p["releasable"]}
        for p in load_workspace(str(root)) if p["path"] != "."
    ] + [{"path": "app", "name": "app", "releasable": "app", "depends_on": ["kernel"]}]
    rels = [
        {"name": r.name, "tag_format": r.tag_format}
        for r in load_releasables(str(root))
    ] + [{"name": "app", "tag_format": "app/v{version}"}]
    make_workspace(root, members, releasables=rels)
    make_state_for_every_releasable(root)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "fixture")

    with pytest.raises(ExtractError) as exc:
        resolve_departure(str(root), "kernel", str(tmp_path / "out"), delete_with_rm=True)
    message = str(exc.value)
    printed = re.search(r"rlsbl rewrite go-module-path --from-module \S+ --to-module", message)
    assert printed, message

    # Execute the printed command, with the placeholder filled in.
    command = printed.group(0) + " github.com/example/kernel"
    monkeypatch.chdir(root)
    result = rlsbl.app.test(shlex.split(command)[1:])
    assert result.exit_code == 0, result.stderr
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "sever app from kernel")

    # The nested module kept its path.
    assert (root / "kernel" / "vulkan" / "go.mod").read_text().startswith(
        f"module {M}/kernel/vulkan\n"
    )
    assert f"require github.com/example/kernel v0.1.0" in (
        root / "app" / "go.mod"
    ).read_text()
    # The other printed fix: the declared edge goes from depends_on.
    assert "remove 'kernel' from depends_on" in message
    toml.write_text(toml.read_text().replace('depends_on = ["kernel"]\n', ""))
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "app no longer declares kernel")
    departure = resolve_departure(
        str(root), "kernel", str(tmp_path / "out"), delete_with_rm=True,
    )
    assert {m.name for m in departure.members} == {"kernel", "vulkan"}
