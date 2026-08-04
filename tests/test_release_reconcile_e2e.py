"""End-to-end tests for `rlsbl release reconcile` against the REAL safegit binary.

The scenario is the one the command exists for: a history rewrite performed
OUT OF BAND -- a raw `safegit scrub`, not `rlsbl release scrub` -- which moves
every commit it touches and thereby invalidates two pieces of release metadata
that live outside the commit graph. The local tags follow the rewrite, but the
remote's tags and the GitHub Releases attached to them still point at commits
that no longer exist.

`rlsbl release reconcile` reads safegit's rewrite journal, works out which tags
that rewrite moved, re-pushes them with explicit leases, and recreates their
GitHub Releases from the changelog.

The git side is real (real rewrite, real bare remote, real force-push with
lease). Only the gh/network boundary is mocked, exactly as the scrub e2e tests
do.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.changelog.generate import generate_changelog
from rlsbl.commands.release_reconcile import (
    ReconcileError,
    plan_reconcile,
    run_cmd,
    snapshot_remote_refs,
)
from rlsbl.commands.release_scrub import _load_rewrite_journal
from rlsbl.context import ProjectContext

from githarness import (
    add_remote as _add_remote,
    commit_file as _commit_file,
    git as _git,
    init_repo,
    remote_ref as _remote_ref,
)

MOD = "rlsbl.commands.release_reconcile"

SECRET = "SECRETTOKEN456"
REPLACEMENT = "REDACTEDVALUE"


@pytest.fixture
def e2e_env(safegit_bin, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "PATH", str(safegit_bin.parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    return tmp_path


def _jsonl_line(commits, user_facing=True, description="A change", type_="fix"):
    entry = {"commits": commits, "user_facing": user_facing}
    if user_facing:
        entry["description"] = description
        entry["type"] = type_
    return json.dumps(entry) + "\n"


def _setup_released_repo(env):
    """A repo with one released version (tag + CHANGELOG) and a bare remote."""
    repo = env / "repo"
    init_repo(repo, email="e2e@test.local", name="E2E")

    c1 = _commit_file(repo, "config.env", f"token={SECRET}\n", "add config")
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "1.0.0.jsonl").write_text(
        _jsonl_line([c1], description="**Ship it.** The first release.",
                    type_="feature")
    )
    (changes / "unreleased.jsonl").write_text("")
    _git(repo, "add", ".rlsbl/changes/1.0.0.jsonl",
         ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog")
    generate_changelog(str(repo))
    _git(repo, "add", "CHANGELOG.md", ".rlsbl")
    _git(repo, "commit", "-q", "-m", "generate changelog")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "release v1.0.0")

    _add_remote(repo, env / "remote")
    _git(repo, "push", "--no-verify", "origin", "v1.0.0")
    return repo


def _raw_safegit_scrub(repo):
    """Rewrite history OUT OF BAND: raw safegit, no rlsbl orchestration.

    Nothing about this invocation is orchestrated: rlsbl never sees it, no
    --remap-shas-in is passed, and no release state is written -- which is
    exactly the situation `rlsbl release reconcile` exists to repair. safegit
    does not stand in the way of a direct scrub in a repo with a .rlsbl/
    directory; `--approve-consequential` is here only because safegit declares
    scrub consequential and `--json` does not answer its prompt.
    """
    result = subprocess.run(
        ["safegit", "--approve-consequential", "scrub", "match", "--json",
         "--pattern", SECRET, "--replace", REPLACEMENT,
         "--entire-history", "--reason", "remove leaked token"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"raw safegit scrub failed ({result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _gh_recorder(calls, existing_tags=("v1.0.0",)):
    def fake_gh(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["release", "view"]:
            if args[2] in existing_tags:
                return '{"body": "old notes"}'
            raise subprocess.CalledProcessError(1, "gh release view")
        return ""

    return fake_gh


def _run_reconcile(repo, *, gh_calls=None, flags=None, gh_available=True):
    ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
    patches = [
        patch(f"{MOD}.check_gh_installed", return_value=gh_available),
        patch(f"{MOD}.check_gh_auth", return_value=gh_available),
        patch(f"{MOD}.run_gh",
              side_effect=_gh_recorder(gh_calls if gh_calls is not None else [])),
    ]
    for p in patches:
        p.start()
    try:
        run_cmd(flags or {}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


class TestReconcileAfterRawRewrite:

    def test_remap_then_reconcile_restores_tags_and_releases(
        self, e2e_env, monkeypatch,
    ):
        repo = _setup_released_repo(e2e_env)
        old_remote_tag = _remote_ref(repo, "refs/tags/v1.0.0")
        monkeypatch.chdir(repo)

        _raw_safegit_scrub(repo)

        # The rewrite moved the tag locally; the remote still holds the old one.
        new_local_tag = _git(repo, "rev-parse", "refs/tags/v1.0.0")
        assert new_local_tag != old_remote_tag
        assert _remote_ref(repo, "refs/tags/v1.0.0") == old_remote_tag, (
            "a raw rewrite leaves the remote tag stale -- that is the bug "
            "reconcile exists to fix"
        )

        # A journal was written by the rewrite: that is what reconcile reads.
        journal = _load_rewrite_journal()
        assert journal is not None and journal["commit_map"]

        # Repair the changelog hashes (the existing remap path), then reconcile
        # the metadata that lives outside the commit graph.
        from rlsbl.changelog.files import remap_jsonl_hashes

        remap_jsonl_hashes(str(repo / ".rlsbl" / "changes"), journal["commit_map"])
        generate_changelog(str(repo))

        gh_calls = []
        _run_reconcile(repo, gh_calls=gh_calls)

        # --- The remote tag now matches the rewritten local tag ---
        assert _remote_ref(repo, "refs/tags/v1.0.0") == new_local_tag, (
            "reconcile must force-push the rewritten tag to the remote"
        )
        # And it resolves to a commit that exists in the rewritten history.
        remote_peeled = _git(repo, "rev-parse", "refs/tags/v1.0.0^{}")
        assert remote_peeled in _git(repo, "rev-list", "HEAD").split()

        # --- The GitHub Release was recreated on the moved tag ---
        assert ["release", "delete", "v1.0.0", "--yes"] in gh_calls
        created = [c for c in gh_calls if c[:2] == ["release", "create"]]
        assert created, "the Release must be recreated"
        assert created[0][2] == "v1.0.0"
        notes = created[0][created[0].index("--notes") + 1]
        assert "Ship it" in notes, "notes must come from the changelog"

    def test_a_second_run_is_a_no_op(self, e2e_env, monkeypatch):
        """Reconcile is idempotent: once the remote matches, nothing moves."""
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)
        _run_reconcile(repo)

        gh_calls = []
        _run_reconcile(repo, gh_calls=gh_calls)
        assert gh_calls == [], "a reconciled repo must not touch the forge again"

    def test_a_divergence_the_journal_does_not_explain_is_refused(
        self, e2e_env, monkeypatch, capsys,
    ):
        """Fail-closed: reconcile force-pushes, so an unexplained divergence
        must never be swept along."""
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        # Someone moves the tag by hand onto an unrelated commit after the
        # rewrite: the journal's map cannot explain remote -> local.
        extra = _commit_file(repo, "note.txt", "hand-made\n", "manual commit")
        _git(repo, "tag", "-d", "v1.0.0")
        _git(repo, "tag", "v1.0.0", extra)

        with pytest.raises(SystemExit) as exc:
            _run_reconcile(repo)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "does not explain" in err
        assert "v1.0.0" in err

    def test_no_journal_is_a_hard_error(self, e2e_env, monkeypatch, capsys):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        with pytest.raises(SystemExit) as exc:
            _run_reconcile(repo)
        assert exc.value.code == 1
        assert "no safegit rewrite journal" in capsys.readouterr().err

    def test_dry_run_plans_without_touching_anything(
        self, e2e_env, monkeypatch, capsys,
    ):
        repo = _setup_released_repo(e2e_env)
        old_remote_tag = _remote_ref(repo, "refs/tags/v1.0.0")
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        gh_calls = []
        _run_reconcile(repo, gh_calls=gh_calls,
                       flags={"dry-run": True})

        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "v1.0.0" in out
        assert gh_calls == []
        assert _remote_ref(repo, "refs/tags/v1.0.0") == old_remote_tag, (
            "a dry run must not move the remote"
        )


class TestPlanReconcile:
    """The planner's classification, without the forge in the picture."""

    def test_tags_absent_from_the_remote_are_left_alone(self, e2e_env, monkeypatch):
        repo = _setup_released_repo(e2e_env)
        _git(repo, "tag", "v9.9.9")  # never pushed
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        journal = _load_rewrite_journal()
        tags, skipped = plan_reconcile(
            journal["commit_map"], snapshot_remote_refs(),
        )
        assert skipped.get("v9.9.9") == "not on the remote"
        assert [t["refname"] for t in tags] == ["refs/tags/v1.0.0"]

    def test_an_unexplained_divergence_raises(self, e2e_env, monkeypatch):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)
        with pytest.raises(ReconcileError):
            plan_reconcile({}, snapshot_remote_refs())
