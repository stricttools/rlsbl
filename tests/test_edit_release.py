"""Tests for rlsbl.commands.edit_release."""

import unittest
from io import StringIO
from unittest.mock import patch, MagicMock

from rlsbl.commands.edit_release import run_cmd
from rlsbl.release_publication import view_body_args


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
        mock_run.return_value = ""

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["0.23.0"], {}, project_root=".")

        # Verify gh release view was called to check existence
        assert any(c[0] == (view_body_args("v0.23.0"),) for c in mock_run.call_args_list)
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
        mock_run.return_value = ""

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
        mock_run.return_value = ""

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["0.23.0"], {}, project_root=".")

        # read_version should NOT be called when version is explicit
        target.read_version.assert_not_called()
        assert any(c[0] == (view_body_args("v0.23.0"),) for c in mock_run.call_args_list)

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
        mock_run.return_value = ""

        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(["v0.23.0"], {}, project_root=".")

        # Changelog lookup should use "0.23.0" (without "v")
        mock_extract.assert_called_once()
        self.assertEqual(mock_extract.call_args[0][1], "0.23.0")
        # Tag should be "v0.23.0"
        assert any(c[0] == (view_body_args("v0.23.0"),) for c in mock_run.call_args_list)

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
        mock_run.return_value = ""

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.23.0"], {"dry-run": True}, project_root=".")

        output = mock_stdout.getvalue()
        self.assertIn("Would update", output)
        self.assertIn("v0.23.0", output)

        # gh release view should still be called (to check existence)
        assert any(c[0] == (view_body_args("v0.23.0"),) for c in mock_run.call_args_list)
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
        import os

        from rlsbl.workspace import Releasable, get_releasable_dir

        ns = multi_releasable_monorepo_factory(
            releasables=[Releasable(name="alpha")],
            projects=[{"path": "libs/alpha-core", "name": "alpha-core",
                       "releasable": "alpha"}],
            releasable_configs={"alpha": {"publish_mode": "ci", "targets": ["pypi"]}},
        )
        member = ns.root / "libs" / "alpha-core"
        # Targets live ONLY at the releasable level: member config has none.
        (member / ".rlsbl" / "config.json").write_text("{}\n")
        # The canonical changelog lives at the releasable state dir.
        rel_dir = get_releasable_dir(str(ns.root), "alpha")
        with open(os.path.join(rel_dir, "CHANGELOG.md"), "w", encoding="utf-8") as f:
            f.write("# Changelog\n\n## 0.1.0\n\n- Initial release\n")

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
        assert view_body_args("alpha@v0.1.0") in gh_calls
        out = capsys.readouterr().out
        assert "Would update GitHub Release notes for alpha@v0.1.0" in out

    def test_edit_release_reads_changelog_from_releasable_dir(
        self, multi_releasable_monorepo_factory, capsys,
    ):
        """release edit resolves CHANGELOG.md at the releasable state dir.

        For explicit-mode releasables the canonical changelog lives at
        ``.rlsbl-monorepo/releasables/<name>/CHANGELOG.md``, not in the
        member project directory. Reading from the member dir (the old
        behavior) fails with "CHANGELOG.md not found".
        """
        import os

        from rlsbl.workspace import Releasable, get_releasable_dir

        ns = multi_releasable_monorepo_factory(
            releasables=[Releasable(name="alpha")],
            projects=[{"path": "libs/alpha-core", "name": "alpha-core",
                       "releasable": "alpha"}],
            releasable_configs={"alpha": {"publish_mode": "ci", "targets": ["pypi"]}},
        )
        member = ns.root / "libs" / "alpha-core"
        (member / ".rlsbl" / "config.json").write_text("{}\n")
        # CHANGELOG.md lives ONLY at the releasable dir -- NOT the member dir.
        rel_dir = get_releasable_dir(str(ns.root), "alpha")
        with open(os.path.join(rel_dir, "CHANGELOG.md"), "w", encoding="utf-8") as f:
            f.write("# Changelog\n\n## 0.1.0\n\n- Initial release\n")

        gh_calls = []

        def fake_run_gh(args, **kwargs):
            gh_calls.append(list(args))
            return ""

        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.edit_release.run_gh", side_effect=fake_run_gh):
            run_cmd([], {"dry-run": True}, project_root=str(member))

        assert view_body_args("alpha@v0.1.0") in gh_calls
        out = capsys.readouterr().out
        assert "Would update GitHub Release notes for alpha@v0.1.0" in out
        assert "Initial release" in out


