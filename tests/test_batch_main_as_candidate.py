"""One CI gate for a whole batch release (C4, batch path).

`rlsbl monorepo release run` releases each member in two passes: every
member's version-bump commit is first pushed to the release branch untagged
(the per-member CI gate is deferred), then ONE gate waits for CI on the
resulting branch tip, and only then is each member finalized, tagged on that
verified commit, and released.

The properties under test are the batch mirror of the standalone ones:

- exactly one CI wait, whatever the batch size;
- every member's tag points at the single CI-verified commit;
- a red batch leaves no tag, no GitHub Release and no finalized changelog for
  any member, so no version number is burnt.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from githarness import git

from rlsbl.commands.monorepo.batch_release import _cmd_batch_release
from rlsbl.commands.release.release_state import get_state_path, load_release_state
from rlsbl.release_file import get_batch_release_file_path
from rlsbl.utils import run as real_run
from rlsbl.workspace import WORKSPACE_DIR, save_workspace


BATCH_TOML = (
    '[packages.alpha]\n'
    'bump = "patch"\ndescription = "Alpha patch"\n'
    'include = ["npm"]\nexclude = []\n'
    '\n'
    '[packages.beta]\n'
    'bump = "patch"\ndescription = "Beta patch"\n'
    'include = ["npm"]\nexclude = []\n'
)


def _make_pkg(root, name):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}, indent=2) + "\n"
    )
    (d / ".rlsbl").mkdir(exist_ok=True)
    (d / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"], "pipelines": {}}) + "\n"
    )
    changes = d / ".rlsbl" / "changes"
    changes.mkdir(exist_ok=True)
    (changes / "unreleased.jsonl").write_text("")
    return d


def _setup_batch_workspace(root):
    """Two independently-versioned npm packages in an implicit-mode workspace."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@test.local")
    git(root, "config", "user.name", "Test")

    for name in ("alpha", "beta"):
        _make_pkg(root, name)
    save_workspace(str(root), [
        {"path": "alpha", "name": "alpha"},
        {"path": "beta", "name": "beta"},
    ])
    _write_batch_file(root)

    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    git(root, "tag", "alpha@v1.0.0")
    git(root, "tag", "beta@v1.0.0")

    # One covered feature commit per package.
    for name in ("alpha", "beta"):
        (root / name / "feature.txt").write_text("feature\n")
        git(root, "add", f"{name}/feature.txt")
        git(root, "commit", "-q", "-m", f"{name}: add feature")
        sha = git(root, "rev-parse", "HEAD")
        jsonl = root / name / ".rlsbl" / "changes" / "unreleased.jsonl"
        jsonl.write_text(json.dumps({
            "commits": [sha],
            "user_facing": True,
            "description": f"**{name} feature.** It works.",
            "type": "feature",
        }) + "\n")
        git(root, "add", f"{name}/.rlsbl/changes/unreleased.jsonl")
        git(root, "commit", "-q", "-m", f"changelog: {name} feature")


def _write_batch_file(root):
    path = get_batch_release_file_path(str(root))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(BATCH_TOML)


def _fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
    if cmd == "gh":
        return ""
    if cmd == "git" and args and args[0] in ("push", "fetch"):
        return ""
    if (cmd == "git" and args and args[:2] == ["rev-list", "--count"]
            and any("origin/" in a for a in args)):
        return "0"
    return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)


def _batch_patches(ci_return=None, ci_side_effect=None):
    ci_kwargs = (
        {"side_effect": ci_side_effect} if ci_side_effect is not None
        else {"return_value": ci_return}
    )
    return [
        patch("rlsbl.commands.monorepo.batch_release.validate_gh_cli"),
        patch("rlsbl.commands.monorepo.batch_release.validate_gh_push_access"),
        patch("rlsbl.commands.monorepo.batch_release.validate_clean_tree",
              return_value=set()),
        patch("rlsbl.commands.monorepo.batch_release.validate_branch_and_remote",
              return_value="main"),
        patch("rlsbl.commands.release.check_gh_installed", return_value=True),
        patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.run_gh", return_value=""),
        patch("rlsbl.commands.release.run", side_effect=_fake_run),
        patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
        patch("rlsbl.commands.monorepo.batch_release.wait_for_ci_green", **ci_kwargs),
    ]


