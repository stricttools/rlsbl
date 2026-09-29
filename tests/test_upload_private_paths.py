"""A release refuses an upload that carries a private path.

Planning notes (``todo/``), release metadata (``.rlsbl/``), agent instructions
(``CLAUDE.md``), scratch output, and environment files reached PyPI, npm, and
the Go module proxy because nothing listed what an upload carried. The npm
package, the Go module zip, and the Docker build context are listed offline by
the ``upload-private-paths`` check before a release mutates anything; the
Python sdist and wheel are built and checked in CI on the candidate commit,
by the pypi CI template running :mod:`rlsbl.private_paths` over ``uv build``'s
output. Every refusal names the file and the ecosystem's own exclusion, and
each test below applies that exclusion and sees the refusal clear.
"""

import json
import os
import shutil
import subprocess

import pytest

from conftest import make_ctx, run_git

from rlsbl.private_paths import archive_paths, private_match


@pytest.fixture(autouse=True)
def _default_upload_private_paths():
    """The real check, over conftest's default that neutralizes it."""
    yield


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel, rule, directory", [
    ("todo/plan.md", "/todo/", "todo"),
    ("todo/.done/old.md", "/todo/", "todo"),
    ("experiments/.gitignore", "/experiments/", "experiments"),
    ("screenshots/shot.png", "/screenshots/", "screenshots"),
    ("stricttools/docs/index.md", "/stricttools/", "stricttools"),
    (".stricttools/docs/index.md", ".stricttools/", ".stricttools"),
    (".strictmetadata/docs/index.md", ".strictmetadata/", ".strictmetadata"),
    (".rlsbl/config.json", ".rlsbl/", ".rlsbl"),
    ("schema/.rlsbl/config.json", ".rlsbl/", "schema/.rlsbl"),
    (".rlsbl-monorepo/workspace.toml", ".rlsbl-monorepo/", ".rlsbl-monorepo"),
    (".claude/settings.json", ".claude/", ".claude"),
    (".selfdoc/state.json", ".selfdoc/", ".selfdoc"),
    (".strictcli/schema.json", ".strictcli/", ".strictcli"),
    ("CLAUDE.md", "CLAUDE.md", None),
    ("python/CLAUDE.md", "CLAUDE.md", None),
    ("AGENTS.md", "AGENTS.md", None),
    (".env", ".env*", None),
    ("bin/.env.local", ".env*", None),
    ("notes.local-only", "*.local-only", None),
    ("scratch.local-only/probe.py", "*.local-only", "scratch.local-only"),
])
def test_a_private_path_is_named_by_its_rule(rel, rule, directory):
    assert private_match(rel) == (rule, directory)


@pytest.mark.parametrize("rel", [
    "README.md",
    "src/pkg/__init__.py",
    # The plain names are private only at the package directory's root.
    "src/todo/__init__.py",
    "tests/screenshots/expected.png",
    "docs/experiments.md",
    "src/pkg/environment.py",
    # Go's fixture directory: a test's expected tree may hold any of them.
    "internal/gen/testdata/expected/.strictmetadata/docs/index.md",
    "testdata/CLAUDE.md",
])
def test_an_ordinary_path_is_not_private(rel):
    assert private_match(rel) is None


def _check(root, config):
    """Run the check with *config* as the project's own .rlsbl/config.json."""
    from rlsbl import app

    (root / ".rlsbl").mkdir(exist_ok=True)
    (root / ".rlsbl" / "config.json").write_text(json.dumps(config))
    ctx = make_ctx(root)
    return app._check_defs["upload-private-paths"].impl(ctx)


def _texts(result):
    return [p.text for p in result.problems]


def _commit(root):
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "fixture")


def _plant(root, *rels):
    for rel in rels:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"private: {rel}\n")


def test_the_check_is_registered_for_preflight():
    import tomllib

    from rlsbl.checks import CHECK_TARGETS

    with open(os.path.join(os.path.dirname(__file__), "..", "rlsbl", "data",
                           "checks.toml"), "rb") as f:
        spec = tomllib.load(f)["checks"]["upload-private-paths"]
    assert spec["severity"] == "error"
    assert "preflight" in spec["tags"]
    assert CHECK_TARGETS["upload-private-paths"] == frozenset({"npm", "go", "docker"})


# ---------------------------------------------------------------------------
# npm: `npm pack --dry-run --offline`, before anything mutates
# ---------------------------------------------------------------------------

_NPM_CONFIG = {"publish_mode": "ci", "targets": ["npm"]}


