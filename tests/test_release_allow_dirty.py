"""Tests for --allow-dirty flag on rlsbl release."""

import json
import os
from io import StringIO
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


class TestReleaseAllowDirty:
    """Tests that --allow-dirty skips the clean-tree check."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        # Create package.json so npm registry is detected
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f, indent=2)
            f.write("\n")
        # Create CHANGELOG.md with entry for the bumped version
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release with bugfixes and improvements.\n")
        # Create .rlsbl/changes/ for JSONL changelog
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,"description":"Bugfix","type":"fix"}\n')
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"private": False}, f)

    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.is_clean_tree", return_value=False)
    def test_dirty_tree_without_allow_dirty_exits(self, _clean, _gh_auth, _gh_inst):
        """Without --allow-dirty, a dirty tree should cause SystemExit."""
        from rlsbl.commands.release import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}))
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=False)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_allow_dirty_skips_clean_tree_check(self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean,
                                                 _branch, _commit_files, mock_run, _push):
        """With --allow-dirty, a dirty tree should not block the release (dry-run)."""
        from rlsbl.commands.release import run_cmd

        # 1. git status --porcelain (capture pre-existing dirty files)
        # 2. git fetch origin --quiet
        # 3. git rev-list --count HEAD..origin/main
        # 4. tag -l for current version (exists -> bump)
        # 5. tag -l for bumped version (doesn't exist -> proceed)
        # 6. git status --porcelain (pre-hook snapshot)
        # 7. git status --porcelain (post-hook snapshot)
        mock_run.side_effect = [" M notes.txt", "", "0", "v1.0.0", "", " M notes.txt", " M notes.txt"]

        with patch("sys.stdout", new_callable=StringIO):
            # Should not raise SystemExit
            run_cmd(_rc(), {
                "allow-dirty": True,
                "dry-run": True,
                "quiet": False,
            },
            ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}),
)

    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=False)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.extract_changelog_entry", return_value="- Bugfix")
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    def test_allow_dirty_non_dry_run_passes_recheck(self, _changes_dir, _extract, _finalize,
                                                     _gen_ver_file, _validate, _gen_cl,
                                                     _deploy, _tag, _gh_inst,
                                                     _gh_auth, _clean, _branch,
                                                     _commit_files, mock_run, _push,
                                                     _lock, _unlock):
        """With --allow-dirty (non-dry-run), pre-existing dirty files pass the re-check guard."""
        from rlsbl.commands.release import run_cmd

        # Pre-existing dirty file that is NOT a release-managed file
        dirty_file = "notes.txt"
        porcelain_dirty = f" M {dirty_file}"
        # After version bump, porcelain shows both the pre-existing dirty file
        # and the expected release file (package.json)
        porcelain_recheck = f" M {dirty_file}\n M package.json"

        mock_run.side_effect = [
            # run_cmd phase:
            porcelain_dirty,    # git status --porcelain (capture pre-existing dirty)
            "",                 # git fetch origin --quiet
            "0",                # git rev-list --count HEAD..origin/main
            "v1.0.0",           # git tag -l v1.0.0 (exists -> bump)
            "",                 # git tag -l v1.0.1 (doesn't exist -> proceed)
            porcelain_dirty,    # git status --porcelain (pre-hook snapshot)
            porcelain_dirty,    # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            porcelain_dirty,    # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "abc123def456",     # git rev-parse HEAD (pre_release_sha -- before version bump)
            porcelain_recheck,  # git status --porcelain (re-check guard)
            # new_version != current_version, so has_staged_or_modified is short-circuited
            # commit_files is mocked separately (no git add/commit calls here)
            "M package.json",   # git tag v1.0.1
            "",                 # git push origin v1.0.1
            "",                 # git rev-parse HEAD (pushed_sha)
            "",                 # gh release create ...
            "abc123def",        # (unconsumed -- side_effect has one extra entry)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            # Should not raise SystemExit -- the re-check guard must not
            # treat the pre-existing dirty file as unexpected.
            run_cmd(_rc(), {
                "allow-dirty": True,
                "yes": True,
                "quiet": False,
            },
            ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}),
)

    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=False)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_allow_dirty_still_catches_new_unexpected_files(self, _validate, _gen_cl,
                                                             _deploy, _tag,
                                                             _gh_inst, _gh_auth,
                                                             _clean, _branch,
                                                             _commit_files, mock_run,
                                                             _push, _lock, _unlock):
        """With --allow-dirty, genuinely new unexpected files still abort the release."""
        from rlsbl.commands.release import run_cmd

        # Pre-existing dirty file
        porcelain_dirty = " M notes.txt"
        # Re-check shows a NEW unexpected file that wasn't dirty before
        porcelain_recheck = " M notes.txt\n M package.json\n?? surprise.txt"

        mock_run.side_effect = [
            # run_cmd phase:
            porcelain_dirty,    # git status --porcelain (capture pre-existing dirty)
            "",                 # git fetch origin --quiet
            "0",                # git rev-list --count HEAD..origin/main
            "v1.0.0",           # git tag -l v1.0.0 (exists -> bump)
            "",                 # git tag -l v1.0.1 (doesn't exist -> proceed)
            porcelain_dirty,    # git status --porcelain (pre-hook snapshot)
            porcelain_dirty,    # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            porcelain_dirty,    # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "abc123def456",     # git rev-parse HEAD (pre_release_sha -- before version bump)
            porcelain_recheck,  # git status --porcelain (re-check guard) -- has surprise.txt
            "",                 # git reset --hard (rollback after ReleaseAbortError)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(_rc(), {
                    "allow-dirty": True,
                    "yes": True,
                    "quiet": False,
                },
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}),
)
            assert exc_info.value.code == 1
