"""Release branch guard: dev-branch releases and `rlsbl push` are gone.

Replaces the old dev-branch release workflow. The rules now are:

- ``validate_branch_and_remote`` hard-errors on any branch that is not a
  release branch. Both release paths (single-package and monorepo batch)
  go through it, so neither can be started from a dev branch.
- The ``rlsbl push`` command no longer exists; ``pre-push-check`` (the hook
  helper) stays.
- ``prepush-manual-warning`` is an ERROR check, not a warning.
- ``RLSBL_RELEASE_PUSH`` is gone entirely; release-internal pushes bypass
  the hook with ``git push --no-verify`` instead.
- The generated pre-push hook is namespace-aware: ``refs/heads/*`` enforce,
  ``refs/tags/*`` and ``refs/backups/*`` exit 0.

The two coverage tests at the bottom are carried over unchanged from the
deleted dev-branch suite: changelog coverage stays branch-agnostic, which
is still true and still worth pinning.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import git_head, make_ctx, run_git
from rlsbl import app
from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    validate_branch_and_remote,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "rlsbl"


def _make_push_stdin(local_sha, remote_sha, *, branch="main"):
    """Build a push stdin string for a given branch."""
    return (
        f"refs/heads/{branch} {local_sha} "
        f"refs/heads/{branch} {remote_sha}"
    )


@pytest.fixture
def repo_with_origin(tmp_path, monkeypatch):
    """A repo on branch ``dev`` with main + a bare origin."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")
    run_git(repo, "tag", "v0.0.0")

    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        check=True, capture_output=True,
    )
    run_git(repo, "remote", "add", "origin", str(bare))
    run_git(repo, "fetch", "origin")
    run_git(repo, "branch", "--set-upstream-to=origin/main", "main")

    run_git(repo, "checkout", "-q", "-b", "dev")
    (repo / "src.py").write_text("x = 1\n")
    run_git(repo, "add", "src.py")
    run_git(repo, "commit", "-q", "-m", "feat: new feature")
    return repo


# ---------------------------------------------------------------------------
# Hard error off a release branch (the chokepoint both release paths call)
# ---------------------------------------------------------------------------


class TestOffReleaseBranchIsHardError:

    def test_default_release_branches(self, repo_with_origin):
        with pytest.raises(ReleaseValidationError) as exc:
            validate_branch_and_remote({}, cwd=str(repo_with_origin))
        msg = str(exc.value)
        assert "dev" in msg
        assert "release branch" in msg

    def test_configured_release_branches(self, repo_with_origin):
        """A dev branch is refused even when release_branches is customized."""
        with pytest.raises(ReleaseValidationError):
            validate_branch_and_remote(
                {}, config={"release_branches": ["main", "stable"]},
                cwd=str(repo_with_origin),
            )

    def test_release_branch_still_allowed(self, repo_with_origin):
        run_git(repo_with_origin, "checkout", "-q", "main")
        assert validate_branch_and_remote({}, cwd=str(repo_with_origin)) == "main"

    def test_custom_release_branch_allowed(self, repo_with_origin):
        run_git(repo_with_origin, "checkout", "-q", "-b", "stable")
        result = validate_branch_and_remote(
            {}, config={"release_branches": ["stable"]},
            cwd=str(repo_with_origin),
        )
        assert result == "stable"


class TestBothReleasePathsGuarded:
    """Single-package and batch paths both route through the validator."""

    def test_single_package_path_calls_validator(self):
        src = (PKG_ROOT / "commands" / "release" / "__init__.py").read_text()
        assert "validate_branch_and_remote(flags, config=config" in src

    def test_batch_path_calls_validator(self):
        src = (PKG_ROOT / "commands" / "monorepo" / "batch_release.py").read_text()
        assert "validate_branch_and_remote(flags, cwd=workspace_root)" in src

    def test_no_ff_merge_machinery_remains(self):
        offenders = []
        pattern = re.compile(r"needs_ff_merge|dev_branch|BranchValidation|ff-forward")
        for path in sorted(PKG_ROOT.rglob("*.py")):
            for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, "dev-branch release machinery remains:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# `rlsbl push` is gone; pre-push-check stays
# ---------------------------------------------------------------------------


class TestPushCommandRemoved:

    def test_push_command_not_registered(self):
        result = app.test(["push"])
        assert result.exit_code != 0
        assert "push" in result.stderr

    def test_push_cmd_module_deleted(self):
        assert not (PKG_ROOT / "commands" / "push_cmd.py").exists()
        with pytest.raises(ModuleNotFoundError):
            __import__("rlsbl.commands.push_cmd")

    def test_pre_push_check_survives(self):
        result = app.test(["pre-push-check", "--help"])
        assert result.exit_code == 0, result.stderr


# ---------------------------------------------------------------------------
# Manual-push guard is an error, and the env bypass is gone
# ---------------------------------------------------------------------------


class TestManualPushGuard:

    def test_manual_warning_is_an_error_check(self):
        assert app._check_defs["prepush-manual-warning"].severity == "error"

    def test_no_release_push_env_var_in_production_code(self):
        offenders = []
        for path in sorted(PKG_ROOT.rglob("*")):
            if not path.is_file() or path.suffix in {".pyc", ".json"}:
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if "RLSBL_RELEASE_PUSH" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        assert not offenders, "RLSBL_RELEASE_PUSH remains:\n" + "\n".join(offenders)

    def test_detect_manual_push_branches_has_no_env_bypass(self, monkeypatch):
        from rlsbl.git_util import detect_manual_push_branches
        import inspect

        params = inspect.signature(detect_manual_push_branches).parameters
        assert "release_in_progress" not in params

        monkeypatch.setenv("RLSBL_RELEASE_PUSH", "1")
        lines = ["refs/heads/main aaa refs/heads/main bbb"]
        assert detect_manual_push_branches(lines, ["main"]) == ["main"]