def _run_batch(root, *, ci_return=None, ci_side_effect=None):
    patches = _batch_patches(ci_return=ci_return, ci_side_effect=ci_side_effect)
    for p in patches:
        p.start()
    try:
        _cmd_batch_release({"yes": True, "quiet": True}, project_root=str(root))
    finally:
        for p in patches:
            p.stop()


class TestBatchCandidateGate:

    def test_one_gate_for_the_batch_and_every_tag_on_the_verified_commit(
        self, tmp_project,
    ):
        _setup_batch_workspace(tmp_project)
        calls = []

        def fake_wait(sha, **kwargs):
            calls.append(sha)
            return "green", []

        _run_batch(tmp_project, ci_side_effect=fake_wait)

        assert len(calls) == 1, (
            f"a batch must run exactly ONE CI gate, got {len(calls)}"
        )
        verified = calls[0]

        tags = git(tmp_project, "tag", "-l").split()
        assert "alpha@v1.0.1" in tags and "beta@v1.0.1" in tags

        for tag in ("alpha@v1.0.1", "beta@v1.0.1"):
            assert git(tmp_project, "rev-list", "-n", "1", tag) == verified, (
                f"{tag} must point at the single CI-verified batch candidate"
            )

        # The gate saw a commit that already carried BOTH version bumps.
        for name in ("alpha", "beta"):
            blob = git(tmp_project, "show", f"{verified}:{name}/package.json")
            assert json.loads(blob)["version"] == "1.0.1", (
                f"the verified candidate must contain {name}'s version bump"
            )

        # Both changelogs finalized, both state files cleared.
        for name in ("alpha", "beta"):
            changes = tmp_project / name / ".rlsbl" / "changes"
            assert (changes / "1.0.1.jsonl").exists()
            assert load_release_state(
                get_state_path(str(tmp_project / name))
            ) is None

    def test_a_red_batch_gate_leaves_no_tag_and_no_finalization(self, tmp_project):
        _setup_batch_workspace(tmp_project)

        with pytest.raises(SystemExit) as exc:
            _run_batch(
                tmp_project,
                ci_return=("red", [{"name": "test", "passed": False}]),
            )
        assert exc.value.code == 1

        tags = git(tmp_project, "tag", "-l").split()
        assert "alpha@v1.0.1" not in tags and "beta@v1.0.1" not in tags, (
            "a red batch gate must leave every member untagged"
        )

        for name in ("alpha", "beta"):
            changes = tmp_project / name / ".rlsbl" / "changes"
            assert not (changes / "1.0.1.jsonl").exists(), (
                f"{name}'s changelog must not be finalized while CI is red"
            )
            assert os.path.getsize(changes / "unreleased.jsonl") > 0
            # Resumable at the same version -- no number was burnt.
            state = load_release_state(get_state_path(str(tmp_project / name)))
            assert state is not None and state["new_version"] == "1.0.1"

        # The batch file is not archived while the batch is incomplete.
        assert os.path.exists(get_batch_release_file_path(str(tmp_project)))

    def test_the_candidates_are_pushed_before_the_gate(self, tmp_project):
        """Both members' candidates must be on the branch when CI is asked."""
        _setup_batch_workspace(tmp_project)
        observed = {}

        def fake_wait(sha, **kwargs):
            observed["tags_at_gate"] = git(tmp_project, "tag", "-l").split()
            observed["sha"] = sha
            return "green", []

        _run_batch(tmp_project, ci_side_effect=fake_wait)

        assert "alpha@v1.0.1" not in observed["tags_at_gate"], (
            "nothing may be tagged before the gate reports"
        )
        assert "beta@v1.0.1" not in observed["tags_at_gate"]
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", observed["sha"], "HEAD"],
            cwd=str(tmp_project),
        ).returncode == 0
