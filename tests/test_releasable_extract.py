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

from conftest import DEFAULT_RELEASE_FILE, make_releasable_monorepo, make_releasable_state, with_root_member, workspace_toml
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
from rlsbl.changelog.schema import parse_jsonl as _parse_jsonl_hashes
from rlsbl.workspace import (
    get_releasable_changes_dir,
    get_releasable_dir,
    load_workspace,
    save_workspace,
    load_releasables,
    Releasable,
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
    (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(content))


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
    (config_dir / "config.json").write_text(json.dumps({"publish_mode": "ci", "targets": []}) + "\n")
    _make_commit(root, "pkgA/.rlsbl/config.json", json.dumps({"publish_mode": "ci", "targets": []}) + "\n", "config for pkgA")

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
        save_workspace(str(tmp_path), with_root_member(projects))

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
        save_workspace(str(tmp_path), with_root_member(projects))

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
        save_workspace(str(root), with_root_member(projects))

        target = tmp_path / "output"
        projs, proj = validate_extract_preconditions(str(root), "pkg", str(target))
        assert proj.name == "pkg"
        assert len(projs) == 1

    def test_target_exists_error(self, tmp_path, monkeypatch):
        """Error when target path already exists."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")

        root = tmp_path
        projects = [WorkspaceProject({"path": "pkg", "name": "pkg"})]
        save_workspace(str(root), with_root_member(projects))

        target = tmp_path / "output"
        target.mkdir()

        with pytest.raises(ExtractError, match="target path already exists"):
            validate_extract_preconditions(str(root), "pkg", str(target))

    def test_package_not_found_error(self, tmp_path, monkeypatch):
        """Error when package does not exist in workspace."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")

        root = tmp_path
        projects = [WorkspaceProject({"path": "pkg", "name": "pkg"})]
        save_workspace(str(root), with_root_member(projects))

        target = tmp_path / "output"
        with pytest.raises(ExtractError, match="not found"):
            validate_extract_preconditions(str(root), "nonexistent", str(target))

    def test_no_filter_repo_error(self, tmp_path, monkeypatch):
        """Error when git-filter-repo is not installed."""
        monkeypatch.setattr(shutil, "which", lambda name: None)

        root = tmp_path
        projects = [WorkspaceProject({"path": "pkg", "name": "pkg"})]
        save_workspace(str(root), with_root_member(projects))

        target = tmp_path / "output"
        with pytest.raises(ExtractError, match="git-filter-repo is not installed"):
            validate_extract_preconditions(str(root), "pkg", str(target))


# ---------------------------------------------------------------------------
# validate_absorb_preconditions
# ---------------------------------------------------------------------------


def _clean_source(tmp_path, name="source"):
    """Create a clean, committed git source repo suitable for absorb."""
    repo = tmp_path / name
    repo.mkdir()
    _init_git_repo(repo)
    _make_commit(repo, "main.py", "print('src')", "add main")
    return repo


