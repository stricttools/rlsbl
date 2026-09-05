"""``rlsbl transition record``: the typed door onto the operator-declared facts.

Two of the transition record's event kinds are not written by any surgery --
they are DECLARATIONS an operator makes about a repository they read:

* ``non-version-tag`` -- this tag stands outside the version model on purpose;
* ``release-history-closed`` -- this member's or releasable's release history
  is deliberately over.

Before this command the only way to write either was a Python snippet the
backfill's own error message spelled out.  These tests pin the door: what it
accepts, where it writes, what it refuses, and -- the point of recording a fact
at all -- that the two readers of the tag namespace change their answer once
the fact exists.
"""

import os
from pathlib import Path

import pytest

from githarness import commit_file, git, init_repo

from rlsbl.commands.transition_record_cmd import run_cmd
from rlsbl.context import create_context
from rlsbl.transition_record import (
    KIND_CONVERSION,
    KIND_NON_VERSION_TAG,
    KIND_RELEASE_HISTORY_CLOSED,
    get_transition_record_path,
    read_events,
)


def make_repo(tmp_path, name="proj"):
    repo = tmp_path / name
    repo.mkdir()
    init_repo(repo)
    (repo / ".rlsbl").mkdir()
    commit_file(repo, "README.md", "hello\n", "initial")
    return repo


def ctx_for(repo):
    return create_context(Path(repo))


def record_path(repo):
    return Path(get_transition_record_path(str(repo)))


def _flags(**kwargs):
    base = {"dry-run": False, "auto-commit": True, "reason": "because"}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Recording each kind
# ---------------------------------------------------------------------------


class TestRecordingANonVersionTag:

    def test_it_appends_the_validated_event(self, tmp_path):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly",
                   reason="a nightly build marker"),
            ctx=ctx_for(repo),
        )
        events = read_events(str(record_path(repo)))
        assert [e.KIND for e in events] == [KIND_NON_VERSION_TAG]
        assert events[0].tag == "nightly"
        assert events[0].reason == "a nightly build marker"
        assert events[0].id and events[0].recorded_at

    def test_it_commits_the_record(self, tmp_path):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly"),
            ctx=ctx_for(repo),
        )
        assert git(repo, "status", "--porcelain") == ""
        assert "transitions.jsonl" in git(
            repo, "show", "--name-only", "--format=", "HEAD",
        )

    def test_no_auto_commit_leaves_the_file_uncommitted(self, tmp_path):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly",
                   **{"auto-commit": False}),
            ctx=ctx_for(repo),
        )
        assert "transitions.jsonl" in git(
            repo, "status", "--porcelain", "--untracked-files=all",
        )


class TestRecordingAClosedReleaseHistory:

    def test_it_appends_the_validated_event(self, tmp_path):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_RELEASE_HISTORY_CLOSED, subject="widget",
                   reason="extracted into its own repository"),
            ctx=ctx_for(repo),
        )
        events = read_events(str(record_path(repo)))
        assert [e.KIND for e in events] == [KIND_RELEASE_HISTORY_CLOSED]
        assert events[0].subject == "widget"
        assert events[0].reason == "extracted into its own repository"


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


class TestTheDryRun:

    def test_it_writes_nothing(self, tmp_path):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly",
                   **{"dry-run": True}),
            ctx=ctx_for(repo),
        )
        assert not record_path(repo).exists()
        assert git(repo, "status", "--porcelain") == ""

    def test_it_prints_the_event_it_would_write(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly",
                   reason="a nightly build marker", **{"dry-run": True}),
            ctx=ctx_for(repo),
        )
        out = capsys.readouterr().out
        assert '"kind":"non-version-tag"' in out
        assert '"tag":"nightly"' in out
        assert "nothing was written" in out


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