@pytest.fixture
def npm_project(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "package.json").write_text(json.dumps(
        {"name": "planted-pkg", "version": "0.1.0", "engines": {"node": ">=22"}},
    ))
    (root / "index.js").write_text("module.exports = 1;\n")
    _plant(root, "todo/plan.md", "CLAUDE.md", ".env")
    run_git(root, "init", "-q", "-b", "main")
    _commit(root)
    return root


def test_an_npm_package_carrying_private_paths_is_refused(npm_project):
    result = _check(npm_project, _NPM_CONFIG)
    assert result.status == "fail"
    text = "\n".join(_texts(result))
    for rel, rule in (("todo/plan.md", "/todo/"), ("CLAUDE.md", "CLAUDE.md"),
                      (".env", ".env*"), (".rlsbl/config.json", ".rlsbl/")):
        assert f"`npm pack` would ship {rel}, a private path ({rule})" in text
        assert f'add "{rule}" to .npmignore' in text
    assert "index.js" not in text


def test_npmignore_entries_clear_the_npm_refusal(npm_project):
    (npm_project / ".npmignore").write_text("/todo/\nCLAUDE.md\n.env*\n.rlsbl/\n")
    _commit(npm_project)
    result = _check(npm_project, _NPM_CONFIG)
    assert result.status == "pass", _texts(result)


def test_a_files_field_covering_a_private_path_names_the_files_field(npm_project):
    pkg = npm_project / "package.json"
    data = json.loads(pkg.read_text())
    data["files"] = ["index.js", "todo/"]
    pkg.write_text(json.dumps(data))
    result = _check(npm_project, _NPM_CONFIG)
    text = "\n".join(_texts(result))
    assert "`npm pack` would ship todo/plan.md" in text
    assert '"files" field in package.json' in text
    # CLAUDE.md and .env are outside the files field, so npm leaves them out.
    assert "CLAUDE.md" not in text and ".env" not in text

    data["files"] = ["index.js"]
    pkg.write_text(json.dumps(data))
    result = _check(npm_project, _NPM_CONFIG)
    assert result.status == "pass", _texts(result)


def test_an_npm_package_that_does_not_publish_is_not_listed(npm_project):
    result = _check(npm_project, {"publish_mode": "none", "targets": ["npm"]})
    assert result.status in ("pass", "skip"), _texts(result)


# ---------------------------------------------------------------------------
# Go: the module zip, derived offline from the tracked files by Go's rule
# ---------------------------------------------------------------------------

_GO_CONFIG = {"publish_mode": "ci", "targets": ["go"]}
_STUB = "module private.invalid/rlsbl-private\n"


@pytest.fixture
def go_project(tmp_path):
    root = tmp_path / "mod"
    root.mkdir()
    (root / "go.mod").write_text("module github.com/example/planted\n\ngo 1.22\n")
    (root / "VERSION").write_text("0.1.0\n")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n")
    _plant(root, "todo/plan.md", "CLAUDE.md", ".env")
    (root / ".rlsbl").mkdir()
    (root / ".rlsbl" / "config.json").write_text(json.dumps(_GO_CONFIG))
    run_git(root, "init", "-q", "-b", "main")
    _commit(root)
    return root


def test_a_go_module_zip_carrying_private_paths_is_refused(go_project):
    result = _check(go_project, _GO_CONFIG)
    assert result.status == "fail"
    text = "\n".join(_texts(result))
    assert "the Go module zip would ship todo/plan.md" in text
    assert "put a stub go.mod in todo/" in text
    assert "the Go module zip would ship .rlsbl/config.json" in text
    assert "put a stub go.mod in .rlsbl/" in text
    assert "the Go module zip would ship CLAUDE.md" in text
    assert ".claude/CLAUDE.md" in text
    assert "the Go module zip would ship .env" in text
    assert "git rm --cached .env" in text
    assert "main.go" not in text


def test_the_named_go_fixes_clear_the_go_refusal(go_project):
    (go_project / "todo" / "go.mod").write_text(_STUB)
    (go_project / ".rlsbl" / "go.mod").write_text(_STUB)
    (go_project / ".claude").mkdir()
    (go_project / ".claude" / "go.mod").write_text(_STUB)
    run_git(go_project, "mv", "CLAUDE.md", ".claude/CLAUDE.md")
    run_git(go_project, "rm", "-q", "--cached", ".env")
    (go_project / ".gitignore").write_text(".env\n")
    _commit(go_project)
    result = _check(go_project, _GO_CONFIG)
    assert result.status == "pass", _texts(result)


# ---------------------------------------------------------------------------
# Docker: the build context, tracked files less .dockerignore
# ---------------------------------------------------------------------------

_DOCKER_CONFIG = {"publish_mode": "ci", "targets": ["docker"]}


