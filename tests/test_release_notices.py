"""A deprecate or yank notice is recorded in the repository, and every Release
body is composed from it.

``rlsbl release deprecate`` and ``rlsbl release yank`` put a notice at the top
of a past version's GitHub Release. Before the notice was recorded anywhere but
the forge, the next re-sync (``rlsbl release edit``, which the changelog
commands call after changing a released version) rebuilt the body from the
changelog notes and the marker alone, and the notice silently disappeared. The
notice now lives in the version's release archive (``release_notices``), and
:mod:`rlsbl.release_publication` puts it back on top of every body it composes.
"""

import os
import stat
from unittest.mock import MagicMock, patch

import pytest
import tomlkit

from rlsbl.release_file import read_release_file

_SHA = "a" * 40
_TREE = "b" * 40
_MARKER_LINE = f"<!-- rlsbl-ci-sha: {_SHA} -->"
_NOTICE = (
    "> **Deprecated:** tagged but never published to any registry; the "
    "publish failed and was fixed forward. Use v0.111.0 instead."
)

CHANGELOG = """\
# Changelog

## 0.9.1

- Fixed bug Y

## 0.9.0

- Initial release
"""


def _archive(project, version="0.9.1", notices=None):
    releases = project / ".rlsbl" / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    lines = [
        "format_version = 1",
        'bump = "patch"',
        'description = "a release"',
        'include = ["npm"]',
        "exclude = []",
    ]
    if notices is not None:
        lines.append("release_notices = " + tomlkit.item(list(notices)).as_string())
    lines += [f'candidate_sha = "{_SHA}"', "", "[tree_hashes]", f'"." = "{_TREE}"', ""]
    path = releases / f"v{version}.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o444)
    return path


class _FakeForge:
    """A gh stand-in for one past Release, recording every body written."""

    def __init__(self, tag, body, latest="v0.9.2"):
        self.tag = tag
        self.body = body
        self.latest = latest
        self.written = []
        self.edits = []

    def __call__(self, args, **kwargs):
        args = list(args)
        if args[:2] == ["release", "list"]:
            return self.latest
        if args[:3] == ["release", "view", self.tag]:
            return self.body
        if args[:3] == ["release", "edit", self.tag]:
            self.edits.append(args)
            path = args[args.index("--notes-file") + 1]
            with open(path, encoding="utf-8") as f:
                self.written.append(f.read())
            self.body = self.written[-1]
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


def _notes_body(notes="- Fixed bug Y"):
    from rlsbl.release_publication import publication

    return publication(
        tag="v0.9.1", version="0.9.1", candidate_sha=_SHA, notes=notes,
        notices=(),
    ).body


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------


class TestComposition:
    def test_composed_body_equals_what_deprecate_put_on_the_forge(self):
        """Re-syncing an already-deprecated Release reproduces its top unchanged.

        Deprecate prepends ``notice + blank line`` to the body the forge
        held; the composition from the repository must be that same document.
        """
        from rlsbl.release_publication import publication

        before = _notes_body()
        deprecated_on_forge = _NOTICE + "\n\n" + before
        composed = publication(
            tag="v0.9.1", version="0.9.1", candidate_sha=_SHA,
            notes="- Fixed bug Y", notices=(_NOTICE,),
        ).body
        assert composed == deprecated_on_forge

    def test_two_notices_keep_their_forge_order(self):
        from rlsbl.release_publication import publication

        second = "> **Yanked:** broken."
        on_forge = second + "\n\n" + (_NOTICE + "\n\n" + _notes_body())
        composed = publication(
            tag="v0.9.1", version="0.9.1", candidate_sha=_SHA,
            notes="- Fixed bug Y", notices=(second, _NOTICE),
        ).body
        assert composed == on_forge

    def test_markerless_body_carries_the_notices_too(self):
        from rlsbl.release_publication import markerless_body

        plain = markerless_body("0.9.1", "- Fixed bug Y", notices=())
        assert markerless_body("0.9.1", "- Fixed bug Y", notices=(_NOTICE,)) == (
            _NOTICE + "\n\n" + plain
        )


# ---------------------------------------------------------------------------
# The archive field
# ---------------------------------------------------------------------------


class TestArchiveField:
    def test_archive_carrying_notices_reads(self, tmp_path):
        path = _archive(tmp_path, notices=[_NOTICE])
        assert read_release_file(str(path)).release_notices == [_NOTICE]

    def test_unreleased_file_carrying_notices_is_refused(self, tmp_path):
        from rlsbl.commands.release.validate import (
            ReleaseValidationError,
            validate_no_authored_release_commit,
        )

        releases = tmp_path / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        path = releases / "unreleased.toml"
        path.write_text(
            "format_version = 1\n"
            'bump = "patch"\n'
            'description = "next"\n'
            'include = ["npm"]\n'
            "exclude = []\n"
            f"release_notices = [{tomlkit.string(_NOTICE).as_string()}]\n",
            encoding="utf-8",
        )
        config = read_release_file(str(path))
        with pytest.raises(ReleaseValidationError) as exc:
            validate_no_authored_release_commit(config)
        assert "release_notices" in str(exc.value)


