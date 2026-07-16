"""Integration tests for rlsbl.commands.init_cmd (scaffold command)."""

import hashlib
import json
import os
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import make_ctx
from rlsbl.context import ProjectContext
from rlsbl.errors import ConfigError

from rlsbl.commands.init_cmd import (
    BASES_DIR,
    USER_OWNED,
    _load_base,
    _save_base,
    _three_way_merge,
    file_hash,
    process_mappings,
    process_template,
    run_cmd,
)
from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


class TestProcessTemplate:
    """Tests for template variable replacement."""

    def test_replaces_known_variables(self):
        content, unreplaced = process_template(
            "Hello {{name}}, version {{version}}!",
            {"name": "my-pkg", "version": "1.0.0"},
        )
        assert content == "Hello my-pkg, version 1.0.0!"
        assert unreplaced == []

    def test_leaves_unknown_variables_and_reports_them(self):
        content, unreplaced = process_template(
            "{{name}} uses {{unknownVar}}",
            {"name": "my-pkg"},
        )
        assert content == "my-pkg uses {{unknownVar}}"
        assert unreplaced == ["unknownVar"]

    def test_replaces_multiple_occurrences(self):
        content, _ = process_template(
            "{{name}} is {{name}}",
            {"name": "x"},
        )
        assert content == "x is x"

    def test_no_variables_returns_unchanged(self):
        content, unreplaced = process_template("plain text", {})
        assert content == "plain text"
        assert unreplaced == []

    def test_required_vars_present_resolves_normally(self):
        content, unreplaced = process_template(
            "Hello {{name}}, year {{year}}",
            {"name": "pkg", "year": "2026"},
            required_vars={"name", "year"},
        )
        assert content == "Hello pkg, year 2026"
        assert unreplaced == []

    def test_required_vars_missing_raises_config_error(self):
        with pytest.raises(ConfigError, match="name"):
            process_template(
                "Hello {{name}} {{other}}",
                {"other": "val"},
                required_vars={"name"},
            )

    def test_required_vars_missing_includes_template_path(self):
        with pytest.raises(ConfigError, match="mytemplate.tpl"):
            process_template(
                "{{author}} wrote this",
                {},
                template_path="mytemplate.tpl",
                required_vars={"author"},
            )

    def test_required_vars_none_preserves_existing_behavior(self):
        content, unreplaced = process_template(
            "{{missing}} var",
            {},
            required_vars=None,
        )
        assert content == "{{missing}} var"
        assert unreplaced == ["missing"]

    def test_required_vars_subset_of_unreplaced(self):
        """Only variables in required_vars raise; other unreplaced are fine."""
        with pytest.raises(ConfigError, match="critical") as exc_info:
            process_template(
                "{{critical}} and {{optional}}",
                {},
                required_vars={"critical"},
            )
        assert "optional" not in str(exc_info.value)

    def test_required_vars_not_in_template_does_not_error(self):
        """required_vars that don't appear in the template are silently ignored."""
        content, unreplaced = process_template(
            "Hello {{name}}",
            {"name": "pkg"},
            required_vars={"name", "registryUrl"},
        )
        assert content == "Hello pkg"
        assert unreplaced == []


class TestBaseStorage:
    """Tests for _save_base / _load_base helpers."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_save_and_load_roundtrip(self):
        _save_base("foo/bar.txt", "hello world\n")
        assert _load_base("foo/bar.txt") == "hello world\n"

    def test_load_missing_returns_none(self):
        assert _load_base("nonexistent.txt") is None

    def test_save_creates_parent_dirs(self):
        _save_base("a/b/c.txt", "content")
        base_path = os.path.join(BASES_DIR, "a", "b", "c.txt")
        assert os.path.exists(base_path)


class TestThreeWayMerge:
    """Tests for _three_way_merge using git merge-file."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        # git merge-file needs to be able to run; init a repo for safety
        os.system("git init -q .")

    def test_clean_merge_no_conflicts(self):
        # Changes must be non-adjacent so git merge-file resolves them cleanly
        base = "line1\nline2\nline3\nline4\nline5\n"
        ours = "line1\nline2 modified by user\nline3\nline4\nline5\n"
        theirs = "line1\nline2\nline3\nline4 modified by template\nline5\n"
        merged, has_conflicts = _three_way_merge(ours, base, theirs)
        assert not has_conflicts
        assert "line2 modified by user" in merged
        assert "line4 modified by template" in merged

    def test_conflict_detected(self):
        base = "line1\nline2\nline3\n"
        ours = "line1\nline2 user version\nline3\n"
        theirs = "line1\nline2 template version\nline3\n"
        merged, has_conflicts = _three_way_merge(ours, base, theirs)
        assert has_conflicts
        assert "<<<<<<<" in merged
        assert "=======" in merged
        assert ">>>>>>>" in merged

    def test_identical_changes_no_conflict(self):
        base = "line1\nline2\nline3\n"
        ours = "line1\nline2 same change\nline3\n"
        theirs = "line1\nline2 same change\nline3\n"
        merged, has_conflicts = _three_way_merge(ours, base, theirs)
        assert not has_conflicts
        assert merged == "line1\nline2 same change\nline3\n"

    def test_temp_files_cleaned_up(self):
        base = "a\n"
        ours = "a\n"
        theirs = "a\n"
        _three_way_merge(ours, base, theirs)
        # No leftover .ours/.base/.theirs files
        leftover = [f for f in os.listdir(".") if f.endswith((".ours", ".base", ".theirs"))]
        assert leftover == []