@pytest.fixture
def docker_project(tmp_path):
    root = tmp_path / "img"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM scratch\nCOPY . /app\n")
    (root / "VERSION").write_text("0.1.0\n")
    (root / "app.sh").write_text("echo hi\n")
    _plant(root, "todo/plan.md", "sub/CLAUDE.md", ".env")
    (root / ".rlsbl").mkdir()
    (root / ".rlsbl" / "config.json").write_text(json.dumps(_DOCKER_CONFIG))
    run_git(root, "init", "-q", "-b", "main")
    _commit(root)
    return root


def test_a_docker_build_context_carrying_private_paths_is_refused(docker_project):
    result = _check(docker_project, _DOCKER_CONFIG)
    assert result.status == "fail"
    text = "\n".join(_texts(result))
    assert "the Docker build context would ship todo/plan.md" in text
    assert 'add "todo" to .dockerignore' in text
    assert "the Docker build context would ship sub/CLAUDE.md" in text
    assert 'add "**/CLAUDE.md" to .dockerignore' in text
    assert 'add "**/.env*" to .dockerignore' in text
    assert 'add "**/.rlsbl" to .dockerignore' in text


def test_dockerignore_entries_clear_the_docker_refusal(docker_project):
    (docker_project / ".dockerignore").write_text(
        "todo\n**/CLAUDE.md\n**/.env*\n**/.rlsbl\n"
    )
    _commit(docker_project)
    result = _check(docker_project, _DOCKER_CONFIG)
    assert result.status == "pass", _texts(result)


@pytest.mark.parametrize("patterns, path, ignored", [
    (["todo"], "todo/plan.md", True),
    (["todo"], "sub/todo/plan.md", False),
    (["/todo/"], "todo/plan.md", True),
    (["**/CLAUDE.md"], "CLAUDE.md", True),
    (["**/CLAUDE.md"], "a/b/CLAUDE.md", True),
    (["CLAUDE.md"], "a/CLAUDE.md", False),
    (["*.md", "!README.md"], "README.md", False),
    (["*.md", "!README.md"], "NOTES.md", True),
    (["**/.env*"], "bin/.env.local", True),
    (["docs/*.md"], "docs/a.md", True),
    (["docs/*.md"], "docs/sub/a.md", False),
])
def test_the_dockerignore_reader_follows_dockers_rules(patterns, path, ignored):
    from rlsbl.targets.docker import dockerignore_excludes

    assert dockerignore_excludes(patterns, path) is ignored


# ---------------------------------------------------------------------------
# Python: built and checked in CI on the candidate commit
# ---------------------------------------------------------------------------


def _ci_workflow(project_dir):
    from rlsbl.commands.init_cmd import process_template
    from rlsbl.targets import TARGETS

    target = TARGETS["pypi"]
    vars_dict = dict(target.template_vars(str(project_dir), None))
    with open(os.path.join(target.template_dir(), "ci.yml.tpl")) as f:
        content, unreplaced = process_template(f.read(), vars_dict)
    assert unreplaced == []
    return content


def _steps(workflow):
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(workflow)["jobs"]["test"]["steps"]


def _named(steps, name):
    return next(s for s in steps if s.get("name") == name)


def _run_steps(steps, cwd, tmp_path):
    """Run CI steps' scripts in order, as CI would, with RUNNER_TEMP set."""
    # UV_OFFLINE: the suite runs without a network; hatchling comes from the
    # uv cache. In CI the same step builds online.
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path / "runner"), UV_OFFLINE="1")
    os.makedirs(env["RUNNER_TEMP"], exist_ok=True)
    shutil.rmtree(os.path.join(env["RUNNER_TEMP"], "rlsbl-upload"), ignore_errors=True)
    script = "\n".join(step["run"] for step in steps)
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


_HATCH_PYPROJECT = (
    '[project]\nname = "planted"\nversion = "0.1.0"\n\n'
    '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
)


@pytest.fixture
def python_project(tmp_path):
    root = tmp_path / "planted"
    (root / "planted").mkdir(parents=True)
    (root / "pyproject.toml").write_text(_HATCH_PYPROJECT)
    (root / "planted" / "__init__.py").write_text("")
    _plant(root, "todo/plan.md", "CLAUDE.md", ".env")
    return root


def _upload_steps(project_dir):
    steps = _steps(_ci_workflow(project_dir))
    return [
        _named(steps, "Build the upload"),
        _named(steps, "Check the upload carries no private paths"),
    ]


def test_every_python_projects_ci_checks_its_upload_for_private_paths(python_project):
    build, check = _upload_steps(python_project)
    assert "uv build" in build["run"]
    assert "PRIVATE_DIRS" in check["run"]


