"""Integration tests for rlsbl.commands.init_cmd (scaffold command)."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import (
    BASES_DIR,
    HASHES_FILE,
    USER_OWNED,
    _load_base,
    _save_base,
    _three_way_merge,
    file_hash,
    load_hashes,
    process_mappings,
    process_template,
    run_cmd,
    save_hashes,
)


class TestProcessTemplate(unittest.TestCase):
    """Tests for template variable replacement."""

    def test_replaces_known_variables(self):
        content, unreplaced = process_template(
            "Hello {{name}}, version {{version}}!",
            {"name": "my-pkg", "version": "1.0.0"},
        )
        self.assertEqual(content, "Hello my-pkg, version 1.0.0!")
        self.assertEqual(unreplaced, [])

    def test_leaves_unknown_variables_and_reports_them(self):
        content, unreplaced = process_template(
            "{{name}} uses {{unknownVar}}",
            {"name": "my-pkg"},
        )
        self.assertEqual(content, "my-pkg uses {{unknownVar}}")
        self.assertEqual(unreplaced, ["unknownVar"])

    def test_replaces_multiple_occurrences(self):
        content, _ = process_template(
            "{{name}} is {{name}}",
            {"name": "x"},
        )
        self.assertEqual(content, "x is x")

    def test_no_variables_returns_unchanged(self):
        content, unreplaced = process_template("plain text", {})
        self.assertEqual(content, "plain text")
        self.assertEqual(unreplaced, [])


class TestBaseStorage(unittest.TestCase):
    """Tests for _save_base / _load_base helpers."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_save_and_load_roundtrip(self):
        _save_base("foo/bar.txt", "hello world\n")
        self.assertEqual(_load_base("foo/bar.txt"), "hello world\n")

    def test_load_missing_returns_none(self):
        self.assertIsNone(_load_base("nonexistent.txt"))

    def test_save_creates_parent_dirs(self):
        _save_base("a/b/c.txt", "content")
        base_path = os.path.join(BASES_DIR, "a", "b", "c.txt")
        self.assertTrue(os.path.exists(base_path))


