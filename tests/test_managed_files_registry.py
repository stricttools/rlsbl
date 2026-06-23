"""Tests for the managed-files registry and hashes.json: separate concerns for orphan detection and change tracking."""

import json
import os
import subprocess as real_subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import (
    BASES_DIR,
    HASHES_FILE,
    MANAGED_FILES,
    file_hash,
    load_hashes,
    load_managed_files,
    save_hashes,
    save_managed_files,
)
from rlsbl.context import ProjectContext


def _ctx(root="."):
    return ProjectContext(project_root=Path(root), workspace_root=None, config={})


def _make_npm_project(repo):
    """Create a minimal npm project."""
    pkg = {"name": "testpkg", "version": "0.1.0"}
    (repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")


class TestHashesJson:
    """Tests for hashes.json (flat {path: hash} dict for change detection)."""

    def test_hashes_written_on_scaffold(self, mock_git_repo):
        """Scaffold writes hashes.json as a flat dict."""
        from rlsbl.commands.init_cmd import run_cmd

        _make_npm_project(mock_git_repo)
        run_cmd("npm", [], {"no-tag": True, "no-commit": True}, ctx=_ctx())

        assert os.path.exists(HASHES_FILE)
        with open(HASHES_FILE) as f:
            data = json.load(f)
        # Flat dict, not wrapped in a versioned envelope
        assert isinstance(data, dict)
        assert "version" not in data
        assert "files" not in data
        assert len(data) > 0

    def test_save_hashes_writes_flat_dict(self, tmp_project):
        """save_hashes writes a flat {path: hash} dict."""
        os.makedirs(".rlsbl", exist_ok=True)
        save_hashes({"a.txt": "abc123"})

        with open(HASHES_FILE) as f:
            data = json.load(f)
        assert data == {"a.txt": "abc123"}

    def test_load_hashes_reads_flat_dict(self, tmp_project):
        """load_hashes reads a flat {path: hash} dict."""
        os.makedirs(".rlsbl", exist_ok=True)
        with open(HASHES_FILE, "w") as f:
            json.dump({"b.txt": "def456"}, f)

        result = load_hashes()
        assert result == {"b.txt": "def456"}

    def test_load_hashes_empty_when_no_file(self, tmp_project):
        """load_hashes returns empty dict when file doesn't exist."""
        result = load_hashes()
        assert result == {}


class TestManagedFilesJson:
    """Tests for managed-files.json (versioned envelope for orphan tracking)."""

    def test_managed_files_written_on_scaffold(self, mock_git_repo):
        """Scaffold writes managed-files.json with version and files fields."""
        from rlsbl.commands.init_cmd import run_cmd

        _make_npm_project(mock_git_repo)
        run_cmd("npm", [], {"no-tag": True, "no-commit": True}, ctx=_ctx())

        assert os.path.exists(MANAGED_FILES)
        with open(MANAGED_FILES) as f:
            data = json.load(f)
        assert data["version"] == 1
        assert isinstance(data["files"], dict)
        assert len(data["files"]) > 0

    def test_save_managed_files_writes_versioned_schema(self, tmp_project):
        """save_managed_files wraps the files dict in a versioned envelope."""
        os.makedirs(".rlsbl", exist_ok=True)
        save_managed_files({"a.txt": "abc123"})

        with open(MANAGED_FILES) as f:
            data = json.load(f)
        assert data == {"version": 1, "files": {"a.txt": "abc123"}}

    def test_load_managed_files_reads_versioned_schema(self, tmp_project):
        """load_managed_files extracts files from the versioned envelope."""
        os.makedirs(".rlsbl", exist_ok=True)
        with open(MANAGED_FILES, "w") as f:
            json.dump({"version": 1, "files": {"b.txt": "def456"}}, f)

        result = load_managed_files()
        assert result == {"b.txt": "def456"}

    def test_load_managed_files_empty_when_no_file(self, tmp_project):
        """load_managed_files returns empty dict when file doesn't exist."""
        result = load_managed_files()
        assert result == {}


class TestBothFilesCoexist:
    """Tests that hashes.json and managed-files.json coexist independently."""

    def test_scaffold_creates_both_files(self, mock_git_repo):
        """Scaffold creates both hashes.json and managed-files.json."""
        from rlsbl.commands.init_cmd import run_cmd

        _make_npm_project(mock_git_repo)
        run_cmd("npm", [], {"no-tag": True, "no-commit": True}, ctx=_ctx())

        assert os.path.exists(HASHES_FILE), "hashes.json should exist"
        assert os.path.exists(MANAGED_FILES), "managed-files.json should exist"

    def test_hashes_and_managed_files_independent(self, tmp_project):
        """Writing to one file doesn't affect the other."""
        os.makedirs(".rlsbl", exist_ok=True)

        save_hashes({"x.txt": "hash1"})
        save_managed_files({"y.txt": "hash2"})

        assert load_hashes() == {"x.txt": "hash1"}
        assert load_managed_files() == {"y.txt": "hash2"}

    def test_hashes_flat_managed_files_versioned(self, tmp_project):
        """hashes.json is a flat dict, managed-files.json has a versioned envelope."""
        os.makedirs(".rlsbl", exist_ok=True)

        save_hashes({"a.txt": "h1"})
        save_managed_files({"a.txt": "h1"})

        with open(HASHES_FILE) as f:
            hashes_data = json.load(f)
        with open(MANAGED_FILES) as f:
            managed_data = json.load(f)

        # hashes.json: flat dict
        assert "version" not in hashes_data
        assert hashes_data == {"a.txt": "h1"}

        # managed-files.json: versioned envelope
        assert managed_data["version"] == 1
        assert managed_data["files"] == {"a.txt": "h1"}


class TestOrphanDetection:
    """Tests for orphan detection using managed-files.json in _finalize_scaffold."""

    def _run_finalize(self, existing_hashes, new_hashes_dict, mock_git_repo,
                      flags=None, managed_files=None):
        """Helper to run _finalize_scaffold with orphan detection.

        managed_files: dict to pre-populate managed-files.json with. If None,
        managed-files.json is not written (simulates first scaffold).
        """
        from rlsbl.commands.init_cmd import _finalize_scaffold

        if flags is None:
            flags = {"no-commit": True, "no-tag": True}
        else:
            flags = {**flags, "no-commit": True, "no-tag": True}

        # Pre-populate managed-files.json if provided
        if managed_files is not None:
            save_managed_files(managed_files)

        created = []
        skipped = []

        original_run = real_subprocess.run

        def mock_subprocess_run(cmd, *args, **kwargs):
            """Intercept saferm calls and perform os.unlink instead."""
            if isinstance(cmd, list) and cmd and cmd[0] == "saferm":
                target_file = cmd[-1]
                if os.path.exists(target_file):
                    os.unlink(target_file)
                return real_subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            with patch("rlsbl.commands.init_cmd._install_or_update_pre_push_hook"):
                with patch("rlsbl.commands.init_cmd.subprocess.run",
                           side_effect=mock_subprocess_run):
                    _finalize_scaffold(
                        existing_hashes, [new_hashes_dict],
                        created, skipped, [],
                        flags=flags,
                        project_root=mock_git_repo,
                        config={},
                    )
        return created, skipped, mock_out.getvalue()

    def test_orphan_deleted_on_rescaffold(self, mock_git_repo):
        """File in managed-files but absent from new hashes is deleted."""
        _make_npm_project(mock_git_repo)

        # Create an orphan file
        orphan = ".github/workflows/ci-old.yml"
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        with open(orphan, "w") as f:
            f.write("# old workflow\n")
        orphan_hash = file_hash(orphan)

        # Orphan is in managed-files (was template-derived), not in new hashes
        managed = {orphan: orphan_hash}
        existing_hashes = {orphan: orphan_hash}
        new_hashes = {}

        created, _, _ = self._run_finalize(
            existing_hashes, new_hashes, mock_git_repo, managed_files=managed
        )

        assert not os.path.exists(orphan)
        assert any(t == orphan and s == "removed (orphan)" for t, s in created)

    def test_orphan_base_deleted(self, mock_git_repo):
        """When an orphan is deleted, its .rlsbl/bases/ counterpart is also deleted."""
        _make_npm_project(mock_git_repo)

        orphan = ".github/workflows/ci-old.yml"
        base_path = os.path.join(BASES_DIR, orphan)
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        os.makedirs(os.path.dirname(base_path), exist_ok=True)
        with open(orphan, "w") as f:
            f.write("# old workflow\n")
        with open(base_path, "w") as f:
            f.write("# base content\n")
        orphan_hash = file_hash(orphan)

        managed = {orphan: orphan_hash}
        self._run_finalize({}, {}, mock_git_repo, managed_files=managed)

        assert not os.path.exists(orphan)
        assert not os.path.exists(base_path)

    def test_hash_mismatch_protects_user_modified(self, mock_git_repo, capsys):
        """Modified orphan is NOT deleted (hash mismatch), warning printed."""
        _make_npm_project(mock_git_repo)

        orphan = ".github/workflows/ci-old.yml"
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        with open(orphan, "w") as f:
            f.write("# original\n")
        original_hash = file_hash(orphan)

        # Modify the file after recording hash
        with open(orphan, "w") as f:
            f.write("# user modified this\n")

        managed = {orphan: original_hash}
        self._run_finalize({}, {}, mock_git_repo, managed_files=managed)

        # File must still exist (protected by hash mismatch)
        assert os.path.exists(orphan)
        # Warning must be printed to stderr
        err = capsys.readouterr().err
        assert "has been modified" in err
        assert orphan in err

    def test_force_overrides_hash_check(self, mock_git_repo):
        """With force=True, modified orphan IS deleted despite hash mismatch."""
        _make_npm_project(mock_git_repo)

        orphan = ".github/workflows/ci-old.yml"
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        with open(orphan, "w") as f:
            f.write("# original\n")
        original_hash = file_hash(orphan)

        # Modify the file
        with open(orphan, "w") as f:
            f.write("# user modified this\n")

        managed = {orphan: original_hash}
        created, _, _ = self._run_finalize(
            {}, {}, mock_git_repo, flags={"force": True}, managed_files=managed
        )

        assert not os.path.exists(orphan)
        assert any(t == orphan and s == "removed (orphan)" for t, s in created)

    def test_dry_run_shows_would_remove(self, mock_git_repo):
        """Dry-run prints 'Would remove' but does NOT delete."""
        _make_npm_project(mock_git_repo)

        orphan = ".github/workflows/ci-old.yml"
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        with open(orphan, "w") as f:
            f.write("# old workflow\n")
        orphan_hash = file_hash(orphan)

        managed = {orphan: orphan_hash}
        _, _, stdout = self._run_finalize(
            {}, {}, mock_git_repo, flags={"dry-run": True}, managed_files=managed
        )

        # File must still exist
        assert os.path.exists(orphan)
        # "Would remove" must be in output
        assert f"Would remove: {orphan}" in stdout

    def test_already_gone_from_disk(self, mock_git_repo):
        """Orphan not on disk is silently skipped (no crash)."""
        _make_npm_project(mock_git_repo)

        ghost = ".github/workflows/ghost.yml"
        managed = {ghost: "deadbeef"}
        # Should not crash even though file doesn't exist
        self._run_finalize({}, {}, mock_git_repo, managed_files=managed)

    def test_non_template_files_never_orphaned(self, mock_git_repo):
        """Files not in managed-files.json (e.g., CLAUDE.md, .gitignore) are never orphaned.

        This is the core fix: managed-files.json only contains template-derived files
        from apply_plans, so files like CLAUDE.md, .claude/settings.json, .gitignore
        that scaffold touches via other paths never enter the managed-files registry
        and can never be false orphans.
        """
        _make_npm_project(mock_git_repo)

        # Create files that should NOT be orphaned
        (mock_git_repo / "CLAUDE.md").write_text("# Project docs\n")
        (mock_git_repo / ".gitignore").write_text("node_modules/\n")
        claude_dir = mock_git_repo / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text("{}\n")

        # These files are in hashes.json (scaffold touched them) but NOT in managed-files
        existing_hashes = {
            "CLAUDE.md": file_hash("CLAUDE.md"),
            ".gitignore": file_hash(".gitignore"),
            ".claude/settings.json": file_hash(".claude/settings.json"),
        }
        # managed-files is empty (these files never went through apply_plans)
        managed = {}
        new_hashes = {}

        self._run_finalize(
            existing_hashes, new_hashes, mock_git_repo, managed_files=managed
        )

        # All files must still exist -- they were never in managed-files.json
        assert os.path.exists("CLAUDE.md")
        assert os.path.exists(".gitignore")
        assert os.path.exists(".claude/settings.json")

    def test_user_owned_in_managed_files_still_deleted(self, mock_git_repo):
        """USER_OWNED files ARE deleted if they appear in managed-files.json.

        With the new design, _is_orphan_protected is gone. The managed-files registry
        only contains template-derived files by construction. If somehow a USER_OWNED
        file ends up in managed-files.json, orphan detection treats it like any other
        managed file -- this is correct because managed-files.json is the authority.
        """
        _make_npm_project(mock_git_repo)

        # CHANGELOG.md is USER_OWNED but we put it in managed-files to test
        user_file = "CHANGELOG.md"
        (mock_git_repo / user_file).write_text("# Changelog\n")
        user_hash = file_hash(user_file)

        managed = {user_file: user_hash}
        existing_hashes = {user_file: user_hash}

        created, _, _ = self._run_finalize(
            existing_hashes, {}, mock_git_repo, managed_files=managed
        )

        # With the new design, if it's in managed-files and hash matches, it gets deleted
        assert not os.path.exists(user_file)
        assert any(t == user_file and s == "removed (orphan)" for t, s in created)
