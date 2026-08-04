"""A batch-released version keeps its description and context forever.

A standalone release archives its ``unreleased.toml`` as
``.rlsbl/releases/v{version}.toml``, and every later regeneration of that
version's changelog reads its description, context and bump type back out of
that archive.

A batch release has no per-member release file: the metadata lives in the
workspace-level batch TOML, which is archived under a different name.  The
released version's ``.md`` and its ``CHANGELOG.md`` section therefore rendered
the description ONCE, at release time, and lost it on the next
``rlsbl changelog generate`` -- silently, since regeneration is routine (a
later release regenerates every version's file).
"""

import json
import os

import pytest

from githarness import git

from rlsbl.changelog.generate import generate_changelog
from rlsbl.release_file import get_releases_dir, read_release_file

from test_batch_main_as_candidate import (  # noqa: E402
    _run_batch,
    _setup_batch_workspace,
)


def _member_release_archive(root, member, version):
    return os.path.join(
        get_releases_dir(str(root / member)), f"v{version}.toml",
    )


class TestBatchReleaseMetadataSurvivesRegeneration:

    def test_the_description_survives_a_changelog_regeneration(
        self, tmp_project,
    ):
        _setup_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        member = tmp_project / "alpha"
        md_path = member / ".rlsbl" / "changes" / "1.0.1.md"
        assert "Alpha patch" in md_path.read_text(), (
            "precondition: the release itself renders the batch description"
        )

        # Any later regeneration -- the next release does this for every
        # version -- must not strip it.
        generate_changelog(str(member))

        assert "Alpha patch" in md_path.read_text(), (
            "the batch release's description must survive regeneration of "
            "the per-version .md"
        )
        assert "Alpha patch" in (member / "CHANGELOG.md").read_text(), (
            "and must survive in CHANGELOG.md too"
        )

    def test_the_release_metadata_is_archived_like_a_standalone_release(
        self, tmp_project,
    ):
        """The durable record lives where every reader already looks."""
        _setup_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        archive = _member_release_archive(tmp_project, "alpha", "1.0.1")
        assert os.path.isfile(archive), (
            "a batch-released member must archive its release metadata as "
            f"v1.0.1.toml, like a standalone release does; {archive} is missing"
        )

        # It must be a real release document, not a metadata scrap: the undo
        # path restores it as unreleased.toml and re-reads it.
        config = read_release_file(archive)
        assert config.description == "Alpha patch"
        assert config.bump == "patch"
        assert config.include == ["npm"]

        # Archived release files are immutable, like the standalone ones.
        assert not os.access(archive, os.W_OK) or (
            os.stat(archive).st_mode & 0o222 == 0
        ), "the archived release file must be read-only"

    def test_the_archive_is_committed_not_left_dirty(self, tmp_project):
        _setup_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        status = git(tmp_project, "status", "--porcelain")
        assert "v1.0.1.toml" not in status, (
            f"the synthesized archive must be committed; tree: {status}"
        )
