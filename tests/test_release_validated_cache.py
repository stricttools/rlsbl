"""Tests that the .validated cache file does not cause release abort.

The dirty-tree guard in _run_release_mutating checks for unexpected modified
files. Since validate_unreleased() writes .validated during the release flow,
it must be included in the expected_files set (not files_to_commit, since the
file may be gitignored and should not be committed as part of the release).
"""

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


class TestReleaseValidatedCache:
    """Tests that .validated is expected by the dirty-tree guard."""

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
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release.\n")
        # Create .rlsbl/changes/ with unreleased.jsonl and .validated
        changes_dir = os.path.join(".rlsbl", "changes")
        os.makedirs(changes_dir, exist_ok=True)
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,"description":"Bugfix","type":"fix"}\n')
        # The .validated file exists (written by validate_unreleased during release)
        with open(os.path.join(changes_dir, ".validated"), "w") as f:
            f.write("fakehash123\n")
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"private": False}, f)

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.extract_changelog_entry", return_value="- Bugfix")
    def test_validated_cache_does_not_trigger_abort(self, _extract, _finalize,
                                                    _gen_ver_file, _validate, _gen_cl,
                                                    _deploy, _tag, _gh_inst,
                                                    _gh_auth, _clean, _branch,
                                                    _commit_files, mock_run, _push,
                                                    _lock, _unlock, _remote_exists):
        """The .validated file modified by validation must not trigger the dirty-tree abort."""
        from rlsbl.commands.release import run_cmd

        # git status --porcelain shows .validated as modified (written by validation)
        # along with the expected package.json (from version bump)
        porcelain_recheck = " M .rlsbl/changes/.validated\n M package.json"

        mock_run.side_effect = [
            # run_cmd phase:
            "",               # git fetch origin --quiet
            "0",              # git rev-list --count HEAD..origin/main
            "v1.0.0",         # git tag -l v1.0.0 (exists -> bump)
            "",               # git tag -l v1.0.1 (doesn't exist -> proceed)
            "",               # git status --porcelain (pre-hook snapshot)
            "",               # git status --porcelain (pre-selfdoc snapshot)
            "",               # git status --porcelain (post-selfdoc snapshot)
            "",               # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            "",                 # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "abc123def456",     # git rev-parse HEAD (pre_release_sha -- before version bump)
            porcelain_recheck,  # git status --porcelain (re-check guard)
            # new_version != current_version, so has_staged_or_modified is short-circuited
            # commit_files is mocked separately
            "M package.json",   # git tag v1.0.1
            "",                 # git push origin v1.0.1
            "",                 # git rev-parse HEAD (pushed_sha)
            "",                 # gh release create ...
            "abc123def",        # (unconsumed -- side_effect has one extra entry)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            # Should NOT raise SystemExit -- .validated is expected
            run_cmd(_rc(), {
                "yes": True,
                "quiet": False,
            },
            ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}),
)

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_validated_only_dirty_still_aborts_unexpected(self, _validate, _gen_cl,
                                                          _deploy, _tag, _gh_inst,
                                                          _gh_auth, _clean, _branch,
                                                          _commit_files, mock_run, _push,
                                                          _lock, _unlock, _remote_exists):
        """An unexpected file (not .validated, not package.json) still aborts the release."""
        from rlsbl.commands.release import run_cmd

        # git status --porcelain shows an unexpected file alongside expected ones
        porcelain_recheck = " M .rlsbl/changes/.validated\n M package.json\n?? rogue.txt"

        mock_run.side_effect = [
            # run_cmd phase:
            "",               # git fetch origin --quiet
            "0",              # git rev-list --count HEAD..origin/main
            "v1.0.0",         # git tag -l v1.0.0 (exists -> bump)
            "",               # git tag -l v1.0.1 (doesn't exist -> proceed)
            "",               # git status --porcelain (pre-hook snapshot)
            "",               # git status --porcelain (pre-selfdoc snapshot)
            "",               # git status --porcelain (post-selfdoc snapshot)
            "",               # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            "",                 # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "abc123def456",     # git rev-parse HEAD (pre_release_sha -- before version bump)
            porcelain_recheck,  # git status --porcelain (re-check guard) -- has rogue.txt
            "",                 # git reset --hard (rollback after ReleaseAbortError)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            with pytest.raises(SystemExit) as ctx:
                run_cmd(_rc(), {
                    "yes": True,
                    "quiet": False,
                },
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False, "pipelines": {}}),
)
            assert ctx.value.code == 1
