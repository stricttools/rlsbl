"""The version-fate model: what an archived version's record says happened to it.

Every archived release file is in exactly ONE of three states, and the state is
what every reader dispatches on:

* **recorded** -- it carries the release commit (``candidate_sha`` plus
  ``tree_hashes``). The version shipped and rlsbl knows from where.
* **unrecoverable** -- ``unrecoverable = true``. The version shipped, and the
  commit it shipped from cannot be recovered from any source.
* **never released** -- ``never_released = true``. The version NUMBER exists in
  the record (a phantom tag's version, a version claimed and abandoned) but no
  release was ever published under it.

``shipped_as`` is orthogonal to the three: it names the historical tag spelling
a version actually shipped under when that differs from the scheme in effect
today. It is legal on a recorded and on an unrecoverable archive, and refused on
a never-released one -- a version that was never released shipped under nothing.

The "none of the three" state is NOT a schema refusal: the editable
``unreleased.toml`` is exactly that document, and it is the same schema. It is
refused where an ARCHIVE is read for use, by
:func:`rlsbl.release_record.read_entry` (see ``test_release_record.py``).
"""

import os
import pathlib
import subprocess

import pytest

from rlsbl import release_record
from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    validate_no_authored_release_commit,
)
from rlsbl.errors import ReleaseFileError, ReleaseRecordError
from rlsbl.release_file import (
    NEVER_RELEASED_FIELD,
    SHIPPED_AS_FIELD,
    UNRECOVERABLE_FIELD,
    ReleaseConfig,
    read_release_file,
    write_archived_release_file,
    write_release_commit,
    write_unrecoverable_marker,
)

_SHA = "a" * 40
_TREE = "b" * 40

_BASE = (
    'format_version = 1\nbump = "patch"\ninclude = []\nexclude = []\n'
    'description = "x"\n'
)


def _write(tmp_path, body, name="release.toml"):
    f = tmp_path / name
    f.write_text(body)
    return str(f)


def _recorded(extra=""):
    return _BASE + f'candidate_sha = "{_SHA}"\n' + extra + f'[tree_hashes]\n"." = "{_TREE}"\n'


# --------------------------------------------------------------------------- #
# The field names the model is spelled with
# --------------------------------------------------------------------------- #

class TestFieldNames:

    def test_the_marker_key_is_unrecoverable(self):
        # The attribute and the TOML key are the same word now; the old
        # `unanchorable` spelling is gone from both sides.
        assert UNRECOVERABLE_FIELD == "unrecoverable"

    def test_the_never_released_key(self):
        assert NEVER_RELEASED_FIELD == "never_released"

    def test_the_shipped_as_key(self):
        assert SHIPPED_AS_FIELD == "shipped_as"


# --------------------------------------------------------------------------- #
# Reading each of the three states
# --------------------------------------------------------------------------- #

class TestThreeStatesValidate:

    def test_recorded_validates(self, tmp_path):
        cfg = read_release_file(_write(tmp_path, _recorded()))
        assert cfg.candidate_sha == _SHA
        assert cfg.tree_hashes == {".": _TREE}
        assert cfg.unrecoverable is None
        assert cfg.never_released is None

    def test_unrecoverable_validates(self, tmp_path):
        cfg = read_release_file(_write(tmp_path, _BASE + "unrecoverable = true\n"))
        assert cfg.unrecoverable is True
        assert cfg.candidate_sha is None
        assert cfg.never_released is None

    def test_never_released_validates(self, tmp_path):
        cfg = read_release_file(_write(tmp_path, _BASE + "never_released = true\n"))
        assert cfg.never_released is True
        assert cfg.candidate_sha is None
        assert cfg.unrecoverable is None

    def test_the_old_unanchorable_key_is_refused_as_unknown(self, tmp_path):
        # Pre-stable rename: no dual recognition, no compat. The old spelling
        # is simply not a field of this schema any more.
        with pytest.raises(ReleaseFileError) as exc:
            read_release_file(_write(tmp_path, _BASE + "unanchorable = true\n"))
        assert "unanchorable" in str(exc.value)


