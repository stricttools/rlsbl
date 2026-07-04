"""Regression test: compute_release_version must thread preid to bump_version.

Setting preid="alpha" with bump="minor" on version 0.42.0 must produce
0.43.0-alpha.0, not 0.43.0.
"""

from unittest.mock import MagicMock, patch

from rlsbl.commands.release.validate import compute_release_version


class TestComputeReleaseVersionPreid:
    """compute_release_version forwards preid to bump_version."""

    def test_minor_bump_with_alpha_preid(self):
        """minor bump + preid='alpha' on 0.42.0 -> 0.43.0-alpha.0."""
        mock_target = MagicMock()
        mock_target.read_version.return_value = "0.42.0"
        mock_target.tag_format.side_effect = lambda v: f"v{v}"

        with patch("rlsbl.commands.release.tag_exists_locally") as mock_tag_exists:
            # First call: current tag exists (so bump path is taken)
            # Second call: new tag does not exist
            mock_tag_exists.side_effect = [True, False]

            current, new, bump, tag = compute_release_version(
                mock_target, "/fake/path", "minor",
                None, None, lambda msg: None,
                preid="alpha",
            )

        assert current == "0.42.0"
        assert new == "0.43.0-alpha.0"
        assert bump == "minor"
        assert tag == "v0.43.0-alpha.0"
