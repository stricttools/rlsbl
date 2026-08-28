"""What ``resolve_release_scope`` does when the workspace cannot be read.

The per-package fallback (no workspace project, no tag glob, the package's own
``.rlsbl/changes/``) is the right answer for exactly one situation: this
checkout is not inside a monorepo workspace at all. It is the WRONG answer for a
workspace that exists and does not load -- there the caller would silently
measure the unreleased range against a directory that holds none of the
releasable's entries, and report a coverage figure for a project it failed to
identify.
"""

import pytest

from rlsbl.context import resolve_release_scope
from rlsbl.errors import WorkspaceError


class TestOutsideAWorkspace:
    """The one case the fallback is for."""

    def test_plain_project_gets_its_own_changes_dir(self, tmp_path):
        project = tmp_path / "proj"
        (project / ".rlsbl" / "changes").mkdir(parents=True)

        wsproject, tag_glob, changes_dir, scope = resolve_release_scope(project)

        assert wsproject is None
        assert tag_glob is None
        assert scope is None
        assert changes_dir == str(project / ".rlsbl" / "changes")


class TestUnreadableWorkspace:
    """A workspace that exists but does not load is a hard error."""

    def test_malformed_workspace_toml_propagates(self, tmp_path):
        project = tmp_path / "pkg"
        (project / ".rlsbl" / "changes").mkdir(parents=True)
        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text('[[projects]\npath = "pkg"\n')

        with pytest.raises(Exception) as exc:
            resolve_release_scope(project)

        # Whatever the loader raises, it must not be swallowed into the
        # per-package fallback.
        assert not isinstance(exc.value, SystemExit)

    def test_workspace_rejected_by_validation_propagates(self, tmp_path):
        project = tmp_path / "pkg"
        (project / ".rlsbl" / "changes").mkdir(parents=True)
        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        # Structurally valid TOML the workspace loader refuses: a project
        # entry with no path.
        (ws_dir / "workspace.toml").write_text('[[projects]]\nname = "pkg"\n')

        with pytest.raises(WorkspaceError):
            resolve_release_scope(project)