def test_the_ci_step_refuses_a_python_upload_carrying_private_paths(
    python_project, tmp_path,
):
    result = _run_steps(_upload_steps(python_project), python_project, tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    out = result.stdout
    for rel, rule in (("todo/plan.md", "/todo/"), ("CLAUDE.md", "CLAUDE.md"),
                      (".env", ".env*")):
        assert f"planted-0.1.0.tar.gz would ship {rel}" in out
        assert (
            f'add "{rule}" to exclude under [tool.hatch.build.targets.sdist]'
        ) in out


def test_hatch_sdist_excludes_clear_the_ci_step(python_project, tmp_path):
    steps = _upload_steps(python_project)
    with open(python_project / "pyproject.toml", "a") as f:
        f.write(
            '\n[tool.hatch.build.targets.sdist]\n'
            'exclude = ["/todo/", "CLAUDE.md", ".env*"]\n'
        )
    result = _run_steps(steps, python_project, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the upload carries no private paths" in result.stdout


def test_the_ci_step_is_this_modules_own_source(python_project):
    """CI runs the rule the check runs: one text, embedded, not a copy."""
    import rlsbl.private_paths as module

    with open(module.__file__) as f:
        source = f.read()
    _build, check = _upload_steps(python_project)
    assert source in check["run"]


def test_the_python_fix_is_named_per_build_backend():
    from rlsbl.private_paths import python_fix

    assert "[tool.uv.build-backend]" in python_fix(
        "uv_build", "sdist", "todo/a.md", "/todo/", "todo")
    assert 'add "prune todo" to MANIFEST.in' in python_fix(
        "setuptools.build_meta", "sdist", "todo/a.md", "/todo/", "todo")
    assert 'add "exclude CLAUDE.md" to MANIFEST.in' in python_fix(
        "setuptools.build_meta", "sdist", "CLAUDE.md", "CLAUDE.md", None)


def test_archive_paths_read_each_artifact_kind(tmp_path):
    import io
    import tarfile
    import zipfile

    sdist = tmp_path / "p-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo("p-0.1.0/todo/a.md")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    assert archive_paths(str(sdist)) == ["todo/a.md"]

    gozip = tmp_path / "module.zip"
    with zipfile.ZipFile(gozip, "w") as z:
        z.writestr("github.com/a/b@v0.1.0/CLAUDE.md", "x")
    assert archive_paths(str(gozip)) == ["CLAUDE.md"]

    wheel = tmp_path / "p-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("p/__init__.py", "")
        z.writestr("p-0.1.0.dist-info/METADATA", "")
    assert archive_paths(str(wheel)) == ["p/__init__.py"]


# ---------------------------------------------------------------------------
# The release flow: refused before anything mutates
# ---------------------------------------------------------------------------


def test_a_release_refuses_before_mutating_and_the_fix_clears_it(tmp_project, capsys):
    """An npm package shipping its committed .rlsbl/ is refused in preflight."""
    from test_release_commit import _RELEASE_FILE, _release, _setup_npm_project

    from rlsbl.release_file import ReleaseConfig

    from conftest import _git

    _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
    head = _git(tmp_project, "rev-parse", "HEAD")
    config = ReleaseConfig(
        bump="patch", include=["npm"], exclude=[], description="A patch release.",
    )
    with pytest.raises(SystemExit):
        _release(config)
    err = capsys.readouterr().err
    assert "upload-private-paths" in err
    # Nothing moved: no version bump, no tag, no commit.
    assert _git(tmp_project, "rev-parse", "HEAD") == head
    assert json.loads((tmp_project / "package.json").read_text())["version"] == "1.0.0"
    assert _git(tmp_project, "tag", "--list", "v1.0.1") == ""

    # The fix the refusal names: an .npmignore entry for .rlsbl/.
    (tmp_project / ".npmignore").write_text(".rlsbl/\n")
    _git(tmp_project, "add", ".npmignore")
    _git(tmp_project, "commit", "-q", "-m", "keep .rlsbl out of the package")
    jsonl = tmp_project / ".rlsbl" / "changes" / "unreleased.jsonl"
    with open(jsonl, "a") as f:
        f.write(json.dumps({"format_version": 1, "user_facing": False,
                            "commits": [_git(tmp_project, "rev-parse", "HEAD")]}) + "\n")
    _git(tmp_project, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(tmp_project, "commit", "-q", "-m", "changelog: cover it",
         "--trailer", "Autogenerated: true")
    _release(config)
    assert _git(tmp_project, "tag", "--list", "v1.0.1") == "v1.0.1"
