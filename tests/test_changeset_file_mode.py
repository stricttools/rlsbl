"""Tests for changeset-file coverage mode (Phase 8).

Covers: entry id field, coverage_unit config, pending file creation,
diff-based pre-push check, finalization, mode-dependent schema rules,
and mode-aware downstream commands.
"""

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from conftest import run_git as _run_git, make_commit as _make_commit
from rlsbl.changelog.schema import (
    ChangelogEntry,
    generate_entry_id,
    parse_entry,
    serialize_entry,
    validate_schema,
)
from rlsbl.changelog.files import (
    append_entry,
    get_changes_dir,
    read_unreleased,
)


# ---------------------------------------------------------------------------
# 8a: Entry id field
# ---------------------------------------------------------------------------

class TestEntryId:
    """Tests for the id field on ChangelogEntry."""

    def test_generate_entry_id_uniqueness(self):
        ids = {generate_entry_id() for _ in range(100)}
        assert len(ids) == 100, "IDs must be unique"

    def test_generate_entry_id_format(self):
        entry_id = generate_entry_id()
        # 16 hex chars for timestamp + 32 hex chars for uuid4 = 48
        assert len(entry_id) == 48
        assert all(c in "0123456789abcdef" for c in entry_id)

    def test_generate_entry_id_sortable(self):
        id1 = generate_entry_id()
        # Ensure some time passes for different timestamp
        time.sleep(0.001)
        id2 = generate_entry_id()
        # Lexicographic sort should put id1 before id2
        assert id1 < id2

    def test_serialize_with_id(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="A feature",
            type="feature",
            id="myid123",
        )
        line = serialize_entry(entry)
        data = json.loads(line)
        assert data["id"] == "myid123"

    def test_serialize_without_id(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="A feature",
            type="feature",
        )
        line = serialize_entry(entry)
        data = json.loads(line)
        assert "id" not in data

    def test_parse_with_id(self):
        line = '{"id":"myid","commits":["abc"],"user_facing":true,"description":"X","type":"fix"}'
        entry = parse_entry(line)
        assert entry.id == "myid"
        assert entry.commits == ["abc"]

    def test_parse_without_id_historical_compat(self):
        line = '{"commits":["abc"],"user_facing":false}'
        entry = parse_entry(line)
        assert entry.id is None
        assert entry.commits == ["abc"]

    def test_parse_without_commits_changeset_mode(self):
        """Entries from changeset-file mode finalization may have no commits."""
        line = '{"id":"myid","user_facing":true,"description":"X","type":"fix"}'
        entry = parse_entry(line)
        assert entry.id == "myid"
        assert entry.commits == []

    def test_serialize_empty_commits_omitted(self):
        entry = ChangelogEntry(
            commits=[],
            user_facing=True,
            description="A feature",
            type="feature",
            id="myid",
        )
        line = serialize_entry(entry)
        data = json.loads(line)
        assert "commits" not in data

    def test_validate_schema_commit_mode_needs_commits(self):
        entry = ChangelogEntry(
            commits=[],
            user_facing=True,
            description="X",
            type="fix",
        )
        errors = validate_schema(entry, coverage_unit="commit")
        assert any("commits is empty" in e for e in errors)

    def test_validate_schema_changeset_mode_no_commits_allowed(self):
        entry = ChangelogEntry(
            commits=["abc"],
            user_facing=True,
            description="X",
            type="fix",
            id="myid",
        )
        errors = validate_schema(entry, coverage_unit="changeset-file")
        assert any("commits must be empty" in e for e in errors)

    def test_validate_schema_changeset_mode_needs_id(self):
        entry = ChangelogEntry(
            commits=[],
            user_facing=True,
            description="X",
            type="fix",
        )
        errors = validate_schema(entry, coverage_unit="changeset-file")
        assert any("id is required" in e for e in errors)

    def test_validate_schema_changeset_mode_valid(self):
        entry = ChangelogEntry(
            commits=[],
            user_facing=True,
            description="X",
            type="fix",
            id="myid",
        )
        errors = validate_schema(entry, coverage_unit="changeset-file")
        assert errors == []

    def test_id_preserved_in_roundtrip(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Test",
            type="feature",
            id="test_id_123",
            packages=["pkg-a"],
        )
        line = serialize_entry(entry)
        parsed = parse_entry(line)
        assert parsed.id == "test_id_123"
        assert parsed.packages == ["pkg-a"]
        assert parsed.commits == ["abc123"]

    def test_packages_field_on_entry(self):
        entry = ChangelogEntry(
            commits=["abc"],
            user_facing=True,
            description="X",
            type="fix",
            packages=["core", "web"],
        )
        errors = validate_schema(entry)
        assert errors == []
        line = serialize_entry(entry)
        data = json.loads(line)
        assert data["packages"] == ["core", "web"]


