"""Tests for monorepo extract and absorb operations.

Covers:
- require_filter_repo (installed vs not)
- Extract: basic package extraction, changelog migration, workspace.toml update
- Absorb: basic absorption, workspace.toml update
- Releasable-level extract
- Validation: precondition checks for both operations
"""

import json
import os
import shutil
import subprocess

import pytest

from rlsbl.changelog.schema import ChangelogEntry, serialize_entry, parse_jsonl
from rlsbl.commands.monorepo.extract import (
    ExtractError,
    require_filter_repo,
    cmd_extract,
    cmd_absorb,
    cmd_extract_releasable,
    validate_extract_preconditions,
    validate_absorb_preconditions,
    _filter_changelog_entries,
    _migrate_changelog_to_new_repo,
    _remove_project_from_workspace,
    _find_project,
    _run_git,
)
from rlsbl.workspace import (
    load_workspace,
    save_workspace,
    load_releasables,
    WorkspaceProject,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)

# Check if git-filter-repo is available for integration tests
HAS_FILTER_REPO = shutil.which("git-filter-repo") is not None
skip_no_filter_repo = pytest.mark.skipif(
    not HAS_FILTER_REPO,
    reason="git-filter-repo not installed",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path, branch="main"):
    """Initialize a git repo with an initial commit."""
    subprocess.run(
        ["git", "init", "-q", "-b", branch],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    readme = path / "README.md"
    readme.write_text("# test\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )


def _make_commit(path, filename, content="content", message="change"):
    """Make a commit and return the hash."""
    filepath = path / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content)
    subprocess.run(
        ["git", "add", str(filepath.relative_to(path))],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _write_workspace(tmp_path, projects_toml, releasables_toml=""):
    """Write workspace.toml with raw TOML content."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    content = releasables_toml + "\n" + projects_toml
    (ws_dir / WORKSPACE_FILE).write_text(content)


def _write_changelog_entry(changes_dir, filename, entries):
    """Write changelog entries to a JSONL file."""
    os.makedirs(str(changes_dir), exist_ok=True)
    filepath = changes_dir / filename
    with open(str(filepath), "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(serialize_entry(entry) + "\n")


def _setup_monorepo(tmp_path):
    """Create a monorepo with two packages and changelog entries.

    Returns (root, commit_hashes) where commit_hashes is a dict of
    package_name -> list of commit hashes.
    """
    root = tmp_path / "monorepo"
    root.mkdir()
    _init_git_repo(root)

    # Create package directories
    (root / "pkgA").mkdir()
    (root / "pkgB").mkdir()

    # Write workspace.toml
    _write_workspace(root, """
[[projects]]
path = "pkgA"
name = "pkgA"

[[projects]]
path = "pkgB"
name = "pkgB"
""")

    # Create package files and commit
    hash_a1 = _make_commit(root, "pkgA/main.py", "print('A')", "add pkgA")
    hash_b1 = _make_commit(root, "pkgB/main.py", "print('B')", "add pkgB")

    # Create .rlsbl/changes for pkgA
    changes_a = root / "pkgA" / ".rlsbl" / "changes"
    _write_changelog_entry(changes_a, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[hash_a1[:8]],
            user_facing=True,
            description="Added package A",
            type="feature",
        ),
    ])
    _make_commit(root, "pkgA/.rlsbl/changes/unreleased.jsonl", open(str(changes_a / "unreleased.jsonl")).read(), "changelog for pkgA")

    # Create .rlsbl/changes for pkgB
    changes_b = root / "pkgB" / ".rlsbl" / "changes"
    _write_changelog_entry(changes_b, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[hash_b1[:8]],
            user_facing=True,
            description="Added package B",
            type="feature",
        ),
    ])
    _make_commit(root, "pkgB/.rlsbl/changes/unreleased.jsonl", open(str(changes_b / "unreleased.jsonl")).read(), "changelog for pkgB")

    # Create config.json for pkgA
    config_dir = root / "pkgA" / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"publish_mode": "ci"}) + "\n")
    _make_commit(root, "pkgA/.rlsbl/config.json", json.dumps({"publish_mode": "ci"}) + "\n", "config for pkgA")

    # Commit workspace
    subprocess.run(
        ["git", "add", WORKSPACE_DIR],
        cwd=str(root), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add workspace"],
        cwd=str(root), check=True, capture_output=True, text=True,
    )

    return root, {"pkgA": [hash_a1], "pkgB": [hash_b1]}


def _setup_source_repo(tmp_path):
    """Create a standalone repo suitable for absorbing into a monorepo.

    Returns (repo_path, commit_hash).
    """
    repo = tmp_path / "source_repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Create some source files
    commit_hash = _make_commit(repo, "main.py", "print('source')", "add main")

    # Create .rlsbl/changes
    changes_dir = repo / ".rlsbl" / "changes"
    _write_changelog_entry(changes_dir, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[commit_hash[:8]],
            user_facing=True,
            description="Source feature",
            type="feature",
        ),
    ])
    _make_commit(
        repo,
        ".rlsbl/changes/unreleased.jsonl",
        open(str(changes_dir / "unreleased.jsonl")).read(),
        "add changelog",
    )

    return repo, commit_hash


# ---------------------------------------------------------------------------
# require_filter_repo
# ---------------------------------------------------------------------------


class TestRequireFilterRepo:
    def test_returns_path_when_installed(self, monkeypatch):
        """When git-filter-repo is on PATH, return its path."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")
        path = require_filter_repo()
        assert path == "/usr/bin/git-filter-repo"

    def test_raises_when_not_installed(self, monkeypatch):
        """When git-filter-repo is not on PATH, raise ExtractError."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExtractError, match="git-filter-repo is not installed"):
            require_filter_repo()

    def test_error_includes_install_instructions(self, monkeypatch):
        """The error message includes installation instructions."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExtractError) as exc_info:
            require_filter_repo()
        msg = str(exc_info.value)
        assert "pip install git-filter-repo" in msg
        assert "github.com/newren/git-filter-repo" in msg


# ---------------------------------------------------------------------------
# _find_project
# ---------------------------------------------------------------------------


class TestFindProject:
    def test_finds_existing_project(self):
        projects = [
            WorkspaceProject({"path": "a", "name": "alpha"}),
            WorkspaceProject({"path": "b", "name": "beta"}),
        ]
        result = _find_project(projects, "beta")
        assert result.name == "beta"

    def test_raises_for_missing_project(self):
        projects = [
            WorkspaceProject({"path": "a", "name": "alpha"}),
        ]
        with pytest.raises(ExtractError, match="not found in workspace"):
            _find_project(projects, "nonexistent")

    def test_error_lists_available(self):
        projects = [
            WorkspaceProject({"path": "a", "name": "alpha"}),
            WorkspaceProject({"path": "b", "name": "beta"}),
        ]
        with pytest.raises(ExtractError, match="alpha.*beta"):
            _find_project(projects, "gamma")


# ---------------------------------------------------------------------------
# _filter_changelog_entries
# ---------------------------------------------------------------------------


class TestFilterChangelogEntries:
    def test_filters_by_packages_field(self):
        """Entries with packages field are filtered by package name."""
        entries = [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="For A",
                type="feature",
                packages=["pkgA"],
            ),
            ChangelogEntry(
                commits=["def"],
                user_facing=True,
                description="For B",
                type="feature",
                packages=["pkgB"],
            ),
        ]
        result = _filter_changelog_entries(entries, "pkgA", None)
        assert len(result) == 1
        assert result[0].description == "For A"

    def test_no_packages_field_no_repo_includes_all(self):
        """Without packages field and no repo root, all entries are included."""
        entries = [
            ChangelogEntry(commits=["abc"], user_facing=False),
            ChangelogEntry(commits=["def"], user_facing=False),
        ]
        result = _filter_changelog_entries(entries, "pkgA", None)
        assert len(result) == 2

    def test_packages_field_with_path(self):
        """Entries with packages field matching the path basename."""
        entries = [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="For nested",
                type="feature",
                packages=["nested"],
            ),
        ]
        result = _filter_changelog_entries(entries, "path/to/nested", None)
        assert len(result) == 1

    def test_empty_entries_returns_empty(self):
        result = _filter_changelog_entries([], "pkgA", None)
        assert result == []


