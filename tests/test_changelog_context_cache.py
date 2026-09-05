"""The changelog context is resolved once per check-run context, not once per check.

Every changelog check asks ``_get_all_changelog_contexts`` for the same answer,
and the answer is a pure function of the context object: the workspace root, the
project root, the member list and the releasable list are all fixed for the life
of one ``rlsbl check`` invocation.  Resolving it per check re-walked the
workspace, re-resolved the member, and re-read every releasable's
``unreleased.jsonl`` from disk once per check.

The count below is measured at ``read_unreleased``, which every resolution
performs exactly once per releasable in scope.  With a single-releasable
workspace it is therefore a direct count of resolutions, and it is measured at a
function that exists on both sides of the memoization, so the number it reports
is comparable before and after.

The cache is per context object and nothing else: a fresh context resolves
fresh, so a caller that rebuilds the context (a second check run, a release step
after the tree moved) never reads a stale answer.
"""

import json
from pathlib import Path

import pytest

from conftest import make_workspace, run_git
from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.workspace import Releasable, load_releasables, load_workspace, write_releasable_version


def _workspace(root):
    """One releasable with one member, so one resolution == one read."""
    pkg = Path(root) / "packages" / "alpha"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.json").write_text(
        json.dumps({"name": "alpha", "version": "1.0.0"}, indent=2) + "\n"
    )
    make_workspace(str(root), [
        {"path": "packages/alpha", "name": "alpha", "releasable": "alpha"},
    ], releasables=[Releasable(name="alpha")])
    write_releasable_version(str(root), "alpha", "1.0.0")
    changes = Path(root) / ".rlsbl-monorepo" / "releasables" / "alpha" / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    (changes / "unreleased.jsonl").write_text("")

    run_git(root, "init", "-q")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "initial")
    return root


def _ctx(root, project_root=None):
    projects = load_workspace(str(root))
    return WorkspaceCheckContext(
        project_root=Path(project_root or root),
        workspace_root=Path(root),
        config={},
        projects=projects,
        graph=None,
        releasables=load_releasables(str(root), projects),
    )


@pytest.fixture
def counting_read_unreleased(monkeypatch):
    """Count every ``read_unreleased`` the changelog context resolution performs."""
    from rlsbl.changelog import files as changelog_files

    calls = []
    real = changelog_files.read_unreleased

    def counted(changes_dir, *args, **kwargs):
        calls.append(changes_dir)
        return real(changes_dir, *args, **kwargs)

    monkeypatch.setattr(changelog_files, "read_unreleased", counted)
    return calls


class TestOneResolutionPerContext:

    def test_a_whole_changelog_check_run_resolves_once(
        self, tmp_path, monkeypatch, counting_read_unreleased,
    ):
        """Every changelog check shares one resolution of the same context."""
        _workspace(tmp_path)
        monkeypatch.chdir(tmp_path)
        ctx = _ctx(tmp_path)

        results, _impure, _exit = app.run_checks(
            ctx, tag_expr="changelog", ignore_warnings=True,
        )

        # The run really did exercise the changelog checks (otherwise a count of
        # one would be vacuous).
        assert len(results) > 1
        assert len(counting_read_unreleased) == 1, (
            f"resolved the changelog context {len(counting_read_unreleased)} "
            f"times in one check run: {counting_read_unreleased}"
        )

    def test_a_fresh_context_resolves_fresh(
        self, tmp_path, monkeypatch, counting_read_unreleased,
    ):
        """The cache lives on the context object, so a new one re-reads disk."""
        from rlsbl.checks._common import _get_all_changelog_contexts

        _workspace(tmp_path)
        monkeypatch.chdir(tmp_path)

        first = _ctx(tmp_path)
        _get_all_changelog_contexts(first)
        _get_all_changelog_contexts(first)
        assert len(counting_read_unreleased) == 1

        second = _ctx(tmp_path)
        _get_all_changelog_contexts(second)
        assert len(counting_read_unreleased) == 2

    def test_the_cached_answer_is_the_resolved_one(self, tmp_path, monkeypatch):
        """Caching changes the number of resolutions, never the answer."""
        from rlsbl.checks._common import _get_all_changelog_contexts

        _workspace(tmp_path)
        monkeypatch.chdir(tmp_path)
        ctx = _ctx(tmp_path)

        first = _get_all_changelog_contexts(ctx)
        second = _get_all_changelog_contexts(ctx)
        assert first == second
        assert [c[0] for c in first] == [
            str(tmp_path / ".rlsbl-monorepo" / "releasables" / "alpha" / "changes")
        ]

    def test_the_single_context_resolution_is_cached_too(
        self, tmp_path, monkeypatch, counting_read_unreleased,
    ):
        """The per-member resolution the tag-glob and release-record derivations use."""
        from rlsbl.checks._common import (
            _get_changelog_context,
            _resolve_release_record_dir,
            _resolve_tag_glob,
        )

        _workspace(tmp_path)
        monkeypatch.chdir(tmp_path)
        ctx = _ctx(tmp_path, tmp_path / "packages" / "alpha")

        _get_changelog_context(ctx)
        _resolve_tag_glob(ctx)
        _resolve_release_record_dir(ctx)
        assert len(counting_read_unreleased) == 1
