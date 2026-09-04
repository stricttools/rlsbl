"""A history rewrite must move the release record's anchors, not just the changelog.

The release archive records the commit a version shipped from (its
``candidate_sha``) and the tree each released path carried. A history rewrite
moves those commits. ``rlsbl release scrub`` has always remapped the JSONL
changelog hashes through the rewrite's commit map -- and never the archives, so
after a scrub every guarded release record read hit the DISAGREEMENT error, which blames
the TAG for having moved when the tag is the only one of the two that was
repaired.

These tests pin the repair: the anchors go through the same map, the tree hashes
are recomputed at the new commits and must come out content-identical, and a
version whose content genuinely changed is a hard error rather than a silently
re-anchored archive claiming content it no longer has.
"""

import pytest

from githarness import commit_file, git, init_repo

from rlsbl import release_record
from rlsbl.anchor_remap import remap_release_anchors
from rlsbl.errors import ReleaseRecordError, RlsblError
from rlsbl.release_file import (
    archived_release_path,
    read_release_file,
    write_archived_release_file,
)


def _released_repo(tmp_path, *, version="1.0.0"):
    """A repo with one released version: an anchored archive plus its tag."""
    repo = tmp_path / "repo"
    init_repo(repo)
    sha = commit_file(repo, "README.md", "# hi\n", "initial")
    tree = git(repo, "rev-parse", f"{sha}^{{tree}}")

    releases = repo / ".rlsbl" / "releases"
    write_archived_release_file(
        str(releases), version,
        bump="minor", include=["plain"], description="The first release.",
        candidate_sha=sha, tree_hashes={".": tree},
    )
    git(repo, "tag", f"v{version}", sha)
    return repo, sha


def _rewrite(repo, *, content=None):
    """Move HEAD the way a scrub does. Returns ``(old_sha, new_sha)``.

    With *content* the rewritten commit carries different bytes, which is what a
    scrub that redacted a released file produces; without it the tree is
    untouched and only the commit identity changes.
    """
    old = git(repo, "rev-parse", "HEAD")
    if content is not None:
        (repo / "README.md").write_text(content)
        git(repo, "add", "README.md")
    # A new message guarantees a new commit object even when the tree is
    # untouched, which is exactly what a scrub of a commit MESSAGE produces.
    git(repo, "commit", "-q", "--amend", "-m", "rewritten", "--allow-empty")
    new = git(repo, "rev-parse", "HEAD")
    assert new != old
    # A scrub moves the tags with the history; the archive is what it forgets.
    git(repo, "tag", "-f", "v1.0.0", new)
    return old, new


class TestTheAuditsReproduction:
    """After a scrub, every guarded release record read must answer again."""

    def test_an_unremapped_anchor_breaks_every_release_record_read(self, tmp_path):
        repo, _sha = _released_repo(tmp_path)
        _rewrite(repo)

        releases = str(repo / ".rlsbl" / "releases")
        with pytest.raises(ReleaseRecordError) as exc:
            release_record.read_entry(releases, "1.0.0", tag_glob="v*", cwd=str(repo))
        assert "disagree" in str(exc.value)

    def test_remapping_the_anchor_makes_the_release_record_readable_again(self, tmp_path):
        repo, _sha = _released_repo(tmp_path)
        old, new = _rewrite(repo)

        releases = str(repo / ".rlsbl" / "releases")
        remapped = remap_release_anchors(releases, {old: new}, cwd=str(repo))

        assert [(r.version, r.old_sha, r.new_sha) for r in remapped] == [
            ("1.0.0", old, new)
        ]
        entry = release_record.read_entry(
            releases, "1.0.0", tag_glob="v*", cwd=str(repo),
        )
        assert entry.candidate_sha == new

    def test_the_archive_stays_read_only_and_valid(self, tmp_path):
        import os
        import stat

        repo, _sha = _released_repo(tmp_path)
        old, new = _rewrite(repo)
        releases = str(repo / ".rlsbl" / "releases")
        remap_release_anchors(releases, {old: new}, cwd=str(repo))

        path = archived_release_path(releases, "1.0.0")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o444, f"the archive must be relocked, got {oct(mode)}"
        archive = read_release_file(path)
        assert archive.candidate_sha == new
        assert archive.description == "The first release."


class TestContentIsVerified:

    def test_a_changed_tree_is_a_hard_error_naming_the_version(self, tmp_path):
        repo, _sha = _released_repo(tmp_path)
        old, new = _rewrite(repo, content="# redacted\n")
        releases = str(repo / ".rlsbl" / "releases")

        with pytest.raises(RlsblError) as exc:
            remap_release_anchors(releases, {old: new}, cwd=str(repo))
        message = str(exc.value)
        assert "1.0.0" in message
        assert new[:12] in message

    def test_nothing_is_written_when_one_version_fails(self, tmp_path):
        repo, _sha = _released_repo(tmp_path)
        old, new = _rewrite(repo, content="# redacted\n")
        releases = str(repo / ".rlsbl" / "releases")
        before = archived_release_path(releases, "1.0.0")
        original = open(before, encoding="utf-8").read()

        with pytest.raises(RlsblError):
            remap_release_anchors(releases, {old: new}, cwd=str(repo))
        assert open(before, encoding="utf-8").read() == original


class TestWhatIsLeftAlone:

    def test_an_anchor_the_map_does_not_mention_is_untouched(self, tmp_path):
        repo, sha = _released_repo(tmp_path)
        releases = str(repo / ".rlsbl" / "releases")
        assert remap_release_anchors(releases, {"0" * 40: "1" * 40},
                                     cwd=str(repo)) == []
        assert read_release_file(
            archived_release_path(releases, "1.0.0")
        ).candidate_sha == sha

    def test_an_unanchorable_archive_is_skipped(self, tmp_path):
        repo, _sha = _released_repo(tmp_path)
        releases = repo / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "0.9.0",
            bump="patch", include=["plain"], description="Lost to history.",
            candidate_sha=None, tree_hashes=None, unanchorable=True,
        )
        old, new = _rewrite(repo)
        remapped = remap_release_anchors(str(releases), {old: new}, cwd=str(repo))
        assert [r.version for r in remapped] == ["1.0.0"]