class TestTheRefusals:

    def test_an_empty_reason_is_refused(self, tmp_path):
        repo = make_repo(tmp_path)
        with pytest.raises(SystemExit) as exc:
            run_cmd(
                _flags(kind=KIND_NON_VERSION_TAG, subject="nightly", reason="   "),
                ctx=ctx_for(repo),
            )
        assert exc.value.code == 1
        assert not record_path(repo).exists()

    def test_an_empty_subject_is_refused(self, tmp_path):
        repo = make_repo(tmp_path)
        with pytest.raises(SystemExit):
            run_cmd(
                _flags(kind=KIND_NON_VERSION_TAG, subject="  "),
                ctx=ctx_for(repo),
            )
        assert not record_path(repo).exists()

    def test_a_duplicate_declaration_names_the_existing_event(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly",
                   reason="a nightly build marker"),
            ctx=ctx_for(repo),
        )
        first = read_events(str(record_path(repo)))[0]

        with pytest.raises(SystemExit) as exc:
            run_cmd(
                _flags(kind=KIND_NON_VERSION_TAG, subject="nightly",
                       reason="declared twice"),
                ctx=ctx_for(repo),
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert first.id in err
        assert "a nightly build marker" in err
        # The second declaration reached no line of the record.
        assert len(read_events(str(record_path(repo)))) == 1

    def test_a_duplicate_of_the_other_kind_is_judged_per_subject(self, tmp_path):
        """Same tag string, different kind: two different facts, both legal."""
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="widget"),
            ctx=ctx_for(repo),
        )
        run_cmd(
            _flags(kind=KIND_RELEASE_HISTORY_CLOSED, subject="widget"),
            ctx=ctx_for(repo),
        )
        assert len(read_events(str(record_path(repo)))) == 2

    def test_an_unknown_kind_is_refused_at_routing(self, tmp_path):
        """Structurally unreachable through the choice; refused anyway.

        The kind selection admits exactly the two operator-declared arms, so
        no argv reaches this. It is refused defensively so that widening the
        choice without teaching the router is a hard error rather than an
        event written with a shape nobody checked.
        """
        repo = make_repo(tmp_path)
        with pytest.raises(SystemExit):
            run_cmd(
                _flags(kind=KIND_CONVERSION, subject="whatever"),
                ctx=ctx_for(repo),
            )
        assert not record_path(repo).exists()


# ---------------------------------------------------------------------------
# Where it writes
# ---------------------------------------------------------------------------


class TestStorePlacement:

    def test_a_workspace_writes_the_workspace_scoped_record(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / ".rlsbl-monorepo").mkdir()
        (repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            "[[projects]]\n", encoding="utf-8",
        )
        ctx = create_context(Path(repo), workspace_root=Path(repo))
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly"), ctx=ctx,
        )
        assert (repo / ".rlsbl-monorepo" / "transitions.jsonl").is_file()
        assert not (repo / ".rlsbl" / "transitions.jsonl").exists()


# ---------------------------------------------------------------------------
# The recorded fact changes what the readers say
# ---------------------------------------------------------------------------


class TestTheBackfillStopsReportingTheTag:
    """``rlsbl release backfill`` lists every tag it cannot account for."""

    def _repo(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / ".rlsbl" / "releases").mkdir()
        (repo / ".rlsbl" / "changes").mkdir()
        commit_file(repo, "a.txt", "a\n", "work")
        git(repo, "tag", "nightly-2026-01-01")
        return repo

    def test_before_recording_the_tag_is_unexplained(self, tmp_path):
        from rlsbl.release_backfill import build_plan

        repo = self._repo(tmp_path)
        plan = build_plan(str(repo), use_gh=False)
        assert [e.tag for e in plan.unexplained] == ["nightly-2026-01-01"]

    def test_after_recording_it_is_not(self, tmp_path):
        from rlsbl.release_backfill import build_plan

        repo = self._repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly-2026-01-01",
                   reason="a nightly build marker"),
            ctx=ctx_for(repo),
        )
        plan = build_plan(str(repo), use_gh=False)
        assert plan.unexplained == []


class TestReconcileTreatsItAsExplained:
    """``rlsbl release reconcile`` skips a tag outside the version model."""

    def _preview(self, repo, sha):
        from rlsbl.commands.release_reconcile import (
            Explanations,
            Observation,
            build_preview,
        )
        from rlsbl.targets.base import BaseTarget
        from rlsbl.targets.refs import ref_context

        refname = "refs/tags/nightly-2026-01-01"
        return build_preview(
            observation=Observation(
                remote_refs={refname: sha, f"{refname}^{{}}": sha},
                local_refs={refname: "9" * 40, f"{refname}^{{}}": "9" * 40},
                releases=frozenset(), releases_known=False,
            ),
            explanations=Explanations(),
            target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(repo)),
            releases_dir=os.path.join(str(repo), ".rlsbl", "releases"),
        )

    def test_before_recording_the_divergent_tag_fires_the_tripwire(self, tmp_path):
        from rlsbl.commands.release_reconcile import STATE_REFUSE_FOREIGN

        repo = make_repo(tmp_path)
        preview = self._preview(repo, "8" * 40)
        item = preview.by_key("refs/tags/nightly-2026-01-01")
        assert item is not None and item.state == STATE_REFUSE_FOREIGN

    def test_after_recording_no_verdict_is_owed(self, tmp_path):
        repo = make_repo(tmp_path)
        run_cmd(
            _flags(kind=KIND_NON_VERSION_TAG, subject="nightly-2026-01-01",
                   reason="a nightly build marker"),
            ctx=ctx_for(repo),
        )
        preview = self._preview(repo, "8" * 40)
        assert preview.by_key("refs/tags/nightly-2026-01-01") is None
