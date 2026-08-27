"""The observe allowlist, checked against its own written standard.

``rlsbl/observe_allowlist.py`` states the standard in prose ("no user-visible
mutation", with three admitted categories and three named bans) and declares a
category plus a reason on every entry.  These tests are the machine-checkable
half: the shape of every entry, the app really consuming the declared list, and
a corpus of argvs that the three bans forbid, none of which may match any
prefix.

The one thing prefix matching cannot express: anything APPENDED to an
allowlisted argv still matches.  ``git fetch origin --quiet --prune`` matches
the pinned four-token fetch entry.  What the narrow prefix buys is that the
short mutating forms (``git fetch --prune``, ``git fetch --all``) no longer do
-- and rlsbl's own call sites are the only producer of these argvs, so the
corpus below is about what a future call site could reach, not about an
adversary.
"""

import re
from pathlib import Path

import pytest

from rlsbl import observe_allowlist as oa


RLSBL_PACKAGE = Path(__file__).resolve().parent.parent / "rlsbl"


def _matches(argv):
    """The entries whose prefix matches *argv*, exactly as strictcli matches."""
    hits = []
    for entry in oa.OBSERVE_ALLOWLIST:
        prefix = entry.argv
        if len(prefix) <= len(argv) and tuple(argv[: len(prefix)]) == prefix:
            hits.append(entry)
    return hits


# ---------------------------------------------------------------------------
# The written standard exists, and every entry declares itself against it
# ---------------------------------------------------------------------------


class TestTheStandardIsWrittenDown:

    def test_the_module_states_the_standard(self):
        doc = oa.__doc__ or ""
        assert "no user-visible mutation" in doc.lower(), (
            "the observe standard must be stated in the module that holds the "
            "allowlist -- a standard nobody can point at is not a standard"
        )
        for ban in ("ref update", "index write", "credential emission"):
            assert ban.split()[0] in doc.lower(), (
                f"the standard must name what it forbids; '{ban}' is missing"
            )

    def test_every_category_carries_its_clause_of_the_standard(self):
        assert oa.OBSERVE_CATEGORIES, "the category set must not be empty"
        for name, clause in oa.OBSERVE_CATEGORIES.items():
            assert isinstance(clause, str) and len(clause.strip()) > 20, (
                f"category {name!r} needs a real clause, not a label"
            )


class TestEveryEntrySatisfiesTheStandard:

    @pytest.mark.parametrize(
        "entry", oa.OBSERVE_ALLOWLIST, ids=lambda e: " ".join(e.argv),
    )
    def test_entry_declares_a_known_category(self, entry):
        assert entry.category in oa.OBSERVE_CATEGORIES, (
            f"{' '.join(entry.argv)} declares category {entry.category!r}, "
            f"which is not one of {sorted(oa.OBSERVE_CATEGORIES)}. The "
            f"category set is closed on purpose: a program that fits none of "
            f"them does not satisfy the standard."
        )

    @pytest.mark.parametrize(
        "entry", oa.OBSERVE_ALLOWLIST, ids=lambda e: " ".join(e.argv),
    )
    def test_entry_states_why_it_is_admitted(self, entry):
        assert isinstance(entry.why, str) and entry.why.strip(), (
            f"{' '.join(entry.argv)} carries no reason"
        )

    @pytest.mark.parametrize(
        "entry", oa.OBSERVE_ALLOWLIST, ids=lambda e: " ".join(e.argv),
    )
    def test_entry_is_not_a_bare_binary(self, entry):
        assert len(entry.argv) >= 2, (
            f"{entry.argv!r} is a single token: it would make EVERY "
            f"subcommand of that binary execute under --dry-run"
        )
        assert all(isinstance(tok, str) and tok for tok in entry.argv)

    def test_no_duplicate_prefixes(self):
        seen = [e.argv for e in oa.OBSERVE_ALLOWLIST]
        assert len(seen) == len(set(seen)), "duplicate allowlist prefixes"

    def test_no_entry_is_a_prefix_of_another(self):
        overlaps = [
            (a.argv, b.argv)
            for a in oa.OBSERVE_ALLOWLIST
            for b in oa.OBSERVE_ALLOWLIST
            if a.argv != b.argv
            and len(a.argv) < len(b.argv)
            and b.argv[: len(a.argv)] == a.argv
        ]
        assert not overlaps, (
            f"one entry already covers another, so the narrower one is dead "
            f"and its reason is not the reason anything is admitted: {overlaps}"
        )


