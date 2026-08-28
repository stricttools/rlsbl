"""End-to-end tests for `rlsbl release reconcile` against the REAL safegit binary.

The scenario the command was first written for is still here: a history rewrite
performed OUT OF BAND -- a raw `safegit scrub`, not `rlsbl release scrub` --
which moves every commit it touches and thereby invalidates two pieces of
release metadata that live outside the commit graph. The local tags follow the
rewrite; the remote's tags and the GitHub Releases attached to them still point
at commits that no longer exist.

What changed is the shape of the answer. The reconcile is no longer a
journal-driven tag pusher: it observes origin once, judges every subject against
FOUR merged explanation sources (safegit's journal, the release ledger's
anchors, the lineage records, and the committed scrub archives), and emits one
merged preview whose verdicts are ``materialize``, ``already-correct``,
``re-point-with-lease``, ``refuse-foreign`` or ``refuse-identity-mismatch``.
Consent is file-driven: ``--plan`` writes the plan, ``--apply`` performs it.

The journal-only case in this file is the pin that the merge did not change the
answer where the journal is the only record present.

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
    STATE_ALREADY_CORRECT,
    STATE_MATERIALIZE,
    STATE_REFUSE_FOREIGN,
    STATE_RE_POINT,
    collect_explanations,
    observe_world,
    build_preview,
    plan_path,
    read_plan,
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
        if args[:2] == ["release", "list"]:
            return "\n".join(existing_tags)
        if args[:2] == ["release", "view"]:
            if args[2] in existing_tags:
                if "--json" in args:
                    return "old notes"
                return '{"body": "old notes"}'
            raise subprocess.CalledProcessError(1, "gh release view")
        return ""

    return fake_gh


def _ctx(repo):
    return ProjectContext(project_root=repo, workspace_root=None, config={})


def _run_reconcile(repo, *, mode, gh_calls=None, flags=None, gh_available=True,
                   existing_tags=("v1.0.0",)):
    ctx = _ctx(repo)
    patches = [
        patch(f"{MOD}.check_gh_installed", return_value=gh_available),
        patch(f"{MOD}.check_gh_auth", return_value=gh_available),
        patch(f"{MOD}.run_gh",
              side_effect=_gh_recorder(
                  gh_calls if gh_calls is not None else [],
                  existing_tags=existing_tags,
              )),
    ]
    for p in patches:
        p.start()
    try:
        run_cmd({"mode": mode, **(flags or {})}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


def _plan_then_apply(repo, *, gh_calls=None, existing_tags=("v1.0.0",)):
    _run_reconcile(repo, mode="plan", existing_tags=existing_tags)
    _run_reconcile(repo, mode="apply", gh_calls=gh_calls,
                   existing_tags=existing_tags)


def _observe(repo):
    """The observation and preview a plan would be built from."""
    from rlsbl.commands.release_reconcile import _resolve_identity

    ctx = _ctx(repo)
    target, ref_ctx, releases_dir = _resolve_identity(ctx)
    with patch(f"{MOD}.check_gh_installed", return_value=False):
        observation = observe_world(ctx=ctx)
    explanations = collect_explanations([releases_dir], ref_ctx.lineage_paths)
    return build_preview(
        observation=observation, explanations=explanations, target=target,
        ref_ctx=ref_ctx, releases_dir=releases_dir,
    )


class TestReconcileAfterRawRewrite:

    def test_the_moved_tag_is_re_pointed_with_a_lease(self, e2e_env, monkeypatch):
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

        # A journal was written by the rewrite: that is one of the four sources.
        journal = _load_rewrite_journal()
        assert journal is not None and journal["commit_map"]

        preview = _observe(repo)
        assert preview.by_key("refs/tags/v1.0.0").state == STATE_RE_POINT

        # Repair the changelog hashes (the existing remap path), then reconcile
        # the metadata that lives outside the commit graph.
        from rlsbl.changelog.files import remap_jsonl_hashes

        remap_jsonl_hashes(str(repo / ".rlsbl" / "changes"), journal["commit_map"])
        generate_changelog(str(repo))

        gh_calls = []
        _plan_then_apply(repo, gh_calls=gh_calls)

        assert _remote_ref(repo, "refs/tags/v1.0.0") == new_local_tag, (
            "reconcile must force-push the rewritten tag to the remote"
        )
        remote_peeled = _git(repo, "rev-parse", "refs/tags/v1.0.0^{}")
        assert remote_peeled in _git(repo, "rev-list", "HEAD").split()

    def test_the_release_marker_is_re_pointed_too(self, e2e_env, monkeypatch):
        """A moved tag drags its Release along, but the body's marker is stale.

        The Release follows the TAG NAME, so re-pointing the tag silently moves
        the Release onto the new commit -- while its ``rlsbl-ci-sha`` marker,
        which is what the publish workflow reads, still names the old one.
        """
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        gh_calls = []
        _plan_then_apply(repo, gh_calls=gh_calls)

        edits = [c for c in gh_calls if c[:2] == ["release", "edit"]]
        assert edits, f"the marker must be re-pointed, got {gh_calls}"

    def test_a_second_run_is_a_no_op(self, e2e_env, monkeypatch, capsys):
        """Reconcile is idempotent: once the remote matches, nothing moves."""
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)
        _plan_then_apply(repo)

        preview = _observe(repo)
        assert preview.items == (), (
            "a reconciled repo has nothing left to judge"
        )

    def test_dry_run_plans_without_writing_the_plan_file(
        self, e2e_env, monkeypatch, capsys,
    ):
        repo = _setup_released_repo(e2e_env)
        old_remote_tag = _remote_ref(repo, "refs/tags/v1.0.0")
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        gh_calls = []
        _run_reconcile(repo, mode="plan", gh_calls=gh_calls,
                       flags={"dry-run": True})

        out = capsys.readouterr().out
        assert "re-point-with-lease" in out
        assert "v1.0.0" in out
        assert "Dry run" in out
        assert _remote_ref(repo, "refs/tags/v1.0.0") == old_remote_tag, (
            "a dry run must not move the remote"
        )
        assert not os.path.exists(
            plan_path(repo / ".rlsbl" / "releases")
        ), "a dry run writes no plan file either"

    def test_apply_without_a_plan_is_refused(self, e2e_env, monkeypatch, capsys):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        with pytest.raises(SystemExit) as exc:
            _run_reconcile(repo, mode="apply")
        assert exc.value.code == 1
        assert "no reconcile plan" in capsys.readouterr().err

    def test_a_plan_whose_world_moved_is_refused(
        self, e2e_env, monkeypatch, capsys,
    ):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)
        _run_reconcile(repo, mode="plan")
        assert os.path.exists(plan_path(repo / ".rlsbl" / "releases"))

        # Someone pushes another tag: the observed world is no longer the one
        # the plan's leases were captured from.
        _git(repo, "tag", "v0.9.0")
        _git(repo, "push", "--no-verify", "origin", "v0.9.0")

        with pytest.raises(SystemExit) as exc:
            _run_reconcile(repo, mode="apply")
        assert exc.value.code == 1
        assert "the world changed" in capsys.readouterr().err


class TestThePublicationTripwire:

    def test_a_divergence_no_source_explains_is_refused(
        self, e2e_env, monkeypatch, capsys,
    ):
        """Fail-closed: reconcile force-pushes, so an unexplained divergence
        must never be swept along."""
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        # Someone moves the tag by hand onto an unrelated commit after the
        # rewrite: no journal entry, lineage record or scrub archive maps the
        # remote value to this one.
        extra = _commit_file(repo, "note.txt", "hand-made\n", "manual commit")
        _git(repo, "tag", "-d", "v1.0.0")
        _git(repo, "tag", "v1.0.0", extra)

        with pytest.raises(SystemExit) as exc:
            _run_reconcile(repo, mode="plan")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "no record explains" in err
        assert "v1.0.0" in err

    def test_one_unexplained_ref_aborts_everything(
        self, e2e_env, monkeypatch, capsys,
    ):
        """A repairable ref beside an unexplained one is NOT repaired."""
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)

        # A second released tag that the rewrite WILL legitimately move.
        _commit_file(repo, "extra.py", f"K = '{SECRET}'\n", "more work")
        _git(repo, "tag", "-a", "v1.1.0", "-m", "release v1.1.0")
        _git(repo, "push", "--no-verify", "origin", "v1.1.0")
        repairable_remote = _remote_ref(repo, "refs/tags/v1.1.0")

        _raw_safegit_scrub(repo)

        # And one hand-moved tag nothing explains.
        extra = _commit_file(repo, "note.txt", "hand-made\n", "manual commit")
        _git(repo, "tag", "-d", "v1.0.0")
        _git(repo, "tag", "v1.0.0", extra)

        preview = _observe(repo)
        states = dict(zip(preview.keys, preview.states))
        assert states["refs/tags/v1.0.0"] == STATE_REFUSE_FOREIGN
        assert states["refs/tags/v1.1.0"] == STATE_RE_POINT

        with pytest.raises(SystemExit):
            _run_reconcile(repo, mode="plan")

        assert _remote_ref(repo, "refs/tags/v1.1.0") == repairable_remote, (
            "the repairable tag must be left alone: a reconcile that repaired "
            "around an unexplained divergence would be choosing which half of "
            "an inconsistent world to trust"
        )
        assert not os.path.exists(plan_path(repo / ".rlsbl" / "releases")), (
            "a refused plan must not be written either"
        )


class TestWithoutAJournal:
    """The journal lives under .git and does not survive a clone."""

    def test_no_journal_and_nothing_diverging_is_simply_nothing_to_do(
        self, e2e_env, monkeypatch, capsys,
    ):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)

        _run_reconcile(repo, mode="plan")
        out = capsys.readouterr().out
        assert "No explanation source is present" in out
        assert "Nothing to reconcile" in out

    def test_no_journal_and_a_divergence_is_the_tripwire(
        self, e2e_env, monkeypatch, capsys,
    ):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        extra = _commit_file(repo, "note.txt", "hand-made\n", "manual commit")
        _git(repo, "tag", "-d", "v1.0.0")
        _git(repo, "tag", "v1.0.0", extra)

        with pytest.raises(SystemExit) as exc:
            _run_reconcile(repo, mode="plan")
        assert exc.value.code == 1
        assert "no record explains" in capsys.readouterr().err


class TestTheVerdictClassification:
    """The planner's classification, without the forge in the picture."""

    def test_tags_absent_from_the_remote_are_not_judged_as_refs_to_repair(
        self, e2e_env, monkeypatch,
    ):
        """A local-only tag the ledger does not name is outside the account."""
        repo = _setup_released_repo(e2e_env)
        _git(repo, "tag", "v9.9.9")  # never pushed
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        preview = _observe(repo)
        assert preview.by_key("refs/tags/v9.9.9") is None
        assert preview.by_key("refs/tags/v1.0.0").state == STATE_RE_POINT

    def test_a_matching_ref_is_already_correct(self, e2e_env, monkeypatch):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        preview = _observe(repo)
        # Nothing diverges, so the local-tag half emits nothing at all.
        assert preview.items == ()

    def test_the_remote_snapshot_is_still_read_in_one_call(
        self, e2e_env, monkeypatch,
    ):
        repo = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        refs = snapshot_remote_refs()
        assert "refs/tags/v1.0.0" in refs