# ---------------------------------------------------------------------------
# _migrate_changelog_to_new_repo
# ---------------------------------------------------------------------------


class TestMigrateChangelogToNewRepo:
    def test_migrates_unreleased_entries(self, tmp_path):
        """Unreleased entries relevant to the package are migrated."""
        source_changes = tmp_path / "source_changes"
        source_changes.mkdir()
        target_changes = tmp_path / "target_changes"

        _write_changelog_entry(source_changes, "unreleased.jsonl", [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="Feature A",
                type="feature",
                packages=["pkgA"],
            ),
            ChangelogEntry(
                commits=["def"],
                user_facing=True,
                description="Feature B",
                type="feature",
                packages=["pkgB"],
            ),
        ])

        fw, em = _migrate_changelog_to_new_repo(
            str(source_changes), str(target_changes), "pkgA", None
        )
        assert em == 1
        assert fw >= 1

        # Verify the target has only pkgA's entry
        entries = parse_jsonl(str(target_changes / "unreleased.jsonl"))
        assert len(entries) == 1
        assert entries[0].description == "Feature A"

    def test_creates_empty_unreleased_when_no_entries(self, tmp_path):
        """An empty unreleased.jsonl is created even if no entries match."""
        source_changes = tmp_path / "source_changes"
        source_changes.mkdir()
        target_changes = tmp_path / "target_changes"

        # Write entries that won't match
        _write_changelog_entry(source_changes, "unreleased.jsonl", [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="Feature B",
                type="feature",
                packages=["pkgB"],
            ),
        ])

        _migrate_changelog_to_new_repo(
            str(source_changes), str(target_changes), "pkgA", None
        )

        # unreleased.jsonl should exist (even if empty)
        assert os.path.isfile(str(target_changes / "unreleased.jsonl"))

    def test_migrates_versioned_entries(self, tmp_path):
        """Versioned JSONL files are migrated too."""
        source_changes = tmp_path / "source_changes"
        source_changes.mkdir()
        target_changes = tmp_path / "target_changes"

        # No unreleased
        (source_changes / "unreleased.jsonl").write_text("")

        _write_changelog_entry(source_changes, "1.0.0.jsonl", [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="v1 Feature A",
                type="feature",
                packages=["pkgA"],
            ),
        ])

        fw, em = _migrate_changelog_to_new_repo(
            str(source_changes), str(target_changes), "pkgA", None
        )
        assert em == 1
        assert os.path.isfile(str(target_changes / "1.0.0.jsonl"))