class TestValidateAbsorbPreconditions:
    def test_valid_preconditions(self, tmp_path, monkeypatch):
        """Happy path: source clean git repo, path and name both free."""
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = _clean_source(tmp_path)

        projs = validate_absorb_preconditions(str(root), str(source), "pkgs/new", "new_pkg")
        assert len(projs) == 1

    def test_no_filter_repo(self, tmp_path, monkeypatch):
        """Error when git-filter-repo is not installed."""
        monkeypatch.setattr(shutil, "which", lambda n: None)
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = _clean_source(tmp_path)

        with pytest.raises(ExtractError, match="git-filter-repo is not installed"):
            validate_absorb_preconditions(str(root), str(source), "new", "new_pkg")

    def test_source_not_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])

        with pytest.raises(ExtractError, match="does not exist"):
            validate_absorb_preconditions(
                str(root), str(tmp_path / "nonexistent"), "new", "pkg"
            )

    def test_source_not_git_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = tmp_path / "source"
        source.mkdir()

        with pytest.raises(ExtractError, match="not a git repository"):
            validate_absorb_preconditions(str(root), str(source), "new", "pkg")

    def test_source_dirty_is_rejected(self, tmp_path, monkeypatch):
        """A source with uncommitted changes is a hard error (would be dropped)."""
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = _clean_source(tmp_path)
        (source / "uncommitted.txt").write_text("dirty\n")

        with pytest.raises(ExtractError, match="uncommitted changes"):
            validate_absorb_preconditions(str(root), str(source), "new", "pkg")

    def test_duplicate_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = _clean_source(tmp_path)

        with pytest.raises(ExtractError, match="package 'existing' already exists"):
            validate_absorb_preconditions(str(root), str(source), "different", "existing")

    def test_duplicate_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = _clean_source(tmp_path)

        with pytest.raises(ExtractError, match="path 'existing' already exists"):
            validate_absorb_preconditions(str(root), str(source), "existing", "brand_new")


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

    def test_foreign_tag_pruned_orphan_scheme_tag_kept(self, tmp_path):
        """Only tags matching another CURRENT member's glob are pruned; a
        scheme-parsing tag matching no live member (e.g. this package's own
        pre-rename history under an old prefix) is KEPT, never destroyed.

        Both tags are planted at a commit that touches pkgA so they survive the
        filter-repo path filter and reach the translation step:
        - ``pkgB@v1.0.0`` matches the live member pkgB's glob -> pruned.
        - ``oldpkgA@v0.5.0`` matches no live member glob (the pre-rename shape)
          -> kept.
        """
        root, hashes = _setup_monorepo(tmp_path)
        pkga_commit = hashes["pkgA"][0]
        # Foreign live-member tag -- must be pruned.
        _git_tag(root, "pkgB@v1.0.0", ref=pkga_commit)
        # Orphan scheme tag matching no current member (pre-rename history of
        # pkgA itself) -- must be KEPT under the conservative rule.
        _git_tag(root, "oldpkgA@v0.5.0", ref=pkga_commit)

        target = tmp_path / "extracted"
        result = cmd_extract(str(root), "pkgA", str(target))

        tags = _run_git(str(target), "tag", "-l").split()
        # Foreign live-member tag pruned.
        assert "pkgB@v1.0.0" not in tags
        assert "pkgB@v1.0.0" in result["tags_deleted"]
        # Orphan scheme tag kept -- not destroyed.
        assert "oldpkgA@v0.5.0" in tags
        assert "oldpkgA@v0.5.0" not in result["tags_deleted"]


# ---------------------------------------------------------------------------
# cmd_absorb (history-rewrite integration tests)
# ---------------------------------------------------------------------------


def _git_tag(repo, tag, ref="HEAD"):
    subprocess.run(
        ["git", "tag", tag, ref],
        cwd=str(repo), check=True, capture_output=True, text=True,
    )


def _make_multi_commit(repo, files, message):
    """Write several files and commit them together, returning the hash."""
    for rel, content in files.items():
        fp = repo / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        subprocess.run(
            ["git", "add", rel],
            cwd=str(repo), check=True, capture_output=True, text=True,
        )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(repo), check=True, capture_output=True, text=True,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _setup_released_source_repo(tmp_path):
    """Build a released npm source repo: 2 version tags, finalized JSONL,
    plus one unreleased entry. Returns (repo_path, {"0.1.0": ..., ...}).
    """
    repo = tmp_path / "widget_src"
    repo.mkdir()
    _init_git_repo(repo)

    os.makedirs(str(repo / ".rlsbl"), exist_ok=True)
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n"
    )
    # v0.1.0 feature commit
    c1 = _make_multi_commit(
        repo,
        {
            "package.json": json.dumps({"name": "widget", "version": "0.1.0"}) + "\n",
            "src/index.js": "export const v = 1;\n",
            ".rlsbl/config.json": json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n",
        },
        "feat: initial widget",
    )
    changes = repo / ".rlsbl" / "changes"
    _write_changelog_entry(changes, "0.1.0.jsonl", [
        ChangelogEntry(commits=[c1[:8]], user_facing=True,
                       description="Initial widget", type="feature"),
    ])
    _make_commit(repo, ".rlsbl/changes/0.1.0.jsonl",
                 (changes / "0.1.0.jsonl").read_text(), "changelog 0.1.0")
    _git_tag(repo, "v0.1.0")

    # v0.2.0 feature commit
    c3 = _make_multi_commit(
        repo,
        {
            "package.json": json.dumps({"name": "widget", "version": "0.2.0"}) + "\n",
            "src/feature.js": "export const f = 2;\n",
        },
        "feat: v0.2.0 feature",
    )
    _write_changelog_entry(changes, "0.2.0.jsonl", [
        ChangelogEntry(commits=[c3[:8]], user_facing=True,
                       description="Widget feature", type="feature"),
    ])
    _make_commit(repo, ".rlsbl/changes/0.2.0.jsonl",
                 (changes / "0.2.0.jsonl").read_text(), "changelog 0.2.0")
    _git_tag(repo, "v0.2.0")

    # Unreleased work
    c5 = _make_commit(repo, "src/wip.js", "export const w = 3;\n", "feat: wip")
    _write_changelog_entry(changes, "unreleased.jsonl", [
        ChangelogEntry(commits=[c5[:8]], user_facing=True,
                       description="Work in progress", type="feature"),
    ])
    _make_commit(repo, ".rlsbl/changes/unreleased.jsonl",
                 (changes / "unreleased.jsonl").read_text(), "changelog wip")

    return repo, {"c1": c1, "c3": c3, "c5": c5}