class TestThreeWayMerge(unittest.TestCase):
    """Tests for _three_way_merge using git merge-file."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
        # git merge-file needs to be able to run; init a repo for safety
        os.system("git init -q .")

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_clean_merge_no_conflicts(self):
        # Changes must be non-adjacent so git merge-file resolves them cleanly
        base = "line1\nline2\nline3\nline4\nline5\n"
        ours = "line1\nline2 modified by user\nline3\nline4\nline5\n"
        theirs = "line1\nline2\nline3\nline4 modified by template\nline5\n"
        merged, has_conflicts = _three_way_merge(ours, base, theirs)
        self.assertFalse(has_conflicts)
        self.assertIn("line2 modified by user", merged)
        self.assertIn("line4 modified by template", merged)

    def test_conflict_detected(self):
        base = "line1\nline2\nline3\n"
        ours = "line1\nline2 user version\nline3\n"
        theirs = "line1\nline2 template version\nline3\n"
        merged, has_conflicts = _three_way_merge(ours, base, theirs)
        self.assertTrue(has_conflicts)
        self.assertIn("<<<<<<<", merged)
        self.assertIn("=======", merged)
        self.assertIn(">>>>>>>", merged)

    def test_identical_changes_no_conflict(self):
        base = "line1\nline2\nline3\n"
        ours = "line1\nline2 same change\nline3\n"
        theirs = "line1\nline2 same change\nline3\n"
        merged, has_conflicts = _three_way_merge(ours, base, theirs)
        self.assertFalse(has_conflicts)
        self.assertEqual(merged, "line1\nline2 same change\nline3\n")

    def test_temp_files_cleaned_up(self):
        base = "a\n"
        ours = "a\n"
        theirs = "a\n"
        _three_way_merge(ours, base, theirs)
        # No leftover .ours/.base/.theirs files
        leftover = [f for f in os.listdir(".") if f.endswith((".ours", ".base", ".theirs"))]
        self.assertEqual(leftover, [])


class TestScaffold(unittest.TestCase):
    """Integration tests for the scaffold (init) command."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
        # git init so git merge-file works
        os.system("git init -q .")
        # Create minimal package.json so npm registry is detected
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "0.1.0"}, f)

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def _run_scaffold(self, force=False, update=False):
        """Run scaffold for npm with stdout suppressed."""
        flags = {}
        if force:
            flags["force"] = True
        if update:
            flags["update"] = True
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], flags)

    def test_creates_changelog(self):
        self._run_scaffold()
        self.assertTrue(os.path.exists("CHANGELOG.md"))
        with open("CHANGELOG.md") as f:
            self.assertIn("0.1.0", f.read())

    def test_creates_gitignore(self):
        self._run_scaffold()
        self.assertTrue(os.path.exists(".gitignore"))

    def test_creates_ci_workflow(self):
        self._run_scaffold()
        self.assertTrue(os.path.exists(".github/workflows/ci.yml"))

    def test_creates_publish_workflow(self):
        self._run_scaffold()
        self.assertTrue(os.path.exists(".github/workflows/publish.yml"))

    def test_template_variable_replacement(self):
        """Verify {{name}} and {{version}} are replaced in generated files."""
        self._run_scaffold()
        with open("CHANGELOG.md") as f:
            content = f.read()
        self.assertIn("0.1.0", content)
        self.assertNotIn("{{version}}", content)

    def test_unreplaced_variables_emit_warning(self):
        """Scaffold with a template containing unknown vars should warn."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write("Hello {{name}} from {{planet}}")

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        created, skipped, warnings, hashes = process_mappings(
            tpl_dir, mappings, {"name": "test-pkg"}, force=False,
        )
        self.assertIn(("output.txt", "created"), created)
        self.assertTrue(
            any("planet" in w for w in warnings),
            f"Expected warning about 'planet', got: {warnings}",
        )

    # -- Base storage tests --

    def test_initial_scaffold_saves_bases(self):
        """After initial scaffold, base files should exist in .rlsbl/bases/."""
        self._run_scaffold()
        # CI workflow should have a base stored
        ci_base = _load_base(".github/workflows/ci.yml")
        self.assertIsNotNone(ci_base)
        self.assertGreater(len(ci_base), 0)

    def test_bases_match_generated_files(self):
        """Stored bases should match the rendered template content (identical to file on first scaffold)."""
        self._run_scaffold()
        ci_path = ".github/workflows/ci.yml"
        with open(ci_path) as f:
            file_content = f.read()
        base_content = _load_base(ci_path)
        self.assertEqual(file_content, base_content)

    # -- Three-way merge integration tests --

    def test_update_clean_when_user_did_not_modify(self):
        """When user hasn't modified a file, --update should cleanly overwrite."""
        self._run_scaffold()
        ci_path = ".github/workflows/ci.yml"
        with open(ci_path) as f:
            original = f.read()
        # Re-scaffold (template hasn't changed, so file should be skipped as base==theirs)
        self._run_scaffold()
        with open(ci_path) as f:
            after = f.read()
        self.assertEqual(original, after)

    def test_three_way_merge_preserves_user_additions(self):
        """Three-way merge should preserve user additions when template changes elsewhere."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        # Initial template (5 lines so changes are non-adjacent for clean merge)
        tpl_v1 = "line1\nline2\nline3\nline4\nline5\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v1)

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        process_mappings(tpl_dir, mappings, {}, force=False)

        # User modifies line2
        with open("output.txt", "w") as f:
            f.write("line1\nline2 user edit\nline3\nline4\nline5\n")

        # Template changes line4 (non-adjacent to user's line2 change)
        tpl_v2 = "line1\nline2\nline3\nline4 template update\nline5\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v2)

        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {}, force=False)
        with open("output.txt") as f:
            result = f.read()

        # Both changes should be present (clean merge)
        self.assertIn("line2 user edit", result)
        self.assertIn("line4 template update", result)
        self.assertTrue(any(s == "merged" for _, s in created))

    def test_three_way_merge_detects_conflicts(self):
        """Three-way merge should detect conflicts when both sides change the same line."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        tpl_v1 = "line1\nline2\nline3\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v1)

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        process_mappings(tpl_dir, mappings, {}, force=False)

        # User modifies line2
        with open("output.txt", "w") as f:
            f.write("line1\nline2 user version\nline3\n")

        # Template also modifies line2
        tpl_v2 = "line1\nline2 template version\nline3\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_v2)

        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {}, force=False)
        with open("output.txt") as f:
            result = f.read()

        self.assertIn("<<<<<<<", result)
        self.assertTrue(any("CONFLICTS" in s for _, s in created))
        self.assertTrue(any("conflict" in w.lower() for w in warnings))

    def test_no_base_skips_with_warning(self):
        """When no base is stored (legacy project), skip with a warning."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write("template content v2\n")

        # Create target file directly (no base stored)
        with open("output.txt", "w") as f:
            f.write("different content\n")

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {}, force=False)

        self.assertTrue(any(t == "output.txt" for t, _ in skipped))
        self.assertTrue(
            any("no base stored" in w for w in warnings),
            f"Expected 'no base stored' warning, got: {warnings}",
        )

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
        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {}, force=False)

        self.assertTrue(any(t == "output.txt" for t, _ in skipped))
        # No warning because content matches
        self.assertFalse(any("no base stored" in w for w in warnings))

    def test_template_unchanged_skips(self):
        """When template hasn't changed (base == theirs), skip even if user modified file."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        tpl_content = "line1\nline2\nline3\n"
        with open(os.path.join(tpl_dir, "test.tpl"), "w") as f:
            f.write(tpl_content)

        mappings = [{"template": "test.tpl", "target": "output.txt"}]
        process_mappings(tpl_dir, mappings, {}, force=False)

        # User modifies the file
        with open("output.txt", "w") as f:
            f.write("line1\nline2 customized\nline3\n")

        # Re-run with same template -- should skip (template unchanged)
        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {}, force=False)
        self.assertTrue(any(t == "output.txt" for t, _ in skipped))

        # Verify user customization is preserved
        with open("output.txt") as f:
            self.assertIn("customized", f.read())

    # -- --force tests --

    def test_force_preserves_user_owned_files(self):
        """With --force, scaffold does NOT overwrite user-owned files (e.g. CHANGELOG.md)."""
        with open("CHANGELOG.md", "w") as f:
            f.write("# My custom changelog\n")

        self._run_scaffold(force=True)

        with open("CHANGELOG.md") as f:
            content = f.read()
        # User-owned files must be preserved even with --force
        self.assertIn("My custom changelog", content)

    def test_force_updates_base(self):
        """With --force, the base should be updated to the new template content."""
        self._run_scaffold()
        ci_base_before = _load_base(".github/workflows/ci.yml")
        self._run_scaffold(force=True)
        ci_base_after = _load_base(".github/workflows/ci.yml")
        # Base should exist after force
        self.assertIsNotNone(ci_base_after)
        # Content should match (template hasn't changed)
        self.assertEqual(ci_base_before, ci_base_after)

    # -- Hash tests --

    def test_hashes_saved_after_scaffolding(self):
        """After scaffolding, .rlsbl/hashes.json should exist with entries."""
        self._run_scaffold()
        self.assertTrue(os.path.exists(HASHES_FILE))
        hashes = load_hashes()
        self.assertIsInstance(hashes, dict)
        self.assertGreater(len(hashes), 0)

    def test_hashes_match_actual_file_contents(self):
        """Stored hashes should match SHA-256 of the generated files."""
        self._run_scaffold()
        hashes = load_hashes()
        for path, stored_hash in hashes.items():
            if os.path.exists(path):
                self.assertEqual(
                    stored_hash,
                    file_hash(path),
                    f"Hash mismatch for {path}",
                )

    # -- --update tests --

    def test_update_processes_managed_files(self):
        """--update should still process CI files via three-way merge."""
        self._run_scaffold()

        ci_path = ".github/workflows/ci.yml"
        hashes_before = load_hashes()
        self.assertIn(ci_path, hashes_before)

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            run_cmd("npm", [], {"update": True})

        self.assertTrue(os.path.exists(ci_path))

    def test_hooks_not_user_owned(self):
        """Hooks should not be in USER_OWNED, allowing scaffold --update to merge them."""
        self.assertNotIn(".rlsbl/hooks/pre-release.sh", USER_OWNED)
        self.assertNotIn(".rlsbl/hooks/post-release.sh", USER_OWNED)

    def test_update_merges_hook_changes(self):
        """scaffold --update should three-way merge pre-release.sh when template changes."""
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
        process_mappings(tpl_dir, mappings, {}, force=False)

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

        created, skipped, warnings, _ = process_mappings(tpl_dir, mappings, {}, force=False)
        with open(".rlsbl/hooks/pre-release.sh") as f:
            result = f.read()

        # Both user customization and template update should be present
        self.assertIn("echo user_start", result)
        self.assertIn("echo finished", result)
        self.assertTrue(any(s == "merged" for _, s in created))


