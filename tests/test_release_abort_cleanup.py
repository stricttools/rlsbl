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
                    "npm",
                    ["patch"],
                    {
                        "yes": True,
                        "quiet": True,
                    },
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
                    "npm",
                    ["patch"],
                    {
                        "yes": True,
                        "quiet": True,
                    },
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
                    "npm",
                    ["patch"],
                    {
                        "yes": True,
                        "quiet": True,
                    },
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