# ---------------------------------------------------------------------------
# _remove_project_from_workspace
# ---------------------------------------------------------------------------


class TestRemoveProjectFromWorkspace:
    def test_removes_project(self, tmp_path):
        """Removing a project updates workspace.toml."""
        projects = [
            WorkspaceProject({"path": "a", "name": "alpha"}),
            WorkspaceProject({"path": "b", "name": "beta"}),
        ]
        save_workspace(str(tmp_path), projects)

        updated = _remove_project_from_workspace(str(tmp_path), "alpha", projects)
        assert len(updated) == 1
        assert updated[0].name == "beta"

        # Verify file on disk
        reloaded = load_workspace(str(tmp_path))
        assert len(reloaded) == 1
        assert reloaded[0].name == "beta"

    def test_raises_for_nonexistent_project(self, tmp_path):
        projects = [
            WorkspaceProject({"path": "a", "name": "alpha"}),
        ]
        save_workspace(str(tmp_path), projects)

        with pytest.raises(ExtractError, match="not found"):
            _remove_project_from_workspace(str(tmp_path), "nonexistent", projects)


# ---------------------------------------------------------------------------
# validate_extract_preconditions
# ---------------------------------------------------------------------------


class TestValidateExtractPreconditions:
    def test_valid_preconditions(self, tmp_path, monkeypatch):
        """Happy path: package exists, target does not, filter-repo installed."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")

        root = tmp_path
        projects = [WorkspaceProject({"path": "pkg", "name": "pkg"})]
        save_workspace(str(root), projects)

        target = tmp_path / "output"
        projs, proj = validate_extract_preconditions(str(root), "pkg", str(target))
        assert proj.name == "pkg"
        assert len(projs) == 1

    def test_target_exists_error(self, tmp_path, monkeypatch):
        """Error when target path already exists."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")

        root = tmp_path
        projects = [WorkspaceProject({"path": "pkg", "name": "pkg"})]
        save_workspace(str(root), projects)

        target = tmp_path / "output"
        target.mkdir()

        with pytest.raises(ExtractError, match="target path already exists"):
            validate_extract_preconditions(str(root), "pkg", str(target))

    def test_package_not_found_error(self, tmp_path, monkeypatch):
        """Error when package does not exist in workspace."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")

        root = tmp_path
        projects = [WorkspaceProject({"path": "pkg", "name": "pkg"})]
        save_workspace(str(root), projects)

        target = tmp_path / "output"
        with pytest.raises(ExtractError, match="not found"):
            validate_extract_preconditions(str(root), "nonexistent", str(target))

    def test_no_filter_repo_error(self, tmp_path, monkeypatch):
        """Error when git-filter-repo is not installed."""
        monkeypatch.setattr(shutil, "which", lambda name: None)

        root = tmp_path
        projects = [WorkspaceProject({"path": "pkg", "name": "pkg"})]
        save_workspace(str(root), projects)

        target = tmp_path / "output"
        with pytest.raises(ExtractError, match="git-filter-repo is not installed"):
            validate_extract_preconditions(str(root), "pkg", str(target))


# ---------------------------------------------------------------------------
# validate_absorb_preconditions
# ---------------------------------------------------------------------------


class TestValidateAbsorbPreconditions:
    def test_valid_preconditions(self, tmp_path):
        """Happy path: source exists, is a git repo, name not taken."""
        root = tmp_path / "monorepo"
        root.mkdir()
        projects = [WorkspaceProject({"path": "existing", "name": "existing"})]
        save_workspace(str(root), projects)

        source = tmp_path / "source"
        source.mkdir()
        (source / ".git").mkdir()

        projs = validate_absorb_preconditions(str(root), str(source), "new_pkg")
        assert len(projs) == 1

    def test_source_not_exists(self, tmp_path):
        root = tmp_path / "monorepo"
        root.mkdir()
        projects = [WorkspaceProject({"path": "existing", "name": "existing"})]
        save_workspace(str(root), projects)

        with pytest.raises(ExtractError, match="does not exist"):
            validate_absorb_preconditions(
                str(root), str(tmp_path / "nonexistent"), "pkg"
            )

    def test_source_not_git_repo(self, tmp_path):
        root = tmp_path / "monorepo"
        root.mkdir()
        projects = [WorkspaceProject({"path": "existing", "name": "existing"})]
        save_workspace(str(root), projects)

        source = tmp_path / "source"
        source.mkdir()

        with pytest.raises(ExtractError, match="not a git repository"):
            validate_absorb_preconditions(str(root), str(source), "pkg")

    def test_duplicate_name(self, tmp_path):
        root = tmp_path / "monorepo"
        root.mkdir()
        projects = [WorkspaceProject({"path": "existing", "name": "existing"})]
        save_workspace(str(root), projects)

        source = tmp_path / "source"
        source.mkdir()
        (source / ".git").mkdir()

        with pytest.raises(ExtractError, match="already exists"):
            validate_absorb_preconditions(str(root), str(source), "existing")


# ---------------------------------------------------------------------------
# cmd_extract (integration tests requiring git-filter-repo)
# ---------------------------------------------------------------------------


@skip_no_filter_repo
class TestCmdExtract:
    def test_dry_run(self, tmp_path):
        """Dry run validates but does not create the target repo."""
        root, hashes = _setup_monorepo(tmp_path)
        target = tmp_path / "extracted"

        result = cmd_extract(str(root), "pkgA", str(target), dry_run=True)
        assert result["dry_run"] is True
        assert result["package_name"] == "pkgA"
        assert not target.exists()

    def test_basic_extraction(self, tmp_path):
        """Extract a package and verify the new repo structure."""
        root, hashes = _setup_monorepo(tmp_path)
        target = tmp_path / "extracted"

        result = cmd_extract(str(root), "pkgA", str(target))
        assert result["package_name"] == "pkgA"
        assert os.path.isdir(str(target))
        assert os.path.isdir(str(target / ".git"))

        # The extracted repo should have the package files at root
        assert os.path.isfile(str(target / "main.py"))

    def test_changelog_migration(self, tmp_path):
        """Changelog entries are migrated to the new repo."""
        root, hashes = _setup_monorepo(tmp_path)
        target = tmp_path / "extracted"

        result = cmd_extract(str(root), "pkgA", str(target))

        # Check that .rlsbl/changes exists in the new repo
        changes_dir = target / ".rlsbl" / "changes"
        assert changes_dir.is_dir()
        assert (changes_dir / "unreleased.jsonl").is_file()

    def test_config_creation(self, tmp_path):
        """A .rlsbl/config.json is created in the new repo."""
        root, hashes = _setup_monorepo(tmp_path)
        target = tmp_path / "extracted"

        cmd_extract(str(root), "pkgA", str(target))

        config_path = target / ".rlsbl" / "config.json"
        assert config_path.is_file()
        config = json.loads(config_path.read_text())
        assert "publish_mode" in config
        assert "private" not in config

    def test_workspace_updated(self, tmp_path):
        """The source monorepo's workspace.toml no longer lists the extracted package."""
        root, hashes = _setup_monorepo(tmp_path)
        target = tmp_path / "extracted"

        cmd_extract(str(root), "pkgA", str(target))

        projects = load_workspace(str(root))
        names = [p.name for p in projects]
        assert "pkgA" not in names
        assert "pkgB" in names

    def test_target_exists_error(self, tmp_path):
        """Error when target path already exists."""
        root, hashes = _setup_monorepo(tmp_path)
        target = tmp_path / "extracted"
        target.mkdir()

        with pytest.raises(ExtractError, match="target path already exists"):
            cmd_extract(str(root), "pkgA", str(target))


