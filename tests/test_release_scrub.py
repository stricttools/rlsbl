"""Tests for rlsbl.commands.release_scrub — validation, dry run, full flow, resume, and no-match."""

import json
import os
import stat
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, call, patch

import pytest

from rlsbl.changelog.files import RemapResult
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl, serialize_entry
from rlsbl.commands.release_scrub import run_cmd
from rlsbl.context import ProjectContext
from rlsbl.workspace import WorkspaceProject

# ---------------------------------------------------------------------------
# Module path prefix for patching
# ---------------------------------------------------------------------------
MOD = "rlsbl.commands.release_scrub"


def _ctx(project_root, config=None, workspace_root=None):
    """Create a minimal ProjectContext for scrub tests."""
    if isinstance(project_root, str):
        project_root = Path(project_root)
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    return ProjectContext(
        project_root=project_root,
        workspace_root=workspace_root,
        config=config or {},
    )


def _write_entries(path, entries):
    """Write a list of ChangelogEntry objects to a JSONL file."""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(serialize_entry(entry) + "\n")


# ===========================================================================
# Validation tests (tests 1-4)
# ===========================================================================


class TestValidatePatternOrFileRequired:
    """run_cmd must exit 1 when neither --pattern nor --file is provided."""

    def test_exits_with_error(self):
        flags = {"replace": "XXX", "mangle": False, "reason": "test", "from-commit": "abc"}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx("/fake"))
        assert exc_info.value.code == 1


class TestValidateReplaceOrMangleRequired:
    """run_cmd must exit 1 when neither --replace nor --mangle is provided."""

    def test_exits_with_error(self):
        flags = {"pattern": "secret", "reason": "test", "from-commit": "abc"}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx("/fake"))
        assert exc_info.value.code == 1


class TestValidateReasonRequired:
    """run_cmd must exit 1 when --reason is not provided."""

    def test_exits_with_error(self):
        flags = {"pattern": "secret", "replace": "XXX", "from-commit": "abc"}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx("/fake"))
        assert exc_info.value.code == 1


class TestValidateFromOrEntireHistoryRequired:
    """run_cmd must exit 1 when neither --from-commit nor --entire-history is provided."""

    def test_exits_with_error(self):
        flags = {"pattern": "secret", "replace": "XXX", "reason": "remove secret"}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx("/fake"))
        assert exc_info.value.code == 1


# ===========================================================================
# Test 5: dry run
# ===========================================================================


class TestDryRunShowsPreviewNoMutations:
    """--dry-run should print a preview and NOT mutate files or push."""

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_dry_run(self, mock_run, _req_tool, tmp_path, capsys):
        # Set up a changes dir so we can verify nothing is modified
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased = changes_dir / "unreleased.jsonl"
        entries = [ChangelogEntry(commits=["aaa111"], user_facing=False)]
        _write_entries(str(unreleased), entries)
        original_content = unreleased.read_text()

        # safegit --version returns a valid version, safegit scrub returns dry-run JSON
        safegit_result = json.dumps({
            "rewrites": {"aaa111": "bbb222", "ccc333": "ddd444"},
            "tags": [{"refname": "refs/tags/v1.0.0"}],
        })
        mock_run.side_effect = [
            "safegit 0.18.0",  # safegit --version
            safegit_result,    # safegit scrub match --json ...
        ]

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "remove secret",
            "entire-history": True,
            "dry-run": True,
        }

        # Should NOT call sys.exit — just return
        run_cmd(flags, ctx=_ctx(str(tmp_path)))

        # Verify preview message printed
        captured = capsys.readouterr()
        assert "2 commits would be rewritten" in captured.out
        assert "1 tags affected" in captured.out

        # No files modified
        assert unreleased.read_text() == original_content

        # No force-push calls
        for c in mock_run.call_args_list:
            args = c[0]  # positional args to run()
            if len(args) >= 2 and args[0] == "git":
                assert "--force" not in args[1], "force-push should not happen in dry-run"

        # scrub-result.json should NOT exist
        scrub_result = tmp_path / ".rlsbl" / "releases" / "scrub-result.json"
        assert not scrub_result.exists()


# ===========================================================================
# Test 6: full scrub flow
# ===========================================================================