def _setup_plain_monorepo(tmp_path):
    """A minimal committed monorepo (no pre-existing package conflicts)."""
    root = tmp_path / "mono"
    root.mkdir()
    _init_git_repo(root)
    (root / "existing").mkdir()
    _write_workspace(root, """
[[projects]]
path = "existing"
name = "existing"
""")
    _make_commit(root, "existing/keep.txt", "keep\n", "add existing")
    subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", "workspace"], cwd=str(root),
                   check=True, capture_output=True, text=True)
    return root


@skip_no_filter_repo
class TestCmdAbsorbDryRun:
    def test_dry_run_zero_mutations(self, tmp_path):
        """Dry run reports tags to import but mutates nothing."""
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        head_before = _run_git(str(root), "rev-parse", "HEAD")

        result = cmd_absorb(str(root), str(source), "packages/widget", dry_run=True)
        assert result["dry_run"] is True
        assert result["name"] == "widget"
        assert set(result["tags_to_import"]) == {"0.1.0", "0.2.0"}

        # Nothing changed: no new commit, no new project, no new dir.
        assert _run_git(str(root), "rev-parse", "HEAD") == head_before
        names = [p.name for p in load_workspace(str(root))]
        assert "widget" not in names
        assert not (root / "packages" / "widget").exists()


