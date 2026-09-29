"""Scaffold keeps the private paths out of every upload it sets up.

A newly scaffolded project passes the private-path refusal without hand edits:
the hatch sdist gets an exclude list, the npm package a ``.npmignore``, a Go
module a stub ``go.mod`` in each private directory, and a Docker image a
``.dockerignore``. Each test plants every kind of private path and runs the
real listing (``npm pack``, ``uv build``, Go's zip rule, the Docker context)
against what scaffold wrote.
"""

import json
import os
import shutil
import subprocess

import pytest

from conftest import make_ctx, run_git

from rlsbl.private_paths import (
    PRIVATE_DIRS,
    PRIVATE_FILES,
    PRIVATE_NAME_PATTERNS,
    PRIVATE_ROOT_DIRS,
    private_match,
)

#: One planted file per rule, at the package directory's root and deeper in.
PLANTED = (
    [f"{d}/planted.md" for d in PRIVATE_ROOT_DIRS]
    + [f"{d}/planted.md" for d in PRIVATE_DIRS]
    + [f"sub/{d}/planted.md" for d in PRIVATE_DIRS]
    + list(PRIVATE_FILES)
    + [f"sub/{name}" for name in PRIVATE_FILES]
    + [".env", ".env.local", "sub/.envrc", "notes.local-only", "x.local-only/y"]
)


def test_every_planted_path_is_private():
    for rel in PLANTED:
        assert private_match(rel) is not None, rel


def _plant(root):
    for rel in PLANTED:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"private: {rel}\n")


def _render(target_name, template, root):
    """Render one of *target_name*'s templates with its own variables."""
    from rlsbl.commands.init_cmd import process_template
    from rlsbl.targets import TARGETS

    target = TARGETS[target_name]
    vars_dict = dict(target.template_vars(str(root), None))
    with open(os.path.join(target.template_dir(), template)) as f:
        content, unreplaced = process_template(f.read(), vars_dict)
    assert unreplaced == []
    return content


def _check(root, config):
    """Run the check with *config* as the project's own .rlsbl/config.json."""
    from rlsbl import app

    (root / ".rlsbl").mkdir(exist_ok=True)
    (root / ".rlsbl" / "config.json").write_text(json.dumps(config))
    return app._check_defs["upload-private-paths"].impl(make_ctx(root))


def _texts(result):
    return [p.text for p in result.problems]


# ---------------------------------------------------------------------------
# npm
# ---------------------------------------------------------------------------


def test_the_scaffolded_npmignore_keeps_every_private_path_out(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "package.json").write_text(json.dumps(
        {"name": "planted-pkg", "version": "0.1.0"},
    ))
    (root / "index.js").write_text("module.exports = 1;\n")
    _plant(root)
    (root / ".npmignore").write_text(_render("npm", "npmignore.tpl", root))
    result = _check(root, {"publish_mode": "ci", "targets": ["npm"]})
    assert result.status == "pass", _texts(result)


def test_the_npmignore_stays_user_owned():
    from rlsbl.commands.init_cmd import USER_OWNED

    assert ".npmignore" in USER_OWNED


# ---------------------------------------------------------------------------
# Python (hatchling)
# ---------------------------------------------------------------------------

_HATCH_PYPROJECT = (
    '[project]\nname = "planted"\nversion = "0.1.0"\n\n'
    '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
)


def _sdist_exclude(pyproject):
    import tomllib

    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    return data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]


def test_scaffold_writes_the_hatch_sdist_exclude_list(tmp_path, monkeypatch):
    from rlsbl.private_paths import exclude_entries
    from rlsbl.upload_exclusions import apply_upload_exclusions

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(_HATCH_PYPROJECT)
    created, _skipped, warnings = apply_upload_exclusions({"pypi": "."})
    assert warnings == []
    assert ("pyproject.toml", "updated (hatch sdist exclude)") in created
    assert _sdist_exclude(tmp_path / "pyproject.toml") == list(exclude_entries())


def test_rescaffolding_keeps_the_users_own_excludes(tmp_path, monkeypatch):
    from rlsbl.private_paths import exclude_entries
    from rlsbl.upload_exclusions import apply_upload_exclusions

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        _HATCH_PYPROJECT
        + '\n[tool.hatch.build.targets.sdist]\nexclude = ["/bench/"]\n'
    )
    apply_upload_exclusions({"pypi": "."})
    assert _sdist_exclude(tmp_path / "pyproject.toml") == (
        ["/bench/"] + list(exclude_entries())
    )
    before = (tmp_path / "pyproject.toml").read_text()
    created, skipped, _warnings = apply_upload_exclusions({"pypi": "."})
    assert created == []
    assert ("pyproject.toml", "unchanged (hatch sdist exclude)") in skipped
    assert (tmp_path / "pyproject.toml").read_text() == before