class TestScaffold:
    """Integration tests for the scaffold (init) command."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        # git init so git merge-file works
        os.system("git init -q .")
        # Create minimal package.json so npm registry is detected
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "0.1.0"}, f)

    def _run_scaffold(self):
        """Run scaffold for npm with stdout suppressed."""
        flags = {}
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], flags, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

    def test_creates_changelog(self):
        self._run_scaffold()
        assert os.path.exists("CHANGELOG.md")
        with open("CHANGELOG.md") as f:
            assert "0.1.0" in f.read()

    def test_creates_gitignore(self):
        self._run_scaffold()
        assert os.path.exists(".gitignore")

    def test_creates_ci_workflow(self):
        self._run_scaffold()
        assert os.path.exists(".github/workflows/ci.yml")

    def test_creates_publish_workflow(self):
        self._run_scaffold()
        assert os.path.exists(".github/workflows/publish.yml")

    def test_template_variable_replacement(self):
        """Verify {{name}} and {{version}} are replaced in generated files."""
        self._run_scaffold()
        with open("CHANGELOG.md") as f:
            content = f.read()
        assert "0.1.0" in content
        assert "{{version}}" not in content

    def test_unreplaced_variables_raise_error(self):
        """Scaffold with a template containing unknown vars should raise ConfigError."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write("Hello {{name}} from {{planet}}")

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        with pytest.raises(ConfigError, match="planet"):
            process_mappings(
                tpl_dir, mappings, {"name": "test-pkg"},
            )

    # -- Base storage tests --

    def test_initial_scaffold_saves_bases(self):
        """After initial scaffold, base files should exist in .rlsbl/bases/."""
        self._run_scaffold()
        # CI workflow should have a base stored
        ci_base = _load_base(".github/workflows/ci.yml")
        assert ci_base is not None
        assert len(ci_base) > 0

    def test_bases_match_generated_files(self):
        """Stored bases should match the rendered template content (identical to file on first scaffold)."""
        self._run_scaffold()
        ci_path = ".github/workflows/ci.yml"
        with open(ci_path) as f:
            file_content = f.read()
        base_content = _load_base(ci_path)
        assert file_content == base_content

    # -- Three-way merge integration tests --

    def test_update_clean_when_user_did_not_modify(self):
        """When user hasn't modified a file, scaffold should cleanly overwrite."""
        self._run_scaffold()
        ci_path = ".github/workflows/ci.yml"
        with open(ci_path) as f:
            original = f.read()
        # Re-scaffold (template hasn't changed, so file should be skipped as base==theirs)
        self._run_scaffold()
        with open(ci_path) as f:
            after = f.read()
        assert original == after

    def test_three_way_merge_preserves_user_additions(self):
        """Three-way merge should preserve user additions when template changes elsewhere."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        # Initial template (5 lines so changes are non-adjacent for clean merge)
        tpl_v1 = "line1\nline2\nline3\nline4\nline5\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v1)

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        process_mappings(tpl_dir, mappings, {})

        # User modifies line2
        with open("output.txt", "w") as f:
            f.write("line1\nline2 user edit\nline3\nline4\nline5\n")

        # Template changes line4 (non-adjacent to user's line2 change)
        tpl_v2 = "line1\nline2\nline3\nline4 template update\nline5\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v2)

        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {})
        with open("output.txt") as f:
            result = f.read()

        # Both changes should be present (clean merge)
        assert "line2 user edit" in result
        assert "line4 template update" in result
        assert any(s == "merged" for _, s in created)

    def test_three_way_merge_detects_conflicts(self):
        """Three-way merge should detect conflicts when both sides change the same line."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        tpl_v1 = "line1\nline2\nline3\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v1)

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        process_mappings(tpl_dir, mappings, {})

        # User modifies line2
        with open("output.txt", "w") as f:
            f.write("line1\nline2 user version\nline3\n")

        # Template also modifies line2
        tpl_v2 = "line1\nline2 template version\nline3\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v2)

        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {})
        with open("output.txt") as f:
            result = f.read()

        assert "<<<<<<<" in result
        assert any("CONFLICTS" in s for _, s in created)
        assert any("conflict" in w.lower() for w in warnings)

    def test_no_base_no_scaffold_commit_hard_errors(self):
        """No stored base AND no scaffold commit to heal from -> hard error.

        A divergent legacy file that was never committed under an 'rlsbl
        scaffold' message cannot be merged and must not be silently skipped
        (which leaves it unmergeable forever) or overwritten (which destroys
        local edits).
        """
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write("template content v2\n")

        # Create target file directly (no base stored, never committed)
        with open("output.txt", "w") as f:
            f.write("different content\n")

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        with pytest.raises(ConfigError) as exc:
            process_mappings(tpl_dir, mappings, {})
        msg = str(exc.value)
        assert "output.txt" in msg
        assert "delete" in msg
        assert "rlsbl scaffold" in msg

    def test_no_base_identical_content_skips_silently(self):
        """When no base is stored but file matches template, skip without warning."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        content = "identical content\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(content)
        with open("output.txt", "w") as f:
            f.write(content)

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {})

        assert any(t == "output.txt" for t, _ in skipped)
        # No warning because content matches
        assert not any("no base stored" in w for w in warnings)

    def test_template_unchanged_skips(self):
        """When template hasn't changed (base == theirs), skip even if user modified file."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        tpl_content = "line1\nline2\nline3\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_content)

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        process_mappings(tpl_dir, mappings, {})

        # User modifies the file
        with open("output.txt", "w") as f:
            f.write("line1\nline2 customized\nline3\n")

        # Re-run with same template -- should skip (template unchanged)
        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {})
        assert any(t == "output.txt" for t, _ in skipped)

        # Verify user customization is preserved
        with open("output.txt") as f:
            assert "customized" in f.read()

    # -- re-scaffold tests --

    def test_rescaffold_preserves_user_owned_files(self):
        """Re-scaffold does NOT overwrite user-owned files (e.g. CHANGELOG.md)."""
        with open("CHANGELOG.md", "w") as f:
            f.write("# My custom changelog\n")

        self._run_scaffold()

        with open("CHANGELOG.md") as f:
            content = f.read()
        # User-owned files must be preserved.
        assert "My custom changelog" in content

    def test_rescaffold_keeps_base(self):
        """Re-scaffolding with an unchanged template keeps the stored base."""
        self._run_scaffold()
        ci_base_before = _load_base(".github/workflows/ci.yml")
        self._run_scaffold()
        ci_base_after = _load_base(".github/workflows/ci.yml")
        assert ci_base_after is not None
        # Content should match (template hasn't changed)
        assert ci_base_before == ci_base_after

    # -- merge tests --

    def test_update_processes_managed_files(self):
        """scaffold should process CI files via three-way merge."""
        self._run_scaffold()

        ci_path = ".github/workflows/ci.yml"
        assert os.path.exists(ci_path)

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        assert os.path.exists(ci_path)

    def test_hooks_not_user_owned(self):
        """Hooks should not be in USER_OWNED, allowing scaffold to merge them."""
        assert ".rlsbl/hooks/pre-release.sh" not in USER_OWNED
        assert ".rlsbl/hooks/post-release.sh" not in USER_OWNED

    def test_update_merges_hook_changes(self):
        """scaffold should three-way merge pre-release.sh when template changes."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(os.path.join(tpl_dir, "hooks"))

        # Initial template version (7 lines so changes are well-separated)
        tpl_v1 = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo start\n"
            "step_one\n"
            "step_two\n"
            "step_three\n"
            "echo done\n"
        )
        with open(os.path.join(tpl_dir, "hooks", "pre-release.sh.tpl"), "w") as f:
            f.write(tpl_v1)

        mappings = [{"template": "hooks/pre-release.sh.tpl",
                      "target": ".rlsbl/hooks/pre-release.sh"}]
        process_mappings(tpl_dir, mappings, {})

        # User modifies line 3 (echo start -> echo user_start)
        with open(".rlsbl/hooks/pre-release.sh", "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "echo user_start\n"
                "step_one\n"
                "step_two\n"
                "step_three\n"
                "echo done\n"
            )

        # Template changes line 7 (echo done -> echo finished), non-adjacent
        tpl_v2 = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo start\n"
            "step_one\n"
            "step_two\n"
            "step_three\n"
            "echo finished\n"
        )
        with open(os.path.join(tpl_dir, "hooks", "pre-release.sh.tpl"), "w") as f:
            f.write(tpl_v2)

        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {})
        with open(".rlsbl/hooks/pre-release.sh") as f:
            result = f.read()

        # Both user customization and template update should be present
        assert "echo user_start" in result
        assert "echo finished" in result
        assert any(s == "merged" for _, s in created)


class TestGitignoreSetUnionMerge:
    """Tests for .gitignore set-union merge in scaffold."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        os.system("git init -q .")

    def test_gitignore_appends_new_entries(self):
        """Scaffold on .gitignore appends new entries without conflicts."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        # Existing .gitignore with user entries
        with open(".gitignore", "w") as f:
            f.write("# User entries\nnode_modules/\n.env\n")

        # Template .gitignore with some overlap and some new entries
        with open(os.path.join(tpl_dir, "gitignore.tpl"), "w") as f:
            f.write("# Template entries\nnode_modules/\ndist/\n.credentials.json\n")

        mappings = [{"template": "gitignore.tpl", "target": ".gitignore"}]
        created, skipped, warnings, _ = process_mappings(
            tpl_dir, mappings, {},
        )

        with open(".gitignore") as f:
            result = f.read()

        # User entries preserved
        assert "node_modules/" in result
        assert ".env" in result
        # New entries added
        assert "dist/" in result
        assert ".credentials.json" in result
        # No conflict markers
        assert "<<<<<<<" not in result
        assert "=======" not in result
        assert ">>>>>>>" not in result
        # Should report as updated
        assert any("updated" in s for _, s in created), \
            f"Expected 'updated' status, got created={created}"

    def test_gitignore_no_duplicates(self):
        """Entries already in .gitignore are not duplicated."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        with open(".gitignore", "w") as f:
            f.write("node_modules/\ndist/\n")

        with open(os.path.join(tpl_dir, "gitignore.tpl"), "w") as f:
            f.write("node_modules/\ndist/\n")

        mappings = [{"template": "gitignore.tpl", "target": ".gitignore"}]
        created, skipped, warnings, _ = process_mappings(
            tpl_dir, mappings, {},
        )

        with open(".gitignore") as f:
            result = f.read()

        # Count occurrences
        assert result.count("node_modules/") == 1
        assert result.count("dist/") == 1
        # Should be skipped (unchanged)
        assert any(t == ".gitignore" for t, _ in skipped), \
            f"Expected .gitignore in skipped, got skipped={skipped}"

    def test_gitignore_preserves_user_comments(self):
        """User comments and formatting are preserved in the existing file."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        with open(".gitignore", "w") as f:
            f.write("# My project ignores\nnode_modules/\n\n# Build output\ndist/\n")

        with open(os.path.join(tpl_dir, "gitignore.tpl"), "w") as f:
            f.write("node_modules/\n.rlsbl/lock\n")

        mappings = [{"template": "gitignore.tpl", "target": ".gitignore"}]
        process_mappings(tpl_dir, mappings, {})

        with open(".gitignore") as f:
            result = f.read()

        # User comments preserved
        assert "# My project ignores" in result
        assert "# Build output" in result
        # New entry added
        assert ".rlsbl/lock" in result

    def test_gitignore_preserves_user_entries(self):
        """.gitignore uses additive merge (never removes user entries)."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        with open(".gitignore", "w") as f:
            f.write("# User entries\nnode_modules/\n.env\n")

        with open(os.path.join(tpl_dir, "gitignore.tpl"), "w") as f:
            f.write("# Template\ndist/\n")

        mappings = [{"template": "gitignore.tpl", "target": ".gitignore"}]
        created, skipped, warnings, _ = process_mappings(
            tpl_dir, mappings, {},
        )

        with open(".gitignore") as f:
            result = f.read()

        assert "dist/" in result
        assert ".env" in result
        assert "node_modules/" in result