class TestStatesAreExclusive:

    def test_recorded_and_unrecoverable_together_refused(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(tmp_path, _recorded("unrecoverable = true\n")))

    def test_recorded_and_never_released_together_refused(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(tmp_path, _recorded("never_released = true\n")))

    def test_both_markers_together_refused(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(
                tmp_path, _BASE + "unrecoverable = true\nnever_released = true\n",
            ))

    def test_tree_hashes_alongside_a_marker_refused(self, tmp_path):
        # tree_hashes is half of the release commit, so it is forbidden beside
        # either marker even without candidate_sha.
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(
                tmp_path,
                _BASE + "never_released = true\n"
                f'[tree_hashes]\n"." = "{_TREE}"\n',
            ))


# --------------------------------------------------------------------------- #
# shipped_as
# --------------------------------------------------------------------------- #

class TestShippedAs:

    def test_binds_on_a_recorded_archive(self, tmp_path):
        cfg = read_release_file(
            _write(tmp_path, _recorded('shipped_as = "strictcli@v0.12.0"\n'))
        )
        assert cfg.shipped_as == "strictcli@v0.12.0"

    def test_binds_on_an_unrecoverable_archive(self, tmp_path):
        cfg = read_release_file(_write(
            tmp_path,
            _BASE + 'unrecoverable = true\nshipped_as = "auth-gateway/v0.1.0"\n',
        ))
        assert cfg.shipped_as == "auth-gateway/v0.1.0"

    def test_refused_on_a_never_released_archive(self, tmp_path):
        # A version that was never released shipped under nothing, so there is
        # no historical spelling for it to have shipped under.
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(
                tmp_path,
                _BASE + 'never_released = true\nshipped_as = "lib@v0.1.0"\n',
            ))

    def test_empty_shipped_as_refused(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(tmp_path, _recorded('shipped_as = ""\n')))

    def test_absent_reads_as_absent(self, tmp_path):
        cfg = read_release_file(_write(tmp_path, _recorded()))
        assert cfg.shipped_as is None


# --------------------------------------------------------------------------- #
# The editable release file refuses every flow-owned field
# --------------------------------------------------------------------------- #

class TestEditableFileRefusal:

    def _cfg(self, **kwargs):
        return ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x", **kwargs
        )

    def test_never_released_refused(self):
        with pytest.raises(ReleaseValidationError, match=NEVER_RELEASED_FIELD):
            validate_no_authored_release_commit(self._cfg(never_released=True))

    def test_never_released_false_is_still_authored(self):
        with pytest.raises(ReleaseValidationError, match=NEVER_RELEASED_FIELD):
            validate_no_authored_release_commit(self._cfg(never_released=False))

    def test_shipped_as_refused(self):
        with pytest.raises(ReleaseValidationError, match=SHIPPED_AS_FIELD):
            validate_no_authored_release_commit(self._cfg(shipped_as="lib@v0.1.0"))

    def test_unrecoverable_refused_under_its_new_spelling(self):
        with pytest.raises(ReleaseValidationError, match="unrecoverable"):
            validate_no_authored_release_commit(self._cfg(unrecoverable=True))

    def test_release_commit_fields_still_refused(self):
        with pytest.raises(ReleaseValidationError, match="candidate_sha"):
            validate_no_authored_release_commit(self._cfg(candidate_sha=_SHA))
        with pytest.raises(ReleaseValidationError, match="tree_hashes"):
            validate_no_authored_release_commit(self._cfg(tree_hashes={".": _TREE}))

    def test_every_flow_owned_field_named_in_one_error(self):
        cfg = self._cfg(
            candidate_sha=_SHA, tree_hashes={".": _TREE},
            unrecoverable=True, never_released=True, shipped_as="lib@v0.1.0",
        )
        with pytest.raises(ReleaseValidationError) as exc:
            validate_no_authored_release_commit(cfg)
        text = str(exc.value)
        for name in ("candidate_sha", "tree_hashes", UNRECOVERABLE_FIELD,
                     NEVER_RELEASED_FIELD, SHIPPED_AS_FIELD):
            assert name in text

    def test_a_plain_editable_file_passes(self):
        validate_no_authored_release_commit(self._cfg())


# --------------------------------------------------------------------------- #
# The writer produces each state, and refuses a mixed one
# --------------------------------------------------------------------------- #

