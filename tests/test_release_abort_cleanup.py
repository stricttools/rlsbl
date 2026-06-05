"""Regression tests: an aborted release must leave the working tree clean.

When `rlsbl release` aborts at a pre-mutation check (failing pre-checks hook,
failing built-in tests, failing selfdoc check, etc.), no files in the working
tree should have been modified. Historically, generate_changelog() wrote
CHANGELOG.md to disk before the pre-checks ran, leaving the user with a dirty
working tree to revert after every failed release attempt.

These tests exercise the real run_cmd code path (only mocking gh) and assert
that an aborted release leaves both `git status --porcelain` empty and the
on-disk CHANGELOG.md byte-identical to its pre-release contents.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from pathlib import Path
from rlsbl.context import ProjectContext

from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


def _git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(repo):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _porcelain(repo):
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _setup_releasable_npm_project(repo):
    """Create a git repo with a tagged v1.0.0 release and an unreleased commit
    covered by an unreleased.jsonl entry. Ready for `rlsbl release patch`.

    Pre-creates CHANGELOG.md with a marker that callers can use to detect
    overwrites.
    """
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    # Initial release: package.json @ 1.0.0 and a baseline CHANGELOG.md
    (repo / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
    )
    pre_release_changelog = (
        "# Changelog\n\n"
        "## 1.0.0\n\n"
        "- Initial release.\n"
        "<!-- PRE-RELEASE-MARKER -->\n"
    )
    (repo / "CHANGELOG.md").write_text(pre_release_changelog)
    changes_dir = repo / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    _git(repo, "add", "package.json", "CHANGELOG.md", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v1.0.0")

    # Make an unreleased commit and cover it with a JSONL entry
    (repo / "feature.txt").write_text("new feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(repo)

    # Cover the feature commit with an unreleased entry
    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**Add feature.** New feature is now available.",
        "type": "feature",
    }
    (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: add feature entry")

    return pre_release_changelog


def _install_failing_pre_checks_hook(repo):
    hooks_dir = repo / ".rlsbl" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-checks.sh"
    hook.write_text("#!/bin/bash\necho 'pre-checks intentionally failing' >&2\nexit 1\n")
    hook.chmod(0o755)
    # Commit the hook so the working tree stays clean before the release runs
    _git(repo, "add", ".rlsbl/hooks/pre-checks.sh")
    _git(repo, "commit", "-q", "-m", "add failing pre-checks hook")


class TestReleaseAbortCleanup:
    """Regression tests for the abort-cleanup invariant."""

    def test_failed_pre_checks_hook_leaves_tree_clean(self, tmp_project):
        """Pre-checks hook failure: working tree and CHANGELOG.md must be unchanged."""
        pre_release_changelog = _setup_releasable_npm_project(tmp_project)
        _install_failing_pre_checks_hook(tmp_project)

        # Sanity: tree is clean and CHANGELOG.md has the marker before we start
        assert _porcelain(tmp_project) == ""
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog
        head_before = _git_head(tmp_project)

        from rlsbl.commands.release import run_cmd

        # gh CLI checks are mocked so the test doesn't need real GitHub auth.
        # Everything else (changelog validation, generate_changelog, hooks)
        # runs for real.
        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            # Skip the remote-ahead check so the test doesn't need a remote
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(
                    _rc(),
                    {
                        "yes": True,
                        "quiet": True,
                    },
                
                    ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}),
)

        assert exc_info.value.code == 1, "release should exit 1 on hook failure"

        # The invariants this test was built to enforce
        assert _porcelain(tmp_project) == "", (
            "working tree must be unchanged after an aborted release; got:\n"
            + _porcelain(tmp_project)
        )
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog, (
            "CHANGELOG.md must not be regenerated when pre-checks fail"
        )
        # Per-version .md files must not have been written either
        for entry in (tmp_project / ".rlsbl" / "changes").iterdir():
            assert entry.suffix != ".md", (
                f"per-version .md file leaked into working tree: {entry.name}"
            )
        assert _git_head(tmp_project) == head_before, "HEAD must not move"

    def test_failed_builtin_test_leaves_tree_clean(self, tmp_project):
        """Built-in test failure: working tree and CHANGELOG.md must be unchanged."""
        pre_release_changelog = _setup_releasable_npm_project(tmp_project)

        # Add a failing test script to package.json so npm test (which the
        # built-in runner invokes for npm) returns non-zero.
        pkg_path = tmp_project / "package.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["scripts"] = {"test": "exit 1"}
        pkg_path.write_text(json.dumps(pkg, indent=2) + "\n")
        _git(tmp_project, "add", "package.json")
        _git(tmp_project, "commit", "-q", "-m", "add failing test script")

        assert _porcelain(tmp_project) == ""
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog
        head_before = _git_head(tmp_project)

        from rlsbl.commands.release import run_cmd

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(
                    _rc(),
                    {
                        "yes": True,
                        "quiet": True,
                    },
                
                    ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}),
)

        assert exc_info.value.code == 1
        # .validated may be written by changelog validation; tolerate that
        # specific file but require nothing else changed.
        porcelain = _porcelain(tmp_project)
        for line in porcelain.splitlines():
            path = line.lstrip().split(None, 1)[-1]
            assert path == ".rlsbl/changes/.validated", (
                f"unexpected dirty file after aborted release: {path!r}\nfull porcelain:\n{porcelain}"
            )
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog, (
            "CHANGELOG.md must not be regenerated when built-in tests fail"
        )
        for entry in (tmp_project / ".rlsbl" / "changes").iterdir():
            assert entry.suffix != ".md", (
                f"per-version .md file leaked into working tree: {entry.name}"
            )
        assert _git_head(tmp_project) == head_before, "HEAD must not move"

    def test_failed_selfdoc_check_leaves_tree_clean(self, tmp_project):
        """Selfdoc check failure: working tree and CHANGELOG.md must be unchanged.

        _run_selfdoc_check runs `selfdoc check` via subprocess; a non-zero exit
        aborts the release. Because that step lives between pre-checks and the
        version-bump mutations, no on-disk state should have changed.
        """
        pre_release_changelog = _setup_releasable_npm_project(tmp_project)

        # selfdoc.json must exist for _run_selfdoc_check to even attempt the
        # subprocess call. Minimal config is fine; we never let the real
        # selfdoc tool run -- we intercept at the subprocess layer below.
        (tmp_project / "selfdoc.json").write_text(json.dumps({"docs_dir": "docs"}) + "\n")
        _git(tmp_project, "add", "selfdoc.json")
        _git(tmp_project, "commit", "-q", "-m", "add selfdoc config")

        # Cover the new commit so changelog validation passes.
        config_sha = _git_head(tmp_project)
        changes_dir = tmp_project / ".rlsbl" / "changes"
        with open(changes_dir / "unreleased.jsonl", "a") as f:
            f.write(json.dumps({"commits": [config_sha], "user_facing": False}) + "\n")
        _git(tmp_project, "add", ".rlsbl/changes/unreleased.jsonl")
        _git(tmp_project, "commit", "-q", "-m", "changelog: cover selfdoc config commit")

        assert _porcelain(tmp_project) == ""
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog
        head_before = _git_head(tmp_project)

        from rlsbl.commands.release import run_cmd

        # Drive _run_selfdoc_check into a failing subprocess. We replace the
        # selfdoc invocation with a guaranteed-failing command so the rest of
        # the release flow (changelog validation, hook lookup, etc.) runs for
        # real. require_tool is stubbed so the test does not depend on selfdoc
        # being installed in the test environment.
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "selfdoc" and cmd[1] == "check":
                # Mimic a failing `selfdoc check` with check=True.
                raise subprocess.CalledProcessError(returncode=1, cmd=list(cmd))
            return real_run(cmd, *args, **kwargs)

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.require_tool", return_value=True),
            patch("rlsbl.commands.release.subprocess.run", side_effect=fake_run),
        ):
            # _run_selfdoc_check does not catch CalledProcessError, so it
            # propagates up through run_cmd. Either way the on-disk invariant
            # is what matters; assert that below.
            with pytest.raises((SystemExit, subprocess.CalledProcessError)):
                run_cmd(
                    _rc(exclude=["docs"]),
                    {
                        "yes": True,
                        "quiet": True,
                    },
                
                    ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}),
)

        # .validated may be written by changelog validation; tolerate that
        # specific file but require nothing else changed.
        porcelain = _porcelain(tmp_project)
        for line in porcelain.splitlines():
            path = line.lstrip().split(None, 1)[-1]
            assert path == ".rlsbl/changes/.validated", (
                f"unexpected dirty file after aborted release: {path!r}\nfull porcelain:\n{porcelain}"
            )
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog, (
            "CHANGELOG.md must not be regenerated when selfdoc check fails"
        )
        for entry in (tmp_project / ".rlsbl" / "changes").iterdir():
            assert entry.suffix != ".md", (
                f"per-version .md file leaked into working tree: {entry.name}"
            )

        # HEAD may have moved by exactly one commit: the autogenerated
        # `.validated` cache update written by changelog validation. That
        # commit is documented (Autogenerated trailer, exempted from coverage)
        # and is not a release mutation. Any other movement is a regression.
        head_after = _git_head(tmp_project)
        if head_after != head_before:
            parent = subprocess.run(
                ["git", "rev-parse", "HEAD^"],
                cwd=str(tmp_project),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert parent == head_before, (
                f"HEAD moved more than one commit: {head_before} -> {head_after}"
            )
            msg = subprocess.run(
                ["git", "log", "-1", "--format=%B", head_after],
                cwd=str(tmp_project),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert "Autogenerated: true" in msg, (
                f"unexpected HEAD commit after aborted release:\n{msg}"
            )

    def test_unexpected_files_abort_leaves_tree_clean(self, tmp_project):
        """ReleaseAbortError (unexpected modified files): version bump is
        reverted and no tag is created.

        Injects a rogue file modification during NpmTarget.write_version
        so the unexpected-files check inside _run_release_mutating fires.
        The ReleaseAbortError handler must ``git reset --hard`` to the
        pre-release SHA, leaving the working tree clean.
        """
        pre_release_changelog = _setup_releasable_npm_project(tmp_project)

        # Pre-create .rlsbl/version so it is tracked.  The release flow
        # overwrites it during the mutating phase; git reset --hard will
        # revert the overwrite instead of leaving an untracked file.
        rlsbl_version_file = tmp_project / ".rlsbl" / "version"
        rlsbl_version_file.write_text("0.0.0\n")
        _git(tmp_project, "add", ".rlsbl/version")
        _git(tmp_project, "commit", "-q", "-m", "track .rlsbl/version")

        assert _porcelain(tmp_project) == ""
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog
        head_before = _git_head(tmp_project)

        from rlsbl.commands.release import run_cmd
        from rlsbl.targets.npm import NpmTarget
        from rlsbl.utils import run as real_run

        original_write_version = NpmTarget.write_version

        def rogue_write_version(self, dir_path, version, ctx=None):
            """Write version normally, then modify a tracked file to
            simulate a concurrent process dirtying the tree."""
            result = original_write_version(self, dir_path, version, ctx=ctx)
            # feature.txt is tracked -- modifying it makes it show up
            # as unexpected in the dirty-files guard.
            with open(os.path.join(dir_path, "feature.txt"), "a") as f:
                f.write("rogue modification\n")
            return result

        def fake_run(cmd, args=None, timeout=120, env=None):
            """Intercept gh and git-push calls; let everything else through."""
            if cmd == "gh":
                return ""
            if cmd == "git" and args and args[0] == "push":
                return ""
            return real_run(cmd, args=args, timeout=timeout, env=env)

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
            patch.object(NpmTarget, "write_version", rogue_write_version),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(
                    _rc(),
                    {
                        "yes": True,
                        "quiet": True,
                    },
                    ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}),
                )

        assert exc_info.value.code == 1, (
            "release should exit 1 when unexpected files trigger ReleaseAbortError"
        )

        # The invariants: working tree must be clean after rollback.
        # The .validated cache may have been auto-committed by changelog
        # validation before the mutating phase; if it was not yet tracked,
        # it could appear as untracked.  Tolerate that file only.
        porcelain = _porcelain(tmp_project)
        for line in porcelain.splitlines():
            path = line.lstrip().split(None, 1)[-1]
            assert path == ".rlsbl/changes/.validated", (
                f"unexpected dirty file after ReleaseAbortError rollback: {path!r}\n"
                f"full porcelain:\n{porcelain}"
            )

        # package.json must still be at the original version
        pkg = json.loads((tmp_project / "package.json").read_text())
        assert pkg["version"] == "1.0.0", (
            f"version bump should have been reverted; got {pkg['version']}"
        )

        # CHANGELOG.md must not have been regenerated (or must have been
        # reverted by the rollback)
        assert (tmp_project / "CHANGELOG.md").read_text() == pre_release_changelog, (
            "CHANGELOG.md must be unchanged after ReleaseAbortError rollback"
        )

        # No release tag should exist
        result = subprocess.run(
            ["git", "tag", "-l", "v1.0.1"],
            cwd=str(tmp_project),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "", (
            "v1.0.1 tag should not exist after aborted release"
        )

        # HEAD may have moved by at most one commit (the autogenerated
        # .validated cache commit).  Any other movement is a regression.
        head_after = _git_head(tmp_project)
        if head_after != head_before:
            parent = subprocess.run(
                ["git", "rev-parse", "HEAD^"],
                cwd=str(tmp_project),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert parent == head_before, (
                f"HEAD moved more than one commit: {head_before} -> {head_after}"
            )
            msg = subprocess.run(
                ["git", "log", "-1", "--format=%B", head_after],
                cwd=str(tmp_project),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert "Autogenerated: true" in msg, (
                f"unexpected HEAD commit after ReleaseAbortError rollback:\n{msg}"
            )