# ---------------------------------------------------------------------------
# cmd_absorb (integration tests requiring git subtree)
# ---------------------------------------------------------------------------


class TestCmdAbsorb:
    def test_dry_run(self, tmp_path):
        """Dry run validates but does not modify the monorepo."""
        root, _ = _setup_monorepo(tmp_path)
        source, _ = _setup_source_repo(tmp_path)

        result = cmd_absorb(
            str(root), str(source), "new_pkg", dry_run=True
        )
        assert result["dry_run"] is True
        assert result["package_name"] == "new_pkg"

        # Workspace should still have only original packages
        projects = load_workspace(str(root))
        names = [p.name for p in projects]
        assert "new_pkg" not in names

    def test_basic_absorption(self, tmp_path):
        """Absorb a source repo and verify workspace update."""
        root, _ = _setup_monorepo(tmp_path)
        source, _ = _setup_source_repo(tmp_path)

        result = cmd_absorb(str(root), str(source), "new_pkg")
        assert result["package_name"] == "new_pkg"

        # Workspace should include the new package
        projects = load_workspace(str(root))
        names = [p.name for p in projects]
        assert "new_pkg" in names

        # The package directory should exist in the monorepo
        assert os.path.isdir(str(root / "new_pkg"))

    def test_absorb_with_releasable(self, tmp_path):
        """Absorbed package gets the specified releasable field."""
        root, _ = _setup_monorepo(tmp_path)
        source, _ = _setup_source_repo(tmp_path)

        cmd_absorb(
            str(root), str(source), "new_pkg",
            releasable_name="core",
        )

        projects = load_workspace(str(root))
        new_pkg = None
        for p in projects:
            if p.name == "new_pkg":
                new_pkg = p
                break
        assert new_pkg is not None
        assert new_pkg.releasable == "core"

    def test_changelog_migration(self, tmp_path):
        """Source repo's changelog entries are migrated to the monorepo."""
        root, _ = _setup_monorepo(tmp_path)
        source, _ = _setup_source_repo(tmp_path)

        result = cmd_absorb(str(root), str(source), "new_pkg")
        assert result["entries_migrated"] >= 1

        # Check that changelog files exist in the monorepo
        changes_dir = root / "new_pkg" / ".rlsbl" / "changes"
        assert changes_dir.is_dir()

    def test_source_not_git_repo(self, tmp_path):
        """Error when source is not a git repo."""
        root, _ = _setup_monorepo(tmp_path)
        source = tmp_path / "not_a_repo"
        source.mkdir()

        with pytest.raises(ExtractError, match="not a git repository"):
            cmd_absorb(str(root), str(source), "new_pkg")

    def test_duplicate_name(self, tmp_path):
        """Error when package name already exists in workspace."""
        root, _ = _setup_monorepo(tmp_path)
        source, _ = _setup_source_repo(tmp_path)

        with pytest.raises(ExtractError, match="already exists"):
            cmd_absorb(str(root), str(source), "pkgA")


