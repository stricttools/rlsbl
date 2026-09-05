"""The one consultation over "is this tag explained?".

Three sources answer, in decreasing specificity about a release: the current
scheme's spelling for an archived version, an archive's recorded ``shipped_as``
spelling, and a transition record's ``non-version-tag`` event.  Everything else
is unexplained, and what that costs is each consumer's own decision.
"""

import pytest

from rlsbl import tag_explanation
from rlsbl.release_file import write_archived_release_file
from rlsbl.tag_explanation import (
    SOURCE_ARCHIVED_VERSION,
    SOURCE_NON_VERSION_TAG,
    SOURCE_SHIPPED_AS,
)
from rlsbl.transition_record import (
    NonVersionTagEvent,
    append_event,
    get_transition_record_path,
)

SHA = "a" * 40
TREE = "f" * 40


@pytest.fixture
def releases(tmp_path):
    d = tmp_path / ".rlsbl" / "releases"
    write_archived_release_file(
        str(d), "1.0.0", bump="minor", include=["plain"], description="d",
        candidate_sha=SHA, tree_hashes={".": TREE},
    )
    write_archived_release_file(
        str(d), "0.12.0", bump="minor", include=["plain"], description="d",
        candidate_sha=SHA, tree_hashes={".": TREE},
        shipped_as="strictcli@v0.12.0",
    )
    return d


class TestTheSources:

    def test_a_scheme_spelling_names_its_version(self, tmp_path, releases):
        idx = tag_explanation.build(
            version_tags={"v1.0.0": "1.0.0"}, releases_dirs=[str(releases)],
        )
        e = idx.explain("v1.0.0")
        assert e.source == SOURCE_ARCHIVED_VERSION
        assert e.version == "1.0.0"
        assert "1.0.0" in e.describe()

    def test_a_shipped_as_spelling_names_its_version(self, tmp_path, releases):
        idx = tag_explanation.build(releases_dirs=[str(releases)])
        e = idx.explain("strictcli@v0.12.0")
        assert e.source == SOURCE_SHIPPED_AS
        assert e.version == "0.12.0"
        assert "shipped_as" in e.describe()

    def test_a_member_path_spelling_is_read_the_same_way(self, tmp_path):
        d = tmp_path / "releases"
        write_archived_release_file(
            str(d), "0.1.0", bump="minor", include=["plain"], description="d",
            candidate_sha=SHA, tree_hashes={".": TREE},
            shipped_as="auth-gateway/v0.1.0",
        )
        idx = tag_explanation.build(releases_dirs=[str(d)])
        assert idx.explain("auth-gateway/v0.1.0").version == "0.1.0"

    def test_a_recorded_non_version_tag_carries_its_reason(self, tmp_path):
        path = get_transition_record_path(str(tmp_path))
        append_event(path, NonVersionTagEvent(
            tag="nightly", reason="a nightly build marker",
        ))
        idx = tag_explanation.build(transition_record_paths=[path])
        e = idx.explain("nightly")
        assert e.source == SOURCE_NON_VERSION_TAG
        assert e.version is None
        assert "a nightly build marker" in e.describe()
        assert idx.non_version_tags == ("nightly",)

    def test_anything_else_is_unexplained(self, tmp_path, releases):
        idx = tag_explanation.build(
            version_tags={"v1.0.0": "1.0.0"}, releases_dirs=[str(releases)],
        )
        assert idx.explain("v9.9.9") is None
        assert not idx.explains("some-tag")


class TestTolerance:
    """The backfill's subject is a repository whose archives are not yet valid."""

    def test_a_pre_gate_archive_still_yields_its_shipped_as(self, tmp_path):
        d = tmp_path / "releases"
        d.mkdir(parents=True)
        (d / "v0.1.0.toml").write_text(
            'bump = "minor"\ndescription = "d"\n'
            'shipped_as = "old@v0.1.0"\n',
            encoding="utf-8",
        )
        assert tag_explanation.shipped_as_index(str(d)) == {"old@v0.1.0": "0.1.0"}

    def test_an_unparseable_archive_contributes_nothing(self, tmp_path):
        d = tmp_path / "releases"
        d.mkdir(parents=True)
        (d / "v0.1.0.toml").write_text("this is not toml = = =\n", encoding="utf-8")
        assert tag_explanation.shipped_as_index(str(d)) == {}

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert tag_explanation.shipped_as_index(str(tmp_path / "nope")) == {}

    def test_a_non_archive_file_is_ignored(self, tmp_path):
        d = tmp_path / "releases"
        d.mkdir(parents=True)
        (d / "unreleased.toml").write_text(
            'shipped_as = "nope@v1.0.0"\n', encoding="utf-8",
        )
        assert tag_explanation.shipped_as_index(str(d)) == {}


def test_precedence_puts_the_current_scheme_first(tmp_path):
    """A tag the current scheme names is that version's, whatever else says."""
    d = tmp_path / "releases"
    write_archived_release_file(
        str(d), "0.1.0", bump="minor", include=["plain"], description="d",
        candidate_sha=SHA, tree_hashes={".": TREE}, shipped_as="v2.0.0",
    )
    path = get_transition_record_path(str(tmp_path))
    append_event(path, NonVersionTagEvent(tag="v2.0.0", reason="claimed too"))
    idx = tag_explanation.build(
        version_tags={"v2.0.0": "2.0.0"},
        releases_dirs=[str(d)], transition_record_paths=[path],
    )
    assert idx.explain("v2.0.0").source == SOURCE_ARCHIVED_VERSION
