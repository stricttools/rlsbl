"""End-to-end: a scrub must leave the release record readable.

The audit that produced this test is short to state. ``rlsbl release scrub``
remapped the JSONL changelog hashes through the rewrite's commit map and moved
the tags -- but never the release ARCHIVES, which record each version's
``candidate_sha``. Afterwards the tag pointed at the rewritten commit and the
archive still named the old one, so every guarded release record read raised the
DISAGREEMENT error, whose text accuses the TAG of having moved. The tag was the
one thing the scrub had repaired.

Both directions are exercised against the REAL safegit binary:

* ``rlsbl release scrub`` moves the release commits as part of its own flow, records an
  ``anchor-remap`` transition record event, and commits both.
* A RAW ``safegit scrub`` -- no rlsbl orchestration -- leaves the release record broken,
  and rlsbl heals it from the persisted rewrite journal.

Only the gh/network boundary is mocked, exactly as the other scrub e2e tests do.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from githarness import (
    add_remote as _add_remote,
    commit_file as _commit_file,
    git as _git,
    init_repo,
)

from rlsbl import release_record
from rlsbl.changelog.generate import generate_changelog
from rlsbl.commands.release_scrub import run_cmd as scrub_run_cmd
from rlsbl.context import ProjectContext
from rlsbl.errors import ReleaseRecordError
from rlsbl.transition_record import KIND_RELEASE_COMMIT_REMAP, get_transition_record_path, read_events
from rlsbl.release_file import write_archived_release_file

SCRUB_MOD = "rlsbl.commands.release_scrub"

SECRET = "SCRUBSECRET123"
REPLACEMENT = "REDACTEDVALUES"


@pytest.fixture
def e2e_env(safegit_bin, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "PATH", str(safegit_bin.parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    return tmp_path


def _jsonl_line(commits, description="A change", type_="feature"):
    return json.dumps({
        "commits": commits, "user_facing": True,
        "description": description, "type": type_,
    }) + "\n"


def _setup_released_repo(env):
    """A released repo whose archive release commits the release, and a bare remote.

    The secret lives in a commit MESSAGE, not in a released file: a scrub of a
    message rewrites the commit and leaves every tree byte-identical, which is
    the case where re-recording is provably safe.
    """
    repo = env / "repo"
    init_repo(repo, email="e2e@test.local", name="E2E")

    c1 = _commit_file(repo, "app.py", "print('hi')\n", f"add app ({SECRET})")
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "1.0.0.jsonl").write_text(
        _jsonl_line([c1], "**Ship it.** The first release.")
    )
    (changes / "unreleased.jsonl").write_text("")
    _git(repo, "add", ".rlsbl/changes/1.0.0.jsonl",
         ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog")

    # The release's own ordering: the CANDIDATE is the commit CI verified, and
    # the archive plus the finalized CHANGELOG.md are committed on top of it.
    released = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{released}^{{tree}}")
    write_archived_release_file(
        str(repo / ".rlsbl" / "releases"), "1.0.0",
        bump="minor", include=["plain"], description="The first release.",
        candidate_sha=released, tree_hashes={".": tree},
    )
    generate_changelog(str(repo))
    _git(repo, "add", "-f", "CHANGELOG.md", ".rlsbl")
    _git(repo, "commit", "-q", "-m", "finalize the release")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "release v1.0.0", released)

    _add_remote(repo, env / "remote")
    _git(repo, "push", "--no-verify", "origin", "main")
    _git(repo, "push", "--no-verify", "origin", "v1.0.0")
    return repo, released


def _releases_dir(repo):
    return str(repo / ".rlsbl" / "releases")


def _read_release_record(repo):
    return release_record.read_entry(
        _releases_dir(repo), "1.0.0", tag_glob="v*", cwd=str(repo),
    )


def _run_scrub(repo, ctx_config=None):
    ctx = ProjectContext(
        project_root=repo, workspace_root=None, config=ctx_config or {},
    )
    with patch(f"{SCRUB_MOD}.check_gh_installed", return_value=False), \
         patch(f"{SCRUB_MOD}.check_gh_auth", return_value=False), \
         patch(f"{SCRUB_MOD}.run_gh", return_value=""):
        scrub_run_cmd({
            "pattern": SECRET, "replace": REPLACEMENT,
            "entire-history": True, "reason": "remove leaked token",
        }, ctx=ctx)


def _raw_safegit_scrub(repo):
    result = subprocess.run(
        ["safegit", "--approve-consequential", "scrub", "match", "--json",
         "--pattern", SECRET, "--replace", REPLACEMENT,
         "--entire-history", "--reason", "remove leaked token"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"raw safegit scrub failed:\n{result.stdout}\n{result.stderr}"
    )


class TestScrubMovesTheReleaseCommits:

    def test_the_release_record_reads_again_after_a_scrub(self, e2e_env, monkeypatch):
        repo, released = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)

        _run_scrub(repo)

        entry = _read_release_record(repo)
        assert entry.candidate_sha != released, (
            "the rewrite moved the released commit, so the release commit must move too"
        )
        tag_commit = _git(repo, "rev-parse", "refs/tags/v1.0.0^{}")
        assert entry.candidate_sha == tag_commit, (
            "the release commit and the tag must name the same commit again"
        )

    def test_the_remap_is_recorded_in_the_transition_record(self, e2e_env, monkeypatch):
        repo, released = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)

        _run_scrub(repo)

        events = read_events(
            get_transition_record_path(str(repo)), kinds=[KIND_RELEASE_COMMIT_REMAP],
        )
        assert len(events) == 1, (
            "the release commit move is a repository-surgery fact, and a fresh clone "
            "has no safegit journal to reconstruct it from"
        )
        mappings = events[0].mappings
        assert [m.old_sha for m in mappings] == [released]
        assert mappings[0].new_sha == _read_release_record(repo).candidate_sha

    def test_the_repair_is_committed(self, e2e_env, monkeypatch):
        repo, _released = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)

        _run_scrub(repo)

        assert _git(repo, "status", "--porcelain") == "", (
            "the rewritten archive and the transition record must ride the "
            "scrub's own commit, not be left dirty for the operator"
        )
        tracked = _git(repo, "ls-files", ".rlsbl/transitions.jsonl")
        assert tracked, "the transition record must be committed"


class TestTheRedWithoutTheRepair:

    def test_a_raw_rewrite_breaks_every_release_record_read(self, e2e_env, monkeypatch):
        """The audit's reproduction, with no rlsbl orchestration at all."""
        repo, _released = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)

        _raw_safegit_scrub(repo)

        with pytest.raises(ReleaseRecordError) as exc:
            _read_release_record(repo)
        assert "disagree" in str(exc.value)

    def test_the_journal_heals_it(self, e2e_env, monkeypatch):
        repo, released = _setup_released_repo(e2e_env)
        monkeypatch.chdir(repo)
        _raw_safegit_scrub(repo)

        from rlsbl.commands.release_scrub import heal_release_commits_from_journal

        touched = heal_release_commits_from_journal(str(repo), None, None, str(repo))

        assert touched, "the journal must be able to explain the moved release commit"
        entry = _read_release_record(repo)
        assert entry.candidate_sha != released
        assert entry.candidate_sha == _git(
            repo, "rev-parse", "refs/tags/v1.0.0^{}",
        )
