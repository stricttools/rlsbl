"""`rlsbl check` at a workspace root: project-scoped checks need a releasable.

A workspace root names the WORKSPACE, not one of the releasables in it. The
project-scoped checks -- changelog coverage, version consistency, the target
family -- each answer for ONE project, so standing at the root leaves them with
no project to answer for.

They used to degrade there instead of saying so: target detection found nothing
at the root, so check after check reported SKIP "no targets detected" and one
warn named a remedy in a `.rlsbl/config.json` that a workspace root does not
have and must not grow. A run that reports nothing wrong is indistinguishable
from a clean project.

Workspace-scoped checks are the ones the root position is FOR, and they keep
running there untouched.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

import rlsbl
from conftest import make_workspace
from githarness import commit_file, git, init_repo


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace with one releasable member, standing at its root."""
    repo = tmp_path / "ws"
    repo.mkdir()
    init_repo(repo)

    core = repo / "packages" / "core"
    core.mkdir(parents=True)
    (core / "pyproject.toml").write_text(
        '[project]\nname = "core"\nversion = "0.1.0"\n', encoding="utf-8",
    )

    rel_dir = repo / ".rlsbl-monorepo" / "releasables" / "core"
    (rel_dir / "changes").mkdir(parents=True)
    (rel_dir / "releases").mkdir(parents=True)
    (rel_dir / "version").write_text("0.1.0\n", encoding="utf-8")
    (rel_dir / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
        encoding="utf-8",
    )
    (rel_dir / "changes" / "unreleased.jsonl").write_text("", encoding="utf-8")

    make_workspace(
        repo,
        [{"path": "packages/core", "name": "core", "releasable": "core"}],
        releasables=[{"name": "core", "tag_format": "{name}@v{version}"}],
    )
    commit_file(repo, "packages/core/thing.py", "x = 1\n", "core")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "workspace")
    monkeypatch.chdir(repo)
    return repo


def _check(argv):
    """Drive `rlsbl check` through the argv path the real entry point uses."""
    with patch.object(sys, "argv", ["rlsbl", *argv]):
        rlsbl._check_releasable = rlsbl._extract_check_releasable()
        remaining = list(sys.argv[1:])
    try:
        return rlsbl.app.test(remaining)
    finally:
        rlsbl._check_releasable = None


class TestProjectScopedChecksAtTheRoot:
    def test_they_refuse_without_a_selector(self, workspace):
        result = _check(["check", "--tag", "changelog"])

        assert result.exit_code == 1, (
            "project-scoped checks degraded at the workspace root instead of "
            "refusing; output was:\n" + result.stdout + result.stderr
        )
        combined = result.stdout + result.stderr
        assert "--releasable" in combined, combined
        assert "core" in combined, combined

    def test_the_refusal_does_not_name_a_workspace_root_config_file(
        self, workspace,
    ):
        result = _check(["check", "--tag", "changelog"])

        combined = result.stdout + result.stderr
        assert ".rlsbl/config.json" not in combined, (
            "a workspace root has no .rlsbl/config.json and must not grow one; "
            "naming it as the remedy sends the reader to invent one:\n"
            + combined
        )

    def test_no_check_skips_for_want_of_targets(self, workspace):
        result = _check(["check", "--tag", "changelog"])

        assert "no targets detected" not in (result.stdout + result.stderr), (
            "the root was read as a standalone project with no manifests"
        )

    def test_check_all_fails_loudly_at_the_project_scoped_half(self, workspace):
        result = _check(["check", "--all"])

        assert result.exit_code != 0, result.stdout + result.stderr


class TestWorkspaceScopedChecksAtTheRoot:
    def test_they_still_run(self, workspace):
        result = _check(["check", "--tag", "workspace"])

        combined = result.stdout + result.stderr
        assert "--releasable" not in combined, (
            "workspace-scoped checks are what the root position is for:\n"
            + combined
        )


class TestTheSelector:
    def test_it_scopes_the_project_checks_to_that_releasable(self, workspace):
        result = _check(["check", "--tag", "changelog", "--releasable", "core"])

        combined = result.stdout + result.stderr
        assert "--releasable" not in combined, combined
        assert "changelog-coverage" in combined, combined

    def test_an_unknown_releasable_is_named(self, workspace):
        result = _check(["check", "--tag", "changelog", "--releasable", "nope"])

        assert result.exit_code == 1
        assert "nope" in result.stderr

    def test_it_is_refused_in_a_member_directory(self, workspace, monkeypatch):
        monkeypatch.chdir(workspace / "packages" / "core")

        result = _check(["check", "--tag", "changelog", "--releasable", "core"])

        assert result.exit_code == 1
        assert "workspace root" in result.stderr


class TestStandaloneIsUnchanged:
    def test_the_selector_is_refused_in_a_standalone_repository(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "solo"
        repo.mkdir()
        init_repo(repo)
        (repo / ".rlsbl" / "changes").mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
            encoding="utf-8",
        )
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(
            "", encoding="utf-8",
        )
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "solo"\nversion = "0.1.0"\n', encoding="utf-8",
        )
        commit_file(repo, "README.md", "hi\n", "initial")
        monkeypatch.chdir(repo)

        result = _check(["check", "--tag", "changelog", "--releasable", "core"])

        assert result.exit_code == 1
        assert "standalone" in result.stderr

    def test_project_scoped_checks_run_without_a_selector(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "solo2"
        repo.mkdir()
        init_repo(repo)
        (repo / ".rlsbl" / "changes").mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
            encoding="utf-8",
        )
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(
            "", encoding="utf-8",
        )
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "solo2"\nversion = "0.1.0"\n', encoding="utf-8",
        )
        commit_file(repo, "README.md", "hi\n", "initial")
        monkeypatch.chdir(repo)

        result = _check(["check", "--tag", "changelog"])

        combined = result.stdout + result.stderr
        assert "--releasable" not in combined, combined
