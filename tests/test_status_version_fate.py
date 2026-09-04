"""``rlsbl status`` reports which fate the release record gives each version.

The human line names the latest RELEASE -- never a higher archive recorded
``never_released``, which is a version number no release ever used -- and says
what it passed over, so a display that reports 1.0.0 while 1.1.0 sits in the
archive directory does not read as stale.

The machine payload expresses all three fates: ``latest_release_state`` is the
fate of the archive ``latest_release`` names (``"recorded"`` or
``"unrecoverable"``, null when there is no release), and
``never_released_versions`` lists every archived version above it that was
never released.
"""

import json
import os
import subprocess

import pytest

from conftest import archive_release, make_ctx, release_record_dir


def _npm(repo, version="1.0.0"):
    with open(os.path.join(str(repo), "package.json"), "w") as f:
        json.dump({"name": "pkg-a", "version": version}, f)
    subprocess.run(["git", "add", "package.json"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(repo),
                   check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def phantom_topped(mock_git_repo):
    """A repo whose highest archive is a version that was never released."""
    head = _npm(mock_git_repo)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
    archive_release(release_record_dir(mock_git_repo), "1.0.0", head)
    archive_release(release_record_dir(mock_git_repo), "1.1.0", None,
                    never_released=True)
    return mock_git_repo


def _status(flags=None):
    from rlsbl.commands.status import run_cmd

    return run_cmd("npm", [], flags or {}, ctx=make_ctx("."))


class TestStatusPayloadFates:

    def test_the_payload_names_the_real_latest_and_the_phantom(self, phantom_topped):
        data = _status({"json": True})
        assert data["latest_release"] == "1.0.0"
        assert data["latest_release_state"] == "recorded"
        assert data["never_released_versions"] == ["1.1.0"]

    def test_an_unrecoverable_latest_is_reported_as_such(self, mock_git_repo):
        _npm(mock_git_repo)
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        archive_release(release_record_dir(mock_git_repo), "1.0.0", None,
                        unrecoverable=True)
        data = _status({"json": True})
        assert data["latest_release"] == "1.0.0"
        assert data["latest_release_state"] == "unrecoverable"
        assert data["never_released_versions"] == []

    def test_no_release_at_all_has_no_state(self, mock_git_repo):
        _npm(mock_git_repo)
        data = _status({"json": True})
        assert data["latest_release"] is None
        assert data["latest_release_state"] is None
        assert data["never_released_versions"] == []


class TestStatusHumanLine:

    def test_the_released_line_names_the_phantom_it_passed_over(
        self, phantom_topped, capsys,
    ):
        capsys.readouterr()
        _status()
        released = [
            line for line in capsys.readouterr().out.splitlines()
            if line.startswith("Released:")
        ]
        assert len(released) == 1, released
        assert "1.0.0" in released[0]
        assert "1.1.0" in released[0]
        assert "never released" in released[0]
