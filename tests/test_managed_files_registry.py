"""Tests for the managed-files registry: schema migration, orphan detection, and deletion."""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import (
    BASES_DIR,
    HASHES_FILE,
    USER_OWNED,
    _OLD_HASHES_FILE,
    _is_orphan_protected,
    file_hash,
    load_hashes,
    save_hashes,
)
from rlsbl.context import ProjectContext


def _ctx(root="."):
    return ProjectContext(project_root=Path(root), workspace_root=None, config={})


def _make_npm_project(repo):
    """Create a minimal npm project."""
    pkg = {"name": "testpkg", "version": "0.1.0"}
    (repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")


class TestRegistrySchema:
    """Tests for the versioned managed-files.json schema."""

    def test_registry_written_on_scaffold(self, mock_git_repo):
        """Scaffold writes managed-files.json with version and files fields."""
        from rlsbl.commands.init_cmd import run_cmd

        _make_npm_project(mock_git_repo)
        run_cmd("npm", [], {"no-tag": True, "no-commit": True}, ctx=_ctx())

        assert os.path.exists(HASHES_FILE)
        with open(HASHES_FILE) as f:
            data = json.load(f)
        assert data["version"] == 1
        assert isinstance(data["files"], dict)
        assert len(data["files"]) > 0

    def test_save_hashes_writes_versioned_schema(self, tmp_project):
        """save_hashes wraps the files dict in a versioned envelope."""
        os.makedirs(".rlsbl", exist_ok=True)
        save_hashes({"a.txt": "abc123"})

        with open(HASHES_FILE) as f:
            data = json.load(f)
        assert data == {"version": 1, "files": {"a.txt": "abc123"}}

    def test_load_hashes_reads_versioned_schema(self, tmp_project):
        """load_hashes extracts files from the versioned envelope."""
        os.makedirs(".rlsbl", exist_ok=True)
        with open(HASHES_FILE, "w") as f:
            json.dump({"version": 1, "files": {"b.txt": "def456"}}, f)

        result = load_hashes()
        assert result == {"b.txt": "def456"}

    def test_load_hashes_empty_when_no_file(self, tmp_project):
        """load_hashes returns empty dict when neither file exists."""
        result = load_hashes()
        assert result == {}


class TestBackwardCompatMigration:
    """Tests for hashes.json -> managed-files.json migration."""

    def test_backward_compat_migration(self, tmp_project):
        """Old hashes.json is migrated to managed-files.json and deleted."""
        os.makedirs(".rlsbl", exist_ok=True)
        old_data = {".github/workflows/ci.yml": "abc123", ".rlsbl/hooks/pre-release.sh": "def456"}
        with open(_OLD_HASHES_FILE, "w") as f:
            json.dump(old_data, f)

        result = load_hashes()

        # Returns the old data
        assert result == old_data
        # New file exists with versioned schema
        assert os.path.exists(HASHES_FILE)
        with open(HASHES_FILE) as f:
            data = json.load(f)
        assert data["version"] == 1
        assert data["files"] == old_data
        # Old file is deleted
        assert not os.path.exists(_OLD_HASHES_FILE)

    def test_migration_prefers_new_file(self, tmp_project):
        """When both files exist, managed-files.json takes precedence."""
        os.makedirs(".rlsbl", exist_ok=True)
        # Write old file
        with open(_OLD_HASHES_FILE, "w") as f:
            json.dump({"old": "data"}, f)
        # Write new file
        with open(HASHES_FILE, "w") as f:
            json.dump({"version": 1, "files": {"new": "data"}}, f)

        result = load_hashes()
        assert result == {"new": "data"}
        # Old file is NOT deleted (new file took precedence)
        assert os.path.exists(_OLD_HASHES_FILE)


class TestOrphanProtection:
    """Tests for _is_orphan_protected."""

    def test_user_owned_protected(self):
        """USER_OWNED files are always protected."""
        for path in USER_OWNED:
            assert _is_orphan_protected(path), f"{path} should be protected"

    def test_rlsbl_internal_protected(self):
        """Most .rlsbl/ paths are protected."""
        assert _is_orphan_protected(".rlsbl/config.json")
        assert _is_orphan_protected(".rlsbl/version")
        assert _is_orphan_protected(".rlsbl/changes/unreleased.jsonl")

    def test_rlsbl_lint_not_protected(self):
        """.rlsbl/lint/ configs can be orphaned."""
        assert not _is_orphan_protected(os.path.join(".rlsbl", "lint", "ruff.toml"))

    def test_rlsbl_bases_not_protected(self):
        """.rlsbl/bases/ files can be orphaned."""
        assert not _is_orphan_protected(os.path.join(".rlsbl", "bases", ".github", "workflows", "ci.yml"))

    def test_workflow_files_not_protected(self):
        """Normal workflow files are not protected."""
        assert not _is_orphan_protected(".github/workflows/ci.yml")
        assert not _is_orphan_protected(".github/workflows/publish.yml")

    def test_user_owned_never_deleted(self, mock_git_repo):
        """USER_OWNED files in registry are not deleted when absent from new hashes."""
        from rlsbl.commands.init_cmd import _finalize_scaffold

        _make_npm_project(mock_git_repo)

        # Set up a USER_OWNED file in the existing hashes
        user_file = "CHANGELOG.md"
        (mock_git_repo / user_file).write_text("# Changelog\n")
        existing_hashes = {user_file: file_hash(user_file)}

        # finalize with empty new hashes (the user_owned file is "orphaned")
        created = []
        skipped = []
        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd._install_or_update_pre_push_hook"):
                _finalize_scaffold(
                    existing_hashes, [{}],
                    created, skipped, [],
                    flags={"no-commit": True, "no-tag": True},
                    project_root=mock_git_repo,
                    config={},
                )

        # File must still exist
        assert os.path.exists(user_file)
        # File must still be in registry (not removed)
        assert user_file in existing_hashes


class TestOrphanDetection:
    """Tests for orphan detection and deletion in _finalize_scaffold."""

    def _run_finalize(self, existing_hashes, new_hashes_dict, mock_git_repo, flags=None):
        """Helper to run _finalize_scaffold with orphan detection."""
        from rlsbl.commands.init_cmd import _finalize_scaffold

        if flags is None:
            flags = {"no-commit": True, "no-tag": True}
        else:
            flags = {**flags, "no-commit": True, "no-tag": True}

        created = []
        skipped = []
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            with patch("rlsbl.commands.init_cmd._install_or_update_pre_push_hook"):
                _finalize_scaffold(
                    existing_hashes, [new_hashes_dict],
                    created, skipped, [],
                    flags=flags,
                    project_root=mock_git_repo,
                    config={},
                )
        return created, skipped, mock_out.getvalue()

    def test_orphan_deleted_on_rescaffold(self, mock_git_repo):
        """File present in registry but absent from new hashes is deleted."""
        _make_npm_project(mock_git_repo)

        # Create an orphan file
        orphan = ".github/workflows/ci-old.yml"
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        with open(orphan, "w") as f:
            f.write("# old workflow\n")
        orphan_hash = file_hash(orphan)

        existing_hashes = {orphan: orphan_hash}
        new_hashes = {}  # orphan not in new hashes

        created, _, _ = self._run_finalize(existing_hashes, new_hashes, mock_git_repo)

        assert not os.path.exists(orphan)
        assert orphan not in existing_hashes
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

        existing_hashes = {orphan: orphan_hash}
        self._run_finalize(existing_hashes, {}, mock_git_repo)

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

        existing_hashes = {orphan: original_hash}
        self._run_finalize(existing_hashes, {}, mock_git_repo)

        # File must still exist (protected by hash mismatch)
        assert os.path.exists(orphan)
        assert orphan in existing_hashes
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

        existing_hashes = {orphan: original_hash}
        created, _, _ = self._run_finalize(
            existing_hashes, {}, mock_git_repo, flags={"force": True}
        )

        assert not os.path.exists(orphan)
        assert orphan not in existing_hashes
        assert any(t == orphan and s == "removed (orphan)" for t, s in created)

    def test_dry_run_shows_would_remove(self, mock_git_repo):
        """Dry-run prints 'Would remove' but does NOT delete."""
        _make_npm_project(mock_git_repo)

        orphan = ".github/workflows/ci-old.yml"
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        with open(orphan, "w") as f:
            f.write("# old workflow\n")
        orphan_hash = file_hash(orphan)

        existing_hashes = {orphan: orphan_hash}
        _, _, stdout = self._run_finalize(
            existing_hashes, {}, mock_git_repo, flags={"dry-run": True}
        )

        # File must still exist
        assert os.path.exists(orphan)
        # "Would remove" must be in output
        assert f"Would remove: {orphan}" in stdout

    def test_already_gone_from_disk(self, mock_git_repo):
        """Orphan that doesn't exist on disk is just removed from registry."""
        _make_npm_project(mock_git_repo)

        ghost = ".github/workflows/ghost.yml"
        existing_hashes = {ghost: "deadbeef"}
        self._run_finalize(existing_hashes, {}, mock_git_repo)

        assert ghost not in existing_hashes