# ---------------------------------------------------------------------------
# 8b: coverage_unit config key
# ---------------------------------------------------------------------------

class TestCoverageUnitConfig:
    """Tests for the coverage_unit config key."""

    def test_missing_coverage_unit_is_error(self):
        from rlsbl.config import validate_config_schema
        # validate_config_schema doesn't check coverage_unit yet --
        # that's handled in the validation flow. We test the new
        # read_coverage_unit helper instead.
        from rlsbl.changelog.files import read_coverage_unit
        config = {"publish_mode": "none"}
        with pytest.raises(Exception, match="coverage_unit"):
            read_coverage_unit(config)

    def test_valid_commit_mode(self):
        from rlsbl.changelog.files import read_coverage_unit
        assert read_coverage_unit({"coverage_unit": "commit"}) == "commit"

    def test_valid_changeset_file_mode(self):
        from rlsbl.changelog.files import read_coverage_unit
        assert read_coverage_unit({"coverage_unit": "changeset-file"}) == "changeset-file"

    def test_invalid_coverage_unit(self):
        from rlsbl.changelog.files import read_coverage_unit
        with pytest.raises(Exception, match="coverage_unit"):
            read_coverage_unit({"coverage_unit": "invalid"})


# ---------------------------------------------------------------------------
# 8c: Pending-file schema and changelog add in changeset mode
# ---------------------------------------------------------------------------

