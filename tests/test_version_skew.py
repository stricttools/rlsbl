"""Tests for the release-time version-skew guard.

When dev-sources.toml.local-only declares local checkout overlays, the
release must verify that no overlaid dependency's local checkout is AHEAD
of its registry release: releasing against unreleased dependency features
would ship something the registry cannot satisfy. Local > registry is a
hard error ("release the dependency first"), equal/behind passes, an
unpublished dependency or a registry/network failure is a hard error
(never a silent skip), and a missing overlays file is a no-op.
"""

from unittest.mock import patch

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    _abort_on_version_skew,
)

QUERY_FN = "rlsbl.registry.query_pypi_version"


def _make_checkout(tmp_path, name, version):
    checkout = tmp_path / "checkouts" / name
    checkout.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return checkout


def _make_project(tmp_path, overlays=None):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    if overlays is not None:
        blocks = "".join(
            f'[[overlay]]\npackage = "{pkg}"\npath = "{path}"\n\n'
            for pkg, path in overlays
        )
        (project / "dev-sources.toml.local-only").write_text(blocks)
    return project


class TestVersionSkew:
    def test_local_ahead_aborts_naming_dep(self, tmp_path):
        checkout = _make_checkout(tmp_path, "somedep", "0.5.0")
        project = _make_project(tmp_path, [("somedep", str(checkout))])

        with patch(QUERY_FN, return_value={"status": "found", "version": "0.4.0"}):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_version_skew(str(project))
        msg = str(exc.value)
        assert "release the dependency first" in msg
        assert "somedep" in msg
        assert "0.5.0" in msg
        assert "0.4.0" in msg

    def test_equal_passes(self, tmp_path):
        checkout = _make_checkout(tmp_path, "somedep", "0.4.0")
        project = _make_project(tmp_path, [("somedep", str(checkout))])

        with patch(QUERY_FN, return_value={"status": "found", "version": "0.4.0"}):
            _abort_on_version_skew(str(project))  # must not raise

    def test_behind_passes(self, tmp_path):
        checkout = _make_checkout(tmp_path, "somedep", "0.3.0")
        project = _make_project(tmp_path, [("somedep", str(checkout))])

        with patch(QUERY_FN, return_value={"status": "found", "version": "0.4.0"}):
            _abort_on_version_skew(str(project))  # must not raise

    def test_not_found_aborts(self, tmp_path):
        checkout = _make_checkout(tmp_path, "somedep", "0.1.0")
        project = _make_project(tmp_path, [("somedep", str(checkout))])

        with patch(QUERY_FN, return_value={"status": "not_found"}):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_version_skew(str(project))
        msg = str(exc.value)
        assert "release the dependency first" in msg
        assert "somedep" in msg

    def test_network_error_aborts_cleanly(self, tmp_path):
        checkout = _make_checkout(tmp_path, "somedep", "0.1.0")
        project = _make_project(tmp_path, [("somedep", str(checkout))])

        with patch(QUERY_FN, return_value={"status": "error", "message": "HTTP 503"}):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_version_skew(str(project))
        msg = str(exc.value)
        assert "somedep" in msg
        assert "HTTP 503" in msg

    def test_no_overlay_file_is_noop(self, tmp_path):
        project = _make_project(tmp_path, overlays=None)

        with patch(QUERY_FN) as mock_query:
            _abort_on_version_skew(str(project))  # must not raise
        mock_query.assert_not_called()

    def test_workspace_root_fallback(self, tmp_path):
        """In a monorepo, the overlays file at the workspace root is honored."""
        checkout = _make_checkout(tmp_path, "somedep", "0.5.0")
        ws = _make_project(tmp_path, [("somedep", str(checkout))])
        member = ws / "packages" / "a"
        member.mkdir(parents=True)

        with patch(QUERY_FN, return_value={"status": "found", "version": "0.4.0"}):
            with pytest.raises(ReleaseValidationError):
                _abort_on_version_skew(str(member), workspace_root=str(ws))

    def test_invalid_overlay_file_aborts(self, tmp_path):
        """A malformed overlays file is a hard error, not a silent skip."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "dev-sources.toml.local-only").write_text("not valid toml [[[")

        with pytest.raises(ReleaseValidationError):
            _abort_on_version_skew(str(project))

    def test_multiple_overlays_first_offender_reported(self, tmp_path):
        ok = _make_checkout(tmp_path, "okdep", "1.0.0")
        ahead = _make_checkout(tmp_path, "aheaddep", "2.1.0")
        project = _make_project(
            tmp_path, [("okdep", str(ok)), ("aheaddep", str(ahead))],
        )

        def fake_query(name):
            return {
                "okdep": {"status": "found", "version": "1.0.0"},
                "aheaddep": {"status": "found", "version": "2.0.0"},
            }[name]

        with patch(QUERY_FN, side_effect=fake_query):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_version_skew(str(project))
        assert "aheaddep" in str(exc.value)


class TestAStaleOverlayEntry:
    """An overlay whose checkout is gone blocks the release -- readably.

    The guard reading an unreadable overlay file is a hard error by design (a
    silent skip would let a release ship against unreleased dependency code).
    What the operator got, though, was "path does not exist" with no word about
    which file that entry lives in, what the file is for, or how to clear it --
    and the commonest cause is a leftover: the project stopped depending on the
    package entirely and the block was never removed.
    """

    def _stale(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        (project / "dev-sources.toml.local-only").write_text(
            '[[overlay]]\npackage = "somedep"\npath = "../gone"\n'
        )
        return project

    def test_the_refusal_names_the_file_its_purpose_and_the_remedy(
        self, tmp_path, capsys,
    ):
        project = self._stale(tmp_path)

        with patch(QUERY_FN) as mock_query:
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_version_skew(str(project))
        mock_query.assert_not_called()

        text = capsys.readouterr().err + str(exc.value)
        assert "dev-sources.toml.local-only" in text
        assert "rlsbl dev sync" in text
        assert "stale" in text.lower()
        assert "delete" in text.lower()
        assert "[[overlay]]" in text

    def test_deleting_the_overlay_file_clears_the_refusal(self, tmp_path):
        """The remedy the message names, performed."""
        project = self._stale(tmp_path)
        with pytest.raises(ReleaseValidationError):
            _abort_on_version_skew(str(project))

        (project / "dev-sources.toml.local-only").unlink()
        with patch(QUERY_FN) as mock_query:
            _abort_on_version_skew(str(project))  # must not raise
        mock_query.assert_not_called()

    def test_repointing_the_entry_clears_the_refusal(self, tmp_path):
        """The other remedy: point 'path' at where the checkout really is."""
        project = self._stale(tmp_path)
        with pytest.raises(ReleaseValidationError):
            _abort_on_version_skew(str(project))

        checkout = _make_checkout(tmp_path, "somedep", "0.3.0")
        (project / "dev-sources.toml.local-only").write_text(
            f'[[overlay]]\npackage = "somedep"\npath = "{checkout}"\n'
        )
        with patch(QUERY_FN, return_value={"status": "found", "version": "0.4.0"}):
            _abort_on_version_skew(str(project))  # must not raise
