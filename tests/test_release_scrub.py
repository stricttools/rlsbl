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
# File-mode CLI contract (real safegit: positional path, --from required,
# no --replace/--mangle/--entire-history)
# ===========================================================================


class TestFileModeRealSafegitContract:
    """File mode must follow safegit's actual CLI: `scrub file <path> --from <sha> --reason <r>`.

    safegit scrub file takes a POSITIONAL path, requires --from, and has no
    --replace/--mangle/--entire-history flags (strictcli-go hard-errors on
    unknown flags).
    """

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_file_mode_builds_positional_args(self, mock_run, _req_tool, tmp_path):
        file_dry_run = json.dumps({
            "version": 1, "dry_run": True, "file": "secrets.env",
            "mode": "remove", "from": "abc123", "commit_count": 3,
            "old_head": "def456",
        })
        mock_run.side_effect = [
            "safegit 0.22.0",  # safegit --version
            file_dry_run,      # safegit scrub file --json --dry-run ...
        ]

        flags = {
            "file": "secrets.env",
            "from-commit": "abc123",
            "reason": "remove secrets",
            "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))

        scrub_call = mock_run.call_args_list[1]
        args = scrub_call[0][1]
        assert args[:2] == ["scrub", "file"]
        # Positional path is the LAST argument -- no --file flag exists.
        assert args[-1] == "secrets.env"
        assert "--file" not in args
        assert "--replace" not in args
        assert "--mangle" not in args
        assert "--entire-history" not in args
        assert "--from" in args
        assert args[args.index("--from") + 1] == "abc123"
        assert "--reason" in args
        assert "--json" in args
        assert "--dry-run" in args

    def test_file_mode_requires_from_commit(self, tmp_path):
        """safegit scrub file requires --from; --entire-history does not exist."""
        flags = {"file": "secrets.env", "reason": "r", "entire-history": True}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

    def test_file_mode_rejects_replace(self, tmp_path):
        flags = {"file": "secrets.env", "replace": "XXX", "from-commit": "abc", "reason": "r"}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

    def test_file_mode_rejects_mangle(self, tmp_path):
        flags = {"file": "secrets.env", "mangle": True, "from-commit": "abc", "reason": "r"}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1


class TestRecipeMode:
    """Recipe mode drives `safegit scrub run <recipe.toml>`."""

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_recipe_mode_builds_args(self, mock_run, _req_tool, tmp_path, capsys):
        recipe = tmp_path / "recipe.toml"
        recipe.write_text('[[operations]]\npattern = "secret"\nreplace = "X"\n')

        # Real ScrubRunDryRunResult schema (safegit scrub_run.go)
        dry_json = json.dumps({
            "version": 1,
            "dry_run": True,
            "operation_count": 2,
            "operations": [],
            "total_blob_matches": 4,
            "total_commit_matches": 1,
            "total_tag_matches": 0,
            "total_affected_files": 3,
            "estimated_commits": 5,
            "objects_scanned": 20,
            "binary_skipped": 0,
        })
        mock_run.side_effect = [
            "safegit 0.22.0",  # safegit --version
            dry_json,          # safegit scrub run --json --dry-run ...
        ]

        flags = {
            "recipe": str(recipe),
            "entire-history": True,
            "reason": "multi-op scrub",
            "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))

        args = mock_run.call_args_list[1][0][1]
        assert args[:2] == ["scrub", "run"]
        assert "--json" in args
        assert "--dry-run" in args
        assert str(recipe) in args
        assert "--entire-history" in args
        assert "--reason" in args
        # Recipe mode has no pattern/replace flags of its own
        assert "--pattern" not in args
        assert "--replace" not in args

        captured = capsys.readouterr()
        assert "5 commits would be rewritten" in captured.out
        assert "2 operations" in captured.out

    def test_recipe_mutually_exclusive_with_pattern(self, tmp_path):
        recipe = tmp_path / "recipe.toml"
        recipe.write_text("[[operations]]\n")
        flags = {
            "recipe": str(recipe), "pattern": "x", "replace": "y",
            "entire-history": True, "reason": "r",
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

    def test_recipe_rejects_replace(self, tmp_path):
        recipe = tmp_path / "recipe.toml"
        recipe.write_text("[[operations]]\n")
        flags = {
            "recipe": str(recipe), "replace": "y",
            "entire-history": True, "reason": "r",
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

    def test_recipe_requires_range(self, tmp_path):
        recipe = tmp_path / "recipe.toml"
        recipe.write_text("[[operations]]\n")
        flags = {"recipe": str(recipe), "reason": "r"}
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

    def test_recipe_file_must_exist(self, tmp_path):
        flags = {
            "recipe": str(tmp_path / "missing.toml"),
            "entire-history": True, "reason": "r",
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1


class TestSafegitMinVersion:
    """The scrub flow depends on safegit >= 0.22.0 (--remap-shas-in, the
    persisted rewrite journal, cleanup_ok/pre_rewrite_remotes JSON)."""

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_rejects_old_safegit(self, mock_run, _req_tool, tmp_path, capsys):
        mock_run.side_effect = ["safegit 0.21.1"]
        flags = {
            "pattern": "x", "replace": "y", "reason": "r",
            "entire-history": True, "dry-run": True,
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1
        assert "0.22.0" in capsys.readouterr().err

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_accepts_dirty_build_suffix(self, mock_run, _req_tool, tmp_path):
        """Version strings like '0.21.1+dirty' must parse, not crash."""
        dry_json = json.dumps({
            "version": 1, "dry_run": True, "pattern": "x",
            "total_matches": 0, "estimated_commits": 0,
        })
        mock_run.side_effect = ["safegit 0.22.0+dirty", dry_json]
        flags = {
            "pattern": "x", "replace": "y", "reason": "r",
            "entire-history": True, "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))  # must not raise


class TestScrubOrchestrationHandshake:
    """The safegit scrub subprocess must receive RLSBL_SCRUB_ORCHESTRATED=1."""

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_env_var_set_on_scrub_invocation(self, mock_run, _req_tool, tmp_path):
        dry_json = json.dumps({
            "version": 1, "dry_run": True, "pattern": "x",
            "total_matches": 0, "estimated_commits": 0,
        })
        mock_run.side_effect = ["safegit 0.22.0", dry_json]
        flags = {
            "pattern": "x", "replace": "y", "reason": "r",
            "entire-history": True, "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))

        scrub_call = mock_run.call_args_list[1]
        env = scrub_call[1].get("env")
        assert env is not None, "scrub invocation must pass an env"
        assert env.get("RLSBL_SCRUB_ORCHESTRATED") == "1"


class TestRemapGlobsPassedToSafegit:
    """Every safegit scrub invocation (all three modes) must carry the
    repeatable --remap-shas-in globs derived from changelog_remap_globs, so
    safegit rewrites commit hashes inside the JSONL changelogs at EVERY
    commit of the rewritten history (including HEAD)."""

    @staticmethod
    def _remap_pairs(args):
        return [
            args[i + 1] for i, a in enumerate(args) if a == "--remap-shas-in"
        ]

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_match_mode_standalone_glob(self, mock_run, _req_tool, tmp_path):
        dry_json = json.dumps({
            "version": 1, "dry_run": True, "pattern": "x",
            "total_matches": 0, "estimated_commits": 0,
        })
        mock_run.side_effect = ["safegit 0.22.0", dry_json]
        flags = {
            "pattern": "x", "replace": "y", "reason": "r",
            "entire-history": True, "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))
        args = mock_run.call_args_list[1][0][1]
        assert self._remap_pairs(args) == [".rlsbl/changes/*.jsonl"]

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_file_mode_glob_and_positional_last(self, mock_run, _req_tool, tmp_path):
        dry_json = json.dumps({
            "version": 1, "dry_run": True, "file": "secrets.env",
            "mode": "remove", "from": "abc", "commit_count": 1,
        })
        mock_run.side_effect = ["safegit 0.22.0", dry_json]
        flags = {
            "file": "secrets.env", "from-commit": "abc",
            "reason": "r", "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))
        args = mock_run.call_args_list[1][0][1]
        assert self._remap_pairs(args) == [".rlsbl/changes/*.jsonl"]
        # The positional path must still come last.
        assert args[-1] == "secrets.env"

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_recipe_mode_glob(self, mock_run, _req_tool, tmp_path):
        recipe = tmp_path / "recipe.toml"
        recipe.write_text('[[operations]]\npattern = "x"\nreplace = "y"\n')
        dry_json = json.dumps({
            "version": 1, "dry_run": True, "operation_count": 1,
            "estimated_commits": 0,
        })
        mock_run.side_effect = ["safegit 0.22.0", dry_json]
        flags = {
            "recipe": str(recipe), "reason": "r",
            "entire-history": True, "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))
        args = mock_run.call_args_list[1][0][1]
        assert self._remap_pairs(args) == [".rlsbl/changes/*.jsonl"]


class TestEmptyOutputMeansNoMatches:
    """safegit scrub execute emits NO JSON when there is nothing to rewrite."""

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_empty_execute_output(self, mock_run, _req_tool, tmp_path, capsys):
        mock_run.side_effect = [
            "safegit 0.22.0",  # safegit --version
            "",                # git ls-remote origin (pre-scrub snapshot)
            "",                # safegit scrub match: no matches -> empty stdout
        ]
        flags = {
            "pattern": "nonexistent", "replace": "XXX",
            "reason": "test", "entire-history": True, "yes": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))
        captured = capsys.readouterr()
        assert "No matches found" in captured.out
        # scrub-result.json must not linger
        assert not (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()


# ===========================================================================
# Test 5: dry run
# ===========================================================================


class TestDryRunShowsPreviewNoMutations:
    """--dry-run should print a real preview (from the actual dry-run JSON
    schema, which has NO rewrites/tags keys) and NOT mutate files or push."""

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_dry_run_match(self, mock_run, _req_tool, tmp_path, capsys):
        # Set up a changes dir so we can verify nothing is modified
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased = changes_dir / "unreleased.jsonl"
        entries = [ChangelogEntry(commits=["aaa111"], user_facing=False)]
        _write_entries(str(unreleased), entries)
        original_content = unreleased.read_text()

        # Real ScrubMatchDryRunResult schema (safegit scrub_match.go)
        safegit_result = json.dumps({
            "version": 1,
            "dry_run": True,
            "pattern": "secret",
            "scope": "entire_history",
            "objects_scanned": 12,
            "binary_skipped": 0,
            "total_matches": 3,
            "blob_matches": 2,
            "commit_matches": 1,
            "tag_matches": 0,
            "file_matches": 1,
            "estimated_commits": 2,
        })
        mock_run.side_effect = [
            "safegit 0.22.0",  # safegit --version
            safegit_result,    # safegit scrub match --json --dry-run ...
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

        # Verify the preview uses the REAL dry-run keys (non-zero counts)
        captured = capsys.readouterr()
        assert "3 matches" in captured.out
        assert "2 commits would be rewritten" in captured.out
        assert "0 commits" not in captured.out

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

    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_dry_run_file(self, mock_run, _req_tool, tmp_path, capsys):
        # Real ScrubFileDryRunResult schema (safegit scrub.go)
        safegit_result = json.dumps({
            "version": 1,
            "dry_run": True,
            "file": "secrets.env",
            "mode": "remove",
            "from": "abc123",
            "commit_count": 7,
            "old_head": "def456",
        })
        mock_run.side_effect = [
            "safegit 0.22.0",
            safegit_result,
        ]
        flags = {
            "file": "secrets.env",
            "from-commit": "abc123",
            "reason": "remove secrets",
            "dry-run": True,
        }
        run_cmd(flags, ctx=_ctx(str(tmp_path)))
        captured = capsys.readouterr()
        assert "7 commits would be rewritten" in captured.out
        assert "secrets.env" in captured.out
        assert "removed" in captured.out


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
    @patch(f"{MOD}.run_gh")
    @patch(f"{MOD}.run")
    def test_full_flow(self, mock_run, mock_run_gh, _req_tool, mock_gen_changelog,
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
            "old_head": "cafebabe5678",
            "new_head": "deadbeef1234",
        })

        # Function-style side effect: robust to extra bookkeeping calls
        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            return ""

        mock_run.side_effect = run_effect

        # gh calls go through run_gh
        def run_gh_effect(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return '{"body": "old notes"}'
            return ""

        mock_run_gh.side_effect = run_gh_effect

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "remove leaked secret",
            "entire-history": True,
            "yes": True,
        }

        ctx = _ctx(str(tmp_path))
        # Fake hashes can't resolve against a real repo; the gate is
        # unit-tested separately in TestPostRemapValidationGate.
        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(flags, ctx=ctx)

        # -- Assertions --

        # 1. rlsbl leaves the JSONL files UNTOUCHED: the in-history remap
        # (safegit --remap-shas-in) is responsible for the worktree content
        # now, and safegit is mocked here.
        updated_unreleased = parse_jsonl(str(unreleased))
        assert updated_unreleased[0].commits == ["old_hash_1"]

        # Versioned file keeps its read-only mode and content
        assert stat.S_IMODE(os.stat(str(versioned)).st_mode) & stat.S_IWUSR == 0
        updated_versioned = parse_jsonl(str(versioned))
        assert updated_versioned[0].commits == ["old_hash_2"]

        # 2. CHANGELOG.md regenerated (for the byte-identical assertion)
        mock_gen_changelog.assert_called_once_with(str(tmp_path))

        # 3. .validated deleted AND its deletion is part of the commit; the
        # scrub commit shrinks to cache/archive artifacts -- no JSONL files,
        # no CHANGELOG.md.
        assert not validated.exists()
        commit_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "safegit" and (c[0][1] or [])[:1] == ["commit"]
        ]
        assert len(commit_calls) == 1
        commit_args = commit_calls[0][0][1]
        assert str(validated) in commit_args, \
            "deleted .validated must be included in the scrub commit"
        assert str(unreleased) not in commit_args
        assert str(versioned) not in commit_args
        assert str(tmp_path / "CHANGELOG.md") not in commit_args
        committed_files = commit_args[commit_args.index("--") + 1:]
        archive_files = [f for f in committed_files if "scrubs" in f]
        assert sorted(committed_files) == sorted(
            [str(validated)] + archive_files
        )

        # Machine-greppable audit trailer on the scrub commit
        assert "--trailer" in commit_args
        trailer_val = commit_args[commit_args.index("--trailer") + 1]
        assert trailer_val == "Scrub-remap: cafebabe5678..deadbeef1234"

        # 4. scrub-result.json created during flow then deleted at the end
        scrub_result = releases_dir / "scrub-result.json"
        assert not scrub_result.exists()

        # 5. Verify specific subprocess calls
        all_calls = mock_run.call_args_list

        # Branch push: explicit lease refspec
        force_lease_calls = [
            c for c in all_calls
            if c[0][0] == "git" and c[0][1][:1] == ["push"]
            and any(a.startswith("--force-with-lease=refs/heads/main:") for a in c[0][1])
        ]
        assert len(force_lease_calls) == 1
        assert "refs/heads/main:refs/heads/main" in force_lease_calls[0][0][1]

        # Tag push: explicit lease too (plain --force is gone)
        force_tag_calls = [
            c for c in all_calls
            if c[0][0] == "git" and c[0][1][:1] == ["push"]
            and any(a.startswith("--force-with-lease=refs/tags/v1.0.0:") for a in c[0][1])
        ]
        assert len(force_tag_calls) == 1

        # gh release delete + create (via run_gh)
        gh_all = mock_run_gh.call_args_list
        gh_delete_calls = [c for c in gh_all if "delete" in c[0][0]]
        assert len(gh_delete_calls) == 1
        assert "v1.0.0" in gh_delete_calls[0][0][0]

        gh_create_calls = [c for c in gh_all if "create" in c[0][0]]
        assert len(gh_create_calls) == 1
        assert "v1.0.0" in gh_create_calls[0][0][0]


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
    @patch(f"{MOD}.run_gh")
    @patch(f"{MOD}.run")
    def test_resume_skips_safegit(self, mock_run, mock_run_gh, _req_tool,
                                   mock_gen_changelog, _extract_cl, _push_timeout,
                                   _get_branch, _gh_installed, _gh_auth,
                                   _acquire_lock, _release_lock, tmp_path):
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

        # scrub-result.json: HASHES_VALIDATED already done
        saved_head = "deadbeef1234abcd"
        scrub_result = releases_dir / "scrub-result.json"
        scrub_result.write_text(json.dumps({
            "rewrites": {"old_hash_1": "new_hash_1"},
            "tags": [{"refname": "refs/tags/v1.0.0"}],
            "new_head": saved_head,
            "completed_steps": ["HASHES_VALIDATED"],
            # Persisted by the journal-recovery fallback so a resumed run
            # can still commit the files repaired before the interruption.
            "remapped_files": [str(unreleased)],
            # Pre-scrub remote snapshot (lease expectations for pushes)
            "remote_refs": {
                "refs/heads/main": "aaa111",
                "refs/tags/v1.0.0": "bbb222",
            },
        }))

        # run() calls: git/safegit only (gh calls go through run_gh)
        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "git" and args and args[:2] == ["rev-parse", "HEAD"]:
                return saved_head
            return ""

        mock_run.side_effect = run_effect

        # gh calls go through run_gh
        def run_gh_effect(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return '{"body": "notes"}'
            return ""

        mock_run_gh.side_effect = run_gh_effect

        flags = {
            "pattern": "secret",
            "replace": "XXX",
            "reason": "remove secret",
            "entire-history": True,
            "yes": True,
        }

        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
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

        # The commit on resume includes the PERSISTED remapped files and the
        # deleted .validated (the in-memory remap list is empty on resume).
        commit_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "safegit" and (c[0][1] or [])[:1] == ["commit"]
        ]
        assert len(commit_calls) == 1
        commit_args = commit_calls[0][0][1]
        assert str(unreleased) in commit_args
        assert str(validated) in commit_args

        # scrub-result.json cleaned up at the end
        assert not scrub_result.exists()

        # Branch push happened, with the lease expectation from the
        # PERSISTED pre-scrub snapshot
        force_lease_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "git" and len(c[0]) > 1 and c[0][1][:1] == ["push"]
            and "--force-with-lease=refs/heads/main:aaa111" in c[0][1]
        ]
        assert len(force_lease_calls) == 1

        # Tag push happened with its own lease expectation
        force_tag_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "git" and len(c[0]) > 1 and c[0][1][:1] == ["push"]
            and "--force-with-lease=refs/tags/v1.0.0:bbb222" in c[0][1]
        ]
        assert len(force_tag_calls) == 1

        # Release recreated (via run_gh)
        gh_create_calls = [c for c in mock_run_gh.call_args_list if "create" in c[0][0]]
        assert len(gh_create_calls) == 1


# ===========================================================================
# Releasable changes dirs must be remapped too
# ===========================================================================


class TestReleasableDirsRemapped:
    """Monorepo scrub must cover .rlsbl-monorepo/releasables/*/changes/ AND
    per-project .rlsbl/changes/ via the --remap-shas-in globs handed to
    safegit -- the in-history remap replaced rlsbl's own worktree remap, so
    rlsbl itself must leave the JSONL files untouched."""

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    @patch(f"{MOD}.load_workspace")
    def test_releasable_changes_remapped(self, mock_load_ws, mock_run,
                                          _req_tool, _gen_cl, _push_timeout,
                                          _get_branch, _gh_installed, _gh_auth,
                                          _acquire_lock, _release_lock, tmp_path):
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / ".rlsbl-monorepo").mkdir()

        proj_dir = ws_root / "packages" / "alpha"
        proj_changes = proj_dir / ".rlsbl" / "changes"
        proj_changes.mkdir(parents=True)
        _write_entries(str(proj_changes / "unreleased.jsonl"), [
            ChangelogEntry(commits=["old_hash_1"], user_facing=False),
        ])

        rel_changes = ws_root / ".rlsbl-monorepo" / "releasables" / "core" / "changes"
        rel_changes.mkdir(parents=True)
        _write_entries(str(rel_changes / "unreleased.jsonl"), [
            ChangelogEntry(commits=["old_hash_2"], user_facing=False),
        ])

        (proj_dir / ".rlsbl" / "releases").mkdir(parents=True)

        mock_load_ws.return_value = [
            WorkspaceProject({"name": "alpha", "path": "packages/alpha"}),
        ]

        safegit_result = json.dumps({
            "rewrites": {"old_hash_1": "new_hash_1", "old_hash_2": "new_hash_2"},
            "tags": [],
            "old_head": "old_hash_1",
            "new_head": "new_hash_1",
        })

        scrub_args = []

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                scrub_args.extend(args)
                return safegit_result
            return ""

        mock_run.side_effect = run_effect

        flags = {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }
        ctx = _ctx(str(proj_dir), workspace_root=str(ws_root))
        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(flags, ctx=ctx)

        # Both trees covered by the globs handed to safegit: the wildcard
        # releasable glob plus the exact per-project glob.
        globs = [
            scrub_args[i + 1]
            for i, a in enumerate(scrub_args) if a == "--remap-shas-in"
        ]
        assert ".rlsbl-monorepo/releasables/*/changes/*.jsonl" in globs
        assert "packages/alpha/.rlsbl/changes/*.jsonl" in globs

        # rlsbl no longer rewrites the JSONL files itself: safegit's
        # in-history remap already produced consistent worktree content
        # (mocked here, so the files simply stay untouched).
        proj_entries = parse_jsonl(str(proj_changes / "unreleased.jsonl"))
        assert proj_entries[0].commits == ["old_hash_1"]
        rel_entries = parse_jsonl(str(rel_changes / "unreleased.jsonl"))
        assert rel_entries[0].commits == ["old_hash_2"]


# ===========================================================================
# Push mechanics: explicit force-with-lease for BOTH branch and tags
# ===========================================================================


class TestPushMechanics:
    """Post-scrub pushes must use --force-with-lease with EXPLICIT expected
    values captured from the remote BEFORE the rewrite. Bare
    --force-with-lease is broken after a scrub (safegit rewrites the
    remote-tracking refs, and tags have no tracking information), and plain
    --force on tags had no safety at all."""

    OLD_HEAD = "aaaa111122223333444455556666777788889999"
    NEW_HEAD = "bbbb111122223333444455556666777788889999"
    OLD_TAG = "cccc111122223333444455556666777788889999"
    NEW_TAG = "dddd111122223333444455556666777788889999"

    def _run(self, tmp_path, mock_run):
        safegit_result = json.dumps({
            "rewrites": {self.OLD_HEAD: self.NEW_HEAD},
            "tags": [{
                "refname": "refs/tags/v1.0.0",
                "old_sha": self.OLD_TAG, "new_sha": self.NEW_TAG,
                "annotated": True,
            }],
            "old_head": self.OLD_HEAD,
            "new_head": self.NEW_HEAD,
        })
        ls_remote_out = (
            f"{self.OLD_HEAD}\trefs/heads/main\n"
            f"{self.OLD_TAG}\trefs/tags/v1.0.0\n"
            f"{self.OLD_HEAD}\trefs/tags/v1.0.0^{{}}\n"
        )

        calls = []

        def run_effect(cmd, args=None, **kw):
            calls.append((cmd, list(args or []), kw))
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "git" and args and args[0] == "ls-remote":
                return ls_remote_out
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            return ""

        mock_run.side_effect = run_effect

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        flags = {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }
        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        return calls

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_explicit_lease_for_branch_and_tags(self, mock_run, _req_tool,
                                                 _gen_cl, _push_timeout,
                                                 _get_branch, _gh_installed,
                                                 _gh_auth, _acquire_lock,
                                                 _release_lock, tmp_path):
        calls = self._run(tmp_path, mock_run)

        # Remote refs were snapshotted BEFORE the scrub ran
        ls_remote_idx = next(
            i for i, (cmd, args, _) in enumerate(calls)
            if cmd == "git" and args[:1] == ["ls-remote"]
        )
        scrub_idx = next(
            i for i, (cmd, args, _) in enumerate(calls)
            if cmd == "safegit" and args[:1] == ["scrub"]
        )
        assert ls_remote_idx < scrub_idx

        push_calls = [
            (args, kw) for cmd, args, kw in calls
            if cmd == "git" and args[:1] == ["push"]
        ]
        assert len(push_calls) == 2

        # Branch push: explicit lease expecting the PRE-scrub remote value
        branch_args, branch_kw = push_calls[0]
        assert f"--force-with-lease=refs/heads/main:{self.OLD_HEAD}" in branch_args
        assert "refs/heads/main:refs/heads/main" in branch_args
        assert branch_kw.get("env", {}).get("RLSBL_RELEASE_PUSH") == "1"

        # Tag push: explicit lease too (plain --force is gone)
        tag_args, tag_kw = push_calls[1]
        assert f"--force-with-lease=refs/tags/v1.0.0:{self.OLD_TAG}" in tag_args
        assert "refs/tags/v1.0.0:refs/tags/v1.0.0" in tag_args
        assert tag_kw.get("env", {}).get("RLSBL_RELEASE_PUSH") == "1"
        for args, _ in push_calls:
            assert "--force" not in args, "plain --force must never be used"

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_non_tag_refname_in_tags_is_skipped_with_warning(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """A non-refs/tags/ refname in the tags[] list must never be
        force-pushed by the TAG step -- the old guard used removeprefix
        without checking the prefix exists, so 'refs/heads/x' passed."""
        rogue = "refs/heads/feature-x"
        safegit_result = json.dumps({
            "rewrites": {self.OLD_HEAD: self.NEW_HEAD},
            "tags": [
                {
                    "refname": rogue,
                    "old_sha": self.OLD_TAG, "new_sha": self.NEW_TAG,
                    "annotated": False,
                },
                {
                    "refname": "refs/tags/v1.0.0",
                    "old_sha": self.OLD_TAG, "new_sha": self.NEW_TAG,
                    "annotated": True,
                },
            ],
            "old_head": self.OLD_HEAD,
            "new_head": self.NEW_HEAD,
        })
        ls_remote_out = (
            f"{self.OLD_HEAD}\trefs/heads/main\n"
            f"{self.OLD_TAG}\trefs/tags/v1.0.0\n"
        )

        calls = []

        def run_effect(cmd, args=None, **kw):
            calls.append((cmd, list(args or []), kw))
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "git" and args and args[0] == "ls-remote":
                return ls_remote_out
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            return ""

        mock_run.side_effect = run_effect

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        flags = {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }
        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(flags, ctx=_ctx(str(tmp_path)))

        push_calls = [
            args for cmd, args, _ in calls
            if cmd == "git" and args[:1] == ["push"]
        ]
        # Branch push + ONE tag push; the rogue refname is never pushed
        assert len(push_calls) == 2, push_calls
        for args in push_calls[1:]:
            assert not any(rogue in a for a in args), (
                "non-tag refnames must never be force-pushed by the tag step"
            )

        err = capsys.readouterr().err
        assert rogue in err
        assert "skip" in err.lower()


# ===========================================================================
# Committed audit archive (whitelisted schema -- never re-leaks scrubbed data)
# ===========================================================================


class TestScrubArchiveWhitelist:
    """The archived scrub record must contain ONLY whitelisted fields:
    commit SHAs, tag refnames, reason, mode, step list. Never the pattern,
    replacement string, file path, or any matched content."""

    def test_build_archive_strips_everything_not_whitelisted(self):
        from rlsbl.commands.release_scrub import _build_scrub_archive

        scrub_data = {
            "version": 1,
            "dry_run": False,
            # Hostile/leaky fields that must NOT survive archiving:
            "pattern": "SECRETPATTERN",
            "replace": "REPLACEMENTXXX",
            "file": "path/to/leaked.env",
            "scope": "*.env",
            "args": ["scrub", "match", "--pattern", "SECRETPATTERN"],
            "blobs_replaced": 2,
            "messages_modified": 0,
            "tags_rewritten": 1,
            # Whitelisted content:
            "rewrites": {"aaa": "bbb"},
            "tags": [{
                "refname": "refs/tags/v1.0.0",
                "old_sha": "ccc", "new_sha": "ddd", "annotated": True,
                "message": "tag message that could contain SECRETPATTERN",
            }],
            "commits_rewritten": 2,
            "old_head": "aaa",
            "new_head": "bbb",
            "completed_steps": ["JSONL_REMAPPED"],
        }

        archive = _build_scrub_archive(scrub_data, "match", "clean leak")

        assert set(archive.keys()) == {
            "schema_version", "mode", "reason", "old_head", "new_head",
            "rewrites", "tags", "commits_rewritten", "completed_steps",
        }
        assert archive["mode"] == "match"
        assert archive["reason"] == "clean leak"
        assert archive["rewrites"] == {"aaa": "bbb"}
        assert archive["tags"] == [{
            "refname": "refs/tags/v1.0.0",
            "old_sha": "ccc", "new_sha": "ddd", "annotated": True,
        }]

        serialized = json.dumps(archive)
        assert "SECRETPATTERN" not in serialized
        assert "REPLACEMENTXXX" not in serialized
        assert "leaked.env" not in serialized


class TestScrubArchiveCommitted:
    """On success the scrub state is archived (committed), not deleted."""

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_archive_written_and_committed(self, mock_run, _req_tool, _gen_cl,
                                            _push_timeout, _get_branch,
                                            _gh_installed, _gh_auth,
                                            _acquire_lock, _release_lock, tmp_path):
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        _write_entries(str(changes_dir / "unreleased.jsonl"), [
            ChangelogEntry(commits=["old_hash_1"], user_facing=False),
        ])

        safegit_result = json.dumps({
            "rewrites": {"old_hash_1": "new_hash_1"},
            "tags": [],
            "old_head": "cafebabe567812345678",
            "new_head": "deadbeef123412345678",
        })

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            return ""

        mock_run.side_effect = run_effect

        flags = {
            "pattern": "SECRETPATTERN", "replace": "REPLACEMENTXXX",
            "reason": "clean leak", "entire-history": True, "yes": True,
        }
        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(flags, ctx=_ctx(str(tmp_path)))

        # Archive exists under .rlsbl/scrubs/scrub-<newhead12>.json
        archive_path = tmp_path / ".rlsbl" / "scrubs" / "scrub-deadbeef1234.json"
        assert archive_path.exists()
        archive = json.loads(archive_path.read_text())
        assert archive["mode"] == "match"
        assert archive["rewrites"] == {"old_hash_1": "new_hash_1"}
        text = archive_path.read_text()
        assert "SECRETPATTERN" not in text
        assert "REPLACEMENTXXX" not in text

        # The archive is part of the scrub commit
        commit_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "safegit" and (c[0][1] or [])[:1] == ["commit"]
        ]
        assert len(commit_calls) == 1
        assert str(archive_path) in commit_calls[0][0][1]

        # scrub-result.json (working state) is gone
        assert not (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()


# ===========================================================================
# Post-remap validation gate
# ===========================================================================


class TestPostRemapValidationGate:
    """After JSONL remap and BEFORE the commit step, every hash in every
    changelog dir must resolve. Otherwise: loud abort with resume intact."""

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_unresolvable_hash_aborts_before_commit(self, mock_run, _req_tool,
                                                     mock_gen_changelog,
                                                     _push_timeout, _get_branch,
                                                     _acquire_lock, _release_lock,
                                                     tmp_path, capsys):
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)

        bogus = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        unreleased = changes_dir / "unreleased.jsonl"
        _write_entries(str(unreleased), [
            ChangelogEntry(commits=[bogus], user_facing=False),
        ])

        safegit_result = json.dumps({
            "rewrites": {"old_hash_1": "new_hash_1"},
            "tags": [],
            "old_head": "old_hash_1",
            "new_head": "new_hash_1",
        })
        mock_run.side_effect = [
            "safegit 0.22.0",  # safegit --version
            "",                # git ls-remote origin (pre-scrub snapshot)
            safegit_result,    # safegit scrub
            # git rev-parse --git-dir (journal lookup; no journal there)
            str(tmp_path / "no-such-gitdir"),
        ]

        flags = {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }

        failures = {str(unreleased): [bogus]}
        with patch(f"{MOD}.validate_all_hashes_resolve", return_value=failures):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(flags, ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

        # Aborted BEFORE commit: no safegit commit call happened
        for c in mock_run.call_args_list:
            if c[0][0] == "safegit":
                assert "commit" not in (c[0][1] or [])

        # Resume state intact
        assert (releases_dir / "scrub-result.json").exists()

        err = capsys.readouterr().err
        assert bogus in err
        assert "resolve" in err.lower()

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_gate_passes_when_all_resolve(self, mock_run, _req_tool,
                                           _gen_cl, _push_timeout, _get_branch,
                                           _gh_installed, _gh_auth,
                                           _acquire_lock, _release_lock, tmp_path):
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        safegit_result = json.dumps({
            "rewrites": {"old": "new"}, "tags": [],
            "old_head": "old", "new_head": "new",
        })

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            return ""

        mock_run.side_effect = run_effect

        flags = {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }
        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}) as mock_gate:
            run_cmd(flags, ctx=_ctx(str(tmp_path)))
        mock_gate.assert_called_once()
        # Flow completed: state cleared
        assert not (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()


# ===========================================================================
# Journal recovery: the persisted rewrite journal repairs dangling hashes
# ===========================================================================


OLD_SHA = "1" * 40
NEW_SHA = "2" * 40


def _journal_records(rid, commit_map, *, complete=True, op="scrub-match"):
    recs = [
        {"phase": "start", "id": rid, "op": op, "reason": "r",
         "created_at": "2026-07-03T00:00:00Z", "old_head": OLD_SHA,
         "commit_map": commit_map, "pre_rewrite_remotes": {}},
        {"phase": "refs", "id": rid,
         "created_at": "2026-07-03T00:00:01Z", "tag_rewrites": []},
    ]
    if complete:
        recs.append({"phase": "complete", "id": rid,
                     "created_at": "2026-07-03T00:00:02Z",
                     "new_head": NEW_SHA, "cleanup_ok": True,
                     "cleanup_errors": []})
    return recs


class TestJournalRecovery:
    """When validation finds dangling hashes AND safegit's persisted rewrite
    journal (.git/safegit/rewrite-maps.jsonl) can fix them, the retained
    remap utility repairs the working-tree JSONL -- the explicit fallback
    for a scrub that ran without --remap-shas-in (older orchestrator, or a
    direct orchestrated scrub) or was interrupted before rlsbl's steps."""

    def _setup(self, tmp_path, journal_groups):
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased = changes_dir / "unreleased.jsonl"
        _write_entries(str(unreleased), [
            ChangelogEntry(commits=[OLD_SHA], user_facing=False),
        ])

        gitdir = tmp_path / "gitdir"
        (gitdir / "safegit").mkdir(parents=True)
        if journal_groups is not None:
            lines = [json.dumps(rec) for group in journal_groups for rec in group]
            (gitdir / "safegit" / "rewrite-maps.jsonl").write_text(
                "\n".join(lines) + "\n"
            )

        safegit_result = json.dumps({
            "rewrites": {OLD_SHA: NEW_SHA}, "tags": [],
            "old_head": OLD_SHA, "new_head": NEW_SHA,
        })

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            if cmd == "git" and args == ["rev-parse", "--git-dir"]:
                return str(gitdir)
            return ""

        return unreleased, run_effect

    def _flags(self):
        return {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_journal_fixes_dangling_hashes(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        unreleased, run_effect = self._setup(
            tmp_path, [_journal_records("id-1", {OLD_SHA: NEW_SHA})],
        )
        mock_run.side_effect = run_effect

        # First validation: dangling; after the journal repair: clean.
        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            side_effect=[{str(unreleased): [OLD_SHA]}, {}],
        ):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        entries = parse_jsonl(str(unreleased))
        assert entries[0].commits == [NEW_SHA]

        # Loud about what it did
        combined = capsys.readouterr()
        combined = combined.out + combined.err
        assert "rewrite journal" in combined
        assert str(unreleased) in combined

        # The repaired file is part of the scrub commit
        commit_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "safegit" and (c[0][1] or [])[:1] == ["commit"]
        ]
        assert len(commit_calls) == 1
        assert str(unreleased) in commit_calls[0][0][1]

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_crashed_rewrite_group_is_surfaced_loudly(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """A start-without-complete group means the rewrite CRASHED. The
        start record was persisted before any refs moved, so its map is
        still used -- but the crash must be surfaced loudly."""
        unreleased, run_effect = self._setup(
            tmp_path,
            [_journal_records("id-1", {OLD_SHA: NEW_SHA}, complete=False)],
        )
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            side_effect=[{str(unreleased): [OLD_SHA]}, {}],
        ):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        assert parse_jsonl(str(unreleased))[0].commits == [NEW_SHA]
        err = capsys.readouterr().err
        assert "CRASHED" in err
        assert "complete" in err

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_last_journal_group_wins(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock, tmp_path,
    ):
        """Multiple rewrite ids in the journal: the LAST start record's
        group provides the map."""
        stale_new = "3" * 40
        unreleased, run_effect = self._setup(
            tmp_path,
            [
                _journal_records("id-old", {OLD_SHA: stale_new}),
                _journal_records("id-new", {OLD_SHA: NEW_SHA}),
            ],
        )
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            side_effect=[{str(unreleased): [OLD_SHA]}, {}],
        ):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        assert parse_jsonl(str(unreleased))[0].commits == [NEW_SHA]

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_no_journal_hard_error(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _acquire_lock, _release_lock, tmp_path, capsys,
    ):
        unreleased, run_effect = self._setup(tmp_path, None)
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            return_value={str(unreleased): [OLD_SHA]},
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

        # Untouched file, resume intact, hash surfaced
        assert parse_jsonl(str(unreleased))[0].commits == [OLD_SHA]
        assert (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()
        assert OLD_SHA in capsys.readouterr().err

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_unfixable_dangles_hard_error_without_mutation(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _acquire_lock, _release_lock, tmp_path, capsys,
    ):
        """A journal whose map cannot fix ANY dangling hash must not touch
        any file -- straight to the hard error."""
        other = "f" * 40
        unreleased, run_effect = self._setup(
            tmp_path, [_journal_records("id-1", {other: NEW_SHA})],
        )
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            return_value={str(unreleased): [OLD_SHA]},
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1
        assert parse_jsonl(str(unreleased))[0].commits == [OLD_SHA]
        assert OLD_SHA in capsys.readouterr().err

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_ambiguous_abbreviated_hash_not_fixable(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _acquire_lock, _release_lock, tmp_path, capsys,
    ):
        """An abbreviated hash matching TWO journal keys is ambiguous: the
        recovery must refuse to guess and hard-error with the hash named."""
        ambiguous = "abcd12"
        key_a = "abcd12" + "a" * 34
        key_b = "abcd12" + "b" * 34

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased = changes_dir / "unreleased.jsonl"
        _write_entries(str(unreleased), [
            ChangelogEntry(commits=[ambiguous], user_facing=False),
        ])

        gitdir = tmp_path / "gitdir"
        (gitdir / "safegit").mkdir(parents=True)
        lines = [
            json.dumps(rec)
            for rec in _journal_records(
                "id-1", {key_a: "3" * 40, key_b: "4" * 40},
            )
        ]
        (gitdir / "safegit" / "rewrite-maps.jsonl").write_text(
            "\n".join(lines) + "\n"
        )

        safegit_result = json.dumps({
            "rewrites": {key_a: "3" * 40, key_b: "4" * 40}, "tags": [],
            "old_head": key_a, "new_head": "3" * 40,
        })

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            if cmd == "git" and args == ["rev-parse", "--git-dir"]:
                return str(gitdir)
            return ""

        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            return_value={str(unreleased): [ambiguous]},
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1
        assert parse_jsonl(str(unreleased))[0].commits == [ambiguous]
        assert ambiguous in capsys.readouterr().err


# ===========================================================================
# pre_rewrite_remotes: informational cross-check only, never the lease source
# ===========================================================================


class TestPreRewriteRemotesCrossCheck:
    """safegit 0.22.0 reports pre_rewrite_remotes -- the LOCAL
    remote-tracking snapshot taken before updateRefs. It may be stale (no
    fetch since the last push), so rlsbl's ls-remote snapshot of the ACTUAL
    remote stays the --force-with-lease authority. When the two disagree,
    an informational warning is printed."""

    OLD_HEAD = "aaaa111122223333444455556666777788889999"
    NEW_HEAD = "bbbb111122223333444455556666777788889999"
    STALE = "cccc111122223333444455556666777788889999"

    def _flags(self):
        return {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }

    def _run(self, tmp_path, mock_run, tracking_sha):
        safegit_result = json.dumps({
            "rewrites": {self.OLD_HEAD: self.NEW_HEAD}, "tags": [],
            "old_head": self.OLD_HEAD, "new_head": self.NEW_HEAD,
            "pre_rewrite_remotes": {
                "refs/remotes/origin/main": tracking_sha,
            },
        })
        ls_remote_out = f"{self.OLD_HEAD}\trefs/heads/main\n"

        calls = []

        def run_effect(cmd, args=None, **kw):
            calls.append((cmd, list(args or []), kw))
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "git" and args and args[0] == "ls-remote":
                return ls_remote_out
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            return ""

        mock_run.side_effect = run_effect

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))
        return calls

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_disagreement_warns_but_ls_remote_stays_lease_authority(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        calls = self._run(tmp_path, mock_run, tracking_sha=self.STALE)

        err = capsys.readouterr().err
        assert "pre_rewrite_remotes" in err
        assert "refs/heads/main" in err
        assert self.STALE[:12] in err

        # Lease expectation comes from the ls-remote snapshot (the ACTUAL
        # remote), never from the possibly-stale local tracking ref.
        push_calls = [
            args for cmd, args, _ in calls
            if cmd == "git" and args[:1] == ["push"]
        ]
        assert len(push_calls) == 1
        assert (
            f"--force-with-lease=refs/heads/main:{self.OLD_HEAD}"
            in push_calls[0]
        )
        assert not any(self.STALE in a for a in push_calls[0])

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_agreement_stays_silent(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        self._run(tmp_path, mock_run, tracking_sha=self.OLD_HEAD)
        assert "pre_rewrite_remotes" not in capsys.readouterr().err


# ===========================================================================
# cleanup_ok consumption: validation depends on old objects being pruned
# ===========================================================================


class TestCleanupOkGate:
    """safegit 0.22.0 reports cleanup_ok/cleanup_errors. rlsbl's hash
    validation gate silently DEPENDS on old objects being pruned (dangling
    old hashes are only detectable because the objects are gone), so
    cleanup_ok=false is a hard error BEFORE the commit step -- with the
    cleanup errors and remediation printed, and resume state intact."""

    OLD = "a" * 40
    NEW = "b" * 40

    def _flags(self):
        return {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }

    def _safegit_result(self):
        return json.dumps({
            "rewrites": {self.OLD: self.NEW}, "tags": [],
            "old_head": self.OLD, "new_head": self.NEW,
            "cleanup_ok": False,
            "cleanup_errors": ["expiring reflogs: exit status 1"],
        })

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_cleanup_failed_aborts_before_commit(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _acquire_lock, _release_lock, tmp_path, capsys,
    ):
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        safegit_result = self._safegit_result()

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            # git cat-file -e <old-sha> succeeds: the object still exists,
            # so the prune really is incomplete.
            return ""

        mock_run.side_effect = run_effect

        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}) as gate:
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

        # The gate fires BEFORE validation (which would falsely pass while
        # old objects still resolve) and BEFORE any commit.
        gate.assert_not_called()
        for c in mock_run.call_args_list:
            if c[0][0] == "safegit":
                assert "commit" not in (c[0][1] or [])

        err = capsys.readouterr().err
        assert "cleanup_ok" in err
        assert "expiring reflogs: exit status 1" in err
        assert "prune" in err

        # Resume state intact
        assert (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_cleanup_failed_but_objects_pruned_proceeds(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """Remediation path: the recorded cleanup_ok=false is stale because
        the operator completed the prune since. The gate re-checks REALITY
        (no pre-rewrite object resolves any more) and proceeds."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        safegit_result = self._safegit_result()

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            if cmd == "git" and args and args[:2] == ["cat-file", "-e"]:
                raise Exception("object missing")
            return ""

        mock_run.side_effect = run_effect

        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        out = capsys.readouterr().out
        assert "prune has been completed" in out
        # Flow completed: state cleared
        assert not (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.generate_changelog")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_identity_rewrite_does_not_deadlock_on_old_head(
        self, mock_run, _req_tool, _gen_cl, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """Tag-annotation-only rewrite: safegit 0.22.0 emits an ALL-IDENTITY
        commit map (every commit maps to itself) and old_head == new_head.
        The head object legitimately exists forever, so with
        cleanup_ok=false the gate must not demand a prune of old_head that
        can never succeed -- that would block the scrub permanently."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        head = "e" * 40
        other = "d" * 40
        safegit_result = json.dumps({
            # All-identity map: nothing to prune, ever.
            "rewrites": {head: head, other: other},
            "tags": [{
                "refname": "refs/tags/v1.0.0",
                "old_sha": "1" * 40, "new_sha": "2" * 40, "annotated": True,
            }],
            "old_head": head, "new_head": head,
            "cleanup_ok": False,
            "cleanup_errors": ["expiring reflogs: exit status 1"],
        })

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            # git cat-file -e succeeds for EVERY sha: head and the identity
            # commits all exist (and always will).
            return ""

        mock_run.side_effect = run_effect

        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        out = capsys.readouterr().out
        assert "Continuing" in out
        # Flow completed: state cleared, no permanent block.
        assert not (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()


# ===========================================================================
# CHANGELOG step: regenerate-and-assert-unchanged
# ===========================================================================


class TestChangelogRegenerateUnchanged:
    """With in-history remap, HEAD's JSONL is already consistent, so the
    CHANGELOG step regenerates and asserts the output is byte-identical to
    disk. A diff is a hard error (something else is wrong): the diff is
    shown, the on-disk originals are restored, and resume state is kept."""

    def _flags(self):
        return {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }

    def _run_effect(self):
        safegit_result = json.dumps({
            "rewrites": {"1" * 40: "2" * 40}, "tags": [],
            "old_head": "1" * 40, "new_head": "2" * 40,
        })

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return safegit_result
            return ""

        return run_effect

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_diff_is_hard_error_with_diff_shown_and_originals_restored(
        self, mock_run, _req_tool, _push_timeout, _get_branch,
        _acquire_lock, _release_lock, tmp_path, capsys,
    ):
        # Real generation (NOT mocked): the on-disk stub cannot match the
        # generated content, so the step must abort.
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        _write_entries(str(changes_dir / "unreleased.jsonl"), [
            ChangelogEntry(commits=["2" * 40], user_facing=True,
                           description="Thing", type="feature"),
        ])
        stale_content = "# Hand-written stale changelog\n"
        (tmp_path / "CHANGELOG.md").write_text(stale_content)

        mock_run.side_effect = self._run_effect()

        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

        # On-disk original restored byte-for-byte
        assert (tmp_path / "CHANGELOG.md").read_text() == stale_content

        # Diff shown, resume intact, no commit happened
        err = capsys.readouterr().err
        assert "differs" in err
        assert "Hand-written stale changelog" in err
        assert "regenerated" in err
        # Concrete drift remediation named: regenerate, commit, re-run.
        assert "rlsbl changelog generate" in err
        assert "rlsbl commit" in err
        assert (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()
        for c in mock_run.call_args_list:
            if c[0][0] == "safegit":
                assert "commit" not in (c[0][1] or [])

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.check_gh_auth", return_value=False)
    @patch(f"{MOD}.check_gh_installed", return_value=False)
    @patch(f"{MOD}.get_current_branch", return_value="main")
    @patch(f"{MOD}.get_push_timeout", return_value=120)
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_identical_regeneration_passes_and_commits_nothing_extra(
        self, mock_run, _req_tool, _push_timeout, _get_branch,
        _gh_installed, _gh_auth, _acquire_lock, _release_lock, tmp_path,
    ):
        from rlsbl.changelog.generate import generate_changelog as real_gen

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        _write_entries(str(changes_dir / "unreleased.jsonl"), [
            ChangelogEntry(commits=["2" * 40], user_facing=True,
                           description="Thing", type="feature"),
        ])
        # Pre-generate so disk matches regeneration exactly.
        real_gen(str(tmp_path))
        original = (tmp_path / "CHANGELOG.md").read_text()

        mock_run.side_effect = self._run_effect()

        with patch(f"{MOD}.validate_all_hashes_resolve", return_value={}):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        # Unchanged on disk; CHANGELOG.md NOT part of the scrub commit
        assert (tmp_path / "CHANGELOG.md").read_text() == original
        commit_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == "safegit" and (c[0][1] or [])[:1] == ["commit"]
        ]
        assert len(commit_calls) == 1
        assert str(tmp_path / "CHANGELOG.md") not in commit_calls[0][0][1]


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
            "safegit 0.22.0",  # safegit --version
            "",                # git ls-remote origin (pre-scrub snapshot)
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
        assert mock_run.call_count == 3  # version check + ls-remote snapshot + scrub


# ===========================================================================
# No-match scrubs still validate changelog hashes (and repair via journal)
# ===========================================================================


class TestNoMatchValidatesHashes:
    """A scrub that finds nothing to rewrite must NOT skip the changelog
    hash validation: dangling hashes left behind by a prior crashed or
    direct scrub would otherwise go unnoticed until a later check, when the
    journal-based repair is unreachable. On a no-match run there is no
    rewrite and therefore no force-push -- just validate, repair from the
    rewrite journal when possible, and commit the repairs."""

    REPAIR_MSG = "scrub: repair changelog hashes from rewrite journal"

    def _flags(self):
        return {
            "pattern": "secret", "replace": "XXX", "reason": "r",
            "entire-history": True, "yes": True,
        }

    def _setup(self, tmp_path, journal_groups, *, empty_stdout=False):
        """Project with one dangling-hash entry, a rewrite journal, and a
        safegit scrub that finds NOTHING to rewrite (either empty stdout or
        a JSON result with empty rewrites)."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased = changes_dir / "unreleased.jsonl"
        _write_entries(str(unreleased), [
            ChangelogEntry(commits=[OLD_SHA], user_facing=False),
        ])

        gitdir = tmp_path / "gitdir"
        (gitdir / "safegit").mkdir(parents=True)
        if journal_groups is not None:
            lines = [json.dumps(rec) for group in journal_groups for rec in group]
            (gitdir / "safegit" / "rewrite-maps.jsonl").write_text(
                "\n".join(lines) + "\n"
            )

        no_match_result = "" if empty_stdout else json.dumps(
            {"rewrites": {}, "tags": []}
        )

        def run_effect(cmd, args=None, **kw):
            if cmd == "safegit" and args == ["--version"]:
                return "safegit 0.22.0"
            if cmd == "safegit" and args and args[0] == "scrub":
                return no_match_result
            if cmd == "git" and args == ["rev-parse", "--git-dir"]:
                return str(gitdir)
            return ""

        return unreleased, run_effect

    def _commit_calls(self, mock_run):
        return [
            c for c in mock_run.call_args_list
            if c[0][0] == "safegit" and (c[0][1] or [])[:1] == ["commit"]
        ]

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_no_match_clean_exits_happily_after_validating(
        self, mock_run, _req_tool, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """No matches + all hashes resolve: still 'nothing to do', but the
        validation must actually have run."""
        unreleased, run_effect = self._setup(tmp_path, None)
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve", return_value={},
        ) as gate:
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        gate.assert_called_once()
        assert "No matches found" in capsys.readouterr().out
        assert self._commit_calls(mock_run) == []
        assert not (tmp_path / ".rlsbl" / "releases" / "scrub-result.json").exists()

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_no_match_dangling_journal_fixable_repairs_and_commits(
        self, mock_run, _req_tool, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """No matches + dangling hashes the journal can fix: repair the
        JSONL, commit the repaired files (no force-push -- no rewrite
        happened), and exit cleanly."""
        unreleased, run_effect = self._setup(
            tmp_path, [_journal_records("id-1", {OLD_SHA: NEW_SHA})],
        )
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            side_effect=[{str(unreleased): [OLD_SHA]}, {}],
        ):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        # Repaired on disk
        assert parse_jsonl(str(unreleased))[0].commits == [NEW_SHA]

        # Committed with a clear message; no pushes at all
        commit_calls = self._commit_calls(mock_run)
        assert len(commit_calls) == 1
        commit_args = commit_calls[0][0][1]
        assert self.REPAIR_MSG in commit_args
        assert str(unreleased) in commit_args
        for c in mock_run.call_args_list:
            if c[0][0] == "git":
                assert (c[0][1] or [])[:1] != ["push"], \
                    "a no-match run must never push"

        combined = capsys.readouterr()
        combined = combined.out + combined.err
        assert "rewrite journal" in combined
        assert "No matches found" in combined

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_no_match_dangling_unfixable_hard_error(
        self, mock_run, _req_tool, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """No matches + dangling hashes the journal can NOT fix: hard error
        naming the file and the hashes that remain dangling."""
        other = "f" * 40
        unreleased, run_effect = self._setup(
            tmp_path, [_journal_records("id-1", {other: NEW_SHA})],
        )
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            return_value={str(unreleased): [OLD_SHA]},
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))
        assert exc_info.value.code == 1

        # File untouched, nothing committed, hash and file named
        assert parse_jsonl(str(unreleased))[0].commits == [OLD_SHA]
        assert self._commit_calls(mock_run) == []
        err = capsys.readouterr().err
        assert OLD_SHA in err
        assert str(unreleased) in err

    @patch(f"{MOD}.release_lock")
    @patch(f"{MOD}.acquire_lock")
    @patch(f"{MOD}.require_tool")
    @patch(f"{MOD}.run")
    def test_empty_stdout_path_also_validates_and_repairs(
        self, mock_run, _req_tool, _acquire_lock, _release_lock,
        tmp_path, capsys,
    ):
        """safegit's OTHER no-match shape -- empty stdout instead of a JSON
        result with empty rewrites -- must run the same validation and
        journal repair."""
        unreleased, run_effect = self._setup(
            tmp_path, [_journal_records("id-1", {OLD_SHA: NEW_SHA})],
            empty_stdout=True,
        )
        mock_run.side_effect = run_effect

        with patch(
            f"{MOD}.validate_all_hashes_resolve",
            side_effect=[{str(unreleased): [OLD_SHA]}, {}],
        ):
            run_cmd(self._flags(), ctx=_ctx(str(tmp_path)))

        assert parse_jsonl(str(unreleased))[0].commits == [NEW_SHA]
        commit_calls = self._commit_calls(mock_run)
        assert len(commit_calls) == 1
        assert self.REPAIR_MSG in commit_calls[0][0][1]
        assert "No matches found" in capsys.readouterr().out


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
    @patch(f"{MOD}.run_gh")
    @patch(f"{MOD}.run")
    @patch(f"{MOD}.load_workspace")
    def test_monorepo_tag_correct_project(self, mock_load_ws, mock_run,
                                           mock_run_gh, _req_tool,
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
            "safegit 0.22.0",       # safegit --version
            "",                      # git ls-remote origin (snapshot)
            safegit_result,          # safegit scrub
            "",                      # safegit commit (archive)
            "",                      # git rev-parse (branch target)
            "",                      # git push (branch, lease)
            "",                      # git push (alpha@v1.0.0, lease)
            "",                      # git push (beta@v1.0.0, lease)
        ]

        # gh calls go through run_gh
        def run_gh_effect(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return '{"body": "old"}'
            return ""

        mock_run_gh.side_effect = run_gh_effect

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

        # Verify the create calls used the correct notes (via run_gh)
        gh_create_calls = [c for c in mock_run_gh.call_args_list if "create" in c[0][0]]
        assert len(gh_create_calls) == 2

        # First create: alpha@v1.0.0 with alpha notes
        alpha_create = gh_create_calls[0]
        assert "alpha@v1.0.0" in alpha_create[0][0]

        # Second create: beta@v1.0.0 with beta notes
        beta_create = gh_create_calls[1]
        assert "beta@v1.0.0" in beta_create[0][0]


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
    @patch(f"{MOD}.run_gh")
    @patch(f"{MOD}.run")
    @patch(f"{MOD}.load_workspace")
    def test_standalone_tag_no_prefix(self, mock_load_ws, mock_run, mock_run_gh,
                                       _req_tool, mock_gen_changelog,
                                       _push_timeout, _get_branch,
                                       _gh_installed, _gh_auth,
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
            "safegit 0.22.0",       # safegit --version
            "",                      # git ls-remote origin (snapshot)
            safegit_result,          # safegit scrub
            "",                      # safegit commit (archive)
            "",                      # git rev-parse (branch target)
            "",                      # git push (branch, lease)
            "",                      # git push (v1.0.0, lease)
        ]

        # gh calls go through run_gh
        def run_gh_effect(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return '{"body": "old"}'
            return ""

        mock_run_gh.side_effect = run_gh_effect

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

        # Verify the release was created with root changelog notes (via run_gh)
        gh_create_calls = [c for c in mock_run_gh.call_args_list if "create" in c[0][0]]
        assert len(gh_create_calls) == 1
        assert "v1.0.0" in gh_create_calls[0][0][0]
