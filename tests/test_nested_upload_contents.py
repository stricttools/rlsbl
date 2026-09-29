"""A member's published upload leaves out the members nested inside it.

`npm pack` of a parent lists a nested member's files, a Python parent's sdist
carries its nested members in full, and a Go parent's module zip carries every
nested member that is not a Go module of its own -- so a nested member's change
silently altered the parent's published package, with no parent changelog
entry. npm and Go uploads are listed offline before a release starts (the
``nested-member-upload-contents`` check); a Python upload can only be listed by
building it, which needs the network, so it is built and checked in the
member's CI run on the release commit.
"""

import json
import os
import subprocess

import pytest

from conftest import make_nested_workspace, make_workspace, run_git


def _check(root):
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=load_workspace(str(root)),
    )
    return app._check_defs["nested-member-upload-contents"].impl(ctx)


def _texts(result):
    return [p.text for p in result.problems]


def _commit(root):
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "fixture")


# ---------------------------------------------------------------------------
# npm, offline
# ---------------------------------------------------------------------------


@pytest.fixture
def npm_ws(tmp_path):
    root = tmp_path / "ws"
    for rel, name in (("web", "webpkg"), ("web/child", "webchild")):
        (root / rel).mkdir(parents=True)
        (root / rel / "package.json").write_text(json.dumps(
            {"name": name, "version": "0.1.0", "engines": {"node": ">=22"}},
        ))
        (root / rel / "index.js").write_text("module.exports = 1;\n")
    make_workspace(root, [
        {"path": "web", "name": "web"},
        {"path": "web/child", "name": "child"},
    ])
    run_git(root, "init", "-q", "-b", "main")
    _commit(root)
    return root


def test_an_npm_parent_shipping_a_nested_members_files_is_refused(npm_ws):
    result = _check(npm_ws)
    assert result.status == "fail"
    text = " ".join(_texts(result))
    assert "web: `npm pack` would ship web/child/index.js" in text
    assert "member 'child' owns" in text
    assert '"files"' in text


def test_listing_only_its_own_files_clears_the_npm_refusal(npm_ws):
    pkg = npm_ws / "web" / "package.json"
    data = json.loads(pkg.read_text())
    data["files"] = ["index.js"]
    pkg.write_text(json.dumps(data))
    _commit(npm_ws)
    assert _check(npm_ws).status == "pass", _texts(_check(npm_ws))


# ---------------------------------------------------------------------------
# Go, derived offline from the tracked files
# ---------------------------------------------------------------------------


@pytest.fixture
def go_ws(tmp_path):
    root = tmp_path / "ws"
    (root / "svc" / "py").mkdir(parents=True)
    (root / "svc" / "go.mod").write_text("module github.com/example/nested/svc\n\ngo 1.22\n")
    (root / "svc" / "svc.go").write_text("package svc\n")
    (root / "svc" / "py" / "pyproject.toml").write_text(
        '[project]\nname = "svcpy"\nversion = "0.1.0"\n'
    )
    make_workspace(root, [
        {"path": "svc", "name": "svc"},
        {"path": "svc/py", "name": "svcpy"},
    ])
    run_git(root, "init", "-q", "-b", "main")
    _commit(root)
    return root


def test_a_go_module_zip_carrying_a_nested_member_is_refused(go_ws):
    text = " ".join(_texts(_check(go_ws)))
    assert "svc: the Go module zip would ship svc/py/pyproject.toml" in text
    assert "member 'svcpy' owns" in text
    assert "go.mod" in text


def test_a_go_mod_at_the_nested_members_root_clears_the_go_refusal(go_ws):
    (go_ws / "svc" / "py" / "go.mod").write_text(
        "module github.com/example/nested/svc/py\n\ngo 1.22\n"
    )
    _commit(go_ws)
    assert _check(go_ws).status == "pass", _texts(_check(go_ws))