_MARKER_SHA = "a" * 40
_MARKER_LINE = f"<!-- rlsbl-ci-sha: {_MARKER_SHA} -->"


class _FakeForge:
    """A gh stand-in holding one Release body, recording what edit writes.

    Answers both the bare existence probe (``release view <tag>``) and the
    body read (``release view <tag> --json body -q .body``), so a test fails
    on the body it asserts rather than on an unexpected argv.
    """

    def __init__(self, tag, body):
        self.tag = tag
        self.body = body
        self.written = []

    def __call__(self, args, **kwargs):
        args = list(args)
        if args[:3] == ["release", "view", self.tag]:
            return self.body
        if args[:3] == ["release", "edit", self.tag]:
            path = args[args.index("--notes-file") + 1]
            with open(path, encoding="utf-8") as f:
                self.written.append(f.read())
            self.body = self.written[-1]
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


class TestEditReleaseKeepsCiShaMarker:
    """release edit re-syncs the notes without dropping the rlsbl-ci-sha marker.

    The publish check reads the marker to learn which commit CI verified; a
    notes re-sync that replaces the whole body with the changelog section
    erases it. The re-synced body must be the same document the release flow
    composes through rlsbl.release_publication.
    """

    def _run(self, tmp_path, forge, version="0.23.0"):
        (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        target = MagicMock()
        target.tag_format.side_effect = lambda v: f"v{v}"
        entry = MagicMock()
        entry.name = "npm"
        entry.path = str(tmp_path)
        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.edit_release.resolve_member_context",
                   return_value=MagicMock(targets=[entry])), \
             patch("rlsbl.commands.edit_release.TARGETS", {"npm": target}), \
             patch("rlsbl.commands.edit_release.run_gh", side_effect=forge):
            run_cmd([version], {}, project_root=str(tmp_path))

    def test_existing_marker_is_preserved(self, tmp_path, capsys):
        from rlsbl.release_publication import publication

        forge = _FakeForge("v0.23.0", f"- stale notes\n\n{_MARKER_LINE}\n")
        self._run(tmp_path, forge)

        assert len(forge.written) == 1
        expected = publication(
            tag="v0.23.0", version="0.23.0", candidate_sha=_MARKER_SHA,
            notes="- Added new feature X\n- Fixed bug Y",
        ).body
        assert forge.written[0] == expected
        assert _MARKER_LINE in forge.written[0]
        assert "stale notes" not in forge.written[0]

    def test_markerless_release_gains_no_marker(self, tmp_path, capsys):
        forge = _FakeForge("v0.23.0", "- stale notes\n")
        self._run(tmp_path, forge)

        assert len(forge.written) == 1
        assert "rlsbl-ci-sha" not in forge.written[0]
        assert forge.written[0].rstrip("\n") == "- Added new feature X\n- Fixed bug Y"

    def test_changelog_sync_path_preserves_marker(self, tmp_path, capsys):
        """changelog amend, edit and remove re-sync through `rlsbl release edit`.

        ``_sync_github_release`` shells out to ``rlsbl release edit <version>``;
        the subprocess is routed in-process here so the whole re-sync path runs
        against the fake forge.
        """
        from rlsbl.commands import changelog_cmd

        forge = _FakeForge("v0.23.0", f"- stale notes\n\n{_MARKER_LINE}\n")
        invoked = []

        def fake_run(argv, **kwargs):
            invoked.append(list(argv))
            assert list(argv[:3]) == ["rlsbl", "release", "edit"]
            self._run(tmp_path, forge, version=argv[3])
            return MagicMock(returncode=0, stderr="")

        with patch.object(changelog_cmd.effects, "run", side_effect=fake_run):
            changelog_cmd._sync_github_release("0.23.0")

        assert invoked == [["rlsbl", "release", "edit", "0.23.0"]]
        assert len(forge.written) == 1
        assert _MARKER_LINE in forge.written[0]
        assert "- Added new feature X" in forge.written[0]


if __name__ == "__main__":
    unittest.main()
