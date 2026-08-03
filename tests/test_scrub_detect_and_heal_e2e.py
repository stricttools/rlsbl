"""End-to-end proof that DETECT-AND-HEAL replaces prevent-by-handshake.

safegit used to refuse a destructive history rewrite in a repository carrying
a ``.rlsbl/`` directory unless an orchestration handshake was set in the
environment. That guard is gone. Nothing stops an operator (or an agent, or a
script) from running a raw ``safegit scrub`` in a release-managed repo, and the
rewrite invalidates release metadata that lives outside the commit graph: the
commit hashes named in the JSONL changelog, the remote's tags, and the GitHub
Releases attached to those tags.

The replacement contract is a loop, and this test walks all of it against the
REAL safegit binary:

1. RAW REWRITE -- ``safegit scrub`` runs with no orchestration whatsoever and
   PROCEEDS. This is the assertion the deleted guard used to make false.
2. JOURNAL -- the rewrite leaves ``.git/safegit/rewrite-maps.jsonl`` behind.
   That file is the entire interface between the two tools.
3. DETECT -- rlsbl's changelog hash-resolution check (the ``changelog-hashes``
   error check) fails on the now-dangling hashes. Loud, not silent.
4. HEAL -- ``rlsbl changelog remap --from-journal`` repairs the changelog from
   the journal, and ``rlsbl release reconcile`` re-pushes the tags the rewrite
   moved and recreates their GitHub Releases.

The git side is real (real rewrite, real bare remote, real force-push with
lease). Only the gh/network boundary is mocked, exactly as the scrub and
reconcile e2e tests do.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.changelog.files import read_unreleased
from rlsbl.changelog.generate import generate_changelog
from rlsbl.changelog.validate import check_hashes_resolve
from rlsbl.commands.changelog_cmd import cmd_remap
from rlsbl.commands.release_reconcile import run_cmd as reconcile_run_cmd
from rlsbl.commands.release_scrub import _load_rewrite_journal
from rlsbl.context import ProjectContext

from githarness import (
    add_remote as _add_remote,
    commit_file as _commit_file,
    git as _git,
    init_repo,
    remote_ref as _remote_ref,
)

RECONCILE_MOD = "rlsbl.commands.release_reconcile"

SECRET = "LEAKEDTOKEN789"
REPLACEMENT = "REDACTEDVALUE"


@pytest.fixture
def e2e_env(safegit_bin, monkeypatch, tmp_path):
    """Put the pinned, guard-free safegit first on PATH."""
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


def _setup_managed_repo(env):
    """A release-managed repo: one released version (tag + CHANGELOG + pushed
    tag on a bare remote) plus unreleased changelog entries naming commits that
    a rewrite will move."""
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

    # Unreleased work, tracked in the JSONL the way rlsbl tracks it. These are
    # the hashes a rewrite invalidates and the remap must repair.
    c2 = _commit_file(repo, "app.py", f"KEY = '{SECRET}'\n", "add app")
    (changes / "unreleased.jsonl").write_text(
        _jsonl_line([c2], description="**New app.** It does things.",
                    type_="feature")
    )
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: new app")

    _add_remote(repo, env / "remote")
    _git(repo, "push", "--no-verify", "origin", "v1.0.0")
    return repo, c2


def _raw_safegit_scrub(repo):
    """The unorchestrated rewrite: raw safegit, nothing announced to rlsbl.

    No handshake environment variable is set -- none exists. ``--yes`` is the
    deliberate consent for a destructive operation (``--json`` does not answer
    that confirmation); it says nothing about orchestration.
    """
    return subprocess.run(
        ["safegit", "scrub", "match", "--json", "--yes",
         "--pattern", SECRET, "--replace", REPLACEMENT,
         "--entire-history", "--reason", "remove leaked token"],
        cwd=str(repo), capture_output=True, text=True,
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


def _run_reconcile(repo, gh_calls):
    ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
    patches = [
        patch(f"{RECONCILE_MOD}.check_gh_installed", return_value=True),
        patch(f"{RECONCILE_MOD}.check_gh_auth", return_value=True),
        patch(f"{RECONCILE_MOD}.run_gh", side_effect=_gh_recorder(gh_calls)),
    ]
    for p in patches:
        p.start()
    try:
        reconcile_run_cmd({"yes": True}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


def _unreleased_entries(repo):
    return read_unreleased(str(repo / ".rlsbl" / "changes"))


class TestRawRewriteIsDetectedAndHealed:

    def test_the_whole_loop(self, e2e_env, monkeypatch):
        repo, unreleased_commit = _setup_managed_repo(e2e_env)
        old_remote_tag = _remote_ref(repo, "refs/tags/v1.0.0")
        monkeypatch.chdir(repo)

        # -- 1. The raw rewrite PROCEEDS. --------------------------------
        # The deleted guard made this exit 1 with "run 'rlsbl release scrub'".
        result = _raw_safegit_scrub(repo)
        assert result.returncode == 0, (
            "a raw safegit scrub in a release-managed repo must proceed; "
            f"exit {result.returncode}\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "RLSBL_SCRUB_ORCHESTRATED" not in result.stderr, (
            "no orchestration handshake may be advertised any more"
        )
        assert SECRET not in _git(repo, "show", "HEAD:app.py"), (
            "the rewrite did not actually scrub the secret"
        )

        # -- 2. The rewrite left a JOURNAL. ------------------------------
        journal_path = repo / ".git" / "safegit" / "rewrite-maps.jsonl"
        assert journal_path.is_file(), (
            "the journal is the only interface the heal has; it must exist"
        )
        journal = _load_rewrite_journal()
        assert journal is not None and journal["commit_map"]
        assert unreleased_commit in journal["commit_map"], (
            "the journal must map the commit the changelog names"
        )

        # -- 3. rlsbl DETECTS the damage as a hard error. ----------------
        passed, details = check_hashes_resolve(_unreleased_entries(repo))
        assert not passed, (
            "the changelog-hashes error check must fail after an "
            "out-of-band rewrite -- that failure IS the detection"
        )
        assert any(unreleased_commit in d for d in details), (
            f"the failure must name the dangling hash; got {details}"
        )

        # -- 4a. HEAL the changelog from the journal. --------------------
        cmd_remap({"from-journal": True}, str(repo))

        passed, details = check_hashes_resolve(_unreleased_entries(repo))
        assert passed, f"the remap must make every hash resolve again: {details}"
        new_commit = journal["commit_map"][unreleased_commit]
        assert any(new_commit in e.commits for e in _unreleased_entries(repo)), (
            "the entry must now name the rewritten commit"
        )

        # -- 4b. HEAL the metadata outside the commit graph. -------------
        generate_changelog(str(repo))
        new_local_tag = _git(repo, "rev-parse", "refs/tags/v1.0.0")
        assert _remote_ref(repo, "refs/tags/v1.0.0") == old_remote_tag, (
            "before reconcile the remote tag is still the pre-rewrite one"
        )

        gh_calls = []
        _run_reconcile(repo, gh_calls)

        assert _remote_ref(repo, "refs/tags/v1.0.0") == new_local_tag, (
            "reconcile must force-push the rewritten tag to the remote"
        )
        remote_peeled = _git(repo, "rev-parse", "refs/tags/v1.0.0^{}")
        assert remote_peeled in _git(repo, "rev-list", "HEAD").split(), (
            "the restored tag must point into the rewritten history"
        )
        assert ["release", "delete", "v1.0.0", "--yes"] in gh_calls
        created = [c for c in gh_calls if c[:2] == ["release", "create"]]
        assert created and created[0][2] == "v1.0.0", (
            "the GitHub Release must be recreated on the moved tag"
        )

    def test_a_raw_author_rewrite_also_proceeds(self, e2e_env, monkeypatch):
        """The guard covered author rewrite too; it is equally unblocked."""
        repo, _ = _setup_managed_repo(e2e_env)
        monkeypatch.chdir(repo)

        result = subprocess.run(
            ["safegit", "--yes", "author", "rewrite",
             "--old-name", "E2E", "--new-name", "Renamed"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            "a raw author rewrite in a release-managed repo must proceed; "
            f"exit {result.returncode}\nstderr: {result.stderr}"
        )
        assert "Renamed" in _git(repo, "log", "--format=%an", "-1")
