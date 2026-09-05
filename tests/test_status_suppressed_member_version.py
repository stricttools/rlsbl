"""`rlsbl monorepo status`: a publish-suppressed member reports the releasable version.

A member whose effective ``publish_mode`` is ``"none"`` publishes nothing, so
nothing bumps its manifest and nothing ever will -- the ``version-consistency``
check says so in as many words, passing such a member on the releasable's
version file alone and never looking at its manifest.

The status table was reading that dead manifest anyway, so a suppressed member
sat in the table at whatever version it was created with while its releasable
shipped release after release. The row now reports the same authority the check
uses, annotated so a reader knows which file answered.
"""

import json
import os
import subprocess

import pytest

from conftest import with_root_member

from rlsbl.commands.monorepo import _cmd_status
from rlsbl.workspace import (
    Releasable,
    save_workspace,
    write_releasable_version,
)


def _npm_member(root, subdir, version, *, publish_mode=None):
    path = os.path.join(str(root), subdir)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "package.json"), "w") as f:
        json.dump({"name": subdir, "version": version}, f)
    if publish_mode is not None:
        os.makedirs(os.path.join(path, ".rlsbl"), exist_ok=True)
        with open(os.path.join(path, ".rlsbl", "config.json"), "w") as f:
            json.dump({"publish_mode": publish_mode, "targets": ["npm"]}, f)
    return subdir


def _row(out, name):
    for line in out.splitlines():
        if line.startswith(name + " "):
            return line
    raise AssertionError(f"no row for {name!r} in:\n{out}")


@pytest.fixture
def workspace(mock_git_repo):
    """One releasable at 2.0.0: a published member, and a suppressed one at 0.1.0."""
    _npm_member(mock_git_repo, "published", "2.0.0")
    _npm_member(mock_git_repo, "suppressed", "0.1.0", publish_mode="none")
    save_workspace(
        str(mock_git_repo),
        with_root_member([
            {"path": "published", "name": "published", "releasable": "alpha"},
            {"path": "suppressed", "name": "suppressed", "releasable": "alpha"},
        ]),
        releasables=[Releasable(name="alpha")],
    )
    write_releasable_version(str(mock_git_repo), "alpha", "2.0.0")
    subprocess.run(["git", "add", "-A"], cwd=str(mock_git_repo), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "workspace"],
                   cwd=str(mock_git_repo), check=True, capture_output=True)
    return mock_git_repo


class TestPublishSuppressedMember:
    def test_the_row_reports_the_releasable_version(self, workspace, capsys):
        capsys.readouterr()
        _cmd_status({}, project_root=str(workspace))
        out = capsys.readouterr().out

        row = _row(out, "suppressed")
        assert "2.0.0" in row, (
            "the suppressed member's stale manifest was reported as its "
            "version; row was:\n" + row
        )
        assert "0.1.0" not in row, row

    def test_the_row_says_which_file_answered(self, workspace, capsys):
        capsys.readouterr()
        _cmd_status({}, project_root=str(workspace))
        out = capsys.readouterr().out

        assert "(version file)" in _row(out, "suppressed")

    def test_a_published_member_is_unchanged(self, workspace, capsys):
        capsys.readouterr()
        _cmd_status({}, project_root=str(workspace))
        out = capsys.readouterr().out

        row = _row(out, "published")
        assert "2.0.0" in row
        assert "(version file)" not in row, (
            "a published member's manifest IS its version; annotating it would "
            "say the opposite"
        )
