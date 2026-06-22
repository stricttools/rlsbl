"""Tests for migrate-releasable CLI command and Phase 5a fix.

Covers:
- create_migration_tag skips members without scoped tags (no v* fallback)
- CLI command exists in strictcli schema (help output)
- Dry-run produces output without side effects
- Full migration flow on a test monorepo (happy path)
"""

import json
import os
import shutil
import subprocess
from unittest.mock import patch

import pytest

from rlsbl import app
from rlsbl.changelog.schema import ChangelogEntry, serialize_entry, parse_jsonl
from rlsbl.releasable_migration import (
    cmd_migrate_releasable,
    create_migration_tag,
)
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    WorkspaceProject,
    get_releasable_changes_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_saferm_dir(path, project_name, subdir_name):
    """Mock _saferm_dir that performs actual directory deletion."""
    shutil.rmtree(path)


def _mock_saferm_file(path, project_name, file_name):
    """Mock _saferm_file that performs actual file deletion."""
    os.unlink(path)


def _init_git(path):
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"],
                   cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(path), check=True)
    readme = path / "README.md"
    readme.write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"],
                   cwd=str(path), check=True)


def _make_commit(path, filename, message="change"):
    """Create a file and commit it, returning the commit SHA."""
    filepath = path / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(f"content of {filename}\n")
    subprocess.run(["git", "add", str(filename)], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(path), check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _write_workspace(tmp_path, content):
    """Write raw TOML content to workspace.toml."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


def _make_pypi_project(base, name, version="0.1.0"):
    """Create a minimal pypi project directory with pyproject.toml."""
    proj_dir = base / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return proj_dir


def _write_unreleased_jsonl(project_dir, entries):
    """Write changelog entries to a project's unreleased.jsonl."""
    changes_dir = project_dir / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    lines = [serialize_entry(e) + "\n" for e in entries]
    (changes_dir / "unreleased.jsonl").write_text("".join(lines))


def _setup_migration_monorepo(tmp_path, *, version="0.1.0", tag_a=True,
                                tag_b=True, entries_a=None, entries_b=None):
    """Create a monorepo ready for migration testing.

    Sets up a git repo with two projects (a, b) in explicit releasable mode.

    Args:
        tmp_path: the temp directory root
        version: version for both projects
        tag_a: whether to create a scoped tag for project a
        tag_b: whether to create a scoped tag for project b
        entries_a: changelog entries for project a (list of ChangelogEntry)
        entries_b: changelog entries for project b (list of ChangelogEntry)

    Returns:
        dict with root, proj_a, proj_b paths
    """
    _init_git(tmp_path)

    proj_a = _make_pypi_project(tmp_path, "a", version)
    proj_b = _make_pypi_project(tmp_path, "b", version)

    # Write workspace with explicit releasable mode
    _write_workspace(tmp_path, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"

[[projects]]
path = "b"
name = "b"
releasable = "core"
""")

    # Create commits and tags
    sha_a = _make_commit(tmp_path, "a/src.py", "initial a")
    if tag_a:
        subprocess.run(["git", "tag", "a@v" + version],
                       cwd=str(tmp_path), check=True)

    sha_b = _make_commit(tmp_path, "b/src.py", "initial b")
    if tag_b:
        subprocess.run(["git", "tag", "b@v" + version],
                       cwd=str(tmp_path), check=True)

    # Write changelog entries
    if entries_a is not None:
        _write_unreleased_jsonl(proj_a, entries_a)
    else:
        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=[sha_a], user_facing=True,
                           description="Fix in a", type="fix"),
        ])

    if entries_b is not None:
        _write_unreleased_jsonl(proj_b, entries_b)
    else:
        _write_unreleased_jsonl(proj_b, [
            ChangelogEntry(commits=[sha_b], user_facing=True,
                           description="Feature in b", type="feature"),
        ])

    # Commit changelog files
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add changelogs"],
                   cwd=str(tmp_path), check=True)

    return {"root": tmp_path, "proj_a": proj_a, "proj_b": proj_b}


# ---------------------------------------------------------------------------
# Phase 5a: create_migration_tag skips members without scoped tags
# ---------------------------------------------------------------------------


class TestCreateMigrationTagNoFallback:
    """create_migration_tag must not fall back to unscoped v* tags."""

    def test_skips_member_without_scoped_tag(self, tmp_project):
        """A member with no scoped tag is skipped; no wrong tag is picked up."""
        _init_git(tmp_project)

        # Create two projects
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_pypi_project(tmp_project, "b", "0.1.0")

        # Only tag project a with a scoped tag
        _make_commit(tmp_project, "a/src.py", "initial a")
        subprocess.run(["git", "tag", "a@v0.1.0"],
                       cwd=str(tmp_project), check=True)

        # Create an unscoped v* tag (belongs to some other package)
        _make_commit(tmp_project, "other/file.txt", "other change")
        subprocess.run(["git", "tag", "v0.5.0"],
                       cwd=str(tmp_project), check=True)

        # Project b has no scoped tag
        _make_commit(tmp_project, "b/src.py", "initial b")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = create_migration_tag(
            str(tmp_project), "core", "{name}@v{version}", members,
        )

        # Tag should be created from a@v0.1.0 only
        assert result["status"] == "created"
        assert result["tag"] == "core@v0.1.0"
        assert result["source_tag"] == "a@v0.1.0"
        # b should be in skipped_members
        assert "b" in result["skipped_members"]
        # b should NOT appear in member_tags (the v0.5.0 tag must NOT be picked up)
        assert "b" not in result["member_tags"]
        # a should be in member_tags
        assert result["member_tags"]["a"] == "a@v0.1.0"

    def test_all_members_without_scoped_tags_returns_no_tags(self, tmp_project):
        """When no member has a scoped tag, status is no_tags (not error from v*)."""
        _init_git(tmp_project)

        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_pypi_project(tmp_project, "b", "0.1.0")

        # Create an unscoped v* tag to verify it is NOT picked up
        _make_commit(tmp_project, "root.txt", "root change")
        subprocess.run(["git", "tag", "v0.9.0"],
                       cwd=str(tmp_project), check=True)

        _make_commit(tmp_project, "a/src.py", "initial a")
        _make_commit(tmp_project, "b/src.py", "initial b")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = create_migration_tag(
            str(tmp_project), "core", "{name}@v{version}", members,
        )

        assert result["status"] == "no_tags"
        assert result["tag"] is None
        # Both should be skipped
        assert set(result["skipped_members"]) == {"a", "b"}
        assert result["member_tags"] == {}

    def test_skipped_members_empty_when_all_have_tags(self, tmp_project):
        """When all members have scoped tags, skipped_members is empty."""
        _init_git(tmp_project)

        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_commit(tmp_project, "a/src.py", "initial a")
        subprocess.run(["git", "tag", "a@v0.1.0"],
                       cwd=str(tmp_project), check=True)

        _make_pypi_project(tmp_project, "b", "0.2.0")
        _make_commit(tmp_project, "b/src.py", "initial b")
        subprocess.run(["git", "tag", "b@v0.2.0"],
                       cwd=str(tmp_project), check=True)

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = create_migration_tag(
            str(tmp_project), "core", "{name}@v{version}", members,
        )

        assert result["status"] == "created"
        assert result["skipped_members"] == []


# ---------------------------------------------------------------------------
# Phase 5b: CLI command exists in help output
# ---------------------------------------------------------------------------


class TestMigrateReleasableCLI:
    """CLI command is registered and shows in help."""

    def test_command_in_monorepo_help(self):
        """migrate-releasable appears in monorepo subcommand help output."""
        result = app.test(["monorepo", "--help"])
        assert "migrate-releasable" in result.stdout

    def test_command_help_text(self):
        """migrate-releasable has its own help output with description."""
        result = app.test(["monorepo", "migrate-releasable", "--help"])
        assert "Migrate a releasable" in result.stdout
        assert "releasable_name" in result.stdout


# ---------------------------------------------------------------------------
# Phase 5b: dry-run produces output without side effects
# ---------------------------------------------------------------------------


class TestMigrateReleasableDryRun:
    """Dry-run mode reports what would happen without modifying state."""

    def test_dry_run_returns_state_without_changes(self, tmp_project):
        """dry_run=True returns state info but does not consolidate or tag."""
        setup = _setup_migration_monorepo(tmp_project)
        root = str(setup["root"])

        result = cmd_migrate_releasable(root, "core", dry_run=True)

        assert result["dry_run"] is True
        assert result["releasable_name"] == "core"
        assert set(result["members"]) == {"a", "b"}
        assert result["tag_format"] == "{name}@v{version}"
        # State should be populated
        assert result["state"]["explicit_mode"] is True
        # No side effects
        assert result["changelogs"] is None
        assert result["versions"] is None
        assert result["tag"] is None
        assert result["cleanup"] is None

    def test_dry_run_no_releasable_changes_dir_created(self, tmp_project):
        """Dry run should not create the releasable's changes directory."""
        setup = _setup_migration_monorepo(tmp_project)
        root = str(setup["root"])

        changes_dir = get_releasable_changes_dir(root, "core")

        cmd_migrate_releasable(root, "core", dry_run=True)

        assert not os.path.exists(changes_dir)

    def test_dry_run_no_migration_tag_created(self, tmp_project):
        """Dry run should not create any git tags."""
        setup = _setup_migration_monorepo(tmp_project)
        root = str(setup["root"])

        # Count tags before
        before = subprocess.run(
            ["git", "tag", "-l"], cwd=root,
            capture_output=True, text=True, check=True,
        )
        tags_before = set(before.stdout.strip().splitlines())

        cmd_migrate_releasable(root, "core", dry_run=True)

        # Count tags after
        after = subprocess.run(
            ["git", "tag", "-l"], cwd=root,
            capture_output=True, text=True, check=True,
        )
        tags_after = set(after.stdout.strip().splitlines())

        assert tags_before == tags_after


# ---------------------------------------------------------------------------
# Phase 5b: full migration flow (happy path)
# ---------------------------------------------------------------------------


class TestMigrateReleasableFullFlow:
    """Full migration flow on a test monorepo."""

    @patch("rlsbl.releasable_cleanup._saferm_file", side_effect=_mock_saferm_file)
    @patch("rlsbl.releasable_cleanup._saferm_dir", side_effect=_mock_saferm_dir)
    def test_full_migration(self, mock_dir, mock_file, tmp_project):
        """Complete migration: changelogs, versions, tag, cleanup."""
        setup = _setup_migration_monorepo(tmp_project)
        root = str(setup["root"])

        result = cmd_migrate_releasable(root, "core", dry_run=False, yes=True)

        assert result["releasable_name"] == "core"
        assert result["dry_run"] is False

        # Changelogs were consolidated
        changelogs = result["changelogs"]
        assert changelogs is not None
        assert changelogs["entries_merged"] == 2
        assert set(changelogs["source_projects"]) == {"a", "b"}
        assert os.path.isfile(changelogs["dest_path"])

        # Versions were consolidated
        versions = result["versions"]
        assert versions is not None
        assert versions["status"] == "ok"
        assert versions["version"] == "0.1.0"

        # Migration tag was created
        tag = result["tag"]
        assert tag is not None
        assert tag["status"] == "created"
        assert tag["tag"] == "core@v0.1.0"

        # Verify the tag exists in git
        check = subprocess.run(
            ["git", "rev-parse", "core@v0.1.0"],
            cwd=root, capture_output=True, text=True,
        )
        assert check.returncode == 0

    @patch("rlsbl.releasable_cleanup._saferm_file", side_effect=_mock_saferm_file)
    @patch("rlsbl.releasable_cleanup._saferm_dir", side_effect=_mock_saferm_dir)
    def test_full_migration_cleans_per_package_state(self, mock_dir, mock_file, tmp_project):
        """Migration removes per-package .rlsbl/changes/ and .rlsbl/releases/."""
        setup = _setup_migration_monorepo(tmp_project)
        root = str(setup["root"])

        # Create per-package .rlsbl/releases/ dirs to verify cleanup
        for name in ("a", "b"):
            releases_dir = tmp_project / name / ".rlsbl" / "releases"
            releases_dir.mkdir(parents=True, exist_ok=True)
            (releases_dir / "unreleased.toml").write_text("[release]\n")

        # Re-commit to keep tree clean
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add releases dirs"],
                       cwd=root, check=True)

        result = cmd_migrate_releasable(root, "core", dry_run=False, yes=True)

        cleanup = result["cleanup"]
        assert cleanup is not None
        assert len(cleanup) > 0

        # Per-package changes/ and releases/ dirs should be gone
        for name in ("a", "b"):
            changes_path = os.path.join(root, name, ".rlsbl", "changes")
            releases_path = os.path.join(root, name, ".rlsbl", "releases")
            assert not os.path.isdir(changes_path)
            assert not os.path.isdir(releases_path)

    @patch("rlsbl.releasable_cleanup._saferm_file", side_effect=_mock_saferm_file)
    @patch("rlsbl.releasable_cleanup._saferm_dir", side_effect=_mock_saferm_dir)
    def test_migration_with_partial_scoped_tags(self, mock_dir, mock_file, tmp_project):
        """Migration works when only some members have scoped tags."""
        setup = _setup_migration_monorepo(tmp_project, tag_a=True, tag_b=False)
        root = str(setup["root"])

        result = cmd_migrate_releasable(root, "core", dry_run=False, yes=True)

        tag = result["tag"]
        assert tag["status"] == "created"
        assert tag["tag"] == "core@v0.1.0"
        assert "b" in tag["skipped_members"]
        assert "a" in tag["member_tags"]

    @patch("rlsbl.releasable_cleanup._saferm_file", side_effect=_mock_saferm_file)
    @patch("rlsbl.releasable_cleanup._saferm_dir", side_effect=_mock_saferm_dir)
    def test_migration_with_no_scoped_tags(self, mock_dir, mock_file, tmp_project):
        """Migration succeeds even with no scoped tags (tag step reports no_tags)."""
        setup = _setup_migration_monorepo(tmp_project, tag_a=False, tag_b=False)
        root = str(setup["root"])

        result = cmd_migrate_releasable(root, "core", dry_run=False, yes=True)

        tag = result["tag"]
        assert tag["status"] == "no_tags"
        assert tag["tag"] is None

        # But changelogs and versions should still be consolidated
        assert result["changelogs"]["entries_merged"] == 2
        assert result["versions"]["status"] == "ok"

    def test_migration_rejects_non_explicit_mode(self, tmp_project):
        """Migration raises WorkspaceError if workspace is not in explicit mode."""
        _init_git(tmp_project)
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")

        from rlsbl.errors import WorkspaceError
        with pytest.raises(WorkspaceError, match="not in explicit mode"):
            cmd_migrate_releasable(str(tmp_project), "core", dry_run=False, yes=True)

    def test_migration_rejects_unknown_releasable(self, tmp_project):
        """Migration raises WorkspaceError for a non-existent releasable name."""
        _init_git(tmp_project)
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")

        from rlsbl.errors import WorkspaceError
        with pytest.raises(WorkspaceError, match="not found"):
            cmd_migrate_releasable(str(tmp_project), "nonexistent",
                                   dry_run=False, yes=True)