class TestTheAppConsumesTheDeclaredList:

    def test_app_allowlist_is_the_declared_one(self):
        import rlsbl

        assert list(rlsbl.app.proc_observe_allowlist) == oa.prefixes(), (
            "the app must take its allowlist from observe_allowlist.py, or "
            "the standard governs a list nothing uses"
        )


# ---------------------------------------------------------------------------
# The three bans, as a corpus no prefix may match
# ---------------------------------------------------------------------------


REF_UPDATES = [
    # The branch-creating split: `-b` writes a ref, so only the spelling that
    # puts `--prefix` in the third position is admitted.
    ["git", "subtree", "split", "-b", "mirror", "--prefix", "pkg"],
    ["git", "subtree", "split", "-b", "mirror", "--prefix=pkg"],
    ["git", "subtree", "push", "--prefix=pkg", "origin", "main"],
    ["git", "subtree", "add", "--prefix", "pkg", "origin", "main"],
    ["git", "subtree", "pull", "--prefix", "pkg", "origin", "main"],
    ["git", "push", "origin", "HEAD:refs/heads/main"],
    ["git", "push", "--tags"],
    ["git", "fetch", "--prune"],
    ["git", "fetch", "--all"],
    ["git", "fetch", "origin", "--tags"],
    ["git", "fetch", "origin", "--prune"],
    ["git", "update-ref", "refs/heads/main", "HEAD"],
    ["git", "commit", "-m", "x"],
    ["git", "tag", "-a", "v1.0.0", "-m", "x"],
    ["git", "checkout", "main"],
    ["git", "reset", "--hard", "HEAD"],
    ["git", "merge", "origin/main"],
    ["git", "rebase", "origin/main"],
    ["git", "stash", "push"],
    ["git", "remote", "add", "origin", "git@example:x/y"],
    ["git", "merge-file", "a", "b", "c"],
]

WORKTREE_WRITES = [
    # The clone entry is admitted for its scratch DESTINATION, so every other
    # clone spelling -- and every clone that writes into an existing tree --
    # must miss it.
    ["git", "clone", "/some/repo.git", "/home/user/project"],
    ["git", "clone", "--quiet", "/some/repo.git", "/home/user/project"],
    ["git", "clone", "--single-branch", "--branch", "main", "r", "d"],
    ["git", "clone", "--quiet", "--single-branch", "--branch", "release", "r", "d"],
    ["git", "init"],
    ["git", "clean", "-fdx"],
    ["git", "checkout", "--", "."],
]

INDEX_WRITES = [
    ["git", "add", "."],
    # The bare forms: they refresh the index and take index.lock, which is why
    # every call site now passes --no-optional-locks.
    ["git", "status", "--porcelain"],
    ["git", "diff", "--name-only"],
    ["git", "diff-index", "--quiet", "HEAD"],
    ["git", "rm", "--cached", "f"],
]

CREDENTIAL_EMISSION = [
    ["gh", "auth", "token"],
    ["gh", "auth", "token", "--hostname", "github.com"],
    # `gh auth status` prints no token -- until it is asked for one. Both
    # spellings put the live credential on stdout, so neither may ride the
    # status entry's prefix.
    ["gh", "auth", "status", "--show-token"],
    ["gh", "auth", "status", "-t"],
    ["git", "credential", "fill"],
]