@skip_no_filter_repo
class TestAbsorbHistoryRewrite:
    def test_full_history_rewrite(self, tmp_path, monkeypatch):
        root = _setup_plain_monorepo(tmp_path)
        source, hashes = _setup_released_source_repo(tmp_path)

        result = cmd_absorb(str(root), str(source), "packages/widget")
        assert result["name"] == "widget"
        assert set(result["tags_imported"]) == {"widget@v0.1.0", "widget@v0.2.0"}

        # 1. git log for the dest path shows full rewritten history.
        log = _run_git(str(root), "log", "--oneline", "--", "packages/widget")
        assert "initial widget" in log
        assert "v0.2.0 feature" in log
        assert "wip" in log

        # 2. describe resolves the imported monorepo-scheme tag.
        desc = _run_git(str(root), "describe", "--tags", "--match", "widget@v*")
        assert desc.startswith("widget@v0.2.0")

        # 3. every JSONL hash resolves in monorepo history.
        changes_dir = root / "packages" / "widget" / ".rlsbl" / "changes"
        all_hashes = []
        for jf in changes_dir.glob("*.jsonl"):
            for entry in _parse_jsonl_hashes(str(jf)):
                all_hashes.extend(entry.commits)
        assert all_hashes  # sanity: there are hashes to check
        for h in all_hashes:
            # cat-file -e raises via check=True if the object is missing.
            _run_git(str(root), "cat-file", "-e", h + "^{commit}")

        # 6. bare v* tags are absent (only monorepo-scheme tags remain).
        tags = _run_git(str(root), "tag", "-l").split()
        assert "v0.1.0" not in tags
        assert "v0.2.0" not in tags
        assert "widget@v0.1.0" in tags
        assert "widget@v0.2.0" in tags

    def test_compute_release_version_bumps_forward(self, tmp_path, monkeypatch):
        """With imported tags, the package computes a forward bump -- the
        destroyed-tag guard does not fire."""
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        cmd_absorb(str(root), str(source), "packages/widget")

        from rlsbl.commands.release.validate import compute_release_version
        import rlsbl.commands.release as release_mod
        from rlsbl.utils import RemoteTagResult, RemoteTagState
        from rlsbl.targets import TARGETS, detect_targets

        monkeypatch.setattr(
            release_mod, "remote_tag_commit",
            lambda tag, **kw: RemoteTagResult(state=RemoteTagState.ABSENT),
        )
        monkeypatch.chdir(str(root))

        dest_full = os.path.join(str(root), "packages", "widget")
        entry = detect_targets(dest_full)[0]
        target = TARGETS[entry.name]

        cur, new, bump, tag = compute_release_version(
            target, entry.path, "patch", "widget", "packages/widget",
            lambda *a, **k: None, project_dir=dest_full,
        )
        assert cur == "0.2.0"
        assert new == "0.2.1"  # patch bump forward, NOT a re-release of 0.2.0
        assert bump == "patch"
        assert tag == target.monorepo_tag_format("widget", "0.2.1", path="packages/widget")

    def test_changelog_coverage_passes(self, tmp_path, monkeypatch):
        """All unreleased package commits are covered with zero hand-fixups."""
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        cmd_absorb(str(root), str(source), "packages/widget")

        from rlsbl.changelog.validate import check_coverage
        from rlsbl.targets import TARGETS, detect_targets

        dest_full = os.path.join(str(root), "packages", "widget")
        entry_t = detect_targets(dest_full)[0]
        target = TARGETS[entry_t.name]
        tag_glob = target.monorepo_tag_glob("widget", path="packages/widget")

        proj = None
        for p in load_workspace(str(root)):
            if p.name == "widget":
                proj = p
        assert proj is not None

        changes_dir = root / "packages" / "widget" / ".rlsbl" / "changes"
        entries = _parse_jsonl_hashes(str(changes_dir / "unreleased.jsonl"))

        monkeypatch.chdir(str(root))
        ok, details = check_coverage(entries, tag_glob=tag_glob, project=proj.to_dict())
        assert ok, details

    def test_working_tree_clean_after_absorb(self, tmp_path):
        """Absorb self-commits: the working tree is clean afterward."""
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        cmd_absorb(str(root), str(source), "packages/widget", registry_name="widget-npm")

        status = _run_git(str(root), "status", "--porcelain")
        assert status == ""

        proj = next(p for p in load_workspace(str(root)) if p.name == "widget")
        assert proj["path"] == "packages/widget"
        assert proj.registry_name == "widget-npm"

    def test_source_dirty_rejected(self, tmp_path):
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        (source / "dirty.txt").write_text("x\n")
        with pytest.raises(ExtractError, match="uncommitted changes"):
            cmd_absorb(str(root), str(source), "packages/widget")


@skip_no_filter_repo
class TestAbsorbReleasable:
    def test_releasable_routing_and_residue_removal(self, tmp_path):
        """--releasable routes the changelog to the releasable dir and removes
        the per-package residue."""
        root = tmp_path / "mono_rel"
        root.mkdir()
        _init_git_repo(root)
        _write_workspace(root, """
[[projects]]
path = "existing"
name = "existing"
releasable = "core"
""", releasables_toml="""
[[releasables]]
name = "core"
""")
        (root / "existing").mkdir()
        _make_commit(root, "existing/keep.txt", "keep\n", "add existing")
        # Seed the releasable changes dir so routing appends into it.
        rel_changes = root / ".rlsbl-monorepo" / "releasables" / "core" / "changes"
        os.makedirs(str(rel_changes), exist_ok=True)
        (rel_changes / "unreleased.jsonl").write_text("")
        subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "workspace"], cwd=str(root),
                       check=True, capture_output=True, text=True)

        source, _ = _setup_released_source_repo(tmp_path)
        result = cmd_absorb(
            str(root), str(source), "packages/widget",
            releasable_name="core",
        )

        # Project assigned to the releasable.
        proj = next(p for p in load_workspace(str(root)) if p.name == "widget")
        assert proj.releasable == "core"

        # Changelog routed into the releasable dir.
        assert (rel_changes / "unreleased.jsonl").read_text().strip() != ""

        # Per-package changes residue removed.
        assert not (root / "packages" / "widget" / ".rlsbl" / "changes").exists()

        # Self-committed: clean tree.
        assert _run_git(str(root), "status", "--porcelain") == ""