# ---------------------------------------------------------------------------
# cmd_extract_releasable (integration tests requiring git-filter-repo)
# ---------------------------------------------------------------------------


def _setup_monorepo_with_releasables(tmp_path):
    """Create a monorepo with explicit releasables.

    Returns (root, commit_hashes).
    """
    root = tmp_path / "monorepo_rel"
    root.mkdir()
    _init_git_repo(root)

    # Create package directories
    (root / "pkgA").mkdir()
    (root / "pkgB").mkdir()
    (root / "pkgC").mkdir()

    # Write workspace.toml with releasables
    _write_workspace(root, """
[[projects]]
path = "pkgA"
name = "pkgA"
releasable = "core"

[[projects]]
path = "pkgB"
name = "pkgB"
releasable = "core"

[[projects]]
path = "pkgC"
name = "pkgC"
releasable = "extras"
""", releasables_toml="""
[[releasables]]
name = "core"

[[releasables]]
name = "extras"
""")

    hash_a = _make_commit(root, "pkgA/main.py", "print('A')", "add pkgA")
    hash_b = _make_commit(root, "pkgB/main.py", "print('B')", "add pkgB")
    hash_c = _make_commit(root, "pkgC/main.py", "print('C')", "add pkgC")

    # Add changelog for pkgA
    changes_a = root / "pkgA" / ".rlsbl" / "changes"
    _write_changelog_entry(changes_a, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[hash_a[:8]],
            user_facing=True,
            description="Feature A",
            type="feature",
            packages=["pkgA"],
        ),
    ])
    _make_commit(
        root, "pkgA/.rlsbl/changes/unreleased.jsonl",
        open(str(changes_a / "unreleased.jsonl")).read(), "changelog A"
    )

    # Commit workspace
    subprocess.run(
        ["git", "add", WORKSPACE_DIR],
        cwd=str(root), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add workspace"],
        cwd=str(root), check=True, capture_output=True, text=True,
    )

    return root, {"pkgA": [hash_a], "pkgB": [hash_b], "pkgC": [hash_c]}