REMOTE_MUTATIONS = [
    ["gh", "api", "--method", "POST", "repos/x/y/topics"],
    ["gh", "api", "--method", "PUT", "repos/x/y/topics"],
    ["gh", "api", "repos/x/y", "--method", "DELETE"],
    ["gh", "release", "create", "v1.0.0"],
    ["gh", "release", "delete", "v1.0.0"],
    ["gh", "run", "rerun", "1"],
    ["gh", "workflow", "run", "ci.yml"],
    ["gh", "repo", "create", "x/y"],
    ["npm", "publish"],
    ["npm", "install"],
    ["go", "get", "example.com/m"],
    # -mod=mod turns a read into a manifest write: go updates go.mod and
    # go.sum in place. rlsbl issues `go list -m ...` and `go list -e -f ...`
    # only, so the entries are pinned to those and this cannot match.
    ["go", "list", "-mod=mod", "all"],
    ["uv", "publish"],
    ["uv", "sync"],
    ["ruff", "check", "--fix", "."],
    ["ruff", "format", "."],
    ["safegit", "commit", "-m", "x"],
    ["saferm", "delete", "f"],
    ["selfdoc", "gen"],
]


class TestTheBansHold:

    @pytest.mark.parametrize("argv", REF_UPDATES, ids=" ".join)
    def test_no_prefix_admits_a_ref_update(self, argv):
        assert not _matches(argv), (
            f"{' '.join(argv)} matches {[' '.join(e.argv) for e in _matches(argv)]}: "
            f"the standard forbids ref updates, and an observe really runs "
            f"under --dry-run"
        )

    @pytest.mark.parametrize("argv", WORKTREE_WRITES, ids=" ".join)
    def test_no_prefix_admits_a_worktree_write(self, argv):
        assert not _matches(argv), (
            f"{' '.join(argv)} matches "
            f"{[' '.join(e.argv) for e in _matches(argv)]}: the scratch-write "
            f"clause admits loose objects and brand-new scratch directories "
            f"only, never a write into a tree somebody already has"
        )

    @pytest.mark.parametrize("argv", INDEX_WRITES, ids=" ".join)
    def test_no_prefix_admits_an_index_write(self, argv):
        assert not _matches(argv), (
            f"{' '.join(argv)} matches "
            f"{[' '.join(e.argv) for e in _matches(argv)]}: the standard "
            f"forbids taking index.lock, which is a real hazard in a shared "
            f"worktree"
        )

    @pytest.mark.parametrize("argv", CREDENTIAL_EMISSION, ids=" ".join)
    def test_no_prefix_admits_credential_emission(self, argv):
        assert not _matches(argv), (
            f"{' '.join(argv)} matches "
            f"{[' '.join(e.argv) for e in _matches(argv)]}: a live credential "
            f"must never transit an rlsbl pipe"
        )

    @pytest.mark.parametrize("argv", REMOTE_MUTATIONS, ids=" ".join)
    def test_no_prefix_admits_a_remote_or_local_mutation(self, argv):
        assert not _matches(argv), (
            f"{' '.join(argv)} matches "
            f"{[' '.join(e.argv) for e in _matches(argv)]}"
        )


class TestTheReadsStillMatch:
    """The other half: the argvs rlsbl really issues must stay observable."""

    @pytest.mark.parametrize("argv", [
        ["git", "rev-parse", "HEAD"],
        ["git", "--no-optional-locks", "status", "--porcelain"],
        ["git", "--no-optional-locks", "diff", "--name-only"],
        ["git", "--no-optional-locks", "diff-index", "--quiet", "HEAD"],
        ["git", "merge-file", "-p", "a", "b", "c"],
        ["git", "tag", "--list", "v*"],
        ["gh", "api", "--method", "GET", "repos/x/y"],
        ["gh", "auth", "status", "--hostname", "github.com"],
        ["gh", "run", "view", "1", "--log-failed"],
        ["npm", "view", "pkg", "version"],
        ["go", "list", "-m", "all"],
        ["go", "list", "-e", "-f", "{{.Name}}", "./..."],
    ], ids=" ".join)
    def test_read_argv_is_observable(self, argv):
        assert _matches(argv), f"{' '.join(argv)} is a read but matches nothing"


