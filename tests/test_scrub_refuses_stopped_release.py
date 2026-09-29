"""`rlsbl release scrub` refuses while a release is stopped mid-flight.

The scrub removes the release checkout (it pins the pre-rewrite history) and
rewrites the history a stopped release's state records -- the commit it
started from, its candidate -- so running it beside a stopped release would
strand that release. It refuses before anything is removed or rewritten,
naming ``rlsbl release resume`` and ``rlsbl release abandon``; the test gives
the release up with the abandon it names, and the scrub then runs.

Real git and the real safegit; gh is faked as absent.
"""

import json
import os
from unittest.mock import patch

import pytest

from githarness import commit_file, git

from rlsbl import release_checkout
from rlsbl.changelog.generate import generate_changelog
from rlsbl.commands.release_scrub import run_cmd
from rlsbl.context import ProjectContext

from test_release_abandon import _stall, abandon
from test_release_version_fate import build

MOD = "rlsbl.commands.release_scrub"
SECRET = "SECRETTOKEN123"


def _scrub(repo):
    ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
    with patch(f"{MOD}.check_gh_installed", return_value=False):
        run_cmd({
            "pattern": SECRET, "replace": "REDACTED", "entire-history": True,
            "reason": "leaked token",
        }, ctx=ctx)


def test_the_scrub_refuses_a_stopped_release_and_runs_once_it_is_abandoned(
    safegit_bin, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv(
        "PATH", str(safegit_bin.parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    repo = build(tmp_path, files="0.29.4")
    commit_file(repo, "config.env", f"token={SECRET}\n", "add config")
    commit_file(repo, ".gitignore", ".rlsbl/releases/scrub-result.json\n"
                ".rlsbl/releases/in-progress.json\n", "ignore run state")
    generate_changelog(str(repo))
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "generate changelog")
    head = git(repo, "rev-parse", "HEAD")
    checkout = release_checkout.prepare_checkout(str(repo), head)
    state = _stall(repo, "0.29.4")
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as exc:
        _scrub(repo)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "stopped mid-flight" in err and state in err
    assert "rlsbl release resume" in err and "rlsbl release abandon" in err
    assert os.path.isdir(checkout), "the release checkout was removed"
    assert git(repo, "rev-parse", "HEAD") == head

    # The named fix: give the stopped release up.
    result = abandon(repo, monkeypatch)
    assert result.exit_code == 0, result.stderr + result.stdout
    assert not os.path.exists(state)

    _scrub(repo)
    assert SECRET not in git(repo, "log", "--all", "-p")
    assert not os.path.isdir(checkout)


def test_a_workspace_names_the_member_to_run_the_fix_from(tmp_path):
    from conftest import make_workspace
    from rlsbl.commands.release_scrub import _in_flight_releases
    from rlsbl.workspace import Releasable, get_releasable_dir

    make_workspace(
        tmp_path,
        [{"path": "pkg", "name": "pkg", "releasable": "core"}],
        releasables=[Releasable(name="core", tag_format="{name}@v{version}")],
    )
    releases = os.path.join(get_releasable_dir(str(tmp_path), "core"), "releases")
    os.makedirs(releases)
    state = os.path.join(releases, "in-progress.json")
    with open(state, "w") as f:
        json.dump({"new_version": "0.2.0", "monorepo_name": "pkg"}, f)

    ctx = ProjectContext(project_root=tmp_path / "pkg", workspace_root=tmp_path, config={})
    assert _in_flight_releases(ctx) == [(str(tmp_path / "pkg"), state)]