@skip_no_filter_repo
class TestCmdExtractReleasable:
    def test_dry_run(self, tmp_path):
        """Dry run returns member info without modifying anything."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted_rel"

        result = cmd_extract_releasable(
            str(root), "core", str(target), dry_run=True
        )
        assert result["dry_run"] is True
        assert result["releasable_name"] == "core"
        assert set(result["member_packages"]) == {"pkgA", "pkgB"}
        assert result["is_monorepo"] is True
        assert not target.exists()

    def test_multi_member_creates_monorepo(self, tmp_path):
        """Extracting a multi-member releasable creates a new monorepo."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted_rel"

        result = cmd_extract_releasable(str(root), "core", str(target))
        assert result["is_monorepo"] is True

        # Should have workspace.toml
        ws_file = target / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.is_file()

        # Both packages should be in the new workspace
        new_projects = load_workspace(str(target))
        names = [p.name for p in new_projects]
        assert "pkgA" in names
        assert "pkgB" in names

    def test_single_member_creates_flat_repo(self, tmp_path):
        """Extracting a single-member releasable creates a flat repo."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted_extras"

        result = cmd_extract_releasable(str(root), "extras", str(target))
        assert result["is_monorepo"] is False

        # Should have files at root, not in pkgC/
        assert os.path.isfile(str(target / "main.py"))

    def test_source_workspace_updated(self, tmp_path):
        """Source monorepo removes extracted releasable and its members."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted_rel"

        cmd_extract_releasable(str(root), "core", str(target))

        # Source should still have pkgC but not pkgA or pkgB
        projects = load_workspace(str(root))
        names = [p.name for p in projects]
        assert "pkgA" not in names
        assert "pkgB" not in names
        assert "pkgC" in names

        # The "core" releasable should be removed, "extras" should remain
        releasables = load_releasables(str(root), projects)
        rel_names = [r.name for r in releasables]
        assert "core" not in rel_names
        assert "extras" in rel_names

    def test_nonexistent_releasable_error(self, tmp_path):
        """Error when the releasable does not exist."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted"

        with pytest.raises(ExtractError, match="not found"):
            cmd_extract_releasable(str(root), "nonexistent", str(target))

    def test_target_exists_error(self, tmp_path):
        """Error when target path already exists."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted"
        target.mkdir()

        with pytest.raises(ExtractError, match="target path already exists"):
            cmd_extract_releasable(str(root), "core", str(target))
