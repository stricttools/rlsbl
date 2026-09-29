"""`rlsbl release scrub` beside the persistent release checkout, against the REAL safegit.

The release checkout (``<git common dir>/rlsbl/release-checkout``) is a git
worktree detached at the commit the last release started from. Its HEAD is a
ref git counts as reachable, so it pins the pre-rewrite history, and safegit's
post-rewrite verification finds the scrubbed content still there. The scrub
removes the checkout before the rewrite; the next release creates it afresh.

The second test covers a failure inside safegit after it has rewritten the
local history: the rewrite is saved, nothing is pushed, and re-running the same
command after the reported fix finishes the scrub.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl import release_checkout
from rlsbl.changelog.generate import generate_changelog
from rlsbl.commands.release_scrub import run_cmd
from rlsbl.context import ProjectContext

from githarness import (
    add_remote,
    commit_file,
    git,
    init_repo,
    remote_ref,
)

MOD = "rlsbl.commands.release_scrub"
SECRET = "SECRETTOKEN123"


@pytest.fixture
def repo(safegit_bin, monkeypatch, tmp_path):
    """A released-shape repo: secret in history, changelog, a bare origin."""
    monkeypatch.setenv(
        "PATH", str(safegit_bin.parent) + os.pathsep + os.environ.get("PATH", "")
    )
    repo = tmp_path / "repo"
    init_repo(repo, email="e2e@test.local", name="E2E")
    # What scaffold's .gitignore holds for the scrub's resume state.
    commit_file(repo, ".gitignore", ".rlsbl/releases/scrub-result.json\n",
                "ignore run state")
    c1 = commit_file(repo, "config.env", f"token={SECRET}\n", "add config")
    commit_file(repo, "app.txt", "app\n", "add app")
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text(
        json.dumps({"commits": [c1], "user_facing": False}) + "\n"
    )
    git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    git(repo, "commit", "-q", "-m", "changelog")
    generate_changelog(str(repo))
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "generate changelog")
    add_remote(repo, tmp_path / "remote")
    monkeypatch.chdir(repo)
    return repo


def _flags():
    return {
        "pattern": SECRET,
        "replace": "REDACTED",
        "entire-history": True,
        "reason": "leaked token",
    }


def _scrub(repo):
    ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
    with patch(f"{MOD}.check_gh_installed", return_value=False):
        run_cmd(_flags(), ctx=ctx)


def _secret_reachable(repo):
    """Does any object reachable from any ref or worktree HEAD hold SECRET?"""
    grep = subprocess.run(
        ["git", "grep", "-l", SECRET]
        + git(repo, "rev-list", "--all").splitlines(),
        cwd=repo, capture_output=True, text=True,
    )
    return bool(grep.stdout.strip())


def test_scrub_completes_with_a_release_checkout_pinning_old_history(repo):
    """The checkout left by an earlier release does not fail the scrub."""
    checkout = release_checkout.prepare_checkout(
        str(repo), git(repo, "rev-parse", "HEAD"),
    )
    assert os.path.isdir(checkout)

    _scrub(repo)

    assert not _secret_reachable(repo)
    assert remote_ref(repo, "refs/heads/main") == git(repo, "rev-parse", "HEAD")
    assert not os.path.exists(checkout)
    assert str(checkout) not in git(repo, "worktree", "list")
    assert not (repo / ".rlsbl" / "releases" / "scrub-result.json").exists()


def test_scrub_failing_inside_safegit_after_the_rewrite_is_finished_by_a_rerun(
    repo, tmp_path, capsys,
):
    """A worktree rlsbl does not own pins the old history: safegit rewrites
    main, then its verification fails. Nothing is pushed, the rewrite is
    saved, and the fix the error names -- remove the holder, prune, re-run --
    finishes the scrub."""
    pre_head = git(repo, "rev-parse", "HEAD")
    holder = tmp_path / "holder"
    git(repo, "worktree", "add", "--detach", "-q", str(holder), pre_head)

    with pytest.raises(SystemExit) as exc:
        _scrub(repo)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nothing was pushed" in err
    assert "git reflog expire --expire=now --all && git gc --prune=now" in err
    # The local history is rewritten, origin still holds the old one.
    assert git(repo, "rev-parse", "HEAD") != pre_head
    assert remote_ref(repo, "refs/heads/main") == pre_head
    assert (repo / ".rlsbl" / "releases" / "scrub-result.json").exists()

    # The fix the error names.
    git(repo, "worktree", "remove", "--force", str(holder))
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")

    _scrub(repo)

    assert not _secret_reachable(repo)
    assert remote_ref(repo, "refs/heads/main") == git(repo, "rev-parse", "HEAD")
    assert not (repo / ".rlsbl" / "releases" / "scrub-result.json").exists()
    line = (repo / ".rlsbl" / "changes" / "unreleased.jsonl").read_text()
    git(repo, "rev-parse", "--verify",
        json.loads(line)["commits"][0] + "^{commit}")


def test_scrub_rerun_with_different_arguments_is_refused(repo, tmp_path, capsys):
    """A re-run finishing a saved rewrite must be the same scrub."""
    holder = tmp_path / "holder"
    git(repo, "worktree", "add", "--detach", "-q", str(holder),
        git(repo, "rev-parse", "HEAD"))
    with pytest.raises(SystemExit):
        _scrub(repo)
    capsys.readouterr()

    ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
    other = dict(_flags(), pattern="app")
    with pytest.raises(SystemExit) as exc:
        with patch(f"{MOD}.check_gh_installed", return_value=False):
            run_cmd(other, ctx=ctx)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--pattern SECRETTOKEN123" in err


def test_compose_rewrites_maps_each_original_commit_to_its_final_one():
    from rlsbl.commands.release_scrub import compose_rewrites

    saved = {
        "rewrites": {"a": "a1", "b": "b1"},
        "tags": [{"refname": "refs/tags/v1", "old_sha": "a", "new_sha": "a1"}],
        "new_head": "b1",
    }
    again = {
        "rewrites": {"b1": "b2", "c": "c2"},
        "tags": [
            {"refname": "refs/tags/v1", "old_sha": "a1", "new_sha": "a1"},
            {"refname": "refs/tags/v2", "old_sha": "c", "new_sha": "c2"},
        ],
        "new_head": "b2",
    }
    compose_rewrites(saved, again)
    assert saved["rewrites"] == {"a": "a1", "b": "b2", "c": "c2"}
    assert saved["tags"] == [
        {"refname": "refs/tags/v1", "old_sha": "a", "new_sha": "a1"},
        {"refname": "refs/tags/v2", "old_sha": "c", "new_sha": "c2"},
    ]
    assert saved["new_head"] == "b2"
