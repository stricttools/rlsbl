"""Tests for rlsbl.commands.edit_release."""

import unittest
from io import StringIO
from unittest.mock import patch, call, MagicMock

from rlsbl.commands.edit_release import run_cmd


# Shared changelog content for tests
CHANGELOG = """\
# Changelog

## 0.23.0

- Added new feature X
- Fixed bug Y

## 0.22.0

- Initial release
"""


class TestEditRelease(unittest.TestCase):
    """Tests for the release edit command (formerly edit-release)."""

    def _make_mock_target(self, version="0.23.0"):
        """Create a mock target with read_version and tag_format."""
        target = MagicMock()
        target.read_version.return_value = version
        target.tag_format.side_effect = lambda v: f"v{v}"
        return target

    def _make_mock_entry(self, name="npm", path="."):
        """Create a mock TargetEntry."""
        entry = MagicMock()
        entry.name = name
        entry.path = path
        return entry

    @patch("rlsbl.commands.edit_release.run_gh")
    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value="- Added new feature X\n- Fixed bug Y")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_syncs_notes_from_changelog(self, _gh_inst, _gh_auth, mock_targets_dict,
                                         mock_detect, _exists,
                                         mock_extract, mock_run):
        """Verify correct changelog entry is passed to gh release edit."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                run_cmd(["0.23.0"], {}, project_root=".")

        # Verify gh release view was called to check existence
        assert any(c[0] == (["release", "view", "v0.23.0"],) for c in mock_run.call_args_list)
        # Verify gh release edit was called with --notes-file
        edit_call = [c for c in mock_run.call_args_list
                     if c[0][0][:3] == ["release", "edit", "v0.23.0"]]
        self.assertEqual(len(edit_call), 1)
        self.assertIn("--notes-file", edit_call[0][0][0])

        # Verify changelog was extracted for the right version
        mock_extract.assert_called_once()
        self.assertEqual(mock_extract.call_args[0][1], "0.23.0")

    @patch("rlsbl.commands.edit_release.run_gh")
    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value="- Added new feature X")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_version_auto_detection(self, _gh_inst, _gh_auth, mock_targets_dict,
                                     mock_detect, _exists, mock_extract, mock_run):
        """No version arg -- uses current project version."""
        target = self._make_mock_target("0.23.0")
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd([], {}, project_root=".")

        target.read_version.assert_called_once_with(".")
        mock_extract.assert_called_once()
        self.assertEqual(mock_extract.call_args[0][1], "0.23.0")

    @patch("rlsbl.commands.edit_release.run_gh")
    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value="- Fixed bug")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_explicit_version(self, _gh_inst, _gh_auth, mock_targets_dict,
                               mock_detect, _exists, mock_extract, mock_run):
        """Pass '0.23.0' as explicit argument."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["0.23.0"], {}, project_root=".")

        # read_version should NOT be called when version is explicit
        target.read_version.assert_not_called()
        assert any(c[0] == (["release", "view", "v0.23.0"],) for c in mock_run.call_args_list)

    @patch("rlsbl.commands.edit_release.run_gh")
    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value="- Fixed bug")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_version_with_v_prefix(self, _gh_inst, _gh_auth, mock_targets_dict,
                                    mock_detect, _exists, mock_extract, mock_run):
        """Pass 'v0.23.0' -- the 'v' is stripped for changelog lookup."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["v0.23.0"], {}, project_root=".")

        # Changelog lookup should use "0.23.0" (without "v")
        mock_extract.assert_called_once()
        self.assertEqual(mock_extract.call_args[0][1], "0.23.0")
        # Tag should be "v0.23.0"
        assert any(c[0] == (["release", "view", "v0.23.0"],) for c in mock_run.call_args_list)

    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value=None)
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_missing_changelog_entry(self, _gh_inst, _gh_auth, mock_targets_dict,
                                      mock_detect, _exists, mock_extract):
        """Version has no entry in CHANGELOG.md -- exits with error."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(["0.99.0"], {}, project_root=".")

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("no changelog entry found", mock_stderr.getvalue())

    @patch("rlsbl.commands.edit_release.run_gh")
    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value="- Some changes")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_missing_github_release(self, _gh_inst, _gh_auth, mock_targets_dict,
                                     mock_detect, _exists, mock_extract, mock_run):
        """gh release view returns non-zero -- exits with error."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        # Make gh release view fail
        mock_run.side_effect = Exception("release not found")

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(["0.23.0"], {}, project_root=".")

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not found", mock_stderr.getvalue())

    @patch("rlsbl.commands.edit_release.run_gh")
    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value="- Added new feature X")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_dry_run(self, _gh_inst, _gh_auth, mock_targets_dict,
                      mock_detect, _exists, mock_extract, mock_run):
        """--dry-run prints but doesn't call gh release edit."""
        target = self._make_mock_target()
        entry = self._make_mock_entry()
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.23.0"], {"dry-run": True}, project_root=".")

        output = mock_stdout.getvalue()
        self.assertIn("Would update", output)
        self.assertIn("v0.23.0", output)

        # gh release view should still be called (to check existence)
        assert any(c[0] == (["release", "view", "v0.23.0"],) for c in mock_run.call_args_list)
        # gh release edit should NOT be called
        edit_calls = [c for c in mock_run.call_args_list
                      if c[0][0] and "edit" in c[0][0]]
        self.assertEqual(len(edit_calls), 0)


class TestEditReleaseReleasableInheritance:
    """release edit must resolve the primary target with releasable inheritance.

    In explicit releasable mode a member's ``targets`` may live ONLY in the
    releasable-level config.json. Bare detect_targets(project_dir) raises
    ConfigError for such a member (config file present, no targets key), so
    the command must route target resolution through resolve_member_context.
    """

    def test_edit_release_resolves_targets_with_releasable_inheritance(
        self, multi_releasable_monorepo_factory, capsys,
    ):
        from rlsbl.workspace import Releasable

        ns = multi_releasable_monorepo_factory(
            releasables=[Releasable(name="alpha")],
            projects=[{"path": "libs/alpha-core", "name": "alpha-core",
                       "releasable": "alpha"}],
            releasable_configs={"alpha": {"private": False, "targets": ["pypi"]}},
        )
        member = ns.root / "libs" / "alpha-core"
        # Targets live ONLY at the releasable level: member config has none.
        (member / ".rlsbl" / "config.json").write_text("{}\n")
        (member / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.0\n\n- Initial release\n"
        )

        gh_calls = []

        def fake_run_gh(args, **kwargs):
            gh_calls.append(list(args))
            return ""

        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.edit_release.run_gh", side_effect=fake_run_gh):
            run_cmd([], {"dry-run": True}, project_root=str(member))

        # Version auto-detected from the pypi target (releasable-level),
        # tag built with the releasable tag format.
        assert ["release", "view", "alpha@v0.1.0"] in gh_calls
        out = capsys.readouterr().out
        assert "Would update GitHub Release notes for alpha@v0.1.0" in out


if __name__ == "__main__":
    unittest.main()
