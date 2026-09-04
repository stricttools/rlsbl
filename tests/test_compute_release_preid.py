"""Regression test: compute_release_version must thread preid to bump_version.

Setting preid="alpha" with bump="minor" on version 0.42.0 must produce
0.43.0-alpha.0, not 0.43.0.
"""

import json

from unittest.mock import MagicMock

from githarness import commit_file, git, init_repo

from conftest import archive_release, git_head, release_record_dir
from rlsbl.commands.release.validate import compute_release_version


class TestComputeReleaseVersionPreid:
    """compute_release_version forwards preid to bump_version."""

    def test_minor_bump_with_alpha_preid(self, tmp_path):
        """minor bump + preid='alpha' on 0.42.0 -> 0.43.0-alpha.0."""
        # A real repository with 0.42.0 archived, so the bump path is taken
        # because the RELEASE RECORD says the version shipped -- which is what decides
        # it now, rather than a mocked tag read.
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_file(
            repo, "package.json",
            json.dumps({"name": "pkg", "version": "0.42.0"}) + "\n", "initial",
        )
        git(repo, "tag", "v0.42.0")
        archive_release(release_record_dir(repo), "0.42.0", git_head(repo))

        mock_target = MagicMock()
        mock_target.read_version.return_value = "0.42.0"
        mock_target.tag_format.side_effect = lambda v: f"v{v}"

        current, new, bump, tag = compute_release_version(
            mock_target, str(repo), "minor",
            None, None, lambda msg: None,
            preid="alpha", project_dir=str(repo),
        )

        assert current == "0.42.0"
        assert new == "0.43.0-alpha.0"
        assert bump == "minor"
        assert tag == "v0.43.0-alpha.0"
