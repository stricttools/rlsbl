"""Standing at a workspace root: what each command resolves it to.

Every directory inside a workspace now belongs to a member -- the root member
owns whatever no other member claims -- so :func:`rlsbl.workspace.resolve_project`
never answers "no project" inside the tree.  That closes a hole (commands used
to fall through a ``None`` branch at the root) and opens a question: the root
member's directory IS the workspace root, so a directory that used to mean
"nowhere in particular" now means both "the root member" and "the workspace".

Every call site that resolves the current directory therefore falls into one
of two classes, and this file is the record of which:

| Call site | Class | What resolution answers there |
| --- | --- | --- |
| `rlsbl/workspace.py` `resolve_project` | (the resolver) | Most specific member path wins; the root member is the residual. ``None`` only outside the workspace tree. |
| `rlsbl/__init__.py` `_require_sub_project_root` | project-scoped | The directory of the member the command acts on. At the root that is the workspace root itself. |
| `rlsbl/__init__.py` `cmd_release_run` | **workspace-scoped at the root** | The root names the whole workspace, so `--releasable` must say which one to release. Inside a member it stays project-scoped. |
| `rlsbl/ci_checks.py` `release_check_filters` | project-scoped | Which member's CI check runs a release directory maps to. |
| `rlsbl/checks/_common.py` `_resolve_version_and_tag` | project-scoped | The member whose releasable supplies version and tag. |
| `rlsbl/checks/_common.py` `_get_changelog_context` | project-scoped | The member whose releasable's changelog is being validated. |
| `rlsbl/checks/_common.py` `_get_all_changelog_contexts` | **workspace-scoped at the root** | At the root every releasable's changelog is in scope; inside a member, only that member's. |
| `rlsbl/checks/prepush.py` `prepush-gitignore-guard` | project-scoped | The member whose releasable changelog paths must not be ignored. |
| `rlsbl/checks/project.py` (version consistency, releasable version) | project-scoped | The member whose declared version is compared. |
| `rlsbl/checks/changelog.py` (changelog entry) | project-scoped | The member whose CHANGELOG.md is read. |
| `rlsbl/commands/init_cmd.py` (four sites) | project-scoped | The member being scaffolded. |
| `rlsbl/commands/changelog_cmd.py` | project-scoped | The member whose changes directory an entry is appended to. |
| `rlsbl/commands/release/release_state.py` `resume` source | project-scoped | The member whose in-progress release is resumed. |
| `rlsbl/commands/release/__init__.py`, `release/validate.py` | project-scoped | The member being released (reached only after `release run` has chosen one). |
| `rlsbl/commands/release_init.py`, `release_retry.py`, `edit_release.py`, `undo.py`, `deprecate.py`, `yank.py` | project-scoped | The member whose release metadata the command edits. |
| `rlsbl/commands/status.py`, `unreleased.py` | project-scoped | The member being reported on. |

The contract the whole table rests on: **resolution inside a workspace never
returns nothing.**  A site that still branches on ``None`` is either handling
the outside-the-tree case or is dead; the workspace-scoped sites do not test
for ``None`` at all -- they ask whether the directory IS the root.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import make_workspace
from rlsbl.ownership import ROOT_MEMBER_NAME, is_root_member
from rlsbl.workspace import Releasable, resolve_project, write_releasable_version


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pkg(root, path, name):
    d = Path(root) / path
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}, indent=2) + "\n"
    )
    return d


def _multi_releasable_workspace(root):
    """Two releasables, each with one member, plus a dev-node root member."""
    _pkg(root, "packages/alpha", "alpha")
    _pkg(root, "packages/beta", "beta")
    make_workspace(str(root), [
        {"path": "packages/alpha", "name": "alpha", "releasable": "alpha"},
        {"path": "packages/beta", "name": "beta", "releasable": "beta"},
    ], releasables=[Releasable(name="alpha"), Releasable(name="beta")])
    for name in ("alpha", "beta"):
        write_releasable_version(str(root), name, "1.0.0")
        changes = Path(root) / ".rlsbl-monorepo" / "releasables" / name / "changes"
        changes.mkdir(parents=True, exist_ok=True)
        (changes / "unreleased.jsonl").write_text("")
    return root


def _single_releasable_workspace(root):
    """One releasable, one member -- the case where 'just pick it' is tempting."""
    _pkg(root, "packages/alpha", "alpha")
    make_workspace(str(root), [
        {"path": "packages/alpha", "name": "alpha", "releasable": "alpha"},
    ], releasables=[Releasable(name="alpha")])
    write_releasable_version(str(root), "alpha", "1.0.0")
    changes = Path(root) / ".rlsbl-monorepo" / "releasables" / "alpha" / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    (changes / "unreleased.jsonl").write_text("")
    return root


# ---------------------------------------------------------------------------
# The contract every site depends on
# ---------------------------------------------------------------------------


class TestResolutionNeverReturnsNothing:

    def test_the_workspace_root_resolves_to_the_root_member(self, tmp_path):
        _multi_releasable_workspace(tmp_path)
        resolved = resolve_project(str(tmp_path), str(tmp_path))
        assert resolved is not None
        assert is_root_member(resolved)
        assert resolved["name"] == ROOT_MEMBER_NAME

    def test_an_unclaimed_directory_resolves_to_the_root_member(self, tmp_path):
        _multi_releasable_workspace(tmp_path)
        (tmp_path / "docs").mkdir()
        resolved = resolve_project(str(tmp_path), str(tmp_path / "docs"))
        assert is_root_member(resolved)

    def test_a_member_directory_resolves_to_that_member(self, tmp_path):
        _multi_releasable_workspace(tmp_path)
        resolved = resolve_project(str(tmp_path), str(tmp_path / "packages" / "beta"))
        assert resolved["name"] == "beta"

    def test_only_outside_the_tree_is_nothing(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _multi_releasable_workspace(repo)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        assert resolve_project(str(repo), str(outside)) is None


class TestWorkspaceScopedSites:
    """The sites that ask "am I at the root?", not "which member am I in?"."""

    def test_at_the_root_helper_answers_by_directory(self, tmp_path, monkeypatch):
        from rlsbl import _at_workspace_root

        _multi_releasable_workspace(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert _at_workspace_root(str(tmp_path))
        monkeypatch.chdir(tmp_path / "packages" / "alpha")
        assert not _at_workspace_root(str(tmp_path))

    def test_changelog_checks_iterate_every_releasable_at_the_root(self, tmp_path):
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.checks._common import _get_all_changelog_contexts
        from rlsbl.workspace import load_releasables, load_workspace

        _multi_releasable_workspace(tmp_path)
        projects = load_workspace(str(tmp_path))
        ctx = WorkspaceCheckContext(
            project_root=tmp_path,
            workspace_root=tmp_path,
            config={},
            projects=projects,
            graph=None,
            releasables=load_releasables(str(tmp_path), projects),
        )
        contexts = _get_all_changelog_contexts(ctx)
        dirs = {os.path.basename(os.path.dirname(c[0])) for c in contexts}
        assert dirs == {"alpha", "beta"}

    def test_inside_a_member_only_that_releasable_is_in_scope(self, tmp_path):
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.checks._common import _get_all_changelog_contexts
        from rlsbl.workspace import load_releasables, load_workspace

        _multi_releasable_workspace(tmp_path)
        projects = load_workspace(str(tmp_path))
        ctx = WorkspaceCheckContext(
            project_root=tmp_path / "packages" / "alpha",
            workspace_root=tmp_path,
            config={},
            projects=projects,
            graph=None,
            releasables=load_releasables(str(tmp_path), projects),
        )
        contexts = _get_all_changelog_contexts(ctx)
        dirs = {os.path.basename(os.path.dirname(c[0])) for c in contexts}
        assert dirs == {"alpha"}


# ---------------------------------------------------------------------------
# `release run` at a workspace root
# ---------------------------------------------------------------------------


def _run_release(root, *args, cwd=None):
    """Invoke `rlsbl release run` as a subprocess, returning the CompletedProcess.

    A subprocess rather than a direct call because the behaviour under test is
    the command's own argument handling and its ``sys.exit`` paths.
    """
    return subprocess.run(
        ["rlsbl", "release", "run", *args],
        cwd=str(cwd or root),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.repo_cwd
class TestReleaseRunSelector:
    """At a workspace root the invocation must name the releasable."""

    def test_a_multi_releasable_root_demands_the_selector(self, tmp_path):
        _multi_releasable_workspace(tmp_path)
        result = _run_release(tmp_path, "--no-allow-dirty", "--no-watch",
                              "--approve-consequential")
        assert result.returncode != 0
        assert "must say which releasable" in result.stderr
        assert "--releasable alpha" in result.stderr
        assert "--releasable beta" in result.stderr

    def test_a_single_releasable_root_demands_it_too(self, tmp_path):
        """One candidate is not a licence to guess: naming it is the contract."""
        _single_releasable_workspace(tmp_path)
        result = _run_release(tmp_path, "--no-allow-dirty", "--no-watch",
                              "--approve-consequential")
        assert result.returncode != 0
        assert "must say which releasable" in result.stderr
        assert "--releasable alpha" in result.stderr

    def test_an_unknown_name_is_refused_with_the_declared_set(self, tmp_path):
        _multi_releasable_workspace(tmp_path)
        result = _run_release(tmp_path, "--releasable", "ghost",
                              "--no-allow-dirty", "--no-watch",
                              "--approve-consequential")
        assert result.returncode != 0
        assert "no releasable named 'ghost'" in result.stderr
        assert "alpha, beta" in result.stderr

    @pytest.mark.parametrize("fixture,name", [
        (_single_releasable_workspace, "alpha"),
        (_multi_releasable_workspace, "beta"),
    ])
    def test_the_selector_reaches_the_named_releasable(self, tmp_path, fixture, name):
        """The named releasable is what the run proceeds with.

        The run stops at the missing release file, which is the first thing
        `release run` reads AFTER resolving what it is releasing -- so
        reaching that message proves the selection succeeded, without
        performing a release.
        """
        fixture(tmp_path)
        result = _run_release(tmp_path, "--releasable", name,
                              "--no-allow-dirty", "--no-watch",
                              "--approve-consequential")
        assert result.returncode != 0
        assert "No release file found" in result.stderr, result.stderr
        assert "must say which releasable" not in result.stderr

    def test_a_member_directory_needs_no_selector(self, tmp_path):
        _multi_releasable_workspace(tmp_path)
        result = _run_release(tmp_path, "--no-allow-dirty", "--no-watch",
                              "--approve-consequential",
                              cwd=tmp_path / "packages" / "alpha")
        assert "must say which releasable" not in result.stderr
        assert "No release file found" in result.stderr, result.stderr

    def test_a_member_directory_refuses_the_selector(self, tmp_path):
        """The directory already names it; two answers must not disagree."""
        _multi_releasable_workspace(tmp_path)
        result = _run_release(tmp_path, "--releasable", "beta",
                              "--no-allow-dirty", "--no-watch",
                              "--approve-consequential",
                              cwd=tmp_path / "packages" / "alpha")
        assert result.returncode != 0
        assert "only accepted at the workspace root" in result.stderr

    def test_a_standalone_repository_refuses_the_selector(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "solo", "version": "1.0.0"}) + "\n"
        )
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "none", "targets": ["npm"]}) + "\n"
        )
        result = _run_release(tmp_path, "--releasable", "alpha",
                              "--no-allow-dirty", "--no-watch",
                              "--approve-consequential")
        assert result.returncode != 0
        assert "standalone repository" in result.stderr