def test_a_non_hatch_backend_is_reported_with_its_fix(tmp_path, monkeypatch):
    from rlsbl.upload_exclusions import apply_upload_exclusions

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0.1.0"\n\n'
        '[build-system]\nrequires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n'
    )
    created, _skipped, warnings = apply_upload_exclusions({"pypi": "."})
    assert created == []
    assert any("MANIFEST.in" in w for w in warnings)


def test_a_scaffolded_hatch_project_passes_the_ci_step(tmp_path, monkeypatch):
    """Real `uv build` of a project carrying every private path, after scaffold."""
    from ruamel.yaml import YAML

    from rlsbl.commands.init_cmd import process_template
    from rlsbl.targets import TARGETS
    from rlsbl.upload_exclusions import apply_upload_exclusions

    root = tmp_path / "planted"
    (root / "planted").mkdir(parents=True)
    (root / "pyproject.toml").write_text(_HATCH_PYPROJECT)
    (root / "planted" / "__init__.py").write_text("")
    _plant(root)
    monkeypatch.chdir(root)
    apply_upload_exclusions({"pypi": "."})

    target = TARGETS["pypi"]
    with open(os.path.join(target.template_dir(), "ci.yml.tpl")) as f:
        workflow, _ = process_template(f.read(), dict(target.template_vars(str(root), None)))
    steps = YAML(typ="safe").load(workflow)["jobs"]["test"]["steps"]
    script = "\n".join(
        s["run"] for s in steps
        if s.get("name") in ("Build the upload", "Check the upload carries no private paths")
    )
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path / "runner"), UV_OFFLINE="1")
    os.makedirs(env["RUNNER_TEMP"])
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the upload carries no private paths" in result.stdout


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def _go_targets(root):
    from rlsbl.targets import TARGETS

    ctx = make_ctx(root, config={"targets": ["go"]})
    return {m["target"]: m for m in TARGETS["go"].shared_template_mappings(ctx)}


def test_a_go_project_gets_a_stub_module_in_rlsbl_state(tmp_path):
    assert ".rlsbl/go.mod" in _go_targets(tmp_path)


def test_a_go_project_gets_a_stub_module_in_each_tracked_private_dir(tmp_path):
    for name in ("todo", ".claude", "stricttools", ".selfdoc"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "note.md").write_text("x\n")
    (tmp_path / ".gitignore").write_text(".selfdoc/\n")
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "add", "-A")
    run_git(tmp_path, "commit", "-q", "-m", "fixture")
    targets = _go_targets(tmp_path)
    for name in ("todo", ".claude", "stricttools"):
        assert f"{name}/go.mod" in targets
    # A private directory the project does not have is not created, and one
    # git ignores never reaches the zip.
    assert ".strictmetadata/go.mod" not in targets
    assert ".selfdoc/go.mod" not in targets


def test_a_python_project_gets_no_stub_module(tmp_path):
    from rlsbl.targets import TARGETS

    (tmp_path / "todo").mkdir()
    ctx = make_ctx(tmp_path, config={"targets": ["pypi"]})
    targets = {m["target"] for m in TARGETS["pypi"].shared_template_mappings(ctx)}
    assert not any(t.endswith("/go.mod") and not t.startswith(("experiments", "screenshots"))
                   for t in targets)


def _scaffold_go_stubs(root):
    """Commit *root*, write the stub modules scaffold maps, and commit them."""
    from rlsbl.commands.init_cmd import process_mappings
    from rlsbl.targets import TARGETS

    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "fixture")
    cwd = os.getcwd()
    os.chdir(root)
    try:
        ctx = make_ctx(root, config={"targets": ["go"]})
        mappings = [
            m for m in TARGETS["go"].shared_template_mappings(ctx)
            if m["target"].endswith("/go.mod")
        ]
        process_mappings(TARGETS["go"].shared_template_dir(), mappings, {})
    finally:
        os.chdir(cwd)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "stub modules")
    return {m["target"] for m in mappings}