def _dev_node_root_ws(tmp_path, root_config=None):
    """A root dev node carrying a private workspace package.json, over an npm member."""
    root = tmp_path / "ws"
    (root / "web").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps(
        {"name": "tooling-workspace", "private": True, "engines": {"node": ">=22"}},
    ))
    (root / "web" / "package.json").write_text(json.dumps(
        {"name": "webpkg", "version": "0.1.0", "engines": {"node": ">=22"}},
    ))
    (root / "web" / "index.js").write_text("module.exports = 1;\n")
    if root_config is not None:
        (root / ".rlsbl").mkdir()
        (root / ".rlsbl" / "config.json").write_text(json.dumps(root_config))
    return root


def test_a_member_outside_every_releasable_uploads_nothing(tmp_path):
    root = _dev_node_root_ws(tmp_path)
    make_workspace(root, [
        {"path": ".", "name": "root", "dev_only": True, "releasable": False},
        {"path": "web", "name": "web"},
    ])
    run_git(root, "init", "-q", "-b", "main")
    _commit(root)
    assert _check(root).status == "pass", _texts(_check(root))


def test_a_member_whose_publishing_is_suppressed_uploads_nothing(tmp_path):
    root = tmp_path / "ws"
    (root / "svc" / "py").mkdir(parents=True)
    (root / "svc" / "go.mod").write_text("module github.com/example/nested/svc\n\ngo 1.22\n")
    (root / "svc" / "svc.go").write_text("package svc\n")
    (root / "svc" / ".rlsbl").mkdir()
    (root / "svc" / ".rlsbl" / "config.json").write_text(json.dumps(
        {"publish_mode": "none", "targets": ["go"]},
    ))
    (root / "svc" / "py" / "pyproject.toml").write_text(
        '[project]\nname = "svcpy"\nversion = "0.1.0"\n'
    )
    make_workspace(root, [
        {"path": "svc", "name": "svc"},
        {"path": "svc/py", "name": "svcpy"},
    ])
    run_git(root, "init", "-q", "-b", "main")
    _commit(root)
    assert _check(root).status == "pass", _texts(_check(root))


def test_nested_go_modules_are_already_left_out(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    assert _check(root).status in ("pass", "skip"), _texts(_check(root))


# ---------------------------------------------------------------------------
# Python, in CI
# ---------------------------------------------------------------------------


def _ci_workflow(member_dir):
    from rlsbl.commands.init_cmd import process_template
    from rlsbl.targets import TARGETS

    target = TARGETS["pypi"]
    vars_dict = dict(target.template_vars(str(member_dir), None))
    with open(os.path.join(target.template_dir(), "ci.yml.tpl")) as f:
        content, _ = process_template(f.read(), vars_dict)
    return content


def _upload_check_step(workflow):
    from ruamel.yaml import YAML

    steps = YAML(typ="safe").load(workflow)["jobs"]["test"]["steps"]
    return next(s for s in steps if s.get("name", "").startswith("Check the upload"))


def test_a_python_parents_ci_builds_and_checks_its_upload(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    step = _upload_check_step(_ci_workflow(root / "sdk"))
    assert "uv build" in step["run"]
    assert "python" in step["run"] and "npm" in step["run"]


def test_a_python_member_without_nested_members_gets_no_such_step(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    workflow = _ci_workflow(root / "sdk" / "python")
    assert "Check the upload" not in workflow


def _run_step(step, cwd, tmp_path):
    """Run the CI step's script as CI would, with RUNNER_TEMP set."""
    # UV_OFFLINE: the suite runs without a network; hatchling comes from the
    # uv cache. In CI the same step builds online.
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path / "runner"), UV_OFFLINE="1")
    os.makedirs(env["RUNNER_TEMP"], exist_ok=True)
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


def test_the_ci_step_refuses_an_sdist_carrying_nested_members(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    step = _upload_check_step(_ci_workflow(root / "sdk"))
    result = _run_step(step, root / "sdk", tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "python/pyproject.toml" in result.stdout
    assert "[tool.hatch.build.targets.sdist]" in result.stdout


def test_excluding_them_from_the_sdist_clears_the_ci_step(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    step = _upload_check_step(_ci_workflow(root / "sdk"))
    with open(root / "sdk" / "pyproject.toml", "a") as f:
        f.write('\n[tool.hatch.build.targets.sdist]\nexclude = ["python", "npm"]\n')
    result = _run_step(step, root / "sdk", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
