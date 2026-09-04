"""What a GitHub Release body carries is decided in exactly one place.

Before ``rlsbl.release_publication`` existed, the release flow and the reconcile
path each composed a Release body for themselves, and they disagreed: the
reconcile path wrote notes with no ``rlsbl-ci-sha`` marker and never marked a
pre-release version as a GitHub pre-release, so a Release it recreated was one
the publish workflow could not judge.

These tests pin the shared document -- notes, marker, pre-release flag -- and
the argv every consumer builds from it.
"""

import pytest

from rlsbl.release_publication import (
    anchor_from_release_record,
    create_args,
    edit_notes_args,
    ensure_marker,
    is_prerelease,
    publication,
    strip_ci_sha_marker,
    view_body_args,
)

SHA = "a" * 40
OTHER = "b" * 40


def _pub(version="1.2.3", notes="### Features\n- a thing\n", sha=SHA):
    return publication(
        tag=f"v{version}", version=version, candidate_sha=sha, notes=notes,
    )


class TestTheBody:

    def test_notes_then_marker(self):
        body = _pub().body
        assert body.startswith("### Features\n- a thing")
        assert body.endswith(f"<!-- rlsbl-ci-sha: {SHA} -->\n")

    def test_an_empty_changelog_section_still_names_the_version(self):
        body = _pub(notes="").body
        assert "Release 1.2.3" in body
        assert f"<!-- rlsbl-ci-sha: {SHA} -->" in body

    def test_a_missing_anchor_is_refused(self):
        with pytest.raises(ValueError) as exc:
            publication(tag="v1.0.0", version="1.0.0", candidate_sha="")
        assert "rlsbl-ci-sha" in str(exc.value)


class TestThePrereleaseFlag:

    @pytest.mark.parametrize("version,expected", [
        ("1.2.3", False),
        ("1.0.0-rc.1", True),
        ("0.1.0-alpha.2", True),
        ("10.20.30", False),
    ])
    def test_a_prerelease_segment_decides(self, version, expected):
        assert is_prerelease(version) is expected
        assert _pub(version=version).prerelease is expected

    def test_the_flag_reaches_the_argv(self):
        assert "--prerelease" in create_args(_pub("1.0.0-rc.1"), "notes.md")
        assert "--prerelease" not in create_args(_pub("1.0.0"), "notes.md")


class TestMarkerReconciliation:

    def test_a_body_already_carrying_the_marker_needs_no_write(self):
        pub = _pub()
        assert pub.reconciled_body(f"Notes\n\n{pub.marker}\n") is None

    def test_a_stale_marker_is_replaced_not_duplicated(self):
        pub = _pub()
        new = pub.reconciled_body(f"Notes\n\n<!-- rlsbl-ci-sha: {OTHER} -->\n")
        assert new.count("rlsbl-ci-sha") == 1
        assert OTHER not in new
        assert "Notes" in new

    def test_a_body_with_no_marker_gains_one(self):
        new = _pub().reconciled_body("Notes\n")
        assert new == f"Notes\n\n<!-- rlsbl-ci-sha: {SHA} -->\n"

    def test_stripping_leaves_the_rest(self):
        assert strip_ci_sha_marker(
            f"a\n<!-- rlsbl-ci-sha: {SHA} -->\nb\n"
        ) == "a\nb\n"


class TestTheGhSurface:

    def test_repo_scoping_is_explicit(self):
        assert view_body_args("v1.0.0", repo="o/r")[-2:] == ["--repo", "o/r"]
        assert "--repo" not in view_body_args("v1.0.0")

    def test_create_names_title_and_notes_file(self):
        args = create_args(_pub(), "/tmp/notes.md")
        assert args[:3] == ["release", "create", "v1.2.3"]
        assert args[args.index("--title") + 1] == "v1.2.3"
        assert args[args.index("--notes-file") + 1] == "/tmp/notes.md"

    def test_ensure_marker_writes_only_when_needed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pub = _pub()
        calls = []

        def gh(args, **kwargs):
            calls.append(list(args))
            if args[:2] == ["release", "view"]:
                return "Notes\n"
            return ""

        assert ensure_marker(pub, gh=gh) is True
        edits = [c for c in calls if c[:2] == ["release", "edit"]]
        assert len(edits) == 1

        calls.clear()

        def gh_correct(args, **kwargs):
            calls.append(list(args))
            if args[:2] == ["release", "view"]:
                return f"Notes\n\n{pub.marker}\n"
            return ""

        assert ensure_marker(pub, gh=gh_correct) is False
        assert [c for c in calls if c[:2] == ["release", "edit"]] == []

    def test_the_notes_file_is_removed_afterwards(self, tmp_path, monkeypatch):
        import os

        monkeypatch.chdir(tmp_path)
        seen = {}

        def gh(args, **kwargs):
            if "--notes-file" in args:
                path = args[args.index("--notes-file") + 1]
                seen["path"] = path
                seen["body"] = open(path, encoding="utf-8").read()
            return ""

        from rlsbl.release_publication import create_release

        create_release(_pub(), gh=gh)
        assert seen["body"] == _pub().body
        assert not os.path.exists(seen["path"])


class TestTheAnchorComesFromTheReleaseRecord:

    def test_it_reads_the_archive(self, tmp_path):
        from rlsbl.release_file import write_archived_release_file

        releases = str(tmp_path / "releases")
        write_archived_release_file(
            releases, "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=SHA, tree_hashes={".": "c" * 40},
        )
        assert anchor_from_release_record(releases, "1.0.0") == SHA

    def test_an_unanchorable_version_has_no_anchor(self, tmp_path):
        from rlsbl.release_file import write_archived_release_file

        releases = str(tmp_path / "releases")
        write_archived_release_file(
            releases, "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=None, tree_hashes=None,
            unanchorable=True,
        )
        assert anchor_from_release_record(releases, "1.0.0") is None

    def test_an_absent_archive_answers_none(self, tmp_path):
        assert anchor_from_release_record(str(tmp_path), "9.9.9") is None

    def test_it_answers_where_the_guarded_read_refuses(self, tmp_path):
        """The read that repair paths need, on the repository they run in.

        ``release_record.read_entry`` refuses when the tag and the anchor disagree --
        which is exactly the state a repair is called to end, so the repair
        cannot be made to depend on that read.
        """
        from githarness import commit_file, git, init_repo
        from rlsbl import release_record
        from rlsbl.errors import ReleaseRecordError
        from rlsbl.release_file import write_archived_release_file

        repo = tmp_path / "repo"
        init_repo(repo)
        sha = commit_file(repo, "a.txt", "a\n", "one")
        tree = git(repo, "rev-parse", f"{sha}^{{tree}}")
        releases = str(repo / ".rlsbl" / "releases")
        write_archived_release_file(
            releases, "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=sha, tree_hashes={".": tree},
        )
        second = commit_file(repo, "b.txt", "b\n", "two")
        git(repo, "tag", "v1.0.0", second)

        with pytest.raises(ReleaseRecordError):
            release_record.read_entry(releases, "1.0.0", tag_glob="v*", cwd=str(repo))
        assert anchor_from_release_record(releases, "1.0.0") == sha