def test_the_stub_modules_clear_a_scaffolded_go_project(tmp_path):
    root = tmp_path / "mod"
    root.mkdir()
    (root / "go.mod").write_text("module github.com/example/planted\n\ngo 1.22\n")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n")
    for rel in PLANTED:
        # A Go module zip can leave out only whole directories, so a private
        # FILE is kept out by where it lives: the agent file under .claude/,
        # and environment and local-only files never committed.
        if "/" not in rel or private_match(rel)[0] in PRIVATE_NAME_PATTERNS:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private\n")
    _scaffold_go_stubs(root)
    result = _check(root, {"publish_mode": "ci", "targets": ["go"]})
    # Directories nested below sub/ are not at the module root, so scaffold
    # writes no stub there; everything at the root is cleared.
    leftover = [t for t in _texts(result) if "would ship sub/" not in t]
    assert leftover == [], leftover


def test_go_build_skips_a_stubbed_private_dir(tmp_path):
    """The go command treats a stubbed directory as another module."""
    if shutil.which("go") is None:
        pytest.skip("go is not installed")

    root = tmp_path / "mod"
    root.mkdir()
    (root / "go.mod").write_text("module example.invalid/planted\n\ngo 1.22\n")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n")
    (root / "todo").mkdir()
    (root / "todo" / "broken.go").write_text("package broken\n\nthis is not go\n")
    assert "todo/go.mod" in _scaffold_go_stubs(root)
    env = dict(os.environ, GOFLAGS="-mod=mod", GOTOOLCHAIN="local", GOPROXY="off")
    result = subprocess.run(
        ["go", "build", "./..."], cwd=root, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


def test_a_docker_project_gets_a_dockerignore(tmp_path):
    from rlsbl.commands.init_cmd import USER_OWNED
    from rlsbl.targets import TARGETS

    targets = {m["target"] for m in TARGETS["docker"].template_mappings(make_ctx(tmp_path, {}))}
    assert ".dockerignore" in targets
    assert ".dockerignore" in USER_OWNED


def test_the_scaffolded_dockerignore_keeps_every_private_path_out(tmp_path):
    root = tmp_path / "img"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM scratch\nCOPY . /app\n")
    (root / "VERSION").write_text("0.1.0\n")
    _plant(root)
    (root / ".dockerignore").write_text(_render("docker", "dockerignore.tpl", root))
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A", "-f")
    run_git(root, "commit", "-q", "-m", "fixture")
    result = _check(root, {"publish_mode": "ci", "targets": ["docker"]})
    assert result.status == "pass", _texts(result)


def test_a_stub_module_is_not_an_unregistered_workspace_member(tmp_path, monkeypatch):
    """The stub in a member's todo/ is a marker, not a project to register."""
    from conftest import capture_all_checks
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import WorkspaceProject

    repo = tmp_path / "repo"
    (repo / "svc" / "todo").mkdir(parents=True)
    monkeypatch.chdir(repo)
    (repo / "svc" / "go.mod").write_text("module example.invalid/svc\n\ngo 1.22\n")
    (repo / "svc" / "todo" / "go.mod").write_text("module private.invalid/rlsbl-private\n")
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "fixture")
    ctx = WorkspaceCheckContext(
        project_root=repo, workspace_root=repo, config={},
        projects=[WorkspaceProject({"name": "svc", "path": "svc"})],
    )
    result = capture_all_checks()["workspace-unregistered"](ctx)
    assert result.status == "pass", [p.text for p in result.problems]


def test_rlsbl_own_sdist_excludes_every_private_path():
    """rlsbl's own next release passes the refusal its CI applies."""
    from rlsbl.private_paths import exclude_entries

    repo = os.path.join(os.path.dirname(__file__), "..")
    assert set(exclude_entries()) <= set(_sdist_exclude(os.path.join(repo, "pyproject.toml")))


def test_rlsbl_own_ci_runs_the_current_rule():
    """rlsbl's own CI checks its upload with the rule as it stands."""
    from ruamel.yaml import YAML

    import rlsbl.private_paths as module

    repo = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(repo, ".github", "workflows", "ci-pypi.yml")) as f:
        steps = YAML(typ="safe").load(f)["jobs"]["test"]["steps"]
    names = [s.get("name") for s in steps]
    assert names.index("Build the upload") + 1 == names.index(
        "Check the upload carries no private paths")
    check = steps[names.index("Check the upload carries no private paths")]
    with open(module.__file__) as f:
        assert f.read() in check["run"]
