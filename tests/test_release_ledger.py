"""The release ledger: archived release files as the authority on what shipped.

Covers :mod:`rlsbl.ledger` -- the archive enumeration, the two questions it
answers differently (what bounds the unreleased range vs what the latest
release is), the three read errors, and the consumers that were migrated onto
it.


The enumeration
===============

Every place that answered "what was released" from git's tag namespace, its
question, and what became of it.  A site stays tag-based only when its
question is about TAGS -- translating a version into a tag name, enumerating a
tag scheme, or observing the tag namespace as a reconciler's input -- never
when it is about which version is current.

Unreleased range / coverage -- MIGRATED to the highest anchored version whose
candidate_sha is an ancestor of the checkout (``ledger.range_anchor``):

===================================================  ==========================
Site                                                 Was
===================================================  ==========================
``rlsbl/changelog/resolve.py`` ``_unreleased_range``  ``git describe`` +
                                                     ``<tag>..HEAD``
``rlsbl/changelog/validate.py`` ``check_in_range``    the above
``rlsbl/changelog/validate.py`` ``check_coverage``    the above
``rlsbl/changelog/validate.py`` ``check_no_orphans``  the above
``rlsbl/changelog/files.py`` ``_warn_stale_entries``  the above
``rlsbl/checks/changelog.py`` (the four callers)      passed only a tag glob
``rlsbl/tool_checks.py`` ``release_context_env``      ``git describe`` for
                                                     ``RLSBL_UNRELEASED_RANGE``
``rlsbl/commands/status.py`` ``_collect_status``      the above
``rlsbl/commands/unreleased.py`` ``run_cmd``          ``git describe`` +
                                                     ``git log <tag>..HEAD``
``rlsbl/commands/monorepo/commands.py``               ``git tag -l <glob>
``_latest_tag_for_glob`` (status coverage)           --sort=-v:refname``
``rlsbl/commands/monorepo/batch_release_init.py``     the same tag-list dialect
===================================================  ==========================

Latest-release FACT -- MIGRATED to the absolute highest archived version,
annotated when the checkout does not contain it
(``ledger.latest_release_fact``):

======================================================  =======================
Site                                                    Was
======================================================  =======================
``rlsbl/commands/status.py`` last-release line          unscoped
                                                        ``git describe
                                                        --tags --abbrev=0``
``rlsbl/commands/unreleased.py`` header / payload tag   ``git describe``
``rlsbl/commands/watch.py`` commit labeling and         ``git describe
release-page URL                                        --exact-match``
``rlsbl/commands/undo.py`` ``_find_latest_tag``         ``git describe
                                                        --tags --abbrev=0``
``rlsbl/commands/undo.py`` predecessor lookup           ``git describe
                                                        --tags --abbrev=0
                                                        <tag>^``
======================================================  =======================

Release preparation -- MIGRATED to the ledger:

* ``rlsbl/commands/release/validate.py`` ``compute_release_version`` decided
  "first release vs bump" from ``tag_exists_locally``; it now decides from the
  ledger, and ``_abort_on_destroyed_tag`` diagnoses a destroyed tag from the
  archive rather than from the finalized changelog alone.
* ``rlsbl/commands/release/validate.py`` gained
  ``ledger.require_checkout_contains_latest``: preparing a release on a
  checkout that does not contain the latest release's candidate is a hard
  error.

Legitimately still tag-based, with the reason:

* ``rlsbl/utils.py`` ``tag_exists_locally`` / ``tag_exists_on_remote`` /
  ``remote_tag_commit`` -- tag EXISTENCE, asked to refuse a colliding tag
  before creating one.  Not a question about which version is current.
* ``rlsbl/ledger.py`` ``tag_for_version`` -- tag TRANSLATION, the inverse of
  the glob construction.  Names the tag a version carries; never picks a
  version.
* ``rlsbl/tag_glob.py`` -- glob construction and tag parsing for the three tag
  schemes.  Scheme machinery, no anchoring.
* ``rlsbl/commands/release_reconcile.py`` local ``git tag -l`` -- the OBSERVE
  layer of a reconciler.  Its input IS the tag namespace; converging it is the
  whole job.
* ``rlsbl/releasable_migration.py`` ``_git_describe_tag`` -- reads the
  PRE-migration per-package tag namespace to derive the releasable's first
  tag.  The old scheme's tags are the migration's input, and per-package
  archives are not consolidated yet at that point.
* ``rlsbl/commands/monorepo/extract*.py`` tag-decision reads -- DEFERRED, not
  examined here: those files were under concurrent edit.

``rlsbl/utils.py`` ``get_last_version_tag`` -- the ``git describe`` primitive
every one of those sites went through -- had no callers left after the
migration and was DELETED.  Its last would-be consumer, ``RLSBL_LAST_TAG``,
needs a tag REF for a consumer subprocess, and now selects the version from
the ledger and translates it with ``tag_for_version``.
"""