class TestTheScratchWriteCategory:
    """Two entries write, and are admitted anyway -- narrowly, and on record.

    A branchless ``git subtree split`` leaves loose objects; the mirror's
    inspection ``git clone`` fills a directory the same observation just
    created.  Neither touches a ref, an index or a worktree anybody had, which
    is the scratch-write clause.  Both are pinned to the exact spelling their
    one call site issues, because the two hazards they sit next to -- ``-b``
    on the split, a durable destination on the clone -- are invisible to
    prefix matching.
    """

    SPLIT = ["git", "subtree", "split", "--prefix", "packages/mylib"]
    CLONE = ["git", "clone", "--quiet", "--single-branch", "--branch", "main",
             "git@host:o/r.git", "/tmp/rlsbl-mirror-observe-x/mirror"]

    @pytest.mark.parametrize("argv", [SPLIT, CLONE], ids=lambda a: " ".join(a[:4]))
    def test_the_pinned_scratch_write_is_observable(self, argv):
        assert _matches(argv), (
            f"{' '.join(argv)} matches nothing, so under --dry-run it is "
            f"RECORDED: the mirror reconciler's observation then reads a "
            f"result that does not exist and the preview truncates"
        )

    @pytest.mark.parametrize("argv", [SPLIT, CLONE], ids=lambda a: " ".join(a[:4]))
    def test_they_declare_the_scratch_write_category(self, argv):
        entries = _matches(argv)
        assert [e.category for e in entries] == ["scratch-write"], entries

    def test_the_split_entry_records_its_branch_residual(self):
        entry = next(
            e for e in oa.OBSERVE_ALLOWLIST
            if list(e.argv) == ["git", "subtree", "split", "--prefix"]
        )
        assert "-b" in entry.why, (
            f"the split entry must say that -b creates a ref and that prefix "
            f"matching cannot refuse an appended flag: {entry.why!r}"
        )

    def test_the_clone_entry_records_that_the_reason_is_the_destination(self):
        entry = next(
            e for e in oa.OBSERVE_ALLOWLIST if e.argv[:2] == ("git", "clone")
        )
        assert "DESTINATION" in entry.why or "destination" in entry.why, (
            f"the clone entry must say the justification is where it writes, "
            f"not which flags it carries: {entry.why!r}"
        )

    def test_the_category_states_what_it_forbids(self):
        clause = oa.OBSERVE_CATEGORIES["scratch-write"]
        for word in ("ref", "index", "worktree"):
            assert word in clause.lower(), (
                f"the scratch-write clause must name what it still forbids; "
                f"'{word}' is missing from {clause!r}"
            )

    def test_the_mirror_issues_the_pinned_split_spelling(self):
        """``--prefix=<path>`` is git-identical and allowlist-invisible."""
        import inspect

        from rlsbl.commands.monorepo import mirror_cmd

        src = inspect.getsource(mirror_cmd.compute_split_sha)
        assert '"subtree", "split", "--prefix", project_path' in src, (
            "compute_split_sha must issue --prefix as its own token, or the "
            f"pinned entry matches nothing it runs:\n{src}"
        )