class TestAbsorbCliBinding:
    def test_positional_binding_order_locked(self):
        """CLI positional order is source_repo FIRST, dest_path SECOND.

        strictcli binds bottom-decorator-first; this pins the order so a future
        edit cannot silently invert the two positionals.
        """
        from rlsbl import app

        def walk(commands, groups, prefix):
            for name, cmd in commands.items():
                yield prefix + name, cmd
            for gname, group in groups.items():
                yield from walk(group.commands, group._groups, prefix + gname + " ")

        cmd = None
        for path, c in walk(app._commands, app._groups, ""):
            if path == "monorepo absorb":
                cmd = c
        assert cmd is not None
        assert [a.name for a in cmd.args] == ["source_repo", "dest_path"]


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


def _setup_monorepo_with_releasable_state(tmp_path):
    """Explicit-mode monorepo whose release state is in the REAL place.

    ``_setup_monorepo_with_releasables`` above declares ``[[releasables]]`` but
    keeps changelog state in per-package ``<pkg>/.rlsbl/changes/`` -- the
    pre-releasable layout. This one puts version, changes/, releases/ and
    config.json under ``.rlsbl-monorepo/releasables/<name>/``, which is where
    the releasable model actually keeps them, and creates NO per-package
    changes dirs at all.

    Layout: releasable ``core`` (members pkgA, pkgB) and releasable ``extras``
    (member pkgC). Each carries the released state for the version the factory
    tags (``ns.initial_version``: locked JSONL, generated .md, archived release
    file) plus the unreleased entries added below.

    Returns (root, commit_hashes).
    """
    root = tmp_path / "monorepo_rel_state"
    ns = make_releasable_monorepo(
        root,
        releasables=[Releasable(name="core"), Releasable(name="extras")],
        projects=[
            {"path": "pkgA", "name": "pkgA", "releasable": "core"},
            {"path": "pkgB", "name": "pkgB", "releasable": "core"},
            {"path": "pkgC", "name": "pkgC", "releasable": "extras"},
        ],
    )

    hash_a = _make_commit(root, "pkgA/main.py", "print('A')", "add pkgA")
    hash_b = _make_commit(root, "pkgB/main.py", "print('B')", "add pkgB")
    hash_c = _make_commit(root, "pkgC/main.py", "print('C')", "add pkgC")

    # Unreleased entries referencing the commits above. The released version
    # (``ns.initial_version``, the one the factory tags) already has its full
    # trio, so this pass only adds the unreleased side -- naming that version
    # again would be refused as a rewrite of released state.
    make_releasable_state(
        root,
        "core",
        version=ns.initial_version,
        unreleased_entries=[
            ChangelogEntry(
                commits=[hash_a], user_facing=True,
                description="Feature A", type="feature", packages=["pkgA"],
            ),
            ChangelogEntry(
                commits=[hash_b], user_facing=True,
                description="Feature B", type="feature", packages=["pkgB"],
            ),
        ],
        release_file=DEFAULT_RELEASE_FILE,
    )
    make_releasable_state(
        root,
        "extras",
        version=ns.initial_version,
        unreleased_entries=[
            ChangelogEntry(
                commits=[hash_c], user_facing=True,
                description="Feature C", type="feature", packages=["pkgC"],
            ),
        ],
    )

    subprocess.run(
        ["git", "add", WORKSPACE_DIR],
        cwd=str(root), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "releasable state"],
        cwd=str(root), check=True, capture_output=True, text=True,
    )

    return root, {"pkgA": [hash_a], "pkgB": [hash_b], "pkgC": [hash_c]}


