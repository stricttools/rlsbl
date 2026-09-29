"""A nested Python member's workspace sources belong in the root pyproject.toml.

uv refuses a ``{ workspace = true }`` source declared in a member nested
inside another member (probed with uv 0.9.17: "references a workspace in
`tool.uv.sources` ... but is not a workspace member"), while the same entry
in the uv workspace root's pyproject.toml resolves. The
``nested-member-uv-sources`` check refuses the nested declaration and names
the root file.
"""

import subprocess

from conftest import make_workspace

ROOT_PYPROJECT = (
    '[project]\nname = "ws-root"\nversion = "0.0.0"\nrequires-python = ">=3.11"\n'
    '[tool.uv.workspace]\nmembers = ["sdk", "sdk/python"]\n'
)
BUILD = '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
SOURCES = '[tool.uv.sources]\nsgsdk = { workspace = true }\n'


def _ws(tmp_path):
    root = tmp_path / "ws"
    (root / "sdk" / "src" / "sgsdk").mkdir(parents=True)
    (root / "sdk" / "python" / "src" / "sgpy").mkdir(parents=True)
    (root / "sdk" / "src" / "sgsdk" / "__init__.py").write_text("")
    (root / "sdk" / "python" / "src" / "sgpy" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT)
    (root / "sdk" / "pyproject.toml").write_text(
        '[project]\nname = "sgsdk"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n' + BUILD
    )
    (root / "sdk" / "python" / "pyproject.toml").write_text(
        '[project]\nname = "sgpy"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["sgsdk"]\n' + SOURCES + BUILD
    )
    make_workspace(root, [
        {"path": "sdk", "name": "sdk"},
        {"path": "sdk/python", "name": "sgpy"},
    ])
    return root


def _check(root):
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=load_workspace(str(root)),
    )
    return app._check_defs["nested-member-uv-sources"].impl(ctx)


def test_a_workspace_source_in_a_nested_member_is_refused(tmp_path):
    root = _ws(tmp_path)
    result = _check(root)
    assert result.status == "fail"
    text = " ".join(p.text for p in result.problems)
    assert "sdk/python/pyproject.toml" in text
    assert "sgsdk" in text
    assert f"[tool.uv.sources] in {root / 'pyproject.toml'}" in text


def test_moving_the_source_to_the_root_clears_it_and_uv_resolves(tmp_path):
    root = _ws(tmp_path)
    nested = root / "sdk" / "python" / "pyproject.toml"
    nested.write_text(nested.read_text().replace(SOURCES, ""))
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT + SOURCES)
    assert _check(root).status == "pass", [p.text for p in _check(root).problems]
    locked = subprocess.run(
        ["uv", "lock", "--offline"], cwd=root, capture_output=True, text=True,
    )
    assert locked.returncode == 0, locked.stderr


def test_a_top_level_members_workspace_source_is_not_this_checks_business(tmp_path):
    root = _ws(tmp_path)
    (root / "sdk" / "pyproject.toml").write_text(
        (root / "sdk" / "pyproject.toml").read_text() + SOURCES.replace("sgsdk", "other")
    )
    nested = root / "sdk" / "python" / "pyproject.toml"
    nested.write_text(nested.read_text().replace(SOURCES, ""))
    assert _check(root).status == "pass"