class TestFullScrubFlow:
    """End-to-end scrub: JSONL remap, CHANGELOG regen, push, release recreation."""

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.extract_changelog_entry", return_value="## 1.0.0\n\n- Fix bug\n")
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_full_flow(self, mock_run, _req_tool, mock_gen_changelog,
                       _extract_cl, _push_timeout, _get_branch,
                       _gh_installed, _gh_auth, _acquire_lock, _release_lock,
                       tmp_path):
        # -- Set up project structure --
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        # unreleased.jsonl with a hash that will be remapped
        unreleased = changes_dir / "unreleased.jsonl"
        _write_entries(str(unreleased), [
            ChangelogEntry(commits=["old_hash_1"], user_facing=True, description="New thing", type="feature"),
        ])

        # 1.0.0.jsonl (read-only) with a hash that will be remapped
        versioned = changes_dir / "1.0.0.jsonl"
        _write_entries(str(versioned), [
            ChangelogEntry(commits=["old_hash_2"], user_facing=True, description="Fix bug", type="fix"),
        ])
        os.chmod(str(versioned), 0o444)

        # .validated cache (should be deleted)
        validated = changes_dir / ".validated"
        validated.write_text("abc123")

        # CHANGELOG.md (generate_changelog is mocked, so we just check it's called)
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")

        # safegit scrub result with rewrites
        safegit_result = json.dumps({
            "rewrites": {"old_hash_1": "new_hash_1", "old_hash_2": "new_hash_2"},
            "tags": [{"refname": "refs/tags/v1.0.0"}],
            "new_head": "deadbeef1234",
        })

        # Build the sequence of run() return values
        mock_run.side_effect = [
            "safegit 0.18.0",  # safegit --version
            safegit_result,    # safegit scrub match --json ...
            "",                # safegit commit (COMMITTED step)
            "",                # git push --force-with-lease (BRANCH_PUSHED)
            "",                # git push --force origin v1.0.0 (TAGS_PUSHED)
            '{"body": "old notes"}',  # gh release view v1.0.0
            "",                # gh release delete v1.0.0 --yes
            "",                # gh release create v1.0.0
        ]

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "remove leaked secret",
            "entire-history": True,
            "yes": True,
        }

        ctx = _ctx(str(tmp_path))
        run_cmd(flags, ctx=ctx)

        # -- Assertions --

        # 1. JSONL files have remapped hashes
        updated_unreleased = parse_jsonl(str(unreleased))
        assert updated_unreleased[0].commits == ["new_hash_1"]

        # Versioned file should be re-locked after remap
        assert stat.S_IMODE(os.stat(str(versioned)).st_mode) & stat.S_IWUSR == 0
        updated_versioned = parse_jsonl(str(versioned))
        assert updated_versioned[0].commits == ["new_hash_2"]

        # 2. CHANGELOG.md regenerated
        mock_gen_changelog.assert_called_once_with(str(tmp_path))

        # 3. .validated deleted
        assert not validated.exists()

        # 4. scrub-result.json created during flow then deleted at the end
        scrub_result = releases_dir / "scrub-result.json"
        assert not scrub_result.exists()

        # 5. Verify specific subprocess calls
        all_calls = mock_run.call_args_list

        # git push --force-with-lease origin main
        force_lease_calls = [
            c for c in all_calls
            if c[0][0] == "git" and "--force-with-lease" in c[0][1]
        ]
        assert len(force_lease_calls) == 1
        assert "main" in force_lease_calls[0][0][1]

        # git push --force origin v1.0.0
        force_tag_calls = [
            c for c in all_calls
            if c[0][0] == "git" and "--force" in c[0][1] and "v1.0.0" in c[0][1]
        ]
        assert len(force_tag_calls) == 1

        # gh release delete + create
        gh_delete_calls = [
            c for c in all_calls
            if c[0][0] == "gh" and "delete" in c[0][1]
        ]
        assert len(gh_delete_calls) == 1
        assert "v1.0.0" in gh_delete_calls[0][0][1]

        gh_create_calls = [
            c for c in all_calls
            if c[0][0] == "gh" and "create" in c[0][1]
        ]
        assert len(gh_create_calls) == 1
        assert "v1.0.0" in gh_create_calls[0][0][1]