@skip_no_filter_repo
class TestCmdExtractReleasableOnReleasableState:
    """The extract-releasable cases re-expressed on the real releasable-state
    layout (state under ``.rlsbl-monorepo/releasables/<name>/``, no per-package
    ``.rlsbl/changes/``).

    These assert what the code does TODAY, including where that is a gap: the
    extract implementation reads only per-package ``.rlsbl/changes/``, so on
    this layout it migrates nothing and the extracted repo carries no release
    state. The structural half of the extract (filter, workspace.toml, tags)
    behaves the same as on the per-package layout.
    """

    def test_dry_run(self, tmp_path):
        """Dry run returns member info without modifying anything."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
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
        """Extracting a multi-member releasable creates a new monorepo whose
        workspace.toml keeps the releasable grouping."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_rel"

        result = cmd_extract_releasable(str(root), "core", str(target))
        assert result["is_monorepo"] is True

        ws_file = target / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.is_file()

        new_projects = load_workspace(str(target))
        names = [p.name for p in new_projects]
        assert "pkgA" in names
        assert "pkgB" in names
        assert [r.name for r in load_releasables(str(target), new_projects)] == ["core"]

        # Member files survived the filter.
        assert (target / "pkgA" / "main.py").is_file()
        assert (target / "pkgB" / "main.py").is_file()

    def test_releasable_changelog_is_not_migrated(self, tmp_path):
        """CURRENT BEHAVIOR: extract reads per-package .rlsbl/changes/ only, so
        a releasable whose entries live in its own changes dir migrates zero
        entries and the extracted repo gets no changelog at all."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_rel"

        result = cmd_extract_releasable(str(root), "core", str(target))

        assert result["entries_migrated"] == 0
        assert result["files_written"] == 0
        # No changes dir is created for either member...
        assert not (target / "pkgA" / ".rlsbl" / "changes").exists()
        assert not (target / "pkgB" / ".rlsbl" / "changes").exists()
        # ...and the source's releasable state dir is not carried over: it sits
        # outside every member path, so the filter drops it.
        assert not (
            target / WORKSPACE_DIR / "releasables" / "core"
        ).exists()

    def test_source_releasable_state_left_behind(self, tmp_path):
        """CURRENT BEHAVIOR: the source keeps the extracted releasable's state
        directory even though workspace.toml no longer declares it."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_rel"

        cmd_extract_releasable(str(root), "core", str(target))

        projects = load_workspace(str(root))
        names = [p.name for p in projects]
        assert "pkgA" not in names
        assert "pkgB" not in names
        assert "pkgC" in names
        assert [r.name for r in load_releasables(str(root), projects)] == ["extras"]

        # The orphaned state dir survives in the source monorepo.
        assert os.path.isdir(get_releasable_dir(str(root), "core"))
        assert os.path.isfile(
            os.path.join(get_releasable_changes_dir(str(root), "core"),
                         "unreleased.jsonl")
        )

    def test_single_member_creates_flat_repo(self, tmp_path):
        """Extracting a single-member releasable hoists it to the repo root."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_extras"

        result = cmd_extract_releasable(str(root), "extras", str(target))
        assert result["is_monorepo"] is False

        assert os.path.isfile(str(target / "main.py"))
        assert os.path.isfile(str(target / "pyproject.toml"))
        # A flat repo gets a .rlsbl/config.json, but an empty unreleased.jsonl
        # is written only when a source changes dir was found -- and there is
        # none on this layout.
        assert os.path.isfile(str(target / ".rlsbl" / "config.json"))
        assert not (target / ".rlsbl" / "changes").exists()

    def test_tags_are_kept_for_multi_member(self, tmp_path):
        """The releasable-scheme tag is kept and foreign tags pruned, exactly as
        on the per-package layout."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        _git_tag(root, "extras@v0.3.0")  # foreign -- must be pruned

        target = tmp_path / "extracted_rel"
        cmd_extract_releasable(str(root), "core", str(target))

        tags = _run_git(str(target), "tag", "-l").split()
        # make_releasable_monorepo tags each releasable at its initial version.
        assert "core@v0.1.0" in tags
        assert "extras@v0.1.0" not in tags
        assert "extras@v0.3.0" not in tags
        assert _run_git(str(target), "status", "--porcelain") == ""


