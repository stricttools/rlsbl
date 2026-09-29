"""workspace-unregistered finds an undeclared project at any depth.

workspace.toml is the explicit member list, and a project nobody declared
falls to whichever member encloses it: its changes are attributed to the
wrong changelog, a Go module is never released, and an npm or Python parent ships
it inside its own artifact. The check reads every manifest git lists, at any
depth. Manifests under a tests/, testdata/ or fixtures/ folder are test
inputs, and a scratch directory's contents are throwaway probes.
"""

import json

import pytest

from conftest import make_nested_workspace


def _run(root):
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=load_workspace(str(root)),
    )
    return app._check_defs["workspace-unregistered"].impl(ctx)


def _texts(result):
    return [p.text for p in result.problems]


def _go_mod(path, module):
    path.mkdir(parents=True, exist_ok=True)
    (path / "go.mod").write_text(f"module {module}\n\ngo 1.22\n")


def test_a_declared_nested_workspace_passes(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    assert _run(root).status == "pass", _texts(_run(root))


def test_an_undeclared_project_inside_a_member_is_flagged(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _go_mod(root / "draw" / "extra", "github.com/example/nested/draw/extra")
    result = _run(root)
    assert result.status == "fail"
    assert any(t.startswith("draw/extra:") for t in _texts(result))


def test_an_undeclared_project_two_levels_down_is_flagged(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _go_mod(root / "tools" / "extra", "github.com/example/nested/tools/extra")
    assert any(t.startswith("tools/extra:") for t in _texts(_run(root)))


def test_declaring_it_clears_the_finding(tmp_path):
    from conftest import make_workspace

    root = tmp_path / "ws"
    projects = make_nested_workspace(root, "go")
    _go_mod(root / "draw" / "extra", "github.com/example/nested/draw/extra")
    assert _run(root).status == "fail"
    members = [
        {"path": p["path"], "name": p["name"], "releasable": p["releasable"]}
        for p in projects if p["path"] != "."
    ] + [{"path": "draw/extra", "name": "extra", "releasable": "extra"}]
    rels = [
        {"name": "draw", "tag_format": "draw/v{version}"},
        {"name": "drawcmd", "tag_format": "draw/cmd/v{version}"},
        {"name": "kernel", "tag_format": "kernel/v{version}"},
        {"name": "vulkan", "tag_format": "kernel/vulkan/v{version}"},
        {"name": "extra", "tag_format": "draw/extra/v{version}"},
    ]
    make_workspace(root, members, releasables=rels)
    assert _run(root).status == "pass", _texts(_run(root))


@pytest.mark.parametrize("folder", ["tests", "testdata", "fixtures"])
def test_manifests_under_test_input_folders_are_not_projects(tmp_path, folder):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    fixture = root / "sdk" / folder / "app"
    fixture.mkdir(parents=True)
    (fixture / "package.json").write_text(json.dumps({"name": "fx", "version": "0.0.1"}))
    assert _run(root).status == "pass", _texts(_run(root))


def test_a_private_package_json_is_flagged_too(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    web = root / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({"name": "web", "private": True}))
    assert any(t.startswith("web:") for t in _texts(_run(root)))


def test_a_scratch_directory_at_a_member_root_is_skipped(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _go_mod(root / "draw" / "experiments" / "probe", "example.com/probe")
    assert _run(root).status == "pass", _texts(_run(root))


def test_rlsbl_owned_state_is_not_a_project(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    _go_mod(
        root / ".rlsbl-monorepo" / "releasables" / "draw" / "bases" / "draw" / "experiments",
        "example.com/base",
    )
    assert _run(root).status == "pass", _texts(_run(root))