# ===========================================================================
# Test 7: resume from scrub-result.json
# ===========================================================================


class TestResumeFromScrubResult:
    """When scrub-result.json exists and HEAD matches, resume from where we left off."""

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.extract_changelog_entry", return_value="## 1.0.0\n\n- Fix\n")
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_resume_skips_safegit(self, mock_run, _req_tool, mock_gen_changelog,
                                   _extract_cl, _push_timeout, _get_branch,
                                   _gh_installed, _gh_auth, _acquire_lock,
                                   _release_lock, tmp_path):
        # -- Set up project with scrub-result.json already present --
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        # Unreleased JSONL (already remapped from previous partial run)
        unreleased = changes_dir / "unreleased.jsonl"
        _write_entries(str(unreleased), [
            ChangelogEntry(commits=["new_hash_1"], user_facing=True, description="Feature", type="feature"),
        ])

        # .validated (should be deleted by VALIDATED_DELETED step)
        validated = changes_dir / ".validated"
        validated.write_text("stale")

        # CHANGELOG.md
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")

        # scrub-result.json: JSONL_REMAPPED already done
        saved_head = "deadbeef1234abcd"
        scrub_result = releases_dir / "scrub-result.json"
        scrub_result.write_text(json.dumps({
            "rewrites": {"old_hash_1": "new_hash_1"},
            "tags": [{"refname": "refs/tags/v1.0.0"}],
            "new_head": saved_head,
            "completed_steps": ["JSONL_REMAPPED"],
        }))

        # run() calls expected during resume (safegit scrub is NOT called):
        # 1. safegit --version (version check still runs)
        # 2. git rev-parse HEAD (to verify HEAD matches saved new_head)
        # Then the remaining steps:
        # 3. safegit commit (COMMITTED)
        # 4. git push --force-with-lease (BRANCH_PUSHED)
        # 5. git push --force origin v1.0.0 (TAGS_PUSHED)
        # 6. gh release view v1.0.0
        # 7. gh release delete v1.0.0 --yes
        # 8. gh release create v1.0.0
        mock_run.side_effect = [
            "safegit 0.18.0",  # safegit --version
            saved_head,        # git rev-parse HEAD
            "",                # safegit commit
            "",                # git push --force-with-lease
            "",                # git push --force origin v1.0.0
            '{"body": "notes"}',  # gh release view
            "",                # gh release delete
            "",                # gh release create
        ]

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "remove secret",
            "entire-history": True,
            "yes": True,
        }

        run_cmd(flags, ctx=_ctx(str(tmp_path)))

        # -- Assertions --

        # safegit scrub should NOT have been called (no "scrub" in any run args)
        for c in mock_run.call_args_list:
            cmd, args = c[0][0], c[0][1] if len(c[0]) > 1 else []
            if cmd == "safegit" and isinstance(args, list):
                assert "scrub" not in args, "safegit scrub should be skipped on resume"

        # CHANGELOG regenerated (step was not in completed_steps)
        mock_gen_changelog.assert_called_once()

        # .validated deleted
        assert not validated.exists()

        # scrub-result.json cleaned up at the end
        assert not scrub_result.exists()

        # Branch push happened
        force_lease_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "git" and len(c[0]) > 1 and "--force-with-lease" in c[0][1]
        ]
        assert len(force_lease_calls) == 1

        # Tag push happened
        force_tag_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "git" and len(c[0]) > 1 and "--force" in c[0][1] and "v1.0.0" in c[0][1]
        ]
        assert len(force_tag_calls) == 1

        # Release recreated
        gh_create_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "gh" and len(c[0]) > 1 and "create" in c[0][1]
        ]
        assert len(gh_create_calls) == 1


# ===========================================================================
# Test 8: no matches exits cleanly
# ===========================================================================