import os
import pathlib
import subprocess

import pytest

from rlsbl import ledger
from rlsbl.errors import LedgerError
from rlsbl.release_file import (
    archived_release_path,
    list_archived_versions,
    write_archived_release_file,
    writable_release_file,
)


_TREE = "b" * 40


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@test.local")
    _git(path, "config", "user.name", "Test")
    return path


def _commit(repo, message):
    marker = repo / "log.txt"
    marker.write_text((marker.read_text() if marker.exists() else "") + message + "\n")
    _git(repo, "add", "log.txt")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _archive(releases_dir, version, sha, *, unanchorable=False):
    os.makedirs(releases_dir, exist_ok=True)
    return write_archived_release_file(
        str(releases_dir), version,
        bump="patch", include=["pypi"], description=f"release {version}",
        candidate_sha=None if unanchorable else sha,
        tree_hashes=None if unanchorable else {".": _TREE},
        unanchorable=unanchorable,
    )


class _Repo(type(pathlib.Path())):
    """A repo path that also carries the SHA of each release it archived."""

    shas: dict


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo whose ledger agrees with its tags: three releases, all tagged."""
    r = _Repo(_init_repo(tmp_path / "agree"))
    monkeypatch.chdir(r)
    releases = r / ".rlsbl" / "releases"
    shas = {}
    for version in ("0.1.0", "0.2.0", "0.3.0"):
        shas[version] = _commit(r, f"v{version}")
        _git(r, "tag", f"v{version}")
        _archive(releases, version, shas[version])
    r.shas = shas
    return r


def _releases(repo_path):
    return str(repo_path / ".rlsbl" / "releases")


# --------------------------------------------------------------------------- #
# The enumeration: a filename scan, highest first
# --------------------------------------------------------------------------- #

class TestListArchivedVersions:

    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert list_archived_versions(str(tmp_path / "nope")) == []

    def test_highest_first(self, tmp_path):
        d = tmp_path / "releases"
        for v in ("0.2.0", "0.10.0", "0.2.1"):
            _archive(d, v, "a" * 40)
        assert list_archived_versions(str(d)) == ["0.10.0", "0.2.1", "0.2.0"]

    def test_prereleases_sort_below_their_stable(self, tmp_path):
        d = tmp_path / "releases"
        for v in ("0.5.0", "0.5.0-rc.1", "0.5.0-alpha.2", "0.5.0-beta.1"):
            _archive(d, v, "a" * 40)
        assert list_archived_versions(str(d)) == [
            "0.5.0", "0.5.0-rc.1", "0.5.0-beta.1", "0.5.0-alpha.2",
        ]

    def test_non_archive_files_are_ignored(self, tmp_path):
        d = tmp_path / "releases"
        _archive(d, "0.1.0", "a" * 40)
        (d / "unreleased.toml").write_text("bump = \"\"\n")
        (d / "in-progress.json").write_text("{}")
        (d / "v0.1.0.plan.json").write_text("{}")
        (d / "vNotAVersion.toml").write_text("")
        assert list_archived_versions(str(d)) == ["0.1.0"]

    def test_the_scan_opens_nothing(self, tmp_path, monkeypatch):
        # The scan is a listdir: enumerating history must not cost one parse
        # per released version. Guarded because the whole lazy design rests on
        # it -- rlsbl's own ledger is 224 archives.
        d = tmp_path / "releases"
        for v in ("0.1.0", "0.2.0"):
            _archive(d, v, "a" * 40)
        real_open = open

        def refuse(path, *args, **kwargs):
            if str(path).endswith(".toml"):
                raise AssertionError(f"the scan opened {path}")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", refuse)
        assert list_archived_versions(str(d)) == ["0.2.0", "0.1.0"]


# --------------------------------------------------------------------------- #
# Reading an entry for use: the agree / disagree / missing-anchor fixtures
# --------------------------------------------------------------------------- #

class TestReadEntry:

    def test_agreeing_tag_and_anchor(self, repo):
        entry = ledger.read_entry(_releases(repo), "0.2.0")
        assert entry.candidate_sha == repo.shas["0.2.0"]
        assert entry.anchored is True
        assert entry.unanchorable is False

    def test_no_tag_at_all_is_not_a_disagreement(self, repo):
        # A version whose tag was deleted still reads fine: the archive is the
        # record, and the tag's absence is not evidence against it.
        _git(repo, "tag", "-d", "v0.2.0")
        entry = ledger.read_entry(_releases(repo), "0.2.0")
        assert entry.candidate_sha == repo.shas["0.2.0"]

    def test_tag_pointing_elsewhere_is_a_hard_error(self, repo):
        other = repo.shas["0.1.0"]
        _git(repo, "tag", "-f", "v0.2.0", other)
        with pytest.raises(LedgerError) as exc:
            ledger.read_entry(_releases(repo), "0.2.0")
        text = str(exc.value)
        assert "0.2.0" in text
        assert other in text                      # the tag's commit
        assert repo.shas["0.2.0"] in text         # the anchor's commit
        assert "v0.2.0" in text                   # the tag name

    def test_disagreement_uses_the_monorepo_tag_scheme(self, tmp_path, monkeypatch):
        r = _init_repo(tmp_path / "mono")
        monkeypatch.chdir(r)
        first = _commit(r, "one")
        second = _commit(r, "two")
        releases = r / ".rlsbl-monorepo" / "releasables" / "lib" / "releases"
        _archive(releases, "0.4.0", second)
        _git(r, "tag", "lib@v0.4.0", first)
        with pytest.raises(LedgerError, match="lib@v0.4.0"):
            ledger.read_entry(str(releases), "0.4.0", tag_glob="lib@v*")

    def test_unanchorable_entry_reads_as_such(self, tmp_path, monkeypatch):
        r = _init_repo(tmp_path / "unanchorable")
        monkeypatch.chdir(r)
        _commit(r, "one")
        releases = r / ".rlsbl" / "releases"
        _archive(releases, "0.3.1", None, unanchorable=True)
        entry = ledger.read_entry(str(releases), "0.3.1")
        assert entry.unanchorable is True
        assert entry.candidate_sha is None
        assert entry.anchored is False

    def test_archive_with_neither_anchor_nor_marker_is_a_hard_error(self, repo):
        # A pre-backfill archive: written before anchoring existed.
        path = archived_release_path(_releases(repo), "0.2.0")
        with writable_release_file(path):
            text = open(path).read()
            stripped = "\n".join(
                line for line in text.splitlines()
                if not line.startswith("candidate_sha")
                and not line.startswith("[tree_hashes]")
                and not line.startswith('"." =')
            )
            open(path, "w").write(stripped + "\n")
        with pytest.raises(LedgerError) as exc:
            ledger.read_entry(_releases(repo), "0.2.0")
        text = str(exc.value)
        # The complete single-version recovery: the derived value, the unlock,
        # the edit, the relock, and the re-validation.
        assert repo.shas["0.2.0"] in text
        assert "writable_release_file" in text
        assert "chmod 644" in text
        assert "chmod 444" in text
        assert "tree_hashes" in text
        assert "Re-run the check" in text

    def test_missing_anchor_without_a_tag_says_there_is_nothing_to_derive(self, repo):
        path = archived_release_path(_releases(repo), "0.2.0")
        _git(repo, "tag", "-d", "v0.2.0")
        with writable_release_file(path):
            text = open(path).read()
            open(path, "w").write(
                "\n".join(
                    line for line in text.splitlines()
                    if not line.startswith("candidate_sha")
                    and not line.startswith("[tree_hashes]")
                    and not line.startswith('"." =')
                ) + "\n"
            )
        with pytest.raises(LedgerError) as exc:
            ledger.read_entry(_releases(repo), "0.2.0")
        assert "does not exist locally" in str(exc.value)
        assert "unanchorable" in str(exc.value)


# --------------------------------------------------------------------------- #
# The range anchor: the highest release this checkout CONTAINS
# --------------------------------------------------------------------------- #

class TestRangeAnchor:

    def test_picks_the_highest_contained_release(self, repo):
        entry = ledger.range_anchor(_releases(repo))
        assert entry.version == "0.3.0"

    def test_a_release_not_in_this_history_does_not_bound_the_range(self, repo):
        # 0.4.0 was released on a history this checkout does not have.
        _archive(_releases(repo), "0.4.0", "c" * 40)
        # Fetching that commit is impossible here, so make the object real but
        # unreachable from HEAD: an orphan branch.
        _git(repo, "checkout", "-q", "--orphan", "sidelined")
        sha = _commit(repo, "released elsewhere")
        _git(repo, "checkout", "-q", "main")
        _archive_path = archived_release_path(_releases(repo), "0.4.0")
        with writable_release_file(_archive_path):
            body = open(_archive_path).read().replace("c" * 40, sha)
            open(_archive_path, "w").write(body)
        entry = ledger.range_anchor(_releases(repo))
        assert entry.version == "0.3.0"

    def test_an_unanchorable_version_is_skipped(self, repo):
        # 0.3.0's archive says its commit is unrecoverable, so 0.2.0 bounds it.
        path = archived_release_path(_releases(repo), "0.3.0")
        os.chmod(path, 0o644)
        os.remove(path)
        _archive(_releases(repo), "0.3.0", None, unanchorable=True)
        entry = ledger.range_anchor(_releases(repo))
        assert entry.version == "0.2.0"

    def test_no_archives_means_no_anchor(self, tmp_path, monkeypatch):
        r = _init_repo(tmp_path / "virgin")
        monkeypatch.chdir(r)
        _commit(r, "one")
        assert ledger.range_anchor(str(r / ".rlsbl" / "releases")) is None

    def test_unreleased_range_is_the_anchor_commit(self, repo):
        assert ledger.unreleased_range(_releases(repo)) == (
            f"{repo.shas['0.3.0']}..HEAD"
        )

    def test_unreleased_range_before_the_first_release_is_head(self, tmp_path,
                                                               monkeypatch):
        r = _init_repo(tmp_path / "virgin2")
        monkeypatch.chdir(r)
        _commit(r, "one")
        assert ledger.unreleased_range(str(r / ".rlsbl" / "releases")) == "HEAD"

    def test_the_range_ignores_a_tag_that_disagrees_with_nothing(self, repo):
        # An extra tag on a later commit -- a hand-made one, no archive behind
        # it -- must not move the range. `git describe` would have picked it.
        _commit(repo, "after the release")
        _git(repo, "tag", "v9.9.9")
        assert ledger.unreleased_range(_releases(repo)) == (
            f"{repo.shas['0.3.0']}..HEAD"
        )


# --------------------------------------------------------------------------- #
# Indeterminable ancestry: not a "no"
# --------------------------------------------------------------------------- #

class TestIndeterminable:

    def test_an_anchor_whose_object_is_missing_is_a_hard_error(self, repo):
        _archive(_releases(repo), "0.4.0", "c" * 40)
        with pytest.raises(LedgerError) as exc:
            ledger.range_anchor(_releases(repo))
        text = str(exc.value)
        assert "cannot determine" in text
        assert "git fetch --unshallow" in text
        assert "c" * 40 in text

    def test_truncated_history_is_indeterminable_not_absent(self, tmp_path,
                                                            monkeypatch):
        # A shallow clone can SEE the anchor commit but cannot walk to it, and
        # git answers exit 1 -- the same code it gives for a genuine "no".
        origin = _init_repo(tmp_path / "origin")
        base = _commit(origin, "base")
        for i in range(4):
            _commit(origin, f"later {i}")
        clone = tmp_path / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(clone)],
            check=True, capture_output=True, timeout=60,
        )
        monkeypatch.chdir(clone)
        _archive(clone / ".rlsbl" / "releases", "0.1.0", base)
        with pytest.raises(LedgerError, match="cannot determine"):
            ledger.range_anchor(str(clone / ".rlsbl" / "releases"))


# --------------------------------------------------------------------------- #
# Labelling one commit with the release it IS
# --------------------------------------------------------------------------- #

class TestReleaseAtCommit:

    def test_a_release_candidate_names_its_version(self, repo):
        entry = ledger.release_at_commit(_releases(repo), repo.shas["0.2.0"])
        assert entry.version == "0.2.0"

    def test_a_commit_between_releases_names_nothing(self, repo):
        sha = _commit(repo, "after 0.3.0")
        assert ledger.release_at_commit(_releases(repo), sha) is None

    def test_a_commit_inside_a_later_release_names_nothing(self, repo):
        # An ordinary commit that a LATER release contains: the answer is
        # None, not the release above it and not the release below it.
        ordinary = _commit(repo, "work")
        later = _commit(repo, "v0.4.0")
        _archive(_releases(repo), "0.4.0", later)
        assert ledger.release_at_commit(_releases(repo), ordinary) is None
        assert ledger.release_at_commit(_releases(repo), later).version == "0.4.0"

    def test_survives_the_tag_being_deleted(self, repo):
        _git(repo, "tag", "-d", "v0.2.0")
        entry = ledger.release_at_commit(_releases(repo), repo.shas["0.2.0"])
        assert entry.version == "0.2.0"

    def test_costs_one_archive_read(self, repo, monkeypatch):
        # The whole point of asking range_anchor at the commit rather than
        # scanning: labelling a commit must not parse the entire history.
        opened = []
        real_open = open

        def counting(path, *args, **kwargs):
            if str(path).endswith(".toml"):
                opened.append(str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", counting)
        ledger.release_at_commit(_releases(repo), repo.shas["0.3.0"])
        assert len(opened) == 1, opened


# --------------------------------------------------------------------------- #
# The latest-release fact: the absolute highest, annotated
# --------------------------------------------------------------------------- #

class TestLatestReleaseFact:

    def test_contained_release_is_reported_plainly(self, repo):
        fact = ledger.latest_release_fact(_releases(repo))
        assert fact.version == "0.3.0"
        assert fact.in_checkout is True
        assert fact.label() == "0.3.0"

    def test_release_outside_this_history_is_annotated_not_hidden(self, repo):
        _git(repo, "checkout", "-q", "--orphan", "sidelined")
        sha = _commit(repo, "released elsewhere")
        _git(repo, "checkout", "-q", "main")
        _archive(_releases(repo), "0.4.0", sha)
        fact = ledger.latest_release_fact(_releases(repo))
        assert fact.version == "0.4.0"          # the FACT, not the range anchor
        assert fact.in_checkout is False
        assert fact.label() == "0.4.0 (not in this checkout's history)"

    def test_no_releases_yet(self, tmp_path, monkeypatch):
        r = _init_repo(tmp_path / "virgin3")
        monkeypatch.chdir(r)
        _commit(r, "one")
        fact = ledger.latest_release_fact(str(r / ".rlsbl" / "releases"))
        assert fact.version is None
        assert fact.label() == "(none)"

    def test_unanchorable_latest_is_labelled(self, repo):
        _archive(_releases(repo), "0.4.0", None, unanchorable=True)
        fact = ledger.latest_release_fact(_releases(repo))
        assert fact.version == "0.4.0"
        assert fact.in_checkout is None
        assert fact.label() == "0.4.0 (commit not recoverable)"


# --------------------------------------------------------------------------- #
# Release preparation on a divergent checkout
# --------------------------------------------------------------------------- #

class TestRequireCheckoutContainsLatest:

    def test_passes_when_the_checkout_has_the_latest_release(self, repo):
        ledger.require_checkout_contains_latest(_releases(repo))

    def test_hard_errors_when_it_does_not(self, repo):
        _git(repo, "checkout", "-q", "--orphan", "sidelined")
        sha = _commit(repo, "released elsewhere")
        _git(repo, "checkout", "-q", "main")
        _archive(_releases(repo), "0.4.0", sha)
        with pytest.raises(LedgerError) as exc:
            ledger.require_checkout_contains_latest(_releases(repo))
        text = str(exc.value)
        assert "0.4.0" in text
        assert sha in text
        assert "not an ancestor" in text

    def test_no_releases_is_not_an_error(self, tmp_path, monkeypatch):
        r = _init_repo(tmp_path / "virgin4")
        monkeypatch.chdir(r)
        _commit(r, "one")
        ledger.require_checkout_contains_latest(str(r / ".rlsbl" / "releases"))

    def test_unanchorable_latest_is_not_an_error(self, repo):
        _archive(_releases(repo), "0.4.0", None, unanchorable=True)
        ledger.require_checkout_contains_latest(_releases(repo))


# --------------------------------------------------------------------------- #
# Path derivations
# --------------------------------------------------------------------------- #

class TestPathDerivations:

    def test_releases_dir_pairs_with_the_changes_dir(self):
        assert ledger.releases_dir_for_changes_dir(
            os.path.join("proj", ".rlsbl", "changes")
        ) == os.path.join("proj", ".rlsbl", "releases")

    def test_releasable_layout(self):
        assert ledger.releases_dir_for_changes_dir(
            os.path.join("ws", ".rlsbl-monorepo", "releasables", "www", "changes")
        ) == os.path.join("ws", ".rlsbl-monorepo", "releasables", "www", "releases")

    def test_trailing_separator_is_tolerated(self):
        assert ledger.releases_dir_for_changes_dir(
            os.path.join("proj", ".rlsbl", "changes") + os.sep
        ) == os.path.join("proj", ".rlsbl", "releases")

    @pytest.mark.parametrize("glob,expected", [
        ("v*", "v1.2.3"),
        (None, "v1.2.3"),
        ("mylib@v*", "mylib@v1.2.3"),
        ("pkg/sub/v*", "pkg/sub/v1.2.3"),
    ])
    def test_tag_translation(self, glob, expected):
        assert ledger.tag_for_version(glob, "1.2.3") == expected