@skip_no_filter_repo
class TestExtractRoundTrip:
    """Absorb a released standalone repo into a monorepo, then extract it back
    out, and assert the extracted repo is coherent: standalone tags restored,
    zero foreign/mono-scheme tags, all changelog hashes resolve, coverage
    passes, working tree committed and clean."""

    def test_absorb_then_extract_is_coherent(self, tmp_path):
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)

        cmd_absorb(str(root), str(source), "packages/widget")

        out = tmp_path / "widget_out"
        result = cmd_extract(str(root), "widget", str(out))
        assert result["package_name"] == "widget"

        # 1. Standalone anchor tags restored at the right commits.
        tags = _run_git(str(out), "tag", "-l").split()
        assert "v0.1.0" in tags
        assert "v0.2.0" in tags

        # 2. Zero foreign / monorepo-scheme tags survive.
        assert "widget@v0.1.0" not in tags
        assert "widget@v0.2.0" not in tags
        assert not any("@v" in t or t.endswith("/v0.1.0") for t in tags)

        # 3. The v0.2.0 anchor is the newest reachable tag.
        desc = _run_git(str(out), "describe", "--tags", "--match", "v*")
        assert desc.startswith("v0.2.0")

        # 4. Every surviving JSONL hash resolves in the extracted repo.
        changes_dir = out / ".rlsbl" / "changes"
        all_hashes = []
        for jf in changes_dir.glob("*.jsonl"):
            for entry in _parse_jsonl_hashes(str(jf)):
                all_hashes.extend(entry.commits)
        assert all_hashes  # sanity: there are hashes to check
        for h in all_hashes:
            _run_git(str(out), "cat-file", "-e", h + "^{commit}")

        # 5. Changelog coverage passes in the extracted repo.
        from rlsbl.changelog.validate import check_coverage

        entries = _parse_jsonl_hashes(str(changes_dir / "unreleased.jsonl"))
        prev_cwd = os.getcwd()
        os.chdir(str(out))
        try:
            ok, details = check_coverage(entries, tag_glob="v*")
        finally:
            os.chdir(prev_cwd)
        assert ok, details

        # 6. The extracted repo self-committed: clean working tree.
        assert _run_git(str(out), "status", "--porcelain") == ""

        # 7. Source monorepo no longer lists the extracted package.
        assert "widget" not in [p.name for p in load_workspace(str(root))]

    def test_extract_translation_collision_is_hard_error(self, tmp_path):
        """A pre-existing standalone tag colliding with a translated tag aborts
        the extract with a clear error naming both tags."""
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        # Plant a standalone v0.2.0 in the monorepo -- it will be cloned into
        # the extracted repo and collide with the widget@v0.2.0 translation.
        cmd_absorb(str(root), str(source), "packages/widget")
        _git_tag(root, "v0.2.0")

        out = tmp_path / "widget_out"
        with pytest.raises(ExtractError, match="collision"):
            cmd_extract(str(root), "widget", str(out))


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

    def test_multi_member_recreates_releasable_and_keeps_tags(self, tmp_path):
        """A multi-member extract recreates the [[releasables]] grouping and
        KEEPS the releasable-scheme tags (translates nothing)."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        # Tag the core releasable (shared scheme across pkgA/pkgB) plus a
        # foreign extras tag that must be pruned.
        _git_tag(root, "core@v0.1.0")
        _git_tag(root, "extras@v0.3.0")
        subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                       check=True, capture_output=True, text=True)

        target = tmp_path / "extracted_rel"
        cmd_extract_releasable(str(root), "core", str(target))

        # Releasable grouping recreated: load_releasables validates membership.
        new_projects = load_workspace(str(target))
        new_releasables = load_releasables(str(target), new_projects)
        assert [r.name for r in new_releasables] == ["core"]
        for p in new_projects:
            assert p.releasable == "core"

        tags = _run_git(str(target), "tag", "-l").split()
        # Releasable-scheme tag kept unchanged; foreign extras tag pruned.
        assert "core@v0.1.0" in tags
        assert "extras@v0.3.0" not in tags

        # Self-committed, clean tree.
        assert _run_git(str(target), "status", "--porcelain") == ""

    def test_single_member_translates_releasable_tags(self, tmp_path):
        """A single-member extract translates the releasable-scheme tags to
        standalone v{version} and prunes foreign tags."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        _git_tag(root, "extras@v0.3.0")  # the single-member releasable's tag
        _git_tag(root, "core@v0.1.0")    # foreign -- must be pruned

        target = tmp_path / "extracted_extras"
        cmd_extract_releasable(str(root), "extras", str(target))

        tags = _run_git(str(target), "tag", "-l").split()
        assert "v0.3.0" in tags
        assert "extras@v0.3.0" not in tags
        assert "core@v0.1.0" not in tags
        assert _run_git(str(target), "status", "--porcelain") == ""

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