class TestTheFetchIsRetainedByExplicitRuling:
    """`git fetch` is on the list against the ref-update ban, on purpose.

    The standard bans ref updates and names FETCH_HEAD and the remote-tracking
    refs among them, and the pinned `git fetch origin --quiet` writes exactly
    those.  It stays by an explicit ruling: those refs are cache-like git
    plumbing no user workflow reads as state, unlike refs/heads or the index.
    The pin is the ruling's boundary -- the short mutating forms must not
    match.  This group exists so the exception reads as an exception rather
    than sitting unremarked among the ordinary reads.
    """

    PINNED_FETCH = ["git", "fetch", "origin", "--quiet"]

    def test_the_pinned_fetch_is_observable(self):
        assert _matches(self.PINNED_FETCH), (
            "the release flow's one fetch must stay observable, or a preview "
            "of the release stops before it can read the remote"
        )

    def test_the_ruling_is_written_down_where_the_entry_is(self):
        """A retained exception with no recorded reason is indistinguishable
        from an oversight -- which is how it read before this test existed."""
        entry = next(
            e for e in oa.OBSERVE_ALLOWLIST if list(e.argv) == self.PINNED_FETCH
        )
        assert "ruling" in entry.why, (
            f"the fetch entry must say it is an exception and why: {entry.why!r}"
        )
        doc = oa.__doc__ or ""
        assert "ruling" in doc and "cache-like" in doc, (
            "the module docstring must reconcile the retained fetch with the "
            "ref-update ban it stands against"
        )

    @pytest.mark.parametrize("argv", [
        ["git", "fetch", "--prune"],
        ["git", "fetch", "--all"],
        ["git", "fetch", "origin", "--tags"],
    ], ids=" ".join)
    def test_the_ruling_does_not_extend_past_the_pin(self, argv):
        assert not _matches(argv), (
            f"{' '.join(argv)} matches: the ruling covers the pinned argv "
            f"only, and these forms move refs a user reads"
        )


# ---------------------------------------------------------------------------
# The call sites agree with the prefixes
# ---------------------------------------------------------------------------


_BARE_INDEX_TOUCHING = re.compile(
    r'(?:\["git", "(?:status|diff|diff-index)"'
    r'|run\("git", \["(?:status|diff|diff-index)"'
    r'|_git_ok\(\["(?:status|diff|diff-index)"'
    r'|_git_read\(\["(?:status|diff|diff-index)"'
    r'|_git_answer\(\["(?:status|diff|diff-index)")'
)


class TestCallSitesPassTheFlag:

    def test_no_production_call_site_omits_no_optional_locks(self):
        offenders = []
        for path in sorted(RLSBL_PACKAGE.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in _BARE_INDEX_TOUCHING.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(RLSBL_PACKAGE.parent)}:{line}")
        assert not offenders, (
            "git status/diff/diff-index without --no-optional-locks: these "
            "refresh the index and take index.lock, and the allowlist no "
            f"longer admits the bare form, so they would be RECORDED rather "
            f"than observed under --dry-run: {offenders}"
        )


    def test_the_auth_check_issues_the_pinned_argv(self):
        """``check_gh_auth`` must produce the argv the allowlist pins.

        A call site that drops ``--hostname github.com`` matches no prefix, so
        under ``--dry-run`` the auth probe is RECORDED instead of observed:
        ``run_gh_unscoped`` hands back the carrier, nothing raises, and
        ``check_gh_auth`` reports success for a probe that never ran.
        """
        import inspect

        from rlsbl import utils

        src = inspect.getsource(utils.check_gh_auth)
        pinned = [
            e for e in oa.OBSERVE_ALLOWLIST if e.argv[:3] == ("gh", "auth", "status")
        ]
        assert len(pinned) == 1, pinned
        args = ", ".join(f'"{tok}"' for tok in pinned[0].argv[1:])
        assert f"[{args}]" in src, (
            f"check_gh_auth does not issue the pinned argv [{args}]:\n{src}"
        )


class TestTheTokenIsGone:

    def test_no_source_file_asks_gh_for_a_raw_token(self):
        offenders = []
        for path in sorted(RLSBL_PACKAGE.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if '"auth", "token"' in text:
                offenders.append(str(path.relative_to(RLSBL_PACKAGE.parent)))
        assert not offenders, (
            f"`gh auth token` prints a live credential to stdout; these files "
            f"capture it: {offenders}. Let gh apply the credential itself "
            f"(gh api) instead."
        )
