"""A multi-registry scaffold never creates a VERSION disagreeing with a manifest.

The go, zig, and plain targets scaffold a VERSION file, defaulting it to 0.0.0
when none exists, while npm and pypi carry their own versions in their
manifests. An npm+go project with package.json at 0.1.0 and no VERSION would
come out of scaffold carrying two versions. When a scaffold would create
VERSION, every scaffolded target's version must agree with the one it would
write, or the scaffold refuses and writes nothing.
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import run_cmd, run_cmd_multi
from rlsbl.config import read_project_config
from rlsbl.context import ProjectContext, create_context
from rlsbl.errors import ConfigError

_FLAGS = {"auto-commit": False, "auto-tag": False, "skip-shared": True}

_MISMATCH_ERROR = (
    "Scaffolding would create VERSION, but the scaffolded targets disagree "
    "on the project's version:\n"
    "  npm: 0.1.0\n"
    "  go: 0.0.0 (the value VERSION would be created with)\n"
    "A project carries one version. Create VERSION holding the project's "
    "version, then re-run `rlsbl scaffold`. Nothing was written."
)


def _write_npm(root: Path, version: str):
    (root / "package.json").write_text(json.dumps({
        "name": "demo", "version": version,
        "engines": {"node": ">=22"},
    }, indent=2) + "\n")


def _write_go(root: Path):
    (root / "go.mod").write_text("module github.com/acme/gotool\n\ngo 1.23\n")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n")


def _write_config(root: Path, registries: list[str]):
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps({
        "targets": registries, "publish_mode": "ci",
    }, indent=2) + "\n")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _scaffold_multi(root: Path, registries: list[str]) -> str:
    out = StringIO()
    with patch("sys.stdout", out):
        run_cmd_multi(registries, [], dict(_FLAGS), ctx=create_context(root))
    return out.getvalue()


_ORDERS = [["npm", "go"], ["go", "npm"]]


@pytest.mark.parametrize("registries", _ORDERS)
def test_new_version_disagreeing_with_package_json_refuses(
    mock_git_repo, registries,
):
    _write_npm(mock_git_repo, "0.1.0")
    _write_go(mock_git_repo)
    _write_config(mock_git_repo, registries)
    before = _snapshot(mock_git_repo)

    with pytest.raises(ConfigError) as excinfo:
        _scaffold_multi(mock_git_repo, registries)

    assert str(excinfo.value) == _MISMATCH_ERROR
    assert _snapshot(mock_git_repo) == before


@pytest.mark.parametrize("registries", _ORDERS)
def test_creating_version_as_the_error_says_clears_the_refusal(
    mock_git_repo, registries,
):
    _write_npm(mock_git_repo, "0.1.0")
    _write_go(mock_git_repo)
    _write_config(mock_git_repo, registries)
    with pytest.raises(ConfigError):
        _scaffold_multi(mock_git_repo, registries)

    # The fix the error names: create VERSION holding the project's version.
    (mock_git_repo / "VERSION").write_text("0.1.0\n")
    _scaffold_multi(mock_git_repo, registries)

    assert (mock_git_repo / "VERSION").read_text().strip() == "0.1.0"
    assert (mock_git_repo / ".goreleaser.yml").exists()


@pytest.mark.parametrize("registries", _ORDERS)
def test_new_version_agreeing_with_package_json_proceeds(
    mock_git_repo, registries,
):
    _write_npm(mock_git_repo, "0.0.0")
    _write_go(mock_git_repo)
    _write_config(mock_git_repo, registries)

    _scaffold_multi(mock_git_repo, registries)

    assert (mock_git_repo / "VERSION").read_text().strip() == "0.0.0"


@pytest.mark.parametrize("registries", _ORDERS)
def test_existing_version_is_not_compared(mock_git_repo, registries):
    """An existing VERSION keeps today's behavior, even when it disagrees."""
    _write_npm(mock_git_repo, "0.1.0")
    _write_go(mock_git_repo)
    (mock_git_repo / "VERSION").write_text("0.2.0\n")
    _write_config(mock_git_repo, registries)

    _scaffold_multi(mock_git_repo, registries)

    assert (mock_git_repo / "VERSION").read_text() == "0.2.0\n"


def test_single_registry_go_scaffold_creates_version(mock_git_repo):
    _write_go(mock_git_repo)
    ctx = ProjectContext(
        project_root=Path("."), workspace_root=None,
        config=read_project_config("."),
    )
    with patch("sys.stdout", StringIO()):
        run_cmd("go", [], dict(_FLAGS), ctx=ctx)

    assert (mock_git_repo / "VERSION").read_text().strip() == "0.0.0"