class TestHashFunctions:
    """Tests for hash utility functions."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_file_hash_returns_sha256(self):
        with open("test.txt", "w") as f:
            f.write("hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert file_hash("test.txt") == expected



# ---------------------------------------------------------------------------
# Release command tests
# ---------------------------------------------------------------------------


class TestRelease:
    """Tests for rlsbl.commands.release."""

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
        # Create .rlsbl/changes/ with a valid unreleased entry
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,"description":"Bugfix","type":"fix"}\n')
        # Config with required private key
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"publish_mode": "ci", "targets": ["npm"]}, f)

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_dry_run(self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch,
                             _commit_files, mock_run, _tag_local, _push, _remote_exists):
        """Dry run should not modify any files."""
        # 1. git fetch origin --quiet (remote-ahead check)
        # 2. git rev-list --count HEAD..origin/main (0 commits behind)
        # 3. git status --porcelain (pre-hook snapshot)
        # 4. git status --porcelain (post-hook snapshot)
        mock_run.side_effect = ["", "0", "", ""]

        from rlsbl.commands.release import run_cmd

        # Read original file contents
        with open("package.json") as f:
            orig_pkg = f.read()
        with open("CHANGELOG.md") as f:
            orig_cl = f.read()

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(_rc(), {"dry-run": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))

        # Files should be unchanged
        with open("package.json") as f:
            assert f.read() == orig_pkg
        with open("CHANGELOG.md") as f:
            assert f.read() == orig_cl

    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.is_clean_tree", return_value=False)
    def test_release_dirty_tree(self, _clean, _gh_auth, _gh_inst):
        """Dirty working tree should cause SystemExit."""
        from rlsbl.commands.release import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    def test_release_behind_remote_aborts(self, _gh_inst, _gh_auth, _clean,
                                          _branch, mock_run, _remote_exists):
        """Release should abort when local branch is behind origin."""
        from rlsbl.commands.release import run_cmd

        # git fetch succeeds, rev-list returns "3" (3 commits behind)
        mock_run.side_effect = [
            "",   # git fetch origin --quiet
            "3",  # git rev-list --count HEAD..origin/main
        ]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_fetch_failure_warns_but_continues(self, _validate, _gen_cl,
                                                        _gh_inst, _gh_auth,
                                                        _clean, _branch, mock_run,
                                                        _tag_local):
        """If git fetch fails, warn but don't block the release."""
        from rlsbl.commands.release import run_cmd

        # git fetch raises (no network), then porcelain snapshots
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git"),  # git fetch fails
            "",         # git status --porcelain (pre-hook snapshot)
            "",         # git status --porcelain (post-hook snapshot)
        ]

        # Should reach dry-run exit without aborting
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(_rc(), {"quiet": False, "dry-run": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.remote_branch_exists", return_value=False)
    def test_release_empty_remote_continues(self, _remote_exists, _validate,
                                            _gen_cl, _gh_inst, _gh_auth,
                                            _clean, _branch, _commit_files,
                                            mock_run, _tag_local, _push):
        """Empty remote (first push) should skip rev-list and continue."""
        from rlsbl.commands.release import run_cmd

        # With remote_branch_exists=False, rev-list is skipped entirely.
        # 1. git fetch origin --quiet
        # 2. git status --porcelain (pre-hook snapshot)
        # 3. git status --porcelain (post-hook snapshot)
        mock_run.side_effect = ["", "", ""]

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(_rc(), {"dry-run": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))

        assert "Remote branch origin/main does not exist yet" in mock_stderr.getvalue()

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    def test_release_remote_branch_exists_but_revlist_fails_aborts(
            self, _gh_inst, _gh_auth, _clean, _branch, mock_run,
            _remote_exists):
        """When remote branch exists but rev-list fails, abort for safety."""
        from rlsbl.commands.release import run_cmd

        # git fetch succeeds, rev-list raises CalledProcessError
        mock_run.side_effect = [
            "",  # git fetch origin --quiet
            subprocess.CalledProcessError(128, "git"),  # rev-list fails
        ]

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))
        assert exc_info.value.code == 1
        assert "Cannot verify remote-ahead status" in mock_stderr.getvalue()