class TestGitignoreSetUnionMerge(unittest.TestCase):
    """Tests for .gitignore set-union merge in scaffold --update."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
        os.system("git init -q .")

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_gitignore_appends_new_entries(self):
        """Scaffold --update on .gitignore appends new entries without conflicts."""
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
            tpl_dir, mappings, {}, force=False, update=True,
        )

        with open(".gitignore") as f:
            result = f.read()

        # User entries preserved
        self.assertIn("node_modules/", result)
        self.assertIn(".env", result)
        # New entries added
        self.assertIn("dist/", result)
        self.assertIn(".credentials.json", result)
        # No conflict markers
        self.assertNotIn("<<<<<<<", result)
        self.assertNotIn("=======", result)
        self.assertNotIn(">>>>>>>", result)
        # Should report as updated
        self.assertTrue(any("updated" in s for _, s in created),
                        f"Expected 'updated' status, got created={created}")

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
            tpl_dir, mappings, {}, force=False, update=True,
        )

        with open(".gitignore") as f:
            result = f.read()

        # Count occurrences
        self.assertEqual(result.count("node_modules/"), 1)
        self.assertEqual(result.count("dist/"), 1)
        # Should be skipped (unchanged)
        self.assertTrue(any(t == ".gitignore" for t, _ in skipped),
                        f"Expected .gitignore in skipped, got skipped={skipped}")

    def test_gitignore_preserves_user_comments(self):
        """User comments and formatting are preserved in the existing file."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        with open(".gitignore", "w") as f:
            f.write("# My project ignores\nnode_modules/\n\n# Build output\ndist/\n")

        with open(os.path.join(tpl_dir, "gitignore.tpl"), "w") as f:
            f.write("node_modules/\n.rlsbl/lock\n")

        mappings = [{"template": "gitignore.tpl", "target": ".gitignore"}]
        process_mappings(tpl_dir, mappings, {}, force=False, update=True)

        with open(".gitignore") as f:
            result = f.read()

        # User comments preserved
        self.assertIn("# My project ignores", result)
        self.assertIn("# Build output", result)
        # New entry added
        self.assertIn(".rlsbl/lock", result)

    def test_gitignore_force_overwrites(self):
        """With --force, .gitignore should be overwritten entirely."""
        tpl_dir = os.path.join(self.tmp_dir, "_tpls")
        os.makedirs(tpl_dir)

        with open(".gitignore", "w") as f:
            f.write("# User entries\nnode_modules/\n.env\n")

        with open(os.path.join(tpl_dir, "gitignore.tpl"), "w") as f:
            f.write("# Template\ndist/\n")

        mappings = [{"template": "gitignore.tpl", "target": ".gitignore"}]
        created, skipped, warnings, _ = process_mappings(
            tpl_dir, mappings, {}, force=True,
        )

        with open(".gitignore") as f:
            result = f.read()

        # Force should overwrite completely
        self.assertIn("dist/", result)
        self.assertNotIn(".env", result)
        self.assertTrue(any(s == "overwritten" for _, s in created))


