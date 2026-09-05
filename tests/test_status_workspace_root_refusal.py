"""`rlsbl status` at a workspace root that is not itself a project.

``status`` reports on ONE project. Standing at a workspace root whose root
member declares no release target, target auto-detection finds nothing and the
generic "no package.json, pyproject.toml, or go.mod found" is true of the
directory but useless: the reader is standing in a workspace, and the command
that reports on a workspace is `rlsbl monorepo status`.

The refusal is narrow on purpose, so both halves are pinned here: a root member
that DOES carry a manifest is a real project and gets the ordinary report.
"""

import json

import pytest

import rlsbl
from conftest import make_workspace, run_git


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """A git repo whose root is a workspace root with one member package."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "package.json").write_text(
        json.dumps({"name": "pkg", "version": "1.0.0"}) + "\n"
    )
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    make_workspace(repo, [{"path": "pkg", "name": "pkg"}])
    run_git(repo, "add", "pkg", ".rlsbl-monorepo")
    run_git(repo, "commit", "-q", "-m", "initial")

    monkeypatch.chdir(repo)
    return repo


def test_a_bare_workspace_root_is_refused_and_names_monorepo_status(workspace_root):
    result = rlsbl.app.test(["status"])

    assert result.exit_code == 1, result.stdout
    assert "workspace root" in result.stderr
    assert "rlsbl monorepo status" in result.stderr
    # Not the generic manifest-detection error, which says nothing about the
    # workspace the reader is standing in.
    assert "no package.json" not in result.stderr


def test_a_root_member_with_a_manifest_gets_the_ordinary_report(workspace_root):
    """The refusal is about auto-detection finding nothing, not about roots."""
    (workspace_root / "package.json").write_text(
        json.dumps({"name": "root-pkg", "version": "2.3.4"}) + "\n"
    )
    run_git(workspace_root, "add", "package.json")
    run_git(workspace_root, "commit", "-q", "-m", "root manifest")

    result = rlsbl.app.test(["status"])

    assert result.exit_code == 0, result.stderr
    assert "2.3.4" in result.stdout
    assert "rlsbl monorepo status" not in result.stderr


def test_a_named_target_is_honored_at_a_bare_root(workspace_root):
    """`--target` says what to report on, so nothing is auto-detected.

    The refusal is about auto-detection having nothing to find. A caller who
    named a target gets that target's own answer -- here, that the root carries
    no npm project -- rather than a redirection they did not ask for.
    """
    result = rlsbl.app.test(["status", "--target", "npm"])

    assert result.exit_code == 1
    assert "No npm project found" in result.stderr
    assert "rlsbl monorepo status" not in result.stderr
