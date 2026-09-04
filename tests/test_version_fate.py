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
             never_released=False):
    os.makedirs(releases_dir, exist_ok=True)
    recorded = not (unrecoverable or never_released)
    return write_archived_release_file(
        str(releases_dir), version,
        bump="patch", include=["pypi"], description=f"release {version}",
        candidate_sha=sha if recorded else None,
        tree_hashes={".": _TREE} if recorded else None,
        unrecoverable=unrecoverable,
        never_released=never_released,
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