class TestHashFunctions(unittest.TestCase):
    """Tests for hash utility functions."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_file_hash_returns_sha256(self):
        with open("test.txt", "w") as f:
            f.write("hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        self.assertEqual(file_hash("test.txt"), expected)

    def test_save_and_load_hashes_roundtrip(self):
        data = {"file1.txt": "abc123", "dir/file2.txt": "def456"}
        save_hashes(data)
        loaded = load_hashes()
        self.assertEqual(loaded, data)

    def test_load_hashes_returns_empty_dict_when_no_file(self):
        self.assertEqual(load_hashes(), {})


# ---------------------------------------------------------------------------
# Release command tests
# ---------------------------------------------------------------------------


class TestRelease(unittest.TestCase):
    """Tests for rlsbl.commands.release."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
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

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_dry_run(self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch,
                             _commit_files, mock_run, _push):
        """Dry run should not modify any files."""
        # 1. git fetch origin --quiet (remote-ahead check)
        # 2. git rev-list --count HEAD..origin/main (0 commits behind)
        # 3. tag -l for current version (exists -> bump)
        # 4. tag -l for bumped version (doesn't exist -> proceed)
        # 5. git status --porcelain (pre-hook snapshot)
        # 6. git status --porcelain (post-hook snapshot)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        from rlsbl.commands.release import run_cmd

        # Read original file contents
        with open("package.json") as f:
            orig_pkg = f.read()
        with open("CHANGELOG.md") as f:
            orig_cl = f.read()

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", ["patch"], {"dry-run": True, "quiet": False})

        # Files should be unchanged
        with open("package.json") as f:
            self.assertEqual(f.read(), orig_pkg)
        with open("CHANGELOG.md") as f:
            self.assertEqual(f.read(), orig_cl)

    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.is_clean_tree", return_value=False)
    def test_release_dirty_tree(self, _clean, _gh_auth, _gh_inst):
        """Dirty working tree should cause SystemExit."""
        from rlsbl.commands.release import run_cmd

        with self.assertRaises(SystemExit) as ctx:
            run_cmd("npm", ["patch"], {"quiet": True})
        self.assertEqual(ctx.exception.code, 1)

    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    def test_release_behind_remote_aborts(self, _gh_inst, _gh_auth, _clean,
                                          _branch, mock_run):
        """Release should abort when local branch is behind origin."""
        from rlsbl.commands.release import run_cmd

        # git fetch succeeds, rev-list returns "3" (3 commits behind)
        mock_run.side_effect = [
            "",   # git fetch origin --quiet
            "3",  # git rev-list --count HEAD..origin/main
        ]

        with self.assertRaises(SystemExit) as ctx:
            run_cmd("npm", ["patch"], {"quiet": True})
        self.assertEqual(ctx.exception.code, 1)

    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_fetch_failure_warns_but_continues(self, _validate, _gen_cl,
                                                        _gh_inst, _gh_auth,
                                                        _clean, _branch, mock_run):
        """If git fetch fails, warn but don't block the release."""
        from rlsbl.commands.release import run_cmd

        # git fetch raises (no network), then tag -l calls for version checks
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git"),  # git fetch fails
            "v1.0.0",  # tag -l for current version (exists)
            "",         # tag -l for bumped version (doesn't exist)
            "",         # git status --porcelain (pre-hook snapshot)
            "",         # git status --porcelain (post-hook snapshot)
        ]

        # Should reach dry-run exit without aborting
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", ["patch"], {"quiet": False, "dry-run": True})

    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_skip_remote_check_flag(self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean,
                                             _branch, mock_run):
        """--skip-remote-check should bypass the remote-ahead check entirely."""
        from rlsbl.commands.release import run_cmd

        # Only tag -l calls, no fetch or rev-list
        mock_run.side_effect = [
            "v1.0.0",  # tag -l for current version (exists)
            "",         # tag -l for bumped version (doesn't exist)
            "",         # git status --porcelain (pre-hook snapshot)
            "",         # git status --porcelain (post-hook snapshot)
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", ["patch"], {"quiet": False, "dry-run": True, "skip-remote-check": True})


# ---------------------------------------------------------------------------
# Release commit autogenerated trailer tests
# ---------------------------------------------------------------------------


class TestReleaseCommitTrailers(unittest.TestCase):
    """Tests that release commits pass correct autogenerated flag to commit_files."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f, indent=2)
            f.write("\n")
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release.\n")
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,"description":"Bugfix","type":"fix"}\n')

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

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
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    def test_version_bump_commit_is_autogenerated(self, _changes_dir, _extract, _finalize,
                                                    _gen_ver_file, _validate, _gen_cl,
                                                    _deploy, _tag,
                                                    _gh_inst, _gh_auth, _clean, _branch,
                                                    mock_commit_files, mock_run, _push,
                                                    _lock, _unlock):
        """The version-bump commit should be marked autogenerated."""
        from rlsbl.commands.release import run_cmd

        mock_run.side_effect = [
            "",               # git fetch origin --quiet
            "0",              # git rev-list --count HEAD..origin/main
            "v1.0.0",         # git tag -l v1.0.0 (exists -> bump)
            "",               # git tag -l v1.0.1 (doesn't exist)
            "",               # git status --porcelain (pre-hook snapshot)
            "",               # git status --porcelain (post-hook snapshot)
            "",               # git status --porcelain (baseline_dirty in _run_release_mutating)
            "",               # git status --porcelain (re-check guard)
            "package.json",   # git diff --name-only -- package.json
            "M package.json", # git status --porcelain -- package.json
            "",               # git tag v1.0.1
            "",               # git push origin v1.0.1
            "",               # gh release create
            "abc123def",      # git rev-parse HEAD
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", ["patch"], {"yes": True, "quiet": False})

        # First call is the version-bump commit -- should be autogenerated
        version_bump_call = mock_commit_files.call_args_list[0]
        self.assertIn("v1.0.1", version_bump_call[0][0])
        self.assertTrue(version_bump_call[1].get("autogenerated", True))

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
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    def test_finalize_commit_is_autogenerated(self, _changes_dir, _extract, _finalize,
                                               _gen_ver_file, _validate, _gen_cl,
                                               _deploy, _tag,
                                               _gh_inst, _gh_auth, _clean, _branch,
                                               mock_commit_files, mock_run, _push,
                                               _lock, _unlock):
        """The changelog finalization commit should be marked autogenerated."""
        from rlsbl.commands.release import run_cmd

        mock_run.side_effect = [
            "",               # git fetch origin --quiet
            "0",              # git rev-list --count HEAD..origin/main
            "v1.0.0",         # git tag -l v1.0.0 (exists -> bump)
            "",               # git tag -l v1.0.1 (doesn't exist)
            "",               # git status --porcelain (pre-hook snapshot)
            "",               # git status --porcelain (post-hook snapshot)
            "",               # git status --porcelain (baseline_dirty in _run_release_mutating)
            "",               # git status --porcelain (re-check guard)
            "package.json",   # git diff --name-only -- package.json
            "M package.json", # git status --porcelain -- package.json
            "",               # git tag v1.0.1
            "",               # git push origin v1.0.1
            "",               # gh release create
            "abc123def",      # git rev-parse HEAD
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", ["patch"], {"yes": True, "quiet": False})

        # Second call is the finalization commit -- should have autogenerated=True
        finalize_call = mock_commit_files.call_args_list[1]
        self.assertIn("chore: finalize changelog", finalize_call[0][0])
        self.assertTrue(finalize_call[1].get("autogenerated", True))


# ---------------------------------------------------------------------------
# Porcelain parsing tests
# ---------------------------------------------------------------------------


class TestPorcelainParsing(unittest.TestCase):
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
        self.assertEqual(result, {"package.json", "pyproject.toml", ".rlsbl/lock"})

    def test_rename_entry(self):
        """Rename entries use 'R old -> new' format; parser should extract new path."""
        from rlsbl.commands.release import parse_porcelain_paths

        porcelain = "R  old.txt -> new.txt\nM  other.txt"
        result = parse_porcelain_paths(porcelain)
        self.assertIn("new.txt", result)
        self.assertIn("other.txt", result)

    def test_empty_output(self):
        """Empty porcelain output returns empty set."""
        from rlsbl.commands.release import parse_porcelain_paths

        self.assertEqual(parse_porcelain_paths(""), set())

    def test_blank_lines_ignored(self):
        """Blank lines in output are safely ignored."""
        from rlsbl.commands.release import parse_porcelain_paths

        porcelain = "M  file.txt\n\n?? untracked.txt"
        result = parse_porcelain_paths(porcelain)
        self.assertEqual(result, {"file.txt", "untracked.txt"})


# ---------------------------------------------------------------------------
# Undo command tests
# ---------------------------------------------------------------------------


class TestUndo(unittest.TestCase):
    """Tests for rlsbl.commands.undo."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    @patch("rlsbl.commands.undo.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.run")
    def test_undo_no_tags(self, mock_run, _gh_inst, _gh_auth, _clean):
        """When git describe raises, undo should exit with 'no tags found'."""
        mock_run.side_effect = Exception("no tags")

        from rlsbl.commands.undo import run_cmd

        with self.assertRaises(SystemExit) as ctx:
            run_cmd("npm", [], {"yes": True})
        self.assertEqual(ctx.exception.code, 1)

    @patch("rlsbl.commands.undo.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.undo.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.undo.is_clean_tree", return_value=False)
    def test_undo_dirty_tree(self, _clean, _gh_auth, _gh_inst):
        """Dirty working tree should cause SystemExit."""
        from rlsbl.commands.undo import run_cmd

        with self.assertRaises(SystemExit) as ctx:
            run_cmd("npm", [], {"yes": True})
        self.assertEqual(ctx.exception.code, 1)


# ---------------------------------------------------------------------------
# Check command tests
# ---------------------------------------------------------------------------


class TestCheck(unittest.TestCase):
    """Tests for rlsbl.commands.check — npm availability checks."""

    @patch("rlsbl.commands.check.subprocess.run")
    def test_check_npm_available(self, mock_subprocess_run):
        """When npm view raises CalledProcessError with 404, name is available."""
        from rlsbl.commands.check import check_npm_availability

        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            1, "npm", stderr="E404 Not Found"
        )
        result = check_npm_availability("nonexistent-pkg-xyz")
        self.assertEqual(result["status"], "available")

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
        self.assertEqual(result["status"], "taken")


