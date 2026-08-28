"""The release ledger: archived release files as the authority on what shipped.

Covers :mod:`rlsbl.ledger` -- the archive enumeration, the two questions it
answers differently (what bounds the unreleased range vs what the latest
release is), the four read errors, and the consumers that were migrated onto
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
``_coverage_column`` (both status tables)            --sort=-v:refname``
``rlsbl/commands/monorepo/batch_release_init.py``     the same tag-list dialect
``_get_unreleased_commit_count``
===================================================  ==========================

Latest-release FACT -- MIGRATED to the absolute highest archived version:

======================================================  =======================
Site                                                    Was
======================================================  =======================
``rlsbl/commands/status.py`` last-release line          unscoped
                                                        ``git describe
                                                        --tags --abbrev=0``
``rlsbl/commands/unreleased.py`` header / payload       ``git describe``
``rlsbl/commands/monorepo/commands.py`` Released        ``git tag -l <glob>
column (both status tables)                             --sort=-v:refname``
``rlsbl/commands/watch.py`` commit labeling and         ``git describe
release-page URL                                        --exact-match``
``rlsbl/commands/undo.py`` ``_find_latest_release``     ``git describe
(which release is being undone)                         --tags --abbrev=0``
``rlsbl/commands/undo.py``                              ``git describe
``_predecessor_anchor``                                 --tags --abbrev=0
                                                        <tag>^``
======================================================  =======================

``status``, ``unreleased`` and both monorepo status tables go through
``ledger.latest_release_fact``, which annotates the version when the checkout
does not contain it. ``watch`` asks ``ledger.release_at_commit`` -- which
release a given commit IS -- and that costs one archive read, not a scan.

``undo`` reads which VERSION is latest (an archive-existence scan through
``list_archived_versions``) and then reads TWO anchors: the version's own --
the commit CI verified and the tag was created on, which is where its release
commits are found -- and the predecessor's, which bounds that search from
below. Tag deletion still operates on the tag namespace, and a tag pointing
somewhere other than the anchor is reported as a WARNING rather than refused:
undo is an observe-and-repair layer over tags in the same sense ``release
reconcile`` is, and refusing to start on a tag/anchor disagreement would refuse
exactly the repair the operator came for -- the archive wins, and the tag is
being deleted anyway.

Release preparation -- MIGRATED to the ledger:

* ``rlsbl/commands/release/validate.py`` ``compute_release_version`` decided
  "first release vs bump" from ``tag_exists_locally``; it now decides from
  ``ledger.version_is_archived``, and consults the tag only as corroboration
  through the new tri-state ``rlsbl.utils.local_tag_state`` -- whose UNKNOWN
  answer (a preview past its first recorded mutation) may no longer be read as
  "not released". ``_abort_on_destroyed_tag`` names the archive as its
  evidence, falling back to the finalized changelog for repositories whose
  releases predate archiving.
* ``rlsbl/commands/release/validate.py`` gained
  ``ledger.require_checkout_contains_latest``: preparing a release on a
  checkout that does not contain the latest release's candidate is a hard
  error.

Legitimately still tag-based, with the reason:

* ``rlsbl/utils.py`` ``tag_exists_locally`` / ``local_tag_state`` /
  ``tag_exists_on_remote`` / ``remote_tag_commit`` -- tag EXISTENCE, asked to
  refuse a colliding tag before creating one, and to tell a destroyed tag from
  a missing release.  Not a question about which version is current.
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

import io
import os
import pathlib
import subprocess
import sys

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

    def test_the_disagreement_does_not_argue_from_the_file_mode(self, repo):
        """The archive's authority is that the release flow authored it.

        The message used to argue "read-only from the instant the release
        wrote it", which is a premise the repository cannot carry: git records
        no read-only bit, so every fresh clone has writable archives (this
        repository's own were 0644 in bulk when that wording was written). The
        guarantee is that rlsbl rewrites an archive only through its own
        documented unlock paths; the local file mode is hygiene. Both remedies
        stay named either way.
        """
        other = repo.shas["0.1.0"]
        _git(repo, "tag", "-f", "v0.2.0", other)
        with pytest.raises(LedgerError) as exc:
            ledger.read_entry(_releases(repo), "0.2.0")
        text = str(exc.value)
        assert "read-only from" not in text
        assert f"git tag -f\n  v0.2.0 {repo.shas['0.2.0']}" in text
        assert "rlsbl release reconcile" in text

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
# An empty ledger in a repository whose tags say it has released
# --------------------------------------------------------------------------- #

@pytest.fixture
def unbackfilled(tmp_path, monkeypatch):
    """Released, never backfilled: two tagged releases and no archive at all.

    The finalized changelog files are what a real repository of this shape
    carries -- the ledger guard never looks at them, but the backfill script
    does, and the same fixture has to serve both.
    """
    r = _Repo(_init_repo(tmp_path / "unbackfilled"))
    monkeypatch.chdir(r)
    changes = r / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (r / ".rlsbl" / "releases").mkdir(parents=True)
    shas = {}
    for version in ("0.1.0", "0.2.0"):
        shas[version] = _commit(r, f"v{version}")
        _git(r, "tag", f"v{version}")
        f = changes / f"{version}.jsonl"
        f.write_text(
            '{"format_version":1,"commits":["%s"],"user_facing":false}\n'
            % shas[version]
        )
        os.chmod(f, 0o444)
    r.shas = shas
    return r


def _assert_names_the_backfill(text):
    # The fact, the evidence, and the remedy -- in the shape the other three
    # read errors use: what is wrong, why answering anyway would be wrong, and
    # the exact command that fixes it.
    assert "the release ledger is empty" in text
    assert "version tags" in text
    assert "v0.2.0" in text and "v0.1.0" in text
    assert "scripts/backfill_release_anchors.py --dry-run" in text
    assert "scripts/backfill_release_anchors.py\n" in text
    assert "genuinely never released" in text


class TestEmptyLedgerInATaggedRepository:

    def test_range_anchor_refuses(self, unbackfilled):
        with pytest.raises(LedgerError) as exc:
            ledger.range_anchor(_releases(unbackfilled))
        _assert_names_the_backfill(str(exc.value))

    def test_unreleased_range_refuses(self, unbackfilled):
        # The bug this replaces: "HEAD" -- the whole history reported as
        # unreleased, silently.
        with pytest.raises(LedgerError):
            ledger.unreleased_range(_releases(unbackfilled))

    def test_latest_release_fact_refuses(self, unbackfilled):
        # The bug this replaces: "(none)" for a project with two releases.
        with pytest.raises(LedgerError) as exc:
            ledger.latest_release_fact(_releases(unbackfilled))
        _assert_names_the_backfill(str(exc.value))

    def test_release_at_commit_refuses(self, unbackfilled):
        with pytest.raises(LedgerError):
            ledger.release_at_commit(
                _releases(unbackfilled), unbackfilled.shas["0.2.0"]
            )

    def test_preparing_a_release_refuses(self, unbackfilled):
        with pytest.raises(LedgerError):
            ledger.require_checkout_contains_latest(_releases(unbackfilled))

    def test_a_missing_releases_directory_is_the_same_state(self, tmp_path,
                                                            monkeypatch):
        # Not having created the directory is not evidence of never having
        # released -- the tags decide, not the directory's existence.
        r = _init_repo(tmp_path / "nodir")
        monkeypatch.chdir(r)
        _commit(r, "v0.1.0")
        _git(r, "tag", "v0.1.0")
        with pytest.raises(LedgerError):
            ledger.range_anchor(str(r / ".rlsbl" / "releases"))

    def test_the_error_names_the_scheme_and_a_few_tags(self, unbackfilled):
        for extra in ("0.3.0", "0.4.0", "0.5.0"):
            _commit(unbackfilled, f"v{extra}")
            _git(unbackfilled, "tag", f"v{extra}")
        with pytest.raises(LedgerError) as exc:
            ledger.range_anchor(_releases(unbackfilled))
        text = str(exc.value)
        assert '"v*"' in text                    # the scheme it matched under
        assert "v0.5.0" in text                  # highest first
        assert "(and others)" in text            # evidence, not an inventory
        assert "v0.1.0" not in text.split("Matching tags:")[1].splitlines()[0]


class TestEmptyLedgerLeftAlone:

    def test_a_project_before_its_first_release_still_answers(self, tmp_path,
                                                              monkeypatch):
        r = _init_repo(tmp_path / "prefirst")
        monkeypatch.chdir(r)
        _commit(r, "one")
        releases = str(r / ".rlsbl" / "releases")
        assert ledger.range_anchor(releases) is None
        assert ledger.unreleased_range(releases) == "HEAD"
        assert ledger.latest_release_fact(releases).label() == "(none)"
        ledger.require_checkout_contains_latest(releases)

    def test_tags_that_are_not_version_tags_do_not_trip_it(self, tmp_path,
                                                           monkeypatch):
        # The glob alone is not the scheme: these match "v*" and none of them
        # parses as a version, so the repository has still released nothing.
        r = _init_repo(tmp_path / "milestones")
        monkeypatch.chdir(r)
        _commit(r, "one")
        for tag in ("vNext", "v1.2", "verified"):
            _git(r, "tag", tag)
        assert ledger.range_anchor(str(r / ".rlsbl" / "releases")) is None

    def test_another_scopes_tags_do_not_trip_a_releasables_ledger(self, tmp_path,
                                                                  monkeypatch):
        # A workspace where a SIBLING has released and this releasable has not.
        r = _init_repo(tmp_path / "ws")
        monkeypatch.chdir(r)
        _commit(r, "one")
        _git(r, "tag", "www@v0.4.0")
        releases = str(
            r / ".rlsbl-monorepo" / "releasables" / "lib" / "releases"
        )
        assert ledger.range_anchor(releases, tag_glob="lib@v*") is None

    def test_it_does_trip_on_this_releasables_own_tags(self, tmp_path,
                                                       monkeypatch):
        r = _init_repo(tmp_path / "ws2")
        monkeypatch.chdir(r)
        _commit(r, "one")
        _git(r, "tag", "lib@v0.4.0")
        releases = str(
            r / ".rlsbl-monorepo" / "releasables" / "lib" / "releases"
        )
        with pytest.raises(LedgerError) as exc:
            ledger.range_anchor(releases, tag_glob="lib@v*")
        assert "lib@v0.4.0" in str(exc.value)

    def test_one_archive_is_enough_to_silence_it(self, unbackfilled):
        # A half-backfilled ledger is the missing-anchor error's business, not
        # this one's: the guard is inert the instant an archive exists.
        _archive(_releases(unbackfilled), "0.2.0", unbackfilled.shas["0.2.0"])
        assert ledger.range_anchor(_releases(unbackfilled)).version == "0.2.0"

    def test_an_unanswerable_tag_listing_does_not_accuse(self, unbackfilled,
                                                         monkeypatch):
        # No git, a timeout, a preview past its first recorded mutation: the
        # guard cannot read the namespace, so it says nothing rather than
        # naming evidence it never saw.
        monkeypatch.setattr(ledger, "_scheme_tags", lambda *a, **k: [])
        assert ledger.range_anchor(_releases(unbackfilled)) is None


class TestTheBackfillItselfIsNotBlocked:
    """The remedy has to run on exactly the repository the guard refuses.

    The script never calls the guarded reads -- it reads archives and tags
    directly -- so it needs no bypass, and is given none. This is the test that
    keeps that structural property true.
    """

    def test_the_script_imports_no_guarded_read(self):
        """The script never IMPORTS or CALLS the ledger module.

        Asserted against the script's code, not its prose: a comment may
        legitimately explain what the ledger would do with the files this pass
        writes, and a bare word search made that a failure.
        """
        import ast

        path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "scripts" / "backfill_release_anchors.py"
        )
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "ledger" not in alias.name, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert "ledger" not in (node.module or ""), node.module
                for alias in node.names:
                    assert "ledger" not in alias.name, alias.name
            elif isinstance(node, ast.Attribute):
                assert node.attr != "ledger"
            elif isinstance(node, ast.Name):
                assert node.id != "ledger"

    def test_the_script_runs_on_an_unbackfilled_repository(self, unbackfilled):
        import importlib.util

        path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "scripts" / "backfill_release_anchors.py"
        )
        spec = importlib.util.spec_from_file_location(
            "backfill_release_anchors_ledger_probe", path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        out = io.StringIO()
        code = module.run(
            str(unbackfilled), dry_run=False, use_gh=False,
            auto_commit=False, out=out,
        )
        assert code == 0, out.getvalue()
        # And the guard is satisfied by what the script wrote.
        assert ledger.range_anchor(_releases(unbackfilled)).version == "0.2.0"


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