# ---------------------------------------------------------------------------
# release edit keeps a recorded notice
# ---------------------------------------------------------------------------


def _run_edit(project, forge, version="0.9.1"):
    from rlsbl.commands.edit_release import run_cmd

    (project / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    target = MagicMock()
    target.tag_format.side_effect = lambda v: f"v{v}"
    entry = MagicMock()
    entry.name = "npm"
    entry.path = str(project)
    with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
         patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
         patch("rlsbl.commands.edit_release.resolve_member_context",
               return_value=MagicMock(targets=[entry])), \
         patch("rlsbl.commands.edit_release.TARGETS", {"npm": target}), \
         patch("rlsbl.commands.edit_release.run_gh", side_effect=forge):
        run_cmd([version], {}, project_root=str(project))


class TestEditKeepsRecordedNotice:
    def test_resync_of_deprecated_release_keeps_its_notice(self, tmp_path):
        _archive(tmp_path, notices=[_NOTICE])
        forge = _FakeForge(
            "v0.9.1", _NOTICE + "\n\n" + _notes_body("- stale notes"),
        )
        _run_edit(tmp_path, forge)

        assert forge.written == [_NOTICE + "\n\n" + _notes_body()]

    def test_resync_without_recorded_notice_writes_none(self, tmp_path):
        _archive(tmp_path)
        forge = _FakeForge("v0.9.1", _notes_body("- stale notes"))
        _run_edit(tmp_path, forge)

        assert forge.written == [_notes_body()]


# ---------------------------------------------------------------------------
# deprecate and yank record their notice
# ---------------------------------------------------------------------------


def _run_deprecate(project, forge, flags):
    from rlsbl.commands.deprecate import run_cmd

    with patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True), \
         patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True), \
         patch("rlsbl.commands.deprecate.run_gh", side_effect=forge), \
         patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None), \
         patch("rlsbl.commands.deprecate.resolve_member_context",
               return_value=MagicMock(targets=[])), \
         patch("rlsbl.release_publication.commit_files") as commit:
        run_cmd(["0.9.1"], flags, project_root=str(project))
    return commit


def _run_yank(project, forge, flags):
    from rlsbl.commands.yank import run_cmd

    with patch("rlsbl.commands.yank.check_gh_auth", return_value=True), \
         patch("rlsbl.commands.yank.check_gh_installed", return_value=True), \
         patch("rlsbl.commands.yank.run_gh", side_effect=forge), \
         patch("rlsbl.commands.yank.find_workspace_root", return_value=None), \
         patch("rlsbl.commands.yank.resolve_member_context",
               return_value=MagicMock(targets=[])), \
         patch("rlsbl.release_publication.commit_files") as commit:
        run_cmd(["0.9.1"], flags, project_root=str(project))
    return commit


class TestDeprecateRecordsNotice:
    FLAGS = {
        "reason": "tagged but never published to any registry; the publish "
                  "failed and was fixed forward",
        "use": "0.111.0",
    }

    def test_notice_lands_in_archive_and_on_forge(self, tmp_path):
        path = _archive(tmp_path)
        forge = _FakeForge("v0.9.1", _notes_body())
        commit = _run_deprecate(tmp_path, forge, self.FLAGS)

        assert read_release_file(str(path)).release_notices == [_NOTICE]
        # The archive stays locked after the write.
        assert not os.stat(path).st_mode & stat.S_IWUSR
        # The forge got today's layout: the notice, a blank line, the body.
        assert forge.written == [_NOTICE + "\n\n" + _notes_body()]
        assert "--prerelease" in forge.edits[0]
        commit.assert_called_once()
        assert commit.call_args[0][1] == [str(path)]

    def test_second_notice_goes_on_top(self, tmp_path):
        path = _archive(tmp_path, notices=[_NOTICE])
        forge = _FakeForge("v0.9.1", _NOTICE + "\n\n" + _notes_body())
        _run_deprecate(tmp_path, forge, {"reason": "again"})

        second = "> **Deprecated:** again."
        assert read_release_file(str(path)).release_notices == [second, _NOTICE]
        assert forge.written == [second + "\n\n" + _NOTICE + "\n\n" + _notes_body()]

    def test_version_without_archive_is_refused_before_any_write(self, tmp_path, capsys):
        forge = _FakeForge("v0.9.1", _notes_body())
        with pytest.raises(SystemExit) as exc:
            _run_deprecate(tmp_path, forge, self.FLAGS)
        assert exc.value.code == 1
        assert forge.written == []
        assert "v0.9.1.toml" in capsys.readouterr().err


class TestYankRecordsNotice:
    def test_notice_lands_in_archive_and_on_forge(self, tmp_path):
        path = _archive(tmp_path)
        forge = _FakeForge("v0.9.1", _notes_body())
        commit = _run_yank(tmp_path, forge, {"reason": "broken", "use": "0.9.2"})

        notice = "> **Yanked:** broken. Use v0.9.2 instead."
        assert read_release_file(str(path)).release_notices == [notice]
        assert forge.written == [notice + "\n\n" + _notes_body()]
        commit.assert_called_once()