# ---------------------------------------------------------------------------
# Pre-push check command tests
# ---------------------------------------------------------------------------


class TestPrePushCheck(unittest.TestCase):
    """Tests for rlsbl.commands.pre_push_check — version detection."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_detects_npm_version(self):
        """Detects version from package.json."""
        with open("package.json", "w") as f:
            json.dump({"name": "my-pkg", "version": "2.3.4"}, f)

        from rlsbl.commands.pre_push_check import _detect_version

        version, reg_name = _detect_version()
        self.assertEqual(version, "2.3.4")
        self.assertEqual(reg_name, "npm")

    def test_detects_pypi_version(self):
        """Detects version from pyproject.toml."""
        with open("pyproject.toml", "w") as f:
            f.write('[project]\nname = "my-pkg"\nversion = "0.5.0"\n')

        from rlsbl.commands.pre_push_check import _detect_version

        version, reg_name = _detect_version()
        self.assertEqual(version, "0.5.0")
        self.assertEqual(reg_name, "pypi")

    def test_detects_go_version(self):
        """Detects version from go.mod + VERSION file."""
        with open("go.mod", "w") as f:
            f.write("module github.com/user/myapp\n\ngo 1.22\n")
        with open("VERSION", "w") as f:
            f.write("1.4.0\n")

        from rlsbl.commands.pre_push_check import _detect_version

        version, reg_name = _detect_version()
        self.assertEqual(version, "1.4.0")
        self.assertEqual(reg_name, "go")

    def test_no_project(self):
        """Empty directory should return (None, None)."""
        from rlsbl.commands.pre_push_check import _detect_version

        version, reg_name = _detect_version()
        self.assertIsNone(version)
        self.assertIsNone(reg_name)


# ---------------------------------------------------------------------------
# Release target configuration tests
# ---------------------------------------------------------------------------


class TestResolveReleaseTargets(unittest.TestCase):
    """Tests for resolve_release_targets: config-based secondary target resolution."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_missing_config_falls_back_to_auto_detect(self):
        """Without release_targets in config, auto-detect all detected targets."""
        # Create selfdoc.json so docs target is detected
        with open("selfdoc.json", "w") as f:
            f.write("{}")

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {})
        # docs is detected via selfdoc.json
        self.assertIn("docs", result)
        # npm is the primary and must be excluded from secondaries
        self.assertNotIn("npm", result)

    def test_config_release_targets_restricts_secondaries(self):
        """release_targets in config restricts which secondaries run."""
        # Create selfdoc.json so docs target would be auto-detected
        with open("selfdoc.json", "w") as f:
            f.write("{}")
        # Config says only npm participates (but npm is primary, so secondaries = empty)
        os.makedirs(".rlsbl", exist_ok=True)
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"release_targets": ["npm"]}, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {})
        # docs is NOT in the configured list, so it should not appear
        self.assertNotIn("docs", result)
        # npm is primary, excluded from secondaries
        self.assertNotIn("npm", result)
        self.assertEqual(result, {})

    def test_config_release_targets_includes_docs(self):
        """release_targets listing docs includes it even without auto-detect."""
        os.makedirs(".rlsbl", exist_ok=True)
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"release_targets": ["npm", "docs"]}, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {})
        self.assertIn("docs", result)
        self.assertNotIn("npm", result)

    def test_primary_always_excluded_from_secondaries(self):
        """The primary target is never in the secondary set, even if config lists it."""
        os.makedirs(".rlsbl", exist_ok=True)
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"release_targets": ["npm", "docs"]}, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {})
        self.assertNotIn("npm", result)

    def test_unknown_target_in_config_ignored(self):
        """Unknown target names in config are silently filtered out."""
        os.makedirs(".rlsbl", exist_ok=True)
        with open(os.path.join(".rlsbl", "config.json"), "w") as f:
            json.dump({"release_targets": ["npm", "nonexistent", "docs"]}, f)

        from rlsbl.commands.release import resolve_release_targets

        result = resolve_release_targets("npm", {})
        self.assertIn("docs", result)
        self.assertNotIn("nonexistent", result)


