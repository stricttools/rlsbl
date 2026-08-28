"""What a scrub does to the GitHub Releases attached to the tags it moved.

``update_github_releases`` is the scrub flow's ``RELEASES_UPDATED`` step. A
GitHub Release follows its tag NAME, and the tags have already been re-pointed
by the time this step runs -- so the Release is already attached to the
rewritten commit and there is nothing to destroy. What does NOT follow the tag
is the document: the notes, the ``rlsbl-ci-sha`` marker the publish workflow
reads, and the pre-release flag. Those are rewritten in place.

Three properties are pinned here:

* **Nothing is ever deleted.** No ``gh release delete`` argv is issued on any
  path, so a transient failure can never leave a tag with no Release at all.
  The old Release stays exactly where it was and a re-run repairs it.
* **An absent Release is created.** A tag the rewrite moved that carries no
  Release gets one, from the same document -- the materialize shape the
  reconcile command already performs.
* **The document is the one the release flow writes.** Body, title, the
  ``rlsbl-ci-sha`` marker taken from the ledger's anchor, and the pre-release
  flag all come from :mod:`rlsbl.release_publication`, so a Release the scrub
  updates is one the publish workflow can still judge.
"""

import subprocess

from rlsbl.commands.release_reconcile import update_github_releases
from rlsbl.release_file import write_archived_release_file
from rlsbl.release_publication import publication

ANCHOR = "a" * 40
TREE = "f" * 40


class Ctx:
    """The two attributes the update reads off a project context."""

    def __init__(self, root):
        self.config = {}
        self.project_root = root
        self.workspace_root = None


class Recorder:
    """A gh double that records every call and every notes-file body."""

    def __init__(self, existing):
        self.existing = set(existing)
        self.calls = []
        self.bodies = {}

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["release", "view"]:
            if args[2] in self.existing:
                return "old notes"
            raise subprocess.CalledProcessError(1, "gh release view")
        if args[:2] == ["release", "delete"]:
            self.existing.discard(args[2])
            return ""
        if args[:2] in (["release", "create"], ["release", "edit"]):
            path = args[args.index("--notes-file") + 1]
            with open(path, encoding="utf-8") as f:
                self.bodies[args[2]] = f.read()
            self.existing.add(args[2])
        return ""

    def of(self, verb):
        return [c for c in self.calls if c[:2] == ["release", verb]]


def _ledger(tmp_path, version, *, anchor=ANCHOR):
    releases = tmp_path / ".rlsbl" / "releases"
    write_archived_release_file(
        str(releases), version, bump="patch", include=["plain"],
        description="A release.", candidate_sha=anchor,
        tree_hashes={".": TREE},
    )
    return releases


def _update(tmp_path, tags, recorder, *, notes="## notes\n\n- A change.\n"):
    # The notes come from the owning project's CHANGELOG.md, so the file has
    # to be there for the extraction to be reached at all.
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    return update_github_releases(
        tags, ctx=Ctx(str(tmp_path)), project_root=str(tmp_path),
        workspace_projects=None, tag_prefix_index=None,
        gh=recorder, gh_installed=lambda: True, gh_auth=lambda: True,
        extract_entry=lambda _path, _version: notes,
    )