# ---------------------------------------------------------------------------
# Broken target-declaration guard (no-silent-degradation)
# ---------------------------------------------------------------------------


def _commit_all(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=str(repo),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(repo),
                   check=True, capture_output=True, text=True)


class TestBrokenTargetDeclarationGuard:
    """A .rlsbl/config.json that exists but has no ``targets`` key is a broken
    declaration -- extract/absorb must hard-error UP FRONT (before any history
    rewrite) rather than silently importing wrongly-schemed tags. A repo with
    NO config file at all is the legitimate auto-detect path and must succeed.
    """

    # --- absorb: validation level (no filter-repo needed) ---

    def test_validate_absorb_broken_source_config_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = _clean_source(tmp_path, name="broken_src")
        os.makedirs(str(source / ".rlsbl"), exist_ok=True)
        (source / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        _commit_all(source, "add broken config")

        with pytest.raises(ExtractError, match="broken target declaration"):
            validate_absorb_preconditions(str(root), str(source), "pkgs/new", "new_pkg")

    def test_validate_absorb_no_config_source_ok(self, tmp_path, monkeypatch):
        """A source with NO .rlsbl/config.json auto-detects and passes."""
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "monorepo"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "existing", "name": "existing"})])
        source = _clean_source(tmp_path)  # no .rlsbl at all

        projs = validate_absorb_preconditions(str(root), str(source), "pkgs/new", "new_pkg")
        assert len(projs) == 1

    def test_absorb_broken_config_hard_errors_pre_mutation(self, tmp_path, monkeypatch):
        """End-to-end: a broken source config aborts before the monorepo is
        touched (no clone, no merge, no workspace entry)."""
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = _setup_plain_monorepo(tmp_path)
        head_before = _run_git(str(root), "rev-parse", "HEAD")
        source = tmp_path / "broken_src"
        source.mkdir()
        _init_git_repo(source)
        os.makedirs(str(source / ".rlsbl"), exist_ok=True)
        (source / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        _make_commit(source, "main.py", "print('x')\n", "add main")
        _commit_all(source, "add broken config")

        with pytest.raises(ExtractError, match="broken target declaration"):
            cmd_absorb(str(root), str(source), "packages/widget")

        # Pre-mutation: the monorepo is untouched.
        assert _run_git(str(root), "rev-parse", "HEAD") == head_before
        assert not (root / "packages" / "widget").exists()
        assert "widget" not in [p.name for p in load_workspace(str(root))]

    # --- extract: validation level (no filter-repo needed) ---

    def test_validate_extract_broken_config_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "mono"
        root.mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "pkg", "name": "pkg"})])
        pkg_rlsbl = root / "pkg" / ".rlsbl"
        os.makedirs(str(pkg_rlsbl), exist_ok=True)
        (pkg_rlsbl / "config.json").write_text(json.dumps({"publish_mode": "ci"}) + "\n")

        with pytest.raises(ExtractError, match="broken target declaration"):
            validate_extract_preconditions(str(root), "pkg", str(tmp_path / "out"))

    def test_validate_extract_no_config_ok(self, tmp_path, monkeypatch):
        """A package with no .rlsbl/config.json auto-detects and passes."""
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = tmp_path / "mono"
        root.mkdir()
        (root / "pkg").mkdir()
        save_workspace(str(root), [WorkspaceProject({"path": "pkg", "name": "pkg"})])

        projs, proj = validate_extract_preconditions(str(root), "pkg", str(tmp_path / "out"))
        assert proj.name == "pkg"

    def test_extract_broken_config_hard_errors_pre_mutation(self, tmp_path, monkeypatch):
        """End-to-end: a broken package config aborts before the target repo is
        created and before the source workspace is edited."""
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root, _ = _setup_monorepo(tmp_path)
        # Break pkgA's declaration: config exists but declares no targets.
        (root / "pkgA" / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        target = tmp_path / "extracted"

        with pytest.raises(ExtractError, match="broken target declaration"):
            cmd_extract(str(root), "pkgA", str(target))

        assert not target.exists()
        assert "pkgA" in [p.name for p in load_workspace(str(root))]