class TestStatusJson(unittest.TestCase):
    """Tests for rlsbl.commands.status --json flag."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
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

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

    def test_status_json_output(self):
        """With --json, status should output valid JSON with expected keys."""
        from rlsbl.commands.status import run_cmd

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            run_cmd("npm", [], {"json": True})

        output = mock_out.getvalue()
        data = json.loads(output)
        expected_keys = {"name", "version", "target", "branch", "tag",
                         "clean", "changelog", "jsonl_coverage",
                         "commits_ahead", "commits_ahead_tag",
                         "ci", "publish"}
        self.assertEqual(set(data.keys()), expected_keys)
        self.assertEqual(data["name"], "test-pkg")
        self.assertEqual(data["version"], "0.1.0")
        self.assertEqual(data["target"], "npm")
        self.assertTrue(data["clean"])
        self.assertTrue(data["changelog"])


class TestStatusChangelogExemption(unittest.TestCase):
    """Tests for status exempting autogenerated commits from coverage."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
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

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

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
        data = _collect_status("npm")
        # Coverage should show 1/1 (the autogenerated commit exempted)
        self.assertIn("1/1", data["jsonl_coverage"])
        self.assertIn("1 exempted", data["jsonl_coverage"])

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
        data = _collect_status("npm")
        # Should show 1/1 without exemption note
        self.assertIn("1/1", data["jsonl_coverage"])
        self.assertNotIn("exempted", data["jsonl_coverage"])