class TestNothingIsEverDeleted:

    def test_an_existing_release_is_edited_in_place(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=("v1.0.0",))

        updated = _update(tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh)

        assert gh.of("delete") == [], (
            "the tag has already been re-pointed, so its Release is already on "
            "the rewritten commit -- there is nothing to destroy"
        )
        assert gh.of("create") == []
        assert len(gh.of("edit")) == 1
        assert gh.of("edit")[0][2] == "v1.0.0"
        assert f"<!-- rlsbl-ci-sha: {ANCHOR} -->" in gh.bodies["v1.0.0"]
        assert updated == 1

    def test_a_prerelease_tags_release_is_updated(self, tmp_path, monkeypatch):
        """The reproduction: a ``-rc.1`` tag's Release was deleted and never
        recreated, because the version parse ran AFTER the delete and rejected
        every pre-release tag. Nothing is deleted now, and the pre-release
        version is updated like any other."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0-rc.1")
        gh = Recorder(existing=("v1.0.0-rc.1",))

        updated = _update(
            tmp_path, [{"refname": "refs/tags/v1.0.0-rc.1", "new_sha": ANCHOR}],
            gh,
        )

        assert gh.of("delete") == []
        assert len(gh.of("edit")) == 1, (
            "a pre-release version is a released version like any other"
        )
        assert updated == 1

    def test_a_tag_that_is_not_a_version_is_skipped(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.chdir(tmp_path)
        gh = Recorder(existing=("nightly",))

        updated = _update(
            tmp_path, [{"refname": "refs/tags/nightly", "new_sha": ANCHOR}], gh,
        )

        assert gh.calls == [], (
            "a tag whose version cannot be read has no document to write, so "
            "its Release is not even looked up"
        )
        assert updated == 0
        assert "nightly" in capsys.readouterr().err

    def test_a_transient_edit_failure_leaves_the_old_release_present(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Individual failures stay warnings, and the failure window is gone:
        nothing was deleted, so the tag still carries its old Release."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        _ledger(tmp_path, "1.1.0")
        gh = Recorder(existing=("v1.0.0", "v1.1.0"))
        real = gh.__call__

        def flaky(args, **kwargs):
            if args[:3] == ["release", "edit", "v1.0.0"]:
                raise subprocess.CalledProcessError(1, "gh release edit")
            return real(args, **kwargs)

        updated = update_github_releases(
            [{"refname": "refs/tags/v1.0.0"}, {"refname": "refs/tags/v1.1.0"}],
            ctx=Ctx(str(tmp_path)), project_root=str(tmp_path),
            workspace_projects=None, tag_prefix_index=None,
            gh=flaky, gh_installed=lambda: True, gh_auth=lambda: True,
            extract_entry=lambda _p, _v: "notes",
        )

        assert updated == 1, "the other tag is still updated"
        assert gh.of("delete") == []
        assert "v1.0.0" in gh.existing, "the old Release is still on the tag"
        err = capsys.readouterr().err
        assert "v1.0.0" in err
        assert "still" in err, (
            "the warning has to say the old Release is still present -- there "
            "is no window in which the tag has none"
        )
        assert "now has NONE" not in err


class TestTheUpdatedDocument:

    def test_it_is_byte_identical_to_the_shared_publication(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=("v1.0.0",))
        notes = "## 1.0.0\n\n### Fixes\n\n- **Fixed it.** Really.\n"

        _update(tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh, notes=notes)

        expected = publication(
            tag="v1.0.0", version="1.0.0", candidate_sha=ANCHOR, notes=notes,
        ).body
        assert gh.bodies["v1.0.0"] == expected, (
            "the scrub must write the same document the release flow writes, "
            "not a notes-only body"
        )
        assert f"<!-- rlsbl-ci-sha: {ANCHOR} -->" in gh.bodies["v1.0.0"]

    def test_the_marker_comes_from_the_ledger_not_the_moved_tag(
        self, tmp_path, monkeypatch,
    ):
        """The anchor step runs before this one, so the archive already names
        the rewritten commit; the marker is that anchor, never the tag's own
        old value."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0", anchor="b" * 40)
        gh = Recorder(existing=("v1.0.0",))

        _update(
            tmp_path,
            [{"refname": "refs/tags/v1.0.0", "new_sha": "c" * 40}], gh,
        )

        assert f"<!-- rlsbl-ci-sha: {'b' * 40} -->" in gh.bodies["v1.0.0"]

    def test_a_prerelease_version_is_marked_as_one_on_the_edit(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0-rc.1")
        gh = Recorder(existing=("v1.0.0-rc.1",))

        _update(tmp_path, [{"refname": "refs/tags/v1.0.0-rc.1"}], gh)

        assert "--prerelease" in gh.of("edit")[0]

    def test_a_final_version_clears_the_flag_on_the_edit(
        self, tmp_path, monkeypatch,
    ):
        """The flag is stated either way: an edit that omitted it would leave a
        Release that was wrongly marked pre-release marked that way forever."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=("v1.0.0",))

        _update(tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh)

        assert "--prerelease=false" in gh.of("edit")[0]
        assert "--prerelease" not in gh.of("edit")[0]

    def test_the_title_is_written_on_the_edit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=("v1.0.0",))

        _update(tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh)

        args = gh.of("edit")[0]
        assert args[args.index("--title") + 1] == "v1.0.0"

    def test_a_version_with_no_archive_is_written_markerless_and_says_so(
        self, tmp_path, monkeypatch, capsys,
    ):
        """A version the ledger cannot anchor still gets its document --
        without a marker, and with the omission stated rather than hidden."""
        monkeypatch.chdir(tmp_path)
        gh = Recorder(existing=("v0.1.0",))

        _update(tmp_path, [{"refname": "refs/tags/v0.1.0"}], gh)

        assert len(gh.of("edit")) == 1
        assert "rlsbl-ci-sha" not in gh.bodies["v0.1.0"]
        err = capsys.readouterr().err
        assert "0.1.0" in err and "marker" in err

    def test_a_tag_with_no_release_gets_one_created(self, tmp_path, monkeypatch):
        """The materialize shape: a moved tag missing its Release is a gap the
        scrub closes, with the same document an edit would have written."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=())

        updated = _update(tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh)

        assert gh.of("delete") == []
        assert gh.of("edit") == []
        assert len(gh.of("create")) == 1
        assert f"<!-- rlsbl-ci-sha: {ANCHOR} -->" in gh.bodies["v1.0.0"]
        assert updated == 1

    def test_a_creation_failure_is_a_warning_naming_the_tag(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")

        def failing(args, **kwargs):
            if args[:2] == ["release", "view"]:
                raise subprocess.CalledProcessError(1, "gh release view")
            raise subprocess.CalledProcessError(1, "gh release create")

        updated = update_github_releases(
            [{"refname": "refs/tags/v1.0.0"}],
            ctx=Ctx(str(tmp_path)), project_root=str(tmp_path),
            workspace_projects=None, tag_prefix_index=None,
            gh=failing, gh_installed=lambda: True, gh_auth=lambda: True,
            extract_entry=lambda _p, _v: "notes",
        )

        assert updated == 0
        assert "v1.0.0" in capsys.readouterr().err
