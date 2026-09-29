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
                ".rlsbl/releases/in-progress.json\n.rlsbl/lock\n", "ignore run state")
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


def test_a_release_that_stops_while_the_scrub_waits_for_the_lock_is_refused(
    safegit_bin, tmp_path, monkeypatch, capsys,
):
    """A release still running when the scrub starts holds the lock and has
    no state file yet. The scrub waits for the lock; when the release stops
    and lets go, its state file is there, and the scrub refuses before it
    removes the release checkout or rewrites anything."""
    import fcntl
    import threading

    from rlsbl.commands.release.release_state import get_state_path

    monkeypatch.setenv(
        "PATH", str(safegit_bin.parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    repo = build(tmp_path, files="0.29.4")
    commit_file(repo, "config.env", f"token={SECRET}\n", "add config")
    commit_file(repo, ".gitignore", ".rlsbl/releases/scrub-result.json\n"
                ".rlsbl/releases/in-progress.json\n.rlsbl/lock\n",
                "ignore run state")
    generate_changelog(str(repo))
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "generate changelog")
    head = git(repo, "rev-parse", "HEAD")
    checkout = release_checkout.prepare_checkout(str(repo), head)
    monkeypatch.chdir(repo)

    # The running release: holds the lock, then stops, leaving its state.
    lock_path = repo / ".rlsbl" / "lock"
    lock_path.parent.mkdir(exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    state = get_state_path(str(repo))

    def stop_release():
        import time
        time.sleep(1.0)
        os.makedirs(os.path.dirname(state), exist_ok=True)
        with open(state, "w") as f:
            json.dump({"new_version": "0.29.5", "completed_steps": []}, f)
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    releaser = threading.Thread(target=stop_release)
    releaser.start()
    try:
        with pytest.raises(SystemExit) as exc:
            _scrub(repo)
    finally:
        releaser.join()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "stopped mid-flight" in err and state in err
    assert os.path.isdir(checkout), "the release checkout was removed"
    assert git(repo, "rev-parse", "HEAD") == head


def _scrubbable(tmp_path, monkeypatch, safegit_bin, *, ignore_lock=True):
    monkeypatch.setenv(
        "PATH", str(safegit_bin.parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    repo = build(tmp_path, files="0.29.4")
    commit_file(repo, "config.env", f"token={SECRET}\n", "add config")
    commit_file(repo, ".gitignore", ".rlsbl/releases/scrub-result.json\n"
                ".rlsbl/releases/in-progress.json\n"
                + (".rlsbl/lock\n" if ignore_lock else ""), "ignore run state")
    generate_changelog(str(repo))
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "generate changelog")
    head = git(repo, "rev-parse", "HEAD")
    checkout = release_checkout.prepare_checkout(str(repo), head)
    monkeypatch.chdir(repo)
    return repo, checkout


def test_a_release_starting_after_the_checkout_is_removed_waits_for_the_rewrite(
    safegit_bin, tmp_path, monkeypatch,
):
    """The scrub removes the release checkout and then rewrites history. A
    release starting between the two would take the lock, re-create the
    checkout on the old history, and run while safegit rewrites it. The lock
    is held from the removal through the rewrite: a release that asks for it
    once the checkout is gone gets it only after the rewrite is done."""
    import fcntl
    import threading

    from rlsbl.utils import run as real_run

    repo, checkout = _scrubbable(tmp_path, monkeypatch, safegit_bin)
    lock_path = repo / ".rlsbl" / "lock"
    rewritten = threading.Event()
    seen = {}

    def release_starting():
        # A release taking the lock, the way rlsbl.lock does: a blocking flock.
        with open(lock_path, "a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            seen["rewritten_when_locked"] = rewritten.is_set()
            fcntl.flock(fd, fcntl.LOCK_UN)

    contender = threading.Thread(target=release_starting)

    def run(cmd, args=None, **kwargs):
        if cmd == "safegit" and args and "scrub" in args:
            assert not os.path.isdir(checkout), "safegit ran before the checkout was removed"
            contender.start()
            contender.join(timeout=1.0)  # returns early only if it got the lock
            out = real_run(cmd, args, **kwargs)
            rewritten.set()
            return out
        return real_run(cmd, args, **kwargs)

    with patch(f"{MOD}.run", side_effect=run):
        _scrub(repo)
    contender.join(timeout=30)
    assert seen == {"rewritten_when_locked": True}
    assert SECRET not in git(repo, "log", "--all", "-p")


def test_a_lock_file_git_does_not_ignore_is_refused_until_ignored(
    safegit_bin, tmp_path, monkeypatch, capsys,
):
    """The lock the scrub holds through the rewrite is a file in the working
    tree; git must ignore it, or safegit sees a dirty tree. Refused before
    anything is removed or rewritten, naming the .gitignore line."""
    repo, checkout = _scrubbable(tmp_path, monkeypatch, safegit_bin, ignore_lock=False)
    head = git(repo, "rev-parse", "HEAD")

    with pytest.raises(SystemExit) as exc:
        _scrub(repo)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Add `.rlsbl/lock` to .gitignore" in err
    assert os.path.isdir(checkout)
    assert git(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".rlsbl" / "lock").exists()

    # The named fix: ignore the lock file, commit, re-run.
    with open(repo / ".gitignore", "a") as f:
        f.write(".rlsbl/lock\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore the rlsbl lock")
    _scrub(repo)
    assert SECRET not in git(repo, "log", "--all", "-p")
    assert not os.path.isdir(checkout)