class TestWriter:

    def test_writes_a_never_released_archive(self, tmp_path):
        path = write_archived_release_file(
            str(tmp_path), "0.4.0", bump="patch", include=["pypi"],
            description="phantom", candidate_sha=None, tree_hashes=None,
            never_released=True,
        )
        cfg = read_release_file(path)
        assert cfg.never_released is True
        assert cfg.candidate_sha is None

    def test_writes_shipped_as_on_a_recorded_archive(self, tmp_path):
        path = write_archived_release_file(
            str(tmp_path), "0.4.0", bump="patch", include=["pypi"],
            description="d", candidate_sha=_SHA, tree_hashes={".": _TREE},
            shipped_as="lib@v0.4.0",
        )
        assert read_release_file(path).shipped_as == "lib@v0.4.0"

    def test_refuses_a_never_released_archive_with_a_release_commit(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            write_archived_release_file(
                str(tmp_path), "0.4.0", bump="patch", include=["pypi"],
                description="d", candidate_sha=_SHA, tree_hashes={".": _TREE},
                never_released=True,
            )

    def test_refuses_both_markers_at_once(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            write_archived_release_file(
                str(tmp_path), "0.4.0", bump="patch", include=["pypi"],
                description="d", candidate_sha=None, tree_hashes=None,
                unrecoverable=True, never_released=True,
            )

    def test_refuses_shipped_as_on_a_never_released_archive(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            write_archived_release_file(
                str(tmp_path), "0.4.0", bump="patch", include=["pypi"],
                description="d", candidate_sha=None, tree_hashes=None,
                never_released=True, shipped_as="lib@v0.4.0",
            )


# --------------------------------------------------------------------------- #
# The two primitives that edit an ALREADY-WRITTEN archive keep the one-fate
# rule too. `write_archived_release_file` composes a whole document and refuses
# a mixed one on the way out; these two ADD a fate to a document somebody else
# wrote, so each has to read the fate already there and refuse the combination
# rather than produce a two-fate document the schema rejects and every reader
# then raises on.
# --------------------------------------------------------------------------- #

class TestEditingPrimitivesRefuseASecondFate:

    def _archive(self, tmp_path, body, name="v0.4.0.toml"):
        return _write(tmp_path, body, name=name)

    def test_the_marker_refuses_a_never_released_archive(self, tmp_path):
        path = self._archive(
            tmp_path, _BASE + f"{NEVER_RELEASED_FIELD} = true\n",
        )
        with pytest.raises(ReleaseFileError) as exc:
            write_unrecoverable_marker(path)
        text = str(exc.value)
        assert path in text
        assert NEVER_RELEASED_FIELD in text
        assert UNRECOVERABLE_FIELD in text
        # And the file it refused to write is still the readable one-fate
        # document it was.
        assert read_release_file(path).never_released is True

    def test_the_marker_still_refuses_a_recorded_archive(self, tmp_path):
        path = self._archive(tmp_path, _recorded())
        with pytest.raises(ReleaseFileError) as exc:
            write_unrecoverable_marker(path)
        assert "candidate_sha" in str(exc.value)

    def test_the_release_commit_writer_refuses_a_never_released_archive(
        self, tmp_path,
    ):
        path = self._archive(
            tmp_path, _BASE + f"{NEVER_RELEASED_FIELD} = true\n",
        )
        with pytest.raises(ReleaseFileError) as exc:
            write_release_commit(path, candidate_sha=_SHA,
                                 tree_hashes={".": _TREE})
        text = str(exc.value)
        assert path in text
        assert NEVER_RELEASED_FIELD in text
        assert read_release_file(path).never_released is True

    def test_the_release_commit_writer_refuses_an_unrecoverable_archive(
        self, tmp_path,
    ):
        path = self._archive(
            tmp_path, _BASE + f"{UNRECOVERABLE_FIELD} = true\n",
        )
        with pytest.raises(ReleaseFileError) as exc:
            write_release_commit(path, candidate_sha=_SHA,
                                 tree_hashes={".": _TREE})
        text = str(exc.value)
        assert path in text
        assert UNRECOVERABLE_FIELD in text
        assert read_release_file(path).unrecoverable is True

    def test_the_release_commit_writer_preserves_shipped_as(self, tmp_path):
        """`shipped_as` is not a fate, and the remap path re-writes through here.

        The writer clears the release-commit fields it is about to re-author;
        clearing every flow-owned field instead would silently drop the tag
        spelling a version shipped under, on every scrub that moves a commit.
        """
        path = self._archive(
            tmp_path,
            _recorded(f'{SHIPPED_AS_FIELD} = "lib@v0.4.0"\n'),
        )
        write_release_commit(path, candidate_sha="c" * 40,
                             tree_hashes={".": "d" * 40})
        cfg = read_release_file(path)
        assert cfg.shipped_as == "lib@v0.4.0"
        assert cfg.candidate_sha == "c" * 40

    def test_a_recorded_archive_is_still_re_recordable(self, tmp_path):
        path = self._archive(tmp_path, _recorded())
        write_release_commit(path, candidate_sha="c" * 40,
                             tree_hashes={".": "d" * 40})
        assert read_release_file(path).candidate_sha == "c" * 40


# --------------------------------------------------------------------------- #
# The release record reads the third state
# --------------------------------------------------------------------------- #

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


def _archive(releases_dir, version, sha, *, unrecoverable=False,
             never_released=False, shipped_as=None):
    os.makedirs(releases_dir, exist_ok=True)
    recorded = not (unrecoverable or never_released)
    return write_archived_release_file(
        str(releases_dir), version,
        bump="patch", include=["pypi"], description=f"release {version}",
        candidate_sha=sha if recorded else None,
        tree_hashes={".": _TREE} if recorded else None,
        unrecoverable=unrecoverable,
        never_released=never_released,
        shipped_as=shipped_as,
    )


@pytest.fixture
def phantom_topped(tmp_path, monkeypatch):
    """A repo whose HIGHEST archive is a version that was never released.

    Two real releases (0.1.0, 0.2.0, both tagged and recorded) and one phantom
    0.3.0 whose archive says ``never_released = true``. This is the shape that
    made ``rlsbl release undo`` select the phantom and die.
    """
    r = _init_repo(tmp_path / "phantom")
    monkeypatch.chdir(r)
    releases = r / ".rlsbl" / "releases"
    shas = {}
    for version in ("0.1.0", "0.2.0"):
        shas[version] = _commit(r, f"v{version}")
        _git(r, "tag", f"v{version}")
        _archive(releases, version, shas[version])
    _archive(releases, "0.3.0", None, never_released=True)
    _commit(r, "work after the last real release")
    return r, shas


def _releases(repo_path):
    return str(pathlib.Path(repo_path) / ".rlsbl" / "releases")


class TestReleaseRecordReadsNeverReleased:

    def test_read_entry_reports_the_state(self, phantom_topped):
        repo, _shas = phantom_topped
        entry = release_record.read_entry(_releases(repo), "0.3.0")
        assert entry.never_released is True
        assert entry.recorded is False
        assert entry.unrecoverable is False
        assert entry.candidate_sha is None

    def test_an_archive_in_none_of_the_states_is_still_a_hard_error(self, tmp_path,
                                                                    monkeypatch):
        r = _init_repo(tmp_path / "bare")
        monkeypatch.chdir(r)
        _commit(r, "one")
        releases = r / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        (releases / "v0.1.0.toml").write_text(_BASE)
        with pytest.raises(ReleaseRecordError):
            release_record.read_entry(str(releases), "0.1.0")

    def test_the_latest_release_fact_skips_it(self, phantom_topped):
        repo, _shas = phantom_topped
        fact = release_record.latest_release_fact(_releases(repo))
        assert fact.version == "0.2.0"
        assert fact.never_released_above == ("0.3.0",)

    def test_the_range_release_commit_skips_it(self, phantom_topped):
        repo, shas = phantom_topped
        entry = release_record.nearest_release_commit(_releases(repo))
        assert entry.version == "0.2.0"
        assert entry.candidate_sha == shas["0.2.0"]

    def test_the_contains_latest_refusal_ignores_it(self, phantom_topped):
        # 0.3.0 has no commit at all; requiring the checkout to contain it
        # would refuse every release forever.
        repo, _shas = phantom_topped
        release_record.require_checkout_contains_latest(_releases(repo))

    def test_the_label_names_the_phantom(self, phantom_topped):
        repo, _shas = phantom_topped
        fact = release_record.latest_release_fact(_releases(repo))
        label = fact.label()
        assert "0.2.0" in label
        assert "0.3.0" in label
        assert "never released" in label


class TestTheRecordAnswersShippedAs:
    """The tag spelling a version shipped under is readable from the record.

    ``shipped_as`` is what a version's refs are actually named when the
    project's tag scheme changed after it shipped, so the entry that answers
    "what did this project release?" has to answer it too -- otherwise every
    caller has to go around the record layer and re-read the archive.
    """

    def _repo(self, tmp_path, monkeypatch, **archive_kwargs):
        r = _init_repo(tmp_path / "spelling")
        monkeypatch.chdir(r)
        sha = _commit(r, "v0.1.0")
        _git(r, "tag", "v0.1.0")
        _archive(r / ".rlsbl" / "releases", "0.1.0", sha, **archive_kwargs)
        return r

    def test_a_recorded_entry_binds_it(self, tmp_path, monkeypatch):
        r = self._repo(tmp_path, monkeypatch, shipped_as="mylib@v0.1.0")
        entry = release_record.read_entry(_releases(r), "0.1.0")
        assert entry.shipped_as == "mylib@v0.1.0"

    def test_an_archive_without_it_binds_none(self, tmp_path, monkeypatch):
        r = self._repo(tmp_path, monkeypatch)
        assert release_record.read_entry(_releases(r), "0.1.0").shipped_as is None

    def test_an_unrecoverable_entry_binds_it(self, tmp_path, monkeypatch):
        r = _init_repo(tmp_path / "unrec")
        monkeypatch.chdir(r)
        _commit(r, "one")
        _archive(r / ".rlsbl" / "releases", "0.1.0", None,
                 unrecoverable=True, shipped_as="mylib@v0.1.0")
        entry = release_record.read_entry(_releases(r), "0.1.0")
        assert entry.unrecoverable is True
        assert entry.shipped_as == "mylib@v0.1.0"

    def test_a_never_released_entry_has_none(self, phantom_topped):
        # The schema refuses shipped_as on a never-released archive: a version
        # nothing shipped shipped under no name.
        repo, _shas = phantom_topped
        assert release_record.read_entry(_releases(repo), "0.3.0").shipped_as is None


# --------------------------------------------------------------------------- #
# The remedies these two errors print are PERFORMED here, exactly as printed,
# and the error must then be gone. An error that names a remedy is a promise;
# these tests are what keep the promise true as the messages are edited.
# --------------------------------------------------------------------------- #

class TestTheNoFateRemedyPerformedVerbatim:
    """The archive-records-no-fate error names one command; it is run here.

    The message used to print a four-step manual procedure -- unlock the 0444
    archive, hand-write ``candidate_sha`` and a ``[tree_hashes]`` table, relock,
    re-run -- and this class performed those steps. It names
    ``rlsbl release backfill`` now, so the remedy performed here is that
    command, run exactly as the message spells it.
    """

    def _repo_with_a_fateless_archive(self, tmp_path, monkeypatch):
        r = _init_repo(tmp_path / "fateless")
        monkeypatch.chdir(r)
        sha = _commit(r, "v0.1.0")
        _git(r, "tag", "v0.1.0")
        releases = r / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        archive = releases / "v0.1.0.toml"
        archive.write_text(_BASE)
        # Locked like every real archive, so the remedy is real work.
        os.chmod(archive, 0o444)
        return r, sha, archive

    def _backfill(self, repo):
        """Run what the error prints: `rlsbl release backfill`."""
        from pathlib import Path

        from rlsbl.commands.release_backfill import run_cmd
        from rlsbl.context import create_context

        run_cmd(
            {"dry-run": False, "auto-commit": False},
            ctx=create_context(Path(str(repo))),
        )

    def test_running_the_named_command_clears_the_error(
        self, tmp_path, monkeypatch,
    ):
        r, sha, _archive = self._repo_with_a_fateless_archive(
            tmp_path, monkeypatch,
        )

        with pytest.raises(ReleaseRecordError) as exc:
            release_record.read_entry(_releases(r), "0.1.0")
        message = str(exc.value)

        # The error states the evidence about this version, and the command.
        assert f'Its tag "v0.1.0" points at {sha}' in message
        assert "rlsbl release backfill --dry-run" in message
        assert "rlsbl release backfill --approve-consequential" in message

        self._backfill(r)

        entry = release_record.read_entry(_releases(r), "0.1.0")
        assert entry.recorded is True
        assert entry.candidate_sha == sha
        assert entry.unrecoverable is False
        assert entry.never_released is False

    def test_a_tagless_version_is_recorded_unrecoverable_by_the_same_command(
        self, tmp_path, monkeypatch,
    ):
        """With no tag and no version-bump commit, the pass records the marker."""
        r = _init_repo(tmp_path / "tagless")
        monkeypatch.chdir(r)
        _commit(r, "one")
        releases = r / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        archive = releases / "v0.1.0.toml"
        archive.write_text(_BASE)
        os.chmod(archive, 0o444)

        with pytest.raises(ReleaseRecordError) as exc:
            release_record.read_entry(_releases(r), "0.1.0")
        message = str(exc.value)
        assert "does not exist locally" in message
        assert "unrecoverable" in message

        self._backfill(r)

        entry = release_record.read_entry(_releases(r), "0.1.0")
        assert entry.unrecoverable is True

    def test_the_alternative_the_error_names_is_declaring_the_fate(
        self, tmp_path, monkeypatch,
    ):
        """"If it was never actually released, declare that FIRST" -- performed.

        The pass cannot tell a never-released version from a released one whose
        commit is gone, so the error says what only the operator can decide, and
        how to record it. Declaring it is what the backfill then leaves alone.
        """
        r = _init_repo(tmp_path / "phantom")
        monkeypatch.chdir(r)
        _commit(r, "one")
        releases = r / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        archive = releases / "v0.1.0.toml"
        archive.write_text(_BASE)
        os.chmod(archive, 0o444)

        with pytest.raises(ReleaseRecordError) as exc:
            release_record.read_entry(_releases(r), "0.1.0")
        message = str(exc.value)
        assert f"{NEVER_RELEASED_FIELD} = true" in message
        assert "chmod 644" in message and "chmod 444" in message

        os.chmod(archive, 0o644)
        with open(archive, "a", encoding="utf-8") as f:
            f.write(f"{NEVER_RELEASED_FIELD} = true\n")
        os.chmod(archive, 0o444)

        entry = release_record.read_entry(_releases(r), "0.1.0")
        assert entry.never_released is True

        # And the backfill honours the declaration rather than overwriting it.
        before = archive.read_bytes()
        self._backfill(r)
        assert archive.read_bytes() == before


class TestTheEditableFileRemedyPerformedVerbatim:
    """"Remove them from the release file and re-run." -- performed."""

    def test_removing_the_named_fields_clears_the_refusal(self, tmp_path):
        path = _write(
            tmp_path,
            _BASE
            + f'{SHIPPED_AS_FIELD} = "lib@v0.4.0"\n'
            + f'candidate_sha = "{_SHA}"\n',
            name="unreleased.toml",
        )

        with pytest.raises(ReleaseValidationError) as exc:
            validate_no_authored_release_commit(read_release_file(path))
        message = str(exc.value)
        assert "Remove them from" in message

        # The remedy is performed on the named fields and nothing else: every
        # field the error listed is deleted from the file, in place.
        named = [
            name for name in (SHIPPED_AS_FIELD, NEVER_RELEASED_FIELD,
                              UNRECOVERABLE_FIELD, "candidate_sha",
                              "tree_hashes")
            if name in message
        ]
        assert SHIPPED_AS_FIELD in named and "candidate_sha" in named
        kept = [
            line for line in open(path, encoding="utf-8").read().splitlines()
            if not any(line.startswith(f"{name} = ") for name in named)
        ]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")

        # Re-run: clean, and the rest of the release file survived.
        cfg = read_release_file(path)
        validate_no_authored_release_commit(cfg)
        assert cfg.bump == "patch"
        assert cfg.description == "x"