# ---------------------------------------------------------------------------
# Release commit autogenerated trailer tests
# ---------------------------------------------------------------------------


class TestReleaseCommitTrailers:
    """Tests that release commits pass correct autogenerated flag to commit_files."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f, indent=2)
            f.write("\n")
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release.\n")
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,"description":"Bugfix","type":"fix"}\n')
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"publish_mode": "ci", "targets": ["npm"]}, f)

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_on_remote", return_value=False)
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.run_gh", return_value="")
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
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    @patch("rlsbl.app.run_checks", return_value=([], 0))
    def test_version_bump_commit_is_autogenerated(self, _run_checks,
                                                    _changes_dir, _extract, _finalize,
                                                    _gen_ver_file, _validate, _gen_cl,
                                                    _deploy, _tag,
                                                    _gh_inst, _gh_auth, _clean, _branch,
                                                    mock_commit_files, _run_gh, mock_run,
                                                    _tag_local, _tag_remote,
                                                    _push, _lock, _unlock, _remote_exists):
        """The version-bump commit should be marked autogenerated."""
        from rlsbl.commands.release import run_cmd

        mock_run.side_effect = [
            "",               # git fetch origin --quiet
            "0",              # git rev-list --count HEAD..origin/main
            "",               # git status --porcelain (pre-hook snapshot)
            "",               # git status --porcelain (pre-selfdoc snapshot)
            "",               # git status --porcelain (post-selfdoc snapshot)
            "",               # git status --porcelain (post-hook snapshot)
            "",               # git status --porcelain (baseline_dirty in _run_release_mutating)
            "/tmp/fake-repo", # git rev-parse --show-toplevel (for vpath)
            "",               # git rev-parse HEAD (pre_release_sha)
            "",               # git status --porcelain (re-check guard)
            "package.json",   # git diff --name-only -- package.json
            "M package.json", # git status --porcelain -- package.json
            "",               # git log -1 --format=%s (COMMITTED guard)
            "",               # status --porcelain (backfilled .md detection)
            "",               # git tag v1.0.1
            "",               # rev-parse HEAD (PUSHED guard _local_head)
            "",               # rev-parse origin/main (PUSHED guard _remote_head)
            "",               # git push origin v1.0.1
            "abc123def",      # git rev-parse HEAD (pushed sha)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(_rc(), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))

        # First call is the version-bump commit -- should be autogenerated
        version_bump_call = mock_commit_files.call_args_list[0]
        assert "v1.0.1" in version_bump_call[0][0]
        assert version_bump_call[1].get("autogenerated", True)

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_on_remote", return_value=False)
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.run_gh", return_value="")
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
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    @patch("rlsbl.app.run_checks", return_value=([], 0))
    def test_finalize_commit_is_autogenerated(self, _run_checks,
                                               _changes_dir, _extract, _finalize,
                                               _gen_ver_file, _validate, _gen_cl,
                                               _deploy, _tag,
                                               _gh_inst, _gh_auth, _clean, _branch,
                                               mock_commit_files, _run_gh, mock_run,
                                               _tag_local, _tag_remote,
                                               _push, _lock, _unlock, _remote_exists):
        """The changelog finalization commit should be marked autogenerated."""
        from rlsbl.commands.release import run_cmd

        mock_run.side_effect = [
            "",               # git fetch origin --quiet
            "0",              # git rev-list --count HEAD..origin/main
            "",               # git status --porcelain (pre-hook snapshot)
            "",               # git status --porcelain (pre-selfdoc snapshot)
            "",               # git status --porcelain (post-selfdoc snapshot)
            "",               # git status --porcelain (post-hook snapshot)
            "",               # git status --porcelain (baseline_dirty in _run_release_mutating)
            "/tmp/fake-repo", # git rev-parse --show-toplevel (for vpath)
            "",               # git rev-parse HEAD (pre_release_sha)
            "",               # git status --porcelain (re-check guard)
            "package.json",   # git diff --name-only -- package.json
            "M package.json", # git status --porcelain -- package.json
            "",               # git log -1 --format=%s (COMMITTED guard)
            "",               # status --porcelain (backfilled .md detection)
            "",               # git tag v1.0.1
            "",               # rev-parse HEAD (PUSHED guard _local_head)
            "",               # rev-parse origin/main (PUSHED guard _remote_head)
            "",               # git push origin v1.0.1
            "abc123def",      # git rev-parse HEAD (pushed sha)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(_rc(), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))

        # Second call is the finalization commit -- should have autogenerated=True
        finalize_call = mock_commit_files.call_args_list[1]
        assert "chore: finalize changelog" in finalize_call[0][0]
        assert finalize_call[1].get("autogenerated", True)


# ---------------------------------------------------------------------------
# Porcelain parsing tests
# ---------------------------------------------------------------------------


class TestPorcelainParsing:
    """Tests for parse_porcelain_paths in rlsbl.commands.release."""

    def test_stripped_leading_space_on_first_line(self):
        """run() strips stdout, which can remove a leading space from the first line.

        Porcelain format is "XY path" where X/Y are status codes.
        A line like " M pyproject.toml" becomes "M pyproject.toml" after strip.
        The parser must still extract the correct path using lstrip().split(None, 1).
        """
        from rlsbl.commands.release import parse_porcelain_paths

        # Simulate output where run() has stripped leading whitespace from first line
        # Original porcelain: "M  package.json\n M pyproject.toml\n?? .rlsbl/lock"
        # After run().strip(): "M package.json\n M pyproject.toml\n?? .rlsbl/lock"
        porcelain = "M package.json\n M pyproject.toml\n?? .rlsbl/lock"
        result = parse_porcelain_paths(porcelain)
        assert result == {"package.json", "pyproject.toml", ".rlsbl/lock"}

    def test_rename_entry(self):
        """Rename entries use 'R old -> new' format; parser should extract new path."""
        from rlsbl.commands.release import parse_porcelain_paths

        porcelain = "R  old.txt -> new.txt\nM  other.txt"
        result = parse_porcelain_paths(porcelain)
        assert "new.txt" in result
        assert "other.txt" in result

    def test_empty_output(self):
        """Empty porcelain output returns empty set."""
        from rlsbl.commands.release import parse_porcelain_paths

        assert parse_porcelain_paths("") == set()

    def test_blank_lines_ignored(self):
        """Blank lines in output are safely ignored."""
        from rlsbl.commands.release import parse_porcelain_paths

        porcelain = "M  file.txt\n\n?? untracked.txt"
        result = parse_porcelain_paths(porcelain)
        assert result == {"file.txt", "untracked.txt"}


# ---------------------------------------------------------------------------
# Undo command tests
# ---------------------------------------------------------------------------


class TestUndo:
    """Tests for rlsbl.commands.undo."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run")
    def test_undo_no_tags(self, mock_run, _gh_inst, _gh_auth, _clean):
        """When git describe raises, undo should exit with 'no tags found'."""
        mock_run.side_effect = Exception("no tags")

        from pathlib import Path
        from rlsbl.commands.undo import run_cmd
        from rlsbl.context import ProjectContext

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=False)
    def test_undo_dirty_tree(self, _clean, _gh_auth, _gh_inst):
        """Dirty working tree should cause SystemExit."""
        from pathlib import Path
        from rlsbl.commands.undo import run_cmd
        from rlsbl.context import ProjectContext

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", [], {"yes": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Check command tests
# ---------------------------------------------------------------------------


