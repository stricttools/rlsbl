"""The "Next steps" `rlsbl scaffold` prints must name the current release flow.

Agents follow printed next steps literally, so a step naming a retired command
form produces a wrong invocation. The bump type lives in the release file
(`.rlsbl/releases/unreleased.toml`, scaffolded by `rlsbl release init`), and
`rlsbl release [patch|minor|major]` is not an invocation of any command.

Every `rlsbl ...` command a step names in backticks is checked against the CLI
itself: the command path must answer `--help`, and each flag it names must be
one that help advertises.
"""

import json
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import rlsbl
from rlsbl.commands.init_cmd import run_cmd, run_cmd_multi
from rlsbl.config import read_project_config
from rlsbl.context import ProjectContext, create_context

_RETIRED = re.compile(r"rlsbl release \[?(patch|minor|major)")
_BACKTICKED_RLSBL = re.compile(r"`(rlsbl [^`]+)`")


def _write_npm(root: Path):
    (root / "package.json").write_text(json.dumps({
        "name": "demo", "version": "0.1.0",
        "engines": {"node": ">=22"},
    }, indent=2) + "\n")


def _write_pypi(root: Path):
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
    )


def _write_go(root: Path):
    (root / "go.mod").write_text("module github.com/acme/gotool\n\ngo 1.23\n")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n")
    (root / "VERSION").write_text("0.1.0\n")


_WRITERS = {"npm": _write_npm, "pypi": _write_pypi, "go": _write_go}

_FLAGS = {"auto-commit": False, "auto-tag": False, "skip-shared": True}


def _next_steps(output: str) -> list[str]:
    """The numbered lines under the last "Next steps:" header."""
    assert "\nNext steps:" in output, output
    block = output.rsplit("\nNext steps:", 1)[1]
    steps = []
    for line in block.splitlines()[1:]:
        m = re.match(r"\s+\d+\. (.*)", line)
        if not m:
            break
        steps.append(m.group(1))
    assert steps, output
    return steps


def _scaffold_single(registry: str) -> list[str]:
    ctx = ProjectContext(
        project_root=Path("."), workspace_root=None,
        config=read_project_config("."),
    )
    out = StringIO()
    with patch("sys.stdout", out):
        run_cmd(registry, [], dict(_FLAGS), ctx=ctx)
    return _next_steps(out.getvalue())


def _scaffold_multi(root: Path, registries: list[str]) -> list[str]:
    out = StringIO()
    with patch("sys.stdout", out):
        run_cmd_multi(registries, [], dict(_FLAGS), ctx=create_context(root))
    return _next_steps(out.getvalue())


def _assert_current_release_flow(steps: list[str]):
    text = "\n".join(steps)
    assert not _RETIRED.search(text), text
    # Releases are the only push: `rlsbl release run` pushes main itself.
    assert not any("Push to GitHub" in s for s in steps), text
    # The release step appears once, and leaves consent with the human.
    assert sum("`rlsbl release run" in s for s in steps) == 1, text
    assert "--approve-consequential" not in text, text
    assert "allow-dirty" not in text, text
    commands = _BACKTICKED_RLSBL.findall(text)
    assert any(c == "rlsbl release init" for c in commands), text
    assert any(c.startswith("rlsbl release run") for c in commands), text
    for command in commands:
        words = command.split()[1:]
        path = [w for w in words if not w.startswith("-")]
        flags = [w for w in words if w.startswith("--")]
        result = rlsbl.app.test(path + ["--help"])
        assert result.exit_code == 0, (command, result.stderr)
        for flag in flags:
            assert re.search(rf"{re.escape(flag)}\b", result.stdout), (
                command, flag,
            )


@pytest.mark.parametrize("registry", ["npm", "pypi", "go"])
def test_single_registry_next_steps_name_the_current_release_flow(
    mock_git_repo, registry,
):
    _WRITERS[registry](mock_git_repo)
    _assert_current_release_flow(_scaffold_single(registry))


def test_npm_lockfile_step_says_commit_before_releasing(mock_git_repo):
    """Releases are the only push, so the lockfile step names the release."""
    _write_npm(mock_git_repo)
    steps = _scaffold_single("npm")
    assert steps[0] == (
        'Run "npm install" and commit package-lock.json before releasing'
    ), steps
    assert not any("before pushing" in s for s in steps), steps


def _scaffold_multi_project(root: Path, registries: list[str]) -> list[str]:
    for registry in registries:
        _WRITERS[registry](root)
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps({
        "targets": registries, "publish_mode": "ci",
    }, indent=2) + "\n")
    return _scaffold_multi(root, registries)


_NPM_STEP = "NPM_TOKEN"
_PYPI_STEP = "Trusted Publishing on pypi.org"
_GO_STEP = "GoReleaser"


def test_multi_registry_next_steps_name_the_current_release_flow(mock_git_repo):
    steps = _scaffold_multi_project(mock_git_repo, ["npm", "pypi"])
    _assert_current_release_flow(steps)


def test_npm_and_pypi_project_gets_both_registries_steps(mock_git_repo):
    text = "\n".join(_scaffold_multi_project(mock_git_repo, ["npm", "pypi"]))
    assert _NPM_STEP in text, text
    assert _PYPI_STEP in text, text
    assert _GO_STEP not in text, text


def test_npm_and_go_project_gets_no_pypi_steps(mock_git_repo):
    steps = _scaffold_multi_project(mock_git_repo, ["go", "npm"])
    _assert_current_release_flow(steps)
    text = "\n".join(steps)
    assert _NPM_STEP in text, text
    assert _GO_STEP in text, text
    assert "pypi" not in text.lower(), text