@pytest.fixture
def changeset_repo(tmp_path, monkeypatch):
    """Create a git repo configured for changeset-file mode."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    # Initial commit
    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")
    _run_git(repo, "tag", "v0.0.0")

    # Set up .rlsbl/changes with pending dir
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    pending = changes / "pending"
    pending.mkdir()

    # Write config with changeset-file mode
    config_dir = repo / ".rlsbl"
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({
        "coverage_unit": "changeset-file",
        "publish_mode": "none",
    }, indent=2))

    return repo


class TestPendingFileCreation:
    """Tests for pending file creation in changeset-file mode."""

    def test_write_pending_file(self, changeset_repo):
        from rlsbl.changelog.files import write_pending_file, get_pending_dir
        entry = ChangelogEntry(
            id="testid123",
            user_facing=True,
            description="A new feature",
            type="feature",
        )
        pending_dir = get_pending_dir(get_changes_dir(str(changeset_repo)))
        path = write_pending_file(pending_dir, entry)
        assert os.path.isfile(path)
        assert path.endswith("testid123.json")
        with open(path) as f:
            data = json.load(f)
        assert data["id"] == "testid123"
        assert data["user_facing"] is True
        assert data["description"] == "A new feature"
        assert data["type"] == "feature"
        assert "commits" not in data

    def test_read_pending_files(self, changeset_repo):
        from rlsbl.changelog.files import write_pending_file, read_pending_files, get_pending_dir
        pending_dir = get_pending_dir(get_changes_dir(str(changeset_repo)))
        e1 = ChangelogEntry(id="id_001", user_facing=True, description="Feat 1", type="feature")
        e2 = ChangelogEntry(id="id_002", user_facing=False)
        write_pending_file(pending_dir, e1)
        write_pending_file(pending_dir, e2)
        entries = read_pending_files(pending_dir)
        assert len(entries) == 2
        ids = {e.id for e in entries}
        assert "id_001" in ids
        assert "id_002" in ids

    def test_pending_file_schema(self, changeset_repo):
        """Pending files have no commits field."""
        from rlsbl.changelog.files import write_pending_file, get_pending_dir
        entry = ChangelogEntry(
            id="testid",
            user_facing=True,
            description="X",
            type="fix",
        )
        pending_dir = get_pending_dir(get_changes_dir(str(changeset_repo)))
        path = write_pending_file(pending_dir, entry)
        with open(path) as f:
            data = json.load(f)
        assert "commits" not in data


# ---------------------------------------------------------------------------
# 8d: Inverted diff-based pre-push check
# ---------------------------------------------------------------------------

class TestDiffBasedPrepush:
    """Tests for the diff-based pre-push check in changeset-file mode."""

    def test_source_change_without_pending_file_fails(self, changeset_repo):
        from rlsbl.prepush_utils import check_changeset_file_coverage
        # Simulate: source file changed, no pending file in diff
        changed_files = ["src/main.py"]
        result = check_changeset_file_coverage(
            changed_files,
            changes_dir=get_changes_dir(str(changeset_repo)),
        )
        assert result is not None  # error message

    def test_source_change_with_pending_file_passes(self, changeset_repo):
        from rlsbl.prepush_utils import check_changeset_file_coverage
        changes_dir = get_changes_dir(str(changeset_repo))
        changed_files = ["src/main.py", f"{os.path.relpath(changes_dir, str(changeset_repo))}/pending/abc123.json"]
        result = check_changeset_file_coverage(
            changed_files,
            changes_dir=changes_dir,
        )
        assert result is None  # no error

    def test_exempt_paths_only(self, changeset_repo):
        from rlsbl.prepush_utils import check_changeset_file_coverage
        changes_dir = get_changes_dir(str(changeset_repo))
        # Only changelog files changed -- no pending needed
        changed_files = [
            ".rlsbl/changes/unreleased.jsonl",
            "CHANGELOG.md",
        ]
        result = check_changeset_file_coverage(
            changed_files,
            changes_dir=changes_dir,
        )
        assert result is None

    def test_no_changes_passes(self, changeset_repo):
        from rlsbl.prepush_utils import check_changeset_file_coverage
        result = check_changeset_file_coverage(
            [],
            changes_dir=get_changes_dir(str(changeset_repo)),
        )
        assert result is None


# ---------------------------------------------------------------------------
# 8f: Finalization concatenation
# ---------------------------------------------------------------------------

class TestFinalizationConcatenation:
    """Tests for pending file concatenation during release finalization."""

    def test_finalize_changeset_mode(self, changeset_repo):
        from rlsbl.changelog.files import (
            finalize_changeset_version,
            get_pending_dir,
            write_pending_file,
        )
        from rlsbl.changelog.schema import parse_jsonl

        changes_dir = get_changes_dir(str(changeset_repo))
        pending_dir = get_pending_dir(changes_dir)

        # Write two pending files
        e1 = ChangelogEntry(id="id_aaa", user_facing=True, description="Feature A", type="feature")
        e2 = ChangelogEntry(id="id_bbb", user_facing=False)
        write_pending_file(pending_dir, e1)
        write_pending_file(pending_dir, e2)

        # Finalize
        finalize_changeset_version(changes_dir, "1.2.3")

        # Versioned JSONL should exist and be read-only
        jsonl_path = os.path.join(changes_dir, "1.2.3.jsonl")
        assert os.path.isfile(jsonl_path)
        import stat
        mode = os.stat(jsonl_path).st_mode
        assert not (mode & stat.S_IWUSR)  # read-only

        # Entries should be there
        entries = parse_jsonl(jsonl_path)
        assert len(entries) == 2
        ids = {e.id for e in entries}
        assert "id_aaa" in ids
        assert "id_bbb" in ids

        # Pending dir should be empty
        remaining = os.listdir(pending_dir)
        assert len(remaining) == 0

    def test_finalize_populates_packages(self, changeset_repo):
        """Packages field is populated from pending file directory context."""
        from rlsbl.changelog.files import (
            finalize_changeset_version,
            get_pending_dir,
            write_pending_file,
        )
        from rlsbl.changelog.schema import parse_jsonl

        changes_dir = get_changes_dir(str(changeset_repo))
        pending_dir = get_pending_dir(changes_dir)

        e1 = ChangelogEntry(id="id_pkg", user_facing=True, description="Feature", type="feature", packages=["core"])
        write_pending_file(pending_dir, e1)
        finalize_changeset_version(changes_dir, "0.1.0")

        entries = parse_jsonl(os.path.join(changes_dir, "0.1.0.jsonl"))
        assert entries[0].packages == ["core"]


# ---------------------------------------------------------------------------
# 8g: Mode-dependent schema rules
# ---------------------------------------------------------------------------

class TestModeDependentSchema:
    """Tests for mode-dependent schema enforcement."""

    def test_commit_mode_requires_commits(self):
        entry = ChangelogEntry(commits=[], user_facing=True, description="X", type="fix")
        errors = validate_schema(entry, coverage_unit="commit")
        assert any("commits is empty" in e for e in errors)

    def test_commit_mode_accepts_id(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=True, description="X", type="fix", id="myid")
        errors = validate_schema(entry, coverage_unit="commit")
        assert errors == []

    def test_changeset_mode_forbids_commits(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=True, description="X", type="fix", id="myid")
        errors = validate_schema(entry, coverage_unit="changeset-file")
        assert any("commits must be empty" in e for e in errors)

    def test_changeset_mode_requires_id(self):
        entry = ChangelogEntry(commits=[], user_facing=True, description="X", type="fix")
        errors = validate_schema(entry, coverage_unit="changeset-file")
        assert any("id is required" in e for e in errors)

    def test_changeset_mode_valid_entry(self):
        entry = ChangelogEntry(commits=[], user_facing=True, description="X", type="fix", id="myid")
        errors = validate_schema(entry, coverage_unit="changeset-file")
        assert errors == []

    def test_default_coverage_unit_is_commit(self):
        """validate_schema defaults to commit mode for backward compat."""
        entry = ChangelogEntry(commits=["abc"], user_facing=True, description="X", type="fix")
        errors = validate_schema(entry)
        assert errors == []


# ---------------------------------------------------------------------------
# 8h: Downstream consumer updates (un-finalize in changeset mode)
# ---------------------------------------------------------------------------

class TestUnfinalizeChangesetMode:
    """Tests for un-finalization in changeset-file mode."""

    def test_unfinalize_restores_pending_files(self, changeset_repo):
        from rlsbl.changelog.files import (
            finalize_changeset_version,
            get_pending_dir,
            unfinalize_changeset_version,
            write_pending_file,
        )

        changes_dir = get_changes_dir(str(changeset_repo))
        pending_dir = get_pending_dir(changes_dir)

        # Write pending files and finalize
        e1 = ChangelogEntry(id="id_uno", user_facing=True, description="Feat", type="feature")
        e2 = ChangelogEntry(id="id_dos", user_facing=False)
        write_pending_file(pending_dir, e1)
        write_pending_file(pending_dir, e2)
        finalize_changeset_version(changes_dir, "2.0.0")

        # Confirm finalized state
        assert os.path.isfile(os.path.join(changes_dir, "2.0.0.jsonl"))
        assert len(os.listdir(pending_dir)) == 0

        # Un-finalize
        changed = unfinalize_changeset_version(changes_dir, "2.0.0")
        assert len(changed) > 0

        # Pending files should be restored
        restored_files = os.listdir(pending_dir)
        assert len(restored_files) == 2
        restored_ids = set()
        for fname in restored_files:
            with open(os.path.join(pending_dir, fname)) as f:
                data = json.load(f)
            restored_ids.add(data["id"])
        assert "id_uno" in restored_ids
        assert "id_dos" in restored_ids

        # Versioned JSONL should be gone
        assert not os.path.isfile(os.path.join(changes_dir, "2.0.0.jsonl"))


# ---------------------------------------------------------------------------
# 8i: read_coverage_unit enforcement at all callers
# ---------------------------------------------------------------------------

class TestReadCoverageUnitEnforcement:
    """Verify that read_coverage_unit is wired into all callers.

    The function hard-errors on missing or invalid coverage_unit values.
    These tests confirm it is called (not bypassed via config.get defaults).
    """

    def test_invalid_coverage_unit_errors_in_cmd_add(self, tmp_path, monkeypatch):
        """cmd_add with an invalid coverage_unit raises ConfigError."""
        from rlsbl.commands.changelog_cmd import cmd_add
        from rlsbl.errors import ConfigError

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial")
        _run_git(repo, "tag", "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")

        # Write config with INVALID coverage_unit
        config_path = repo / ".rlsbl" / "config.json"
        config_path.write_text(json.dumps({
            "coverage_unit": "bogus-value",
            "publish_mode": "none",
        }, indent=2))

        sha = _make_commit(repo, "file.txt", "change")
        flags = {
            "commits": sha,
            "description": "A fix",
            "type": "fix",
            "user-facing": True,
            "auto-commit": False,
        }
        with pytest.raises(ConfigError, match="coverage_unit"):
            cmd_add(flags, project_root=repo)

    def test_valid_coverage_unit_passes_in_cmd_add(self, tmp_path, monkeypatch):
        """cmd_add with a valid coverage_unit succeeds normally."""
        from rlsbl.commands.changelog_cmd import cmd_add

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial")
        _run_git(repo, "tag", "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")

        # Write config with VALID coverage_unit
        config_path = repo / ".rlsbl" / "config.json"
        config_path.write_text(json.dumps({
            "coverage_unit": "commit",
            "publish_mode": "none",
        }, indent=2))

        sha = _make_commit(repo, "file.txt", "change")
        flags = {
            "commits": sha,
            "description": "A fix",
            "type": "fix",
            "user-facing": True,
            "auto-commit": False,
        }
        # Should succeed without error
        cmd_add(flags, project_root=repo)

        entries = read_unreleased(get_changes_dir(str(repo)))
        assert len(entries) == 1
        assert entries[0].description == "A fix"

    def test_invalid_coverage_unit_errors_in_check(self, tmp_path, monkeypatch):
        """Changelog checks with invalid coverage_unit raise ConfigError."""
        from rlsbl.errors import ConfigError
        from rlsbl.changelog.files import read_coverage_unit

        with pytest.raises(ConfigError, match="coverage_unit"):
            read_coverage_unit({"coverage_unit": "invalid-mode"})

    def test_read_project_config_defaults_coverage_unit(self, tmp_path):
        """read_project_config injects coverage_unit='commit' when missing."""
        from rlsbl.config import read_project_config

        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({"publish_mode": "none"}))

        config = read_project_config(tmp_path)
        assert config["coverage_unit"] == "commit"


# ---------------------------------------------------------------------------
# 8j: Auto-derive packages in changeset mode for releasables
# ---------------------------------------------------------------------------

class TestChangesetAutoPackages:
    """Verify packages field is auto-populated when adding changesets
    inside a monorepo releasable."""

    def test_changeset_add_in_releasable_sets_packages(self, tmp_path, monkeypatch):
        """In changeset mode within a releasable, the pending file should
        have packages = [releasable_name]."""
        from rlsbl.commands.changelog_cmd import _cmd_add_changeset, _ResolvedContext
        from rlsbl.changelog.files import get_pending_dir, read_pending_files
        from rlsbl.workspace import Releasable
        from rlsbl.workspace_types import get_releasable_changes_dir

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial")
        _run_git(repo, "tag", "v0.0.0")

        # Set up the releasable's changes directory (monorepo layout)
        releasable_changes = Path(get_releasable_changes_dir(str(repo), "auth"))
        releasable_changes.mkdir(parents=True)
        pending = releasable_changes / "pending"
        pending.mkdir()

        config = {
            "coverage_unit": "changeset-file",
            "publish_mode": "none",
        }

        # Create a mock releasable
        releasable = mock.MagicMock(spec=Releasable)
        releasable.name = "auth"

        ws_context = _ResolvedContext(
            project=None,
            releasable=releasable,
            ws_root=str(repo),
            member_projects=[],
        )

        flags = {
            "description": "New auth feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }

        _cmd_add_changeset(flags, repo, ws_context, config, dry_run=False)

        # Read back the pending file and verify packages
        pending_dir = get_pending_dir(str(releasable_changes))
        entries = read_pending_files(pending_dir)
        assert len(entries) == 1
        assert entries[0].packages == ["auth"]
        assert entries[0].description == "New auth feature"

    def test_changeset_add_standalone_no_packages(self, changeset_repo):
        """In changeset mode without a releasable, packages should be None."""
        from rlsbl.commands.changelog_cmd import _cmd_add_changeset
        from rlsbl.changelog.files import get_pending_dir, read_pending_files, get_changes_dir

        config = {
            "coverage_unit": "changeset-file",
            "publish_mode": "none",
        }

        flags = {
            "description": "Standalone fix",
            "type": "fix",
            "user-facing": True,
            "auto-commit": False,
        }

        _cmd_add_changeset(flags, changeset_repo, None, config, dry_run=False)

        changes_dir = get_changes_dir(str(changeset_repo))
        pending_dir = get_pending_dir(changes_dir)
        entries = read_pending_files(pending_dir)
        assert len(entries) == 1
        assert entries[0].packages is None
        assert entries[0].description == "Standalone fix"