class TestNoMatchesExitsCleanly:
    """When safegit returns empty rewrites, exit 0 with 'No matches found'."""

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_no_matches(self, mock_run, _req_tool, tmp_path, capsys):
        # Set up minimal project structure
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        safegit_result = json.dumps({
            "rewrites": {},
            "tags": [],
        })

        mock_run.side_effect = [
            "safegit 0.18.0",  # safegit --version
            safegit_result,    # safegit scrub match --json ...
        ]

        flags = {
            "pattern": "nonexistent",
            "replace": "XXX",
            "reason": "test",
            "entire-history": True,
            "yes": True,
        }

        # Should return normally (no sys.exit)
        run_cmd(flags, ctx=_ctx(str(tmp_path)))

        captured = capsys.readouterr()
        assert "No matches found" in captured.out

        # No force-push or gh calls
        assert mock_run.call_count == 2  # only version check + scrub


# ===========================================================================
# Test 9: monorepo tag prefix index -- correct project selection
# ===========================================================================


class TestMonorepoTagCorrectProject:
    """Two projects (alpha, beta) both at v1.0.0. Tag prefix determines which CHANGELOG is used."""

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    @patch(f"{MOD}.load_workspace")
    def test_monorepo_tag_correct_project(self, mock_load_ws, mock_run, _req_tool,
                                           mock_gen_changelog, _push_timeout,
                                           _get_branch, _gh_installed, _gh_auth,
                                           _acquire_lock, _release_lock, tmp_path):
        # -- Set up monorepo with two projects --
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()

        alpha_dir = ws_root / "packages" / "alpha"
        alpha_dir.mkdir(parents=True)
        alpha_changes = alpha_dir / ".rlsbl" / "changes"
        alpha_changes.mkdir(parents=True)
        (alpha_changes / "unreleased.jsonl").write_text("")
        (alpha_dir / "CHANGELOG.md").write_text("## 1.0.0\n\n- Alpha feature\n")

        beta_dir = ws_root / "packages" / "beta"
        beta_dir.mkdir(parents=True)
        beta_changes = beta_dir / ".rlsbl" / "changes"
        beta_changes.mkdir(parents=True)
        (beta_changes / "unreleased.jsonl").write_text("")
        (beta_dir / "CHANGELOG.md").write_text("## 1.0.0\n\n- Beta fix\n")

        # Monorepo workspace dir (for lock)
        (ws_root / ".rlsbl-monorepo").mkdir()
        (ws_root / ".rlsbl").mkdir()

        # Project releases dir for scrub-result.json
        releases_dir = alpha_dir / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        workspace_projects = [
            WorkspaceProject({"name": "alpha", "path": "packages/alpha"}),
            WorkspaceProject({"name": "beta", "path": "packages/beta"}),
        ]
        mock_load_ws.return_value = workspace_projects

        # Track which changelog paths extract_changelog_entry is called with
        extract_calls = []

        def fake_extract(changelog_path, version):
            extract_calls.append(changelog_path)
            if "alpha" in changelog_path:
                return "- Alpha feature"
            if "beta" in changelog_path:
                return "- Beta fix"
            return None

        safegit_result = json.dumps({
            "rewrites": {"old1": "new1"},
            "tags": [
                {"refname": "refs/tags/alpha@v1.0.0"},
                {"refname": "refs/tags/beta@v1.0.0"},
            ],
            "new_head": "abc123",
        })

        mock_run.side_effect = [
            "safegit 0.18.0",       # safegit --version
            safegit_result,          # safegit scrub
            "",                      # safegit commit
            "",                      # git push --force-with-lease
            "",                      # git push --force origin alpha@v1.0.0
            "",                      # git push --force origin beta@v1.0.0
            '{"body": "old"}',       # gh release view alpha@v1.0.0
            "",                      # gh release delete alpha@v1.0.0
            "",                      # gh release create alpha@v1.0.0
            '{"body": "old"}',       # gh release view beta@v1.0.0
            "",                      # gh release delete beta@v1.0.0
            "",                      # gh release create beta@v1.0.0
        ]

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "remove secret",
            "entire-history": True,
            "yes": True,
        }

        ctx = _ctx(str(alpha_dir), workspace_root=str(ws_root))

        with patch(f"{MOD}.extract_changelog_entry", side_effect=fake_extract):
            run_cmd(flags, ctx=ctx)

        # Verify alpha tag used alpha's CHANGELOG
        alpha_cl = os.path.join(str(ws_root), "packages", "alpha", "CHANGELOG.md")
        beta_cl = os.path.join(str(ws_root), "packages", "beta", "CHANGELOG.md")

        assert alpha_cl in extract_calls, f"Expected alpha CHANGELOG to be queried, got {extract_calls}"
        assert beta_cl in extract_calls, f"Expected beta CHANGELOG to be queried, got {extract_calls}"

        # Verify the create calls used the correct notes
        gh_create_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "gh" and len(c[0]) > 1 and "create" in c[0][1]
        ]
        assert len(gh_create_calls) == 2

        # First create: alpha@v1.0.0 with alpha notes
        alpha_create = gh_create_calls[0]
        assert "alpha@v1.0.0" in alpha_create[0][1]
        assert "Alpha feature" in alpha_create[0][1][-1]

        # Second create: beta@v1.0.0 with beta notes
        beta_create = gh_create_calls[1]
        assert "beta@v1.0.0" in beta_create[0][1]
        assert "Beta fix" in beta_create[0][1][-1]