class TestCheck:
    """Tests for rlsbl.commands.check -- npm availability checks."""

    @patch("rlsbl.commands.check.subprocess.run")
    def test_check_npm_available(self, mock_subprocess_run):
        """When npm view raises CalledProcessError with 404, name is available."""
        from rlsbl.commands.check import check_npm_availability

        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            1, "npm", stderr="E404 Not Found"
        )
        result = check_npm_availability("nonexistent-pkg-xyz")
        assert result["status"] == "available"

    @patch("rlsbl.commands.check.subprocess.run")
    def test_check_npm_taken(self, mock_subprocess_run):
        """When npm view succeeds, name is taken."""
        from rlsbl.commands.check import check_npm_availability

        mock_subprocess_run.return_value = subprocess.CompletedProcess(
            args=["npm", "view", "express", "name"],
            returncode=0,
            stdout="express",
            stderr="",
        )
        result = check_npm_availability("express")
        assert result["status"] == "taken"


# ---------------------------------------------------------------------------
# Release target configuration tests
# ---------------------------------------------------------------------------


class TestResolveReleaseTargets:
    """Tests for resolve_release_targets: config-based secondary target resolution."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

    def test_missing_config_falls_back_to_auto_detect(self):
        """Without release_targets in config, auto-detect all detected targets."""
        # Create version.json so spec target is detected
        with open("version.json", "w") as f:
            json.dump({"version": "1.0.0"}, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {}, config={})
        # spec is detected via version.json
        assert "spec" in result
        # npm is the primary and must be excluded from secondaries
        assert "npm" not in result

    def test_config_release_targets_restricts_secondaries(self):
        """release_targets in config restricts which secondaries run."""
        # Create version.json so spec target would be auto-detected
        with open("version.json", "w") as f:
            json.dump({"version": "1.0.0"}, f)
        # Config says only npm participates (but npm is primary, so secondaries = empty)
        os.makedirs(".rlsbl", exist_ok=True)
        config = {"release_targets": ["npm"]}
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump(config, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {}, config=config)
        # spec is NOT in the configured list, so it should not appear
        assert "spec" not in result
        # npm is primary, excluded from secondaries
        assert "npm" not in result
        assert result == {}

    def test_config_release_targets_includes_spec(self):
        """release_targets listing spec includes it even without auto-detect."""
        os.makedirs(".rlsbl", exist_ok=True)
        config = {"release_targets": ["npm", "spec"]}
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump(config, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {}, config=config)
        assert "spec" in result
        assert "npm" not in result

    def test_primary_always_excluded_from_secondaries(self):
        """The primary target is never in the secondary set, even if config lists it."""
        os.makedirs(".rlsbl", exist_ok=True)
        config = {"release_targets": ["npm", "spec"]}
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump(config, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {}, config=config)
        assert "npm" not in result

    def test_unknown_target_in_config_ignored(self):
        """Unknown target names in config are silently filtered out."""
        os.makedirs(".rlsbl", exist_ok=True)
        config = {"release_targets": ["npm", "nonexistent", "spec"]}
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump(config, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {}, config=config)
        assert "spec" in result
        assert "nonexistent" not in result


class TestStatusJson:
    """Tests for rlsbl.commands.status --json flag."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        # Create a git repo
        os.system("git init -q .")
        os.system("git config user.email test@test.local")
        os.system("git config user.name Test")
        # Create minimal npm project
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "0.1.0"}, f, indent=2)
            f.write("\n")
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 0.1.0\n\nInitial release.\n")
        os.system("git add package.json CHANGELOG.md")
        os.system("git commit -q -m initial")

    def test_status_json_output(self):
        """With --json, status should output valid JSON with expected keys."""
        from rlsbl.commands.status import run_cmd

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            run_cmd("npm", [], {"json": True}, ctx=make_ctx("."))

        output = mock_out.getvalue()
        data = json.loads(output)
        expected_keys = {"name", "version", "target", "branch", "tag",
                         "clean", "changelog", "jsonl_coverage",
                         "commits_ahead", "commits_ahead_tag",
                         "ci", "publish", "registry_version", "drift"}
        assert set(data.keys()) == expected_keys
        assert data["name"] == "test-pkg"
        assert data["version"] == "0.1.0"
        assert data["target"] == "npm"
        assert data["clean"]
        assert data["changelog"]


