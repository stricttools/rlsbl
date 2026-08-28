"""What a scrub does to the GitHub Releases attached to the tags it moved.

``recreate_github_releases`` is the scrub flow's ``RELEASES_UPDATED`` step: a
rewritten tag drags its Release onto a commit that no longer exists, so each
existing Release is deleted and recreated against the moved tag.

Two properties are pinned here, both of them destructive-path properties:

* **Nothing is deleted that cannot be recreated.** Every question that could
  make the recreation impossible -- can the tag be parsed into a version at
  all? -- is answered BEFORE the delete. A pre-release tag is a first-class
  version here (``PRERELEASE_INCLUSIVE``), and a tag that is not a version tag
  at all is skipped whole, with its Release left exactly as it was.
* **The recreated Release is the same document the release flow writes.** Body,
  title, the ``rlsbl-ci-sha`` marker taken from the ledger's anchor, and the
  pre-release flag all come from :mod:`rlsbl.release_publication`, so a Release
  the scrub recreates is one the publish workflow can still judge.
"""

import subprocess

from rlsbl.commands.release_reconcile import recreate_github_releases
from rlsbl.release_file import write_archived_release_file
from rlsbl.release_publication import publication

ANCHOR = "a" * 40
TREE = "f" * 40


class Ctx:
    """The two attributes the recreation reads off a project context."""

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
        if args[:2] == ["release", "create"]:
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


def _recreate(tmp_path, tags, recorder, *, notes="## notes\n\n- A change.\n"):
    # The notes come from the owning project's CHANGELOG.md, so the file has
    # to be there for the extraction to be reached at all.
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    return recreate_github_releases(
        tags, ctx=Ctx(str(tmp_path)), project_root=str(tmp_path),
        workspace_projects=None, tag_prefix_index=None,
        gh=recorder, gh_installed=lambda: True, gh_auth=lambda: True,
        extract_entry=lambda _path, _version: notes,
    )


class TestNothingIsDeletedThatCannotBeRecreated:

    def test_a_prerelease_tags_release_is_recreated(self, tmp_path, monkeypatch):
        """The reproduction: a ``-rc.1`` tag's Release was deleted and never
        recreated, because the version parse ran AFTER the delete and rejected
        every pre-release tag."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0-rc.1")
        gh = Recorder(existing=("v1.0.0-rc.1",))

        recreated = _recreate(
            tmp_path, [{"refname": "refs/tags/v1.0.0-rc.1", "new_sha": ANCHOR}],
            gh,
        )

        assert len(gh.of("delete")) == 1
        assert len(gh.of("create")) == 1, (
            "the Release was deleted, so it must be recreated -- a pre-release "
            "version is a released version like any other"
        )
        assert recreated == 1

    def test_a_tag_that_is_not_a_version_is_skipped_before_any_delete(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.chdir(tmp_path)
        gh = Recorder(existing=("nightly",))

        recreated = _recreate(
            tmp_path, [{"refname": "refs/tags/nightly", "new_sha": ANCHOR}], gh,
        )

        assert gh.of("delete") == [], (
            "a tag whose version cannot be read is a Release that cannot be "
            "recreated, so it must not be deleted"
        )
        assert gh.of("create") == []
        assert recreated == 0
        assert "nightly" in capsys.readouterr().err

    def test_a_recreation_failure_leaves_the_others_alone(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Individual failures stay warnings: the rest of the scrub's tags are
        still reconciled."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        _ledger(tmp_path, "1.1.0")
        gh = Recorder(existing=("v1.0.0", "v1.1.0"))
        real = gh.__call__

        def flaky(args, **kwargs):
            if args[:3] == ["release", "create", "v1.0.0"]:
                raise subprocess.CalledProcessError(1, "gh release create")
            return real(args, **kwargs)

        recreated = recreate_github_releases(
            [{"refname": "refs/tags/v1.0.0"}, {"refname": "refs/tags/v1.1.0"}],
            ctx=Ctx(str(tmp_path)), project_root=str(tmp_path),
            workspace_projects=None, tag_prefix_index=None,
            gh=flaky, gh_installed=lambda: True, gh_auth=lambda: True,
            extract_entry=lambda _p, _v: "notes",
        )
        assert recreated == 1
        err = capsys.readouterr().err
        assert "v1.0.0" in err
        assert "now has NONE" in err, (
            "a failure between the delete and the create leaves the tag with "
            "no Release at all, and the warning has to say so"
        )


class TestTheRecreatedDocument:

    def test_it_is_byte_identical_to_the_shared_publication(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=("v1.0.0",))
        notes = "## 1.0.0\n\n### Fixes\n\n- **Fixed it.** Really.\n"

        _recreate(
            tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh, notes=notes,
        )

        expected = publication(
            tag="v1.0.0", version="1.0.0", candidate_sha=ANCHOR, notes=notes,
        ).body
        assert gh.bodies["v1.0.0"] == expected, (
            "the scrub must recreate the same document the release flow "
            "writes, not a notes-only body"
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

        _recreate(
            tmp_path,
            [{"refname": "refs/tags/v1.0.0", "new_sha": "c" * 40}], gh,
        )

        assert f"<!-- rlsbl-ci-sha: {'b' * 40} -->" in gh.bodies["v1.0.0"]

    def test_a_prerelease_version_is_recreated_as_a_prerelease(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0-rc.1")
        gh = Recorder(existing=("v1.0.0-rc.1",))

        _recreate(tmp_path, [{"refname": "refs/tags/v1.0.0-rc.1"}], gh)

        assert "--prerelease" in gh.of("create")[0]

    def test_a_final_version_is_not(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=("v1.0.0",))

        _recreate(tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh)

        assert "--prerelease" not in gh.of("create")[0]

    def test_a_version_with_no_archive_is_recreated_markerless_and_says_so(
        self, tmp_path, monkeypatch, capsys,
    ):
        """A version the ledger cannot anchor still gets its Release back --
        without a marker, and with the omission stated rather than hidden."""
        monkeypatch.chdir(tmp_path)
        gh = Recorder(existing=("v0.1.0",))

        _recreate(tmp_path, [{"refname": "refs/tags/v0.1.0"}], gh)

        assert len(gh.of("create")) == 1
        assert "rlsbl-ci-sha" not in gh.bodies["v0.1.0"]
        err = capsys.readouterr().err
        assert "0.1.0" in err and "marker" in err

    def test_a_tag_with_no_release_is_left_alone(self, tmp_path, monkeypatch):
        """This reconciles Releases; it does not publish new ones."""
        monkeypatch.chdir(tmp_path)
        _ledger(tmp_path, "1.0.0")
        gh = Recorder(existing=())

        recreated = _recreate(tmp_path, [{"refname": "refs/tags/v1.0.0"}], gh)

        assert gh.of("delete") == []
        assert gh.of("create") == []
        assert recreated == 0