# ===========================================================================
# Test 10: standalone tag (no project prefix) uses root CHANGELOG
# ===========================================================================


class TestStandaloneTagNoPrefix:
    """Tag v1.0.0 (no project prefix) in a monorepo uses the workspace root CHANGELOG."""

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    @patch(f"{MOD}.load_workspace")
    def test_standalone_tag_no_prefix(self, mock_load_ws, mock_run, _req_tool,
                                       mock_gen_changelog, _push_timeout,
                                       _get_branch, _gh_installed, _gh_auth,
                                       _acquire_lock, _release_lock, tmp_path):
        # -- Set up monorepo with a root CHANGELOG --
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / ".rlsbl-monorepo").mkdir()
        (ws_root / ".rlsbl").mkdir()
        (ws_root / "CHANGELOG.md").write_text("## 1.0.0\n\n- Root level change\n")

        # Project dir (some sub-project for context)
        proj_dir = ws_root / "packages" / "myproj"
        proj_dir.mkdir(parents=True)
        proj_changes = proj_dir / ".rlsbl" / "changes"
        proj_changes.mkdir(parents=True)
        (proj_changes / "unreleased.jsonl").write_text("")
        releases_dir = proj_dir / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        workspace_projects = [
            WorkspaceProject({"name": "myproj", "path": "packages/myproj"}),
        ]
        mock_load_ws.return_value = workspace_projects

        extract_calls = []

        def fake_extract(changelog_path, version):
            extract_calls.append(changelog_path)
            if changelog_path == os.path.join(str(ws_root), "CHANGELOG.md"):
                return "- Root level change"
            return None

        safegit_result = json.dumps({
            "rewrites": {"old1": "new1"},
            "tags": [{"refname": "refs/tags/v1.0.0"}],
            "new_head": "abc123",
        })

        mock_run.side_effect = [
            "safegit 0.18.0",       # safegit --version
            safegit_result,          # safegit scrub
            "",                      # safegit commit
            "",                      # git push --force-with-lease
            "",                      # git push --force origin v1.0.0
            '{"body": "old"}',       # gh release view v1.0.0
            "",                      # gh release delete v1.0.0
            "",                      # gh release create v1.0.0
        ]

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "remove secret",
            "entire-history": True,
            "yes": True,
        }

        ctx = _ctx(str(proj_dir), workspace_root=str(ws_root))

        with patch(f"{MOD}.extract_changelog_entry", side_effect=fake_extract):
            run_cmd(flags, ctx=ctx)

        # Verify the root CHANGELOG was queried (not any project's)
        root_cl = os.path.join(str(ws_root), "CHANGELOG.md")
        assert root_cl in extract_calls, f"Expected root CHANGELOG to be queried, got {extract_calls}"

        # No project CHANGELOG should have been queried
        proj_changelogs = [c for c in extract_calls if "packages" in c]
        assert len(proj_changelogs) == 0, f"Project CHANGELOGs should not be queried for standalone tags: {proj_changelogs}"

        # Verify the release was created with root changelog notes
        gh_create_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "gh" and len(c[0]) > 1 and "create" in c[0][1]
        ]
        assert len(gh_create_calls) == 1
        assert "v1.0.0" in gh_create_calls[0][0][1]
        assert "Root level change" in gh_create_calls[0][0][1][-1]