class TestMigrateCommand(unittest.TestCase):
    """Tests for rlsbl.commands.migrate."""

    @patch("rlsbl.commands.migrate.subprocess.run")
    def test_migrate_no_migrable(self, mock_subprocess_run):
        """When migrable is not installed, print install instructions and exit 1."""
        mock_subprocess_run.side_effect = FileNotFoundError("migrable not found")

        from rlsbl.commands.migrate import run_cmd

        with self.assertRaises(SystemExit) as ctx:
            run_cmd(None, [], {})
        self.assertEqual(ctx.exception.code, 1)

    def test_migrate_shows_help_text(self):
        """The migrate command should appear in the CLI help output."""
        from rlsbl import app

        result = app.test(["--help"])
        self.assertIn("migrate", result.stdout)


class TestScaffoldAutoDetection:
    """Tests for scaffold auto-detection writing targets to config."""

    def test_single_npm_scaffold_writes_target_to_config(self, mock_git_repo):
        """After scaffolding a single npm project without existing config,
        .rlsbl/config.json should contain targets: ["npm"]."""
        pkg = {"name": "test-pkg", "version": "0.1.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"no-tag": True})

        config_path = mock_git_repo / ".rlsbl" / "config.json"
        assert config_path.exists(), ".rlsbl/config.json should be created"
        config = json.loads(config_path.read_text())
        assert "targets" in config, "config should have a 'targets' key"
        assert config["targets"] == ["npm"], f"expected ['npm'], got {config['targets']}"


class TestScaffoldUntrack(unittest.TestCase):
    """Tests for scaffold untracking files added to .gitignore."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)
        # Create a full git repo with initial commit
        subprocess.run(["git", "init", "-q", "."], check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], check=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True)
        # Create minimal package.json so npm registry is detected
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "0.1.0"}, f)
        subprocess.run(["git", "add", "package.json"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], check=True)

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

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
        self.assertEqual(result.stdout.strip(), target_file)

        # Run scaffold (which writes .gitignore containing .credentials.json)
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {"no-tag": True})

        # Verify it's no longer tracked
        result = subprocess.run(
            ["git", "ls-files", "--cached", target_file],
            capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.strip(), "",
                         f"{target_file} should no longer be tracked after scaffold")

        # Verify the file still exists on disk (untracked, not deleted)
        self.assertTrue(os.path.exists(target_file),
                        f"{target_file} should still exist on disk")

        # Verify the untrack was committed (not just staged)
        status = subprocess.run(
            ["git", "status", "--porcelain", target_file],
            capture_output=True, text=True,
        )
        self.assertEqual(status.stdout.strip(), "",
                         f"{target_file} removal should be committed, not just staged")


# ---------------------------------------------------------------------------
# Release rollback on push failure tests
# ---------------------------------------------------------------------------


class TestReleaseRollbackOnPushFailure(unittest.TestCase):
    """Tests that a failed push during release rolls back local commits and tags."""

    def setUp(self):
        self.orig_dir = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

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

        # Initial commit and tag
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], check=True)
        subprocess.run(["git", "tag", "v1.0.0"], check=True)

        # Record the pre-release HEAD SHA
        self.pre_release_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmp_dir)

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
    def test_rollback_on_push_failure(self, _gh_inst, _gh_auth, _validate,
                                      _gen_cl, _extract, _tag, _deploy, _push):
        """When git push fails, local commits and tag from the release must be undone."""
        from rlsbl.commands.release import run_cmd

        # Run release -- push_if_needed raises CalledProcessError
        with self.assertRaises(subprocess.CalledProcessError):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd("npm", ["patch"], {
                    "yes": True,
                    "quiet": True,
                    "skip-remote-check": True,
                })

        # After push failure, HEAD should be at the pre-release SHA
        # (version-bump and finalize commits should have been rolled back)
        post_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(post_sha, self.pre_release_sha,
                         "HEAD should be rolled back to pre-release position")

        # The tag for the attempted version should not exist locally
        tag_check = subprocess.run(
            ["git", "tag", "-l", "v1.0.1"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(tag_check, "",
                         "Tag v1.0.1 should not exist after failed push")


if __name__ == "__main__":
    unittest.main()