# ---------------------------------------------------------------------------
# Release-internal pushes bypass the hook with --no-verify
# ---------------------------------------------------------------------------


class TestInternalPushesUseNoVerify:

    @pytest.mark.parametrize("relpath", [
        "utils.py",
        "commands/release/execute.py",
        "commands/undo.py",
        "commands/release_scrub.py",
        "commands/monorepo/releasable_rename.py",
    ])
    def test_every_push_invocation_is_no_verify(self, relpath):
        text = (PKG_ROOT / relpath).read_text()
        offenders = []
        for lineno, line in enumerate(text.splitlines(), 1):
            if not re.search(r'\["push"[,\]]', line):
                continue
            if "--no-verify" not in line:
                offenders.append(f"{relpath}:{lineno}: {line.strip()}")
        assert not offenders, (
            "release-internal git push without --no-verify:\n" + "\n".join(offenders)
        )


# ---------------------------------------------------------------------------
# Namespace-aware generated pre-push hook
# ---------------------------------------------------------------------------


class TestPrePushHookNamespaces:
    """The shipped hook exempts tag and backup pushes, enforces on branches."""

    def _run_hook(self, tmp_path, stdin_text):
        from rlsbl.hook_hashes import CURRENT_PRE_PUSH_HOOK

        hook = tmp_path / "pre-push"
        hook.write_text(CURRENT_PRE_PUSH_HOOK)
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

        bindir = tmp_path / "bin"
        bindir.mkdir()
        marker = tmp_path / "delegated"
        fake = bindir / "rlsbl"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$@" > "{marker}"\n'
            "exit 7\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        proc = subprocess.run(
            [str(hook), "origin", "git@example.com:o/r.git"],
            input=stdin_text, text=True, capture_output=True, env=env,
            cwd=str(tmp_path),
        )
        return proc, marker

    def test_branch_push_delegates_to_rlsbl(self, tmp_path):
        stdin = "refs/heads/main aaa refs/heads/main bbb\n"
        proc, marker = self._run_hook(tmp_path, stdin)
        assert proc.returncode == 7, proc.stderr
        assert marker.exists()
        assert "check" in marker.read_text()

    def test_tag_push_is_exempt(self, tmp_path):
        stdin = "refs/tags/v1.2.3 aaa refs/tags/v1.2.3 0000000\n"
        proc, marker = self._run_hook(tmp_path, stdin)
        assert proc.returncode == 0, proc.stderr
        assert not marker.exists()

    def test_backup_push_is_exempt(self, tmp_path):
        stdin = "HEAD aaa refs/backups/main 0000000\n"
        proc, marker = self._run_hook(tmp_path, stdin)
        assert proc.returncode == 0, proc.stderr
        assert not marker.exists()

    def test_mixed_push_with_a_branch_enforces(self, tmp_path):
        stdin = (
            "refs/tags/v1.2.3 aaa refs/tags/v1.2.3 0000000\n"
            "refs/heads/main ccc refs/heads/main ddd\n"
        )
        proc, marker = self._run_hook(tmp_path, stdin)
        assert proc.returncode == 7, proc.stderr
        assert marker.exists()

    def test_previous_hook_version_is_still_recognized(self):
        """The old two-line hook must upgrade in place, not look customized."""
        from rlsbl.hook_hashes import PRE_PUSH_HOOK_HASHES, compute_hook_hash

        old = (
            '#!/usr/bin/env bash\n'
            'export RLSBL_PUSH_STDIN="$(cat)"\n'
            'exec rlsbl check --tag prepush\n'
        )
        assert compute_hook_hash(old) in PRE_PUSH_HOOK_HASHES


# ---------------------------------------------------------------------------
# Carried over: changelog coverage is branch-agnostic
# ---------------------------------------------------------------------------


class TestCoverageEnforcedOnNonReleaseBranch:
    """prepush-changelog-coverage applies to all branches, not just
    release branches. There is no branch-based exemption."""

    def test_uncovered_commit_on_dev_branch_fails(self, tmp_path, monkeypatch):
        """A push to a non-release branch with an uncovered commit fails
        the changelog coverage check."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "tag", "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")

        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": []})
        )

        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

        base_sha = git_head(repo)

        # Switch to a dev branch and make an uncovered commit
        run_git(repo, "checkout", "-b", "dev")
        (repo / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "src.py")
        run_git(repo, "commit", "-q", "-m", "feat: new feature")
        head_sha = git_head(repo)

        # Simulate a push to the dev branch (not main)
        ctx = make_ctx(repo)
        ctx.push_stdin = _make_push_stdin(head_sha, base_sha, branch="dev")

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail", (
            "Coverage check must enforce on dev branches too -- "
            "no branch-based exemption should exist"
        )

    def test_covered_commit_on_dev_branch_passes(self, tmp_path, monkeypatch):
        """A push to a non-release branch with full coverage passes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "tag", "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")

        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": []})
        )

        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

        base_sha = git_head(repo)

        # Switch to dev and make a covered commit
        run_git(repo, "checkout", "-b", "feature-x")
        (repo / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "src.py")
        run_git(repo, "commit", "-q", "-m", "feat: new feature")
        head_sha = git_head(repo)

        # Cover the commit
        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")

        ctx = make_ctx(repo)
        ctx.push_stdin = _make_push_stdin(head_sha, base_sha, branch="feature-x")

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"
