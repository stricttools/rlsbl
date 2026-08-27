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
from pathlib import Path

import pytest

from githarness import git

from rlsbl.changelog.generate import generate_changelog
from rlsbl.release_file import get_releases_dir, read_release_file

from rlsbl.workspace import get_releasable_changes_dir, get_releasable_dir

from test_batch_main_as_candidate import (  # noqa: E402
    _run_batch,
    _setup_batch_workspace,
    _setup_releasable_batch_workspace,
)


def _member_release_archive(root, member, version):
    """A member's release archive: under its releasable, not the package."""
    return os.path.join(
        get_releases_dir(
            str(root / member),
            releasable_dir=get_releasable_dir(str(root), member),
        ),
        f"v{version}.toml",
    )


def _releasable_release_archive(root, releasable, version):
    return os.path.join(
        get_releases_dir(releasable_dir=get_releasable_dir(str(root), releasable)),
        f"v{version}.toml",
    )


class TestBatchReleaseMetadataSurvivesRegeneration:

    def test_the_description_survives_a_changelog_regeneration(
        self, tmp_project,
    ):
        _setup_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        member = tmp_project / "alpha"
        md_path = Path(
            get_releasable_changes_dir(str(tmp_project), "alpha")
        ) / "1.0.1.md"
        assert "Alpha patch" in md_path.read_text(), (
            "precondition: the release itself renders the batch description"
        )

        # Any later regeneration -- the next release does this for every
        # version -- must not strip it. The member's changelog home is its
        # releasable's, so that is what regeneration reads and writes.
        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        canonical = Path(rel_dir) / "CHANGELOG.md"
        generate_changelog(
            str(member),
            changes_dir_override=str(
                get_releasable_changes_dir(str(tmp_project), "alpha")
            ),
            changelog_output_path=str(canonical),
            releases_dir_override=get_releases_dir(releasable_dir=rel_dir),
        )

        assert "Alpha patch" in md_path.read_text(), (
            "the batch release's description must survive regeneration of "
            "the per-version .md"
        )
        assert "Alpha patch" in canonical.read_text(), (
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


class TestReleasableBatchMetadataSurvivesRegeneration:
    """The same durability, for the EXPLICIT (releasable) batch path.

    Only the implicit-package variant of the archive synthesis was locked down,
    yet a releasable batch is the shape that lost its metadata in the field: a
    releasable's release file lives under
    ``.rlsbl-monorepo/releasables/<name>/releases/``, not under any member's
    ``.rlsbl/``, so it reaches ``_synthesize_archive`` through a different
    path resolution. Without this, that path could silently stop archiving and
    every batch-released releasable would lose its description on the next
    ``rlsbl changelog generate`` -- exactly the regression the implicit-mode
    tests were written to prevent.
    """

    def test_the_description_survives_a_changelog_regeneration(self, tmp_project):
        _setup_releasable_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        releasable_dir = get_releasable_dir(str(tmp_project), "alpha")
        changes_dir = get_releasable_changes_dir(str(tmp_project), "alpha")
        md_path = os.path.join(changes_dir, "1.0.1.md")
        with open(md_path, encoding="utf-8") as f:
            assert "Alpha patch" in f.read(), (
                "precondition: the release itself renders the batch description"
            )

        # Prove the regeneration really rewrites this file: without the
        # archived v1.0.1.toml the description has nowhere to come from, and
        # an assertion on an untouched file would pass vacuously.
        os.remove(md_path)
        # The same call `rlsbl changelog generate` makes in releasable mode.
        generate_changelog(
            str(tmp_project / "alpha"),
            changes_dir_override=changes_dir,
            changelog_output_path=os.path.join(releasable_dir, "CHANGELOG.md"),
            releases_dir_override=get_releases_dir(
                releasable_dir=releasable_dir,
            ),
        )

        assert os.path.isfile(md_path), "regeneration must recreate the .md"
        with open(md_path, encoding="utf-8") as f:
            assert "Alpha patch" in f.read(), (
                "a releasable's batch description must survive regeneration"
            )
        with open(
            os.path.join(releasable_dir, "CHANGELOG.md"), encoding="utf-8",
        ) as f:
            assert "Alpha patch" in f.read(), (
                "and must survive in the releasable's CHANGELOG.md too"
            )

    def test_the_release_metadata_is_archived_under_the_releasable(
        self, tmp_project,
    ):
        _setup_releasable_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        archive = _releasable_release_archive(tmp_project, "alpha", "1.0.1")
        assert os.path.isfile(archive), (
            "a batch-released releasable must archive its release metadata as "
            f"v1.0.1.toml in its own releases dir; {archive} is missing"
        )

        config = read_release_file(archive)
        assert config.description == "Alpha patch"
        assert config.bump == "patch"
        assert config.include == ["npm"]

        assert not os.access(archive, os.W_OK) or (
            os.stat(archive).st_mode & 0o222 == 0
        ), "the archived release file must be read-only"

    def test_every_releasable_in_the_batch_gets_its_own_archive(self, tmp_project):
        """Not just the first: each releasable carries its own description."""
        _setup_releasable_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        for name, description in (("alpha", "Alpha patch"), ("beta", "Beta patch")):
            archive = _releasable_release_archive(tmp_project, name, "1.0.1")
            assert os.path.isfile(archive), f"{name} has no archived release file"
            assert read_release_file(archive).description == description

    def test_the_archive_is_committed_not_left_dirty(self, tmp_project):
        _setup_releasable_batch_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        status = git(tmp_project, "status", "--porcelain")
        assert "v1.0.1.toml" not in status, (
            f"the synthesized archive must be committed; tree: {status}"
        )
