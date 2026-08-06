"""Tests for the pre-push empty-candidate-window guard.

The generated monorepo CI router gates each project's job on a
dorny/paths-filter computed against the PUSH's own before-SHA. A push whose
diff matches none of a project's patterns leaves that project's job `skipped`,
and the publish gate refuses a skipped check -- correctly, since a skipped job
proves nothing about the commit. The release then deadlocks on a tag that can
never publish.

Reaching that verdict through the CI gate costs a full CI cycle. The window is
computable from the diff before the push, so it is refused there instead.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.release.execute import (
    _router_pattern_matches,
    _release_router_patterns,
)
from rlsbl.commands.release.release_state import (
    get_state_path,
    load_release_state,
    save_release_state,
)
from rlsbl.workspace import get_releasable_dir

from test_representative_write_elimination import (  # noqa: E402
    _git,
    _git_head,
    _release_patches,
    _rc,
    _setup_releasable_workspace,
)


class TestRouterPatternMatching:
    """The two shapes the router emits, matched the way picomatch does."""

    @pytest.mark.parametrize("path,expected", [
        ("packages/core/package.json", True),
        ("packages/core/src/deep/file.ts", True),
        ("packages/core", True),
        ("packages/coreutils/x.ts", False),
        ("packages/other/package.json", False),
    ])
    def test_directory_globstar(self, path, expected):
        assert _router_pattern_matches(path, "packages/core/**") is expected

    @pytest.mark.parametrize("path,expected", [
        (".rlsbl-monorepo/releasables/alpha/CHANGELOG.md", True),
        (".rlsbl-monorepo/releasables/beta/CHANGELOG.md", False),
    ])
    def test_exact_artifact_path(self, path, expected):
        pattern = ".rlsbl-monorepo/releasables/alpha/CHANGELOG.md"
        assert _router_pattern_matches(path, pattern) is expected

    def test_watch_glob(self):
        assert _router_pattern_matches("shared/proto/a.proto", "shared/**/*.proto")
        assert not _router_pattern_matches("shared/proto/a.txt", "shared/**/*.proto")


class TestReleaseRouterPatterns:
    """The guard asks for the same project set the CI gate demands."""

    def test_releasable_members_and_finalize_artifact(self, tmp_project):
        _setup_releasable_workspace(tmp_project)
        patterns = _release_router_patterns(str(tmp_project), "core", "alpha")
        assert "packages/core/**" in patterns
        assert ".rlsbl-monorepo/releasables/alpha/CHANGELOG.md" in patterns


def _prepare_resumable_candidate(root, core, unrelated_path="docs/notes.md"):
    """Stage the resumed-sibling shape.

    The release's version-bump commit is ALREADY the remote head (a previous
    attempt pushed it as the candidate and CI came back red). The operator
    fixed forward -- but the fix touches somebody else's paths, so the new
    push window no longer contains anything of this project's.
    """
    pkg = json.loads((core / "package.json").read_text())
    pkg["version"] = "1.0.1"
    (core / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    _git(root, "add", "packages/core/package.json")
    _git(root, "commit", "-q", "-m", "alpha@v1.0.1")
    bump_sha = _git_head(root)

    unrelated = root / unrelated_path
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("unrelated\n")
    _git(root, "add", unrelated_path)
    _git(root, "commit", "-q", "-m", "docs: unrelated fix")

    rel_dir = get_releasable_dir(str(root), "alpha")
    state_path = get_state_path(str(root), releasable_dir=rel_dir)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    save_release_state(state_path, {
        "new_version": "1.0.1",
        "tag": "alpha@v1.0.1",
        "branch": "main",
        "registry": "npm",
        "monorepo_name": "core",
        "releasable_name": "alpha",
        "commit_msg": "alpha@v1.0.1",
        "description": "",
        "context": "",
        "include": ["npm"],
        "exclude": [],
        "preid": "",
        "blog": False,
        "completed_steps": [
            "VERSION_BUMPED", "COMMITTED", "SNAPSHOT_REGENERATED",
            "BRANCH_PUSHED",
        ],
        "release_commits": [bump_sha],
        "candidate_sha": bump_sha,
    })
    return state_path, bump_sha


def _run_resume(root, core, remote_head, extra_patches=()):
    """Resume with `git ls-remote` answering with *remote_head*."""
    from rlsbl.commands.release import resume_cmd
    from rlsbl.context import create_context
    from rlsbl.utils import run as real_run

    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "gh":
            return ""
        if cmd == "git" and args and args[0] == "push":
            return ""
        if cmd == "git" and args and args[0] == "fetch":
            return ""
        if cmd == "git" and args and args[0] == "ls-remote":
            return f"{remote_head}\trefs/heads/main\n"
        if (cmd == "git" and args and args[:2] == ["rev-list", "--count"]
                and any("origin/" in a for a in args)):
            return "0"
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

    rel_dir = get_releasable_dir(str(root), "alpha")
    state_path = get_state_path(str(root), releasable_dir=rel_dir)
    ctx = create_context(Path(str(core)), workspace_root=Path(str(root)))
    patches = [
        patch("rlsbl.commands.release.check_gh_installed", return_value=True),
        patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.run_gh", return_value=""),
        patch("rlsbl.commands.release.run", side_effect=fake_run),
        patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
        *extra_patches,
    ]
    for p in patches:
        p.start()
    try:
        resume_cmd(
            load_release_state(state_path),
            {"quiet": True, "skip-lock": True},
            ctx=ctx,
        )
    finally:
        for p in patches:
            p.stop()


class TestEmptyWindowRefusedBeforeTheCiWait:

    def test_resumed_sibling_window_is_refused(self, tmp_project, capsys):
        core = _setup_releasable_workspace(tmp_project)
        _state_path, bump_sha = _prepare_resumable_candidate(tmp_project, core)

        waits = []
        with patch(
            "rlsbl.commands.release.wait_for_ci_green",
            side_effect=lambda *a, **kw: waits.append(a),
        ):
            with pytest.raises(SystemExit) as exc:
                _run_resume(tmp_project, core, remote_head=bump_sha)

        assert exc.value.code == 1
        assert waits == [], (
            "the guard must refuse BEFORE burning a CI wait"
        )
        err = capsys.readouterr().err
        assert "packages/core/**" in err
        assert "docs/notes.md" in err
        assert "rlsbl release resume" in err

    def test_nothing_was_pushed(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        _state_path, bump_sha = _prepare_resumable_candidate(tmp_project, core)

        with patch("rlsbl.commands.release.push_if_needed") as pushed:
            with pytest.raises(SystemExit):
                _run_resume(tmp_project, core, remote_head=bump_sha,
                            extra_patches=(patch(
                                "rlsbl.commands.release.wait_for_ci_green",
                                side_effect=AssertionError("no CI wait"),
                            ),))
        pushed.assert_not_called()

    def test_no_tag_was_created(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        _state_path, bump_sha = _prepare_resumable_candidate(tmp_project, core)

        with pytest.raises(SystemExit):
            _run_resume(tmp_project, core, remote_head=bump_sha)

        tags = subprocess.run(
            ["git", "tag", "--list", "alpha@v1.0.1"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tags == "", "the version must not be burnt"


class TestWindowThatDoesTriggerCiIsAllowed:

    def test_a_window_containing_the_version_bump_passes(self, tmp_project):
        """The normal shape: the push window carries the bump commit.

        Same fixture as the refusal tests, with the remote head one commit
        further BACK -- so the window contains the version bump under
        ``packages/core/`` and the router would run the project's job.
        """
        core = _setup_releasable_workspace(tmp_project)
        base_sha = _git_head(tmp_project)
        state_path, _bump = _prepare_resumable_candidate(tmp_project, core)

        _run_resume(tmp_project, core, remote_head=base_sha)

        tags = subprocess.run(
            ["git", "tag", "--list", "alpha@v1.0.1"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tags == "alpha@v1.0.1", "the release must run to completion"
        assert not os.path.exists(state_path)

    def test_a_standalone_repo_is_not_guarded(self, tmp_project):
        """No router, no paths filter: every push runs the whole CI."""
        from rlsbl.commands.release.execute import _guard_empty_candidate_window

        # monorepo_root/monorepo_name absent -> inert, whatever the diff says.
        _guard_empty_candidate_window(
            candidate_sha="a" * 40, remote_head="b" * 40, needs_push=True,
            state_path="/nonexistent/in-progress.json",
            monorepo_root=None, monorepo_name=None, releasable_name=None,
            version="1.0.1", tag="v1.0.1", branch="main",
            cwd=str(tmp_project), log=lambda m: None,
        )

    def test_no_remote_head_is_not_guarded(self, tmp_project):
        """A branch with no remote head has no before-SHA to reason about."""
        from rlsbl.commands.release.execute import _guard_empty_candidate_window

        _setup_releasable_workspace(tmp_project)
        _guard_empty_candidate_window(
            candidate_sha="a" * 40, remote_head=None, needs_push=True,
            state_path="/nonexistent/in-progress.json",
            monorepo_root=str(tmp_project), monorepo_name="core",
            releasable_name="alpha",
            version="1.0.1", tag="alpha@v1.0.1", branch="main",
            cwd=str(tmp_project), log=lambda m: None,
        )