class TestStatusChangelogExemption:
    """Tests for status exempting autogenerated commits from coverage."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        # Create a git repo
        subprocess.run(["git", "init", "-q", "."], check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], check=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True)
        # Create minimal npm project
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "0.1.0"}, f, indent=2)
            f.write("\n")
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 0.1.0\n\nInitial release.\n")
        subprocess.run(["git", "add", "package.json", "CHANGELOG.md"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], check=True)
        subprocess.run(["git", "tag", "v0.1.0"], check=True)

    def test_autogenerated_commits_exempted_from_coverage(self):
        """Commits with Autogenerated: true trailer should not show as uncovered."""
        from rlsbl.commands.status import _collect_status

        # Set up JSONL changes directory
        changes_dir = os.path.join(".rlsbl", "changes")
        os.makedirs(changes_dir, exist_ok=True)

        # Make a real code commit
        with open("code.js", "w") as f:
            f.write("console.log('hello');\n")
        subprocess.run(["git", "add", "code.js"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat: add code"], check=True)
        code_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Add a JSONL entry covering the code commit
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(json.dumps({
                "commits": [code_sha[:12]],
                "user_facing": True,
                "description": "Add code",
                "type": "feature",
            }) + "\n")
        subprocess.run(["git", "add", os.path.join(changes_dir, "unreleased.jsonl")], check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "changelog: add entry\n\nAutogenerated: true"],
            check=True,
        )

        # Now there are 2 unreleased commits: the code commit and the autogenerated commit.
        # The autogenerated commit should be exempted.
        data = _collect_status("npm", ctx=make_ctx("."))
        # Coverage should show 1/1 (the autogenerated commit exempted)
        assert "1/1" in data["jsonl_coverage"]
        assert "1 exempted" in data["jsonl_coverage"]

    def test_no_exemption_annotation_when_no_autogenerated_commits(self):
        """When there are no autogenerated commits, no exemption note is shown."""
        from rlsbl.commands.status import _collect_status

        changes_dir = os.path.join(".rlsbl", "changes")
        os.makedirs(changes_dir, exist_ok=True)

        # Make a real code commit
        with open("code.js", "w") as f:
            f.write("console.log('hello');\n")
        subprocess.run(["git", "add", "code.js"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat: add code"], check=True)
        code_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Add a JSONL entry covering the code commit
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(json.dumps({
                "commits": [code_sha[:12]],
                "user_facing": True,
                "description": "Add code",
                "type": "feature",
            }) + "\n")

        # Stage but don't commit the JSONL (so it's part of the code commit conceptually)
        # Actually, we need the JSONL file on disk but not as a separate commit
        data = _collect_status("npm", ctx=make_ctx("."))
        # Should show 1/1 without exemption note
        assert "1/1" in data["jsonl_coverage"]
        assert "exempted" not in data["jsonl_coverage"]


class TestScaffoldAutoDetection:
    """Tests for scaffold auto-detection writing targets to config."""

    def test_single_npm_scaffold_writes_target_to_config(self, mock_git_repo):
        """After scaffolding a single npm project without existing config,
        .rlsbl/config.json should contain targets: ["npm"]."""
        pkg = {"name": "test-pkg", "version": "0.1.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"auto-tag": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        config_path = mock_git_repo / ".rlsbl" / "config.json"
        assert config_path.exists(), ".rlsbl/config.json should be created"
        config = json.loads(config_path.read_text())
        assert "targets" in config, "config should have a 'targets' key"
        assert config["targets"] == ["npm"], f"expected ['npm'], got {config['targets']}"


class TestScaffoldUntrack:
    """Tests for scaffold untracking files added to .gitignore."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)
        # Create a full git repo with initial commit
        subprocess.run(["git", "init", "-q", "."], check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], check=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True)
        # Create minimal package.json so npm registry is detected
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "0.1.0"}, f)
        subprocess.run(["git", "add", "package.json"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], check=True)

    def test_scaffold_untracks_gitignored_files(self):
        """Scaffold should untrack files that match .gitignore patterns."""
        # Use .credentials.json which is in the gitignore template but
        # won't be deleted by the lock cleanup (unlike .rlsbl/lock).
        target_file = ".credentials.json"

        with open(target_file, "w") as f:
            f.write('{"secret": "value"}\n')
        subprocess.run(["git", "add", target_file], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add credentials"], check=True)

        # Verify it's tracked
        result = subprocess.run(
            ["git", "ls-files", "--cached", target_file],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == target_file

        # Run scaffold (which writes .gitignore containing .credentials.json)
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"auto-tag": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        # Verify it's no longer tracked
        result = subprocess.run(
            ["git", "ls-files", "--cached", target_file],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "", \
            f"{target_file} should no longer be tracked after scaffold"

        # Verify the file still exists on disk (untracked, not deleted)
        assert os.path.exists(target_file), \
            f"{target_file} should still exist on disk"

        # Verify the untrack was committed (not just staged)
        status = subprocess.run(
            ["git", "status", "--porcelain", target_file],
            capture_output=True, text=True,
        )
        assert status.stdout.strip() == "", \
            f"{target_file} removal should be committed, not just staged"


# ---------------------------------------------------------------------------
# Release rollback on push failure tests
# ---------------------------------------------------------------------------


class TestReleaseRollbackOnPushFailure:
    """Tests that a failed push during release rolls back local commits and tags."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.tmp_dir = str(tmp_path)

        # Initialize a real git repo
        subprocess.run(["git", "init", "-q", "."], check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], check=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True)

        # Create package.json (npm target, version 1.0.0)
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f, indent=2)
            f.write("\n")

        # Create CHANGELOG.md
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release.\n")

        # Create .rlsbl/changes/unreleased.jsonl with a valid entry
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,'
                    '"description":"Bugfix","type":"fix"}\n')
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"publish_mode": "ci", "targets": ["npm"]}, f)

        # Initial commit and tag
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], check=True)
        subprocess.run(["git", "tag", "v1.0.0"], check=True)

        # Record the pre-release HEAD SHA
        self.pre_release_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    @patch("rlsbl.commands.release.push_if_needed",
           side_effect=subprocess.CalledProcessError(1, "git push"))
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.extract_changelog_entry", return_value="- Bugfix")
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased",
           return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.app.run_checks", return_value=([], 0))
    def test_push_failure_after_tag_is_resumable_not_rolled_back(
        self, _run_checks, _gh_inst, _gh_auth, _validate,
        _gen_cl, _extract, _tag, _deploy, _push,
    ):
        """A push failure AFTER tagging must be classified RESUMABLE.

        The release is already TAGGED with finalized changelog files on disk,
        so a failed branch push must NOT trigger a destructive rollback
        (which would destroy exactly the state `release resume` needs).
        Instead the tag and commits are preserved and a failed PUSHED marker
        is recorded. (Previously this rolled back -- the bug this fixes.)
        """
        from rlsbl.commands.release import run_cmd
        from rlsbl.commands.release.release_state import (
            get_state_path, load_release_state,
        )

        # Run release -- push_if_needed raises CalledProcessError post-TAGGED
        with pytest.raises(subprocess.CalledProcessError):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(_rc(), {
                    "yes": True,
                    "quiet": True,
                },
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}),
)

        # HEAD must NOT be rolled back -- the release commits are preserved.
        post_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_sha != self.pre_release_sha, \
            "HEAD must NOT be rolled back after a post-TAGGED push failure"

        # The tag must be preserved for resume.
        tag_check = subprocess.run(
            ["git", "tag", "-l", "v1.0.1"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tag_check == "v1.0.1", \
            "Tag v1.0.1 must be preserved after a post-TAGGED push failure"

        # State preserved with a failed PUSHED marker.
        state = load_release_state(get_state_path("."))
        assert state is not None, "in-progress.json must be preserved"
        assert "TAGGED" in state["completed_steps"]
        assert "PUSHED" in state.get("failed_steps", {})
