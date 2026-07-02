"""Tests for releasable directory scaffolding (Phase 9).

Covers:
- scaffold_releasable_dirs creates correct directory structure
- Version file not overwritten if exists
- Unreleased.jsonl not overwritten if exists
- Hook scripts created from templates
- Hook scripts updated via three-way merge
- Per-package changelog skipped for releasable members
- Per-package changelog skipped for non-releasable projects (existing behavior)
- monorepo add --releasable flag in explicit mode
- monorepo add requires --releasable in explicit mode
- monorepo add validates releasable name exists
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import (
    _is_non_releasable_project,
    _is_releasable_member_project,
    run_cmd,
)
from rlsbl.commands.monorepo.sync import scaffold_releasable_dirs
from rlsbl.commands.monorepo.commands import _cmd_add
from rlsbl.context import create_context
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    get_releasable_changes_dir,
    get_releasable_dir,
    get_releasable_version_path,
    is_explicit_mode,
    load_workspace,
)
from conftest import make_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_explicit_workspace(root, releasables, projects):
    """Create a workspace.toml with [[releasables]] and [[projects]] sections.

    releasables: list of dicts with at least "name" key.
    projects: list of dicts with at least "path", "name", and "releasable" keys.
    """
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)

    lines = []

    # Projects section must come before releasables AoT to avoid
    # TOML parsing issues (a bare key after [[releasables]] gets
    # absorbed into the last releasable table).
    if projects:
        for proj in projects:
            lines.append("[[projects]]")
            lines.append(f'path = "{proj["path"]}"')
            lines.append(f'name = "{proj["name"]}"')
            if "releasable" in proj:
                val = proj["releasable"]
                if isinstance(val, str):
                    lines.append(f'releasable = "{val}"')
                elif val is False:
                    lines.append("releasable = false")
            if proj.get("dev_only"):
                lines.append("dev_only = true")
            if proj.get("library"):
                lines.append("library = true")
            lines.append("")
    else:
        lines.append("projects = []")
        lines.append("")

    for rel in releasables:
        lines.append("[[releasables]]")
        lines.append(f'name = "{rel["name"]}"')
        if "tag_format" in rel:
            lines.append(f'tag_format = "{rel["tag_format"]}"')
        lines.append("")

    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# 9.1: scaffold_releasable_dirs
# ---------------------------------------------------------------------------


class TestScaffoldReleasableDirs:
    """Tests for the scaffold_releasable_dirs function."""

    def test_creates_directory_structure(self, tmp_path):
        """Creates version and changes/unreleased.jsonl for each releasable.

        Hook scripts are no longer scaffolded -- hooks are config-driven.
        """
        _make_explicit_workspace(tmp_path, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])
        (tmp_path / "app").mkdir()

        files = scaffold_releasable_dirs(str(tmp_path))

        rel_dir = get_releasable_dir(str(tmp_path), "www")
        assert os.path.isdir(rel_dir)

        # Version file
        version_path = get_releasable_version_path(str(tmp_path), "www")
        assert os.path.isfile(version_path)
        assert open(version_path).read() == "0.0.0\n"

        # Changes directory
        changes_dir = get_releasable_changes_dir(str(tmp_path), "www")
        assert os.path.isdir(changes_dir)
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        assert os.path.isfile(unreleased)
        assert open(unreleased).read() == ""

        # Hooks directory should NOT be created (config-driven hooks)
        hooks_dir = os.path.join(rel_dir, "hooks")
        assert not os.path.isdir(hooks_dir), (
            "Hook scripts should not be scaffolded -- hooks are config-driven"
        )

        # All created files should be in the returned list
        assert version_path in files
        assert unreleased in files

    def test_multiple_releasables(self, tmp_path):
        """Creates directories for all releasables in the workspace."""
        _make_explicit_workspace(tmp_path, [
            {"name": "www"},
            {"name": "api"},
        ], [
            {"path": "app", "name": "app", "releasable": "www"},
            {"path": "server", "name": "server", "releasable": "api"},
        ])
        (tmp_path / "app").mkdir()
        (tmp_path / "server").mkdir()

        scaffold_releasable_dirs(str(tmp_path))

        for name in ("www", "api"):
            rel_dir = get_releasable_dir(str(tmp_path), name)
            assert os.path.isdir(rel_dir)
            assert os.path.isfile(get_releasable_version_path(str(tmp_path), name))
            assert os.path.isfile(
                os.path.join(get_releasable_changes_dir(str(tmp_path), name), "unreleased.jsonl")
            )

    def test_version_file_not_overwritten(self, tmp_path):
        """Existing version file is preserved (user-owned)."""
        _make_explicit_workspace(tmp_path, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])
        (tmp_path / "app").mkdir()

        # Pre-create version file with custom content
        version_path = get_releasable_version_path(str(tmp_path), "www")
        os.makedirs(os.path.dirname(version_path), exist_ok=True)
        with open(version_path, "w") as f:
            f.write("1.5.0\n")

        files = scaffold_releasable_dirs(str(tmp_path))

        # Version file should NOT be in the created files list
        assert version_path not in files
        # Content should be preserved
        assert open(version_path).read() == "1.5.0\n"

    def test_unreleased_jsonl_not_overwritten(self, tmp_path):
        """Existing unreleased.jsonl is preserved (user-owned)."""
        _make_explicit_workspace(tmp_path, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])
        (tmp_path / "app").mkdir()

        # Pre-create unreleased.jsonl with content
        changes_dir = get_releasable_changes_dir(str(tmp_path), "www")
        os.makedirs(changes_dir, exist_ok=True)
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w") as f:
            f.write('{"commits":["abc"],"user_facing":false}\n')

        files = scaffold_releasable_dirs(str(tmp_path))

        # Should NOT be in created files
        assert unreleased not in files
        # Content preserved
        assert "abc" in open(unreleased).read()

    def test_no_releasables_section_returns_empty(self, tmp_path):
        """Without [[releasables]] section, returns empty list."""
        make_workspace(tmp_path, [
            {"path": "lib", "name": "lib"},
        ])

        files = scaffold_releasable_dirs(str(tmp_path))
        assert files == []

    def test_no_workspace_returns_empty(self, tmp_path):
        """When no workspace.toml exists, returns empty list."""
        files = scaffold_releasable_dirs(str(tmp_path))
        assert files == []

    def test_no_hook_scripts_scaffolded(self, tmp_path):
        """Hook scripts are not scaffolded -- hooks are config-driven."""
        _make_explicit_workspace(tmp_path, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])
        (tmp_path / "app").mkdir()

        files = scaffold_releasable_dirs(str(tmp_path))

        rel_dir = get_releasable_dir(str(tmp_path), "www")
        hooks_dir = os.path.join(rel_dir, "hooks")

        # No hooks directory should be created
        assert not os.path.isdir(hooks_dir)
        # No hook files in the returned files list
        hook_files = [f for f in files if "hooks" in f]
        assert hook_files == []


# ---------------------------------------------------------------------------
# 9.1: Per-package changelog skip for releasable members
# ---------------------------------------------------------------------------


class TestReleasableMemberChangelogSkip:
    """Verify that per-package changelog is skipped for releasable members."""

    def test_is_releasable_member_project_true(self, mock_git_repo):
        """_is_releasable_member_project returns True for a named releasable member."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()

        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        assert _is_releasable_member_project(proj_dir) is True

    def test_is_releasable_member_project_false_no_releasables(self, mock_git_repo):
        """_is_releasable_member_project returns False without [[releasables]]."""
        proj_dir = mock_git_repo / "lib"
        proj_dir.mkdir()

        make_workspace(mock_git_repo, [
            {"path": "lib", "name": "lib"},
        ])

        assert _is_releasable_member_project(proj_dir) is False

    def test_is_releasable_member_project_false_not_in_monorepo(self, tmp_path):
        """_is_releasable_member_project returns False when not in a monorepo."""
        assert _is_releasable_member_project(tmp_path) is False

    def test_is_releasable_member_project_false_for_releasable_false(self, mock_git_repo):
        """_is_releasable_member_project returns False for releasable = false."""
        proj_dir = mock_git_repo / "infra"
        proj_dir.mkdir()

        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
            {"path": "infra", "name": "infra", "releasable": False},
        ])

        assert _is_releasable_member_project(proj_dir) is False

    def test_scaffold_skips_changelog_for_releasable_member(self, mock_git_repo, monkeypatch):
        """Scaffolding a releasable member project skips per-package changelog files."""
        proj_dir = mock_git_repo / "app"
        proj_dir.mkdir()

        _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
            {"path": "app", "name": "app", "releasable": "www"},
        ])

        monkeypatch.chdir(proj_dir)
        ctx = create_context(proj_dir)

        run_cmd("plain", [], {
            "auto-commit": False,
            "auto-tag": False,
            "skip-shared": False,
        }, ctx=ctx)

        changelog = proj_dir / "CHANGELOG.md"
        unreleased = proj_dir / ".rlsbl" / "changes" / "unreleased.jsonl"

        assert not changelog.exists(), (
            "CHANGELOG.md should not be created for releasable member projects"
        )
        assert not unreleased.exists(), (
            "unreleased.jsonl should not be created for releasable member projects"
        )


# ---------------------------------------------------------------------------
# 9.3: monorepo add --releasable flag
# ---------------------------------------------------------------------------


class TestMonorepoAddReleasable:
    """Tests for --releasable flag on monorepo add command."""

    def _setup_explicit_workspace(self, mock_git_repo):
        """Create a workspace with [[releasables]] defined."""
        _make_explicit_workspace(mock_git_repo, [
            {"name": "www"},
            {"name": "api"},
        ], [])  # No projects yet

    def _make_project_dir(self, root, name):
        """Create a project directory with a package.json."""
        proj_dir = root / name
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text(
            json.dumps({"name": f"test-{name}", "version": "0.1.0"})
        )
        return proj_dir

    def test_add_with_releasable_name(self, mock_git_repo):
        """--releasable <name> writes the releasable field to workspace.toml."""
        self._setup_explicit_workspace(mock_git_repo)
        self._make_project_dir(mock_git_repo, "app")

        with patch("rlsbl.commands.monorepo.commands.subprocess.run"):
            _cmd_add(["app"], {
                "releasable": "www",
                "auto-commit": False,
            }, project_root=mock_git_repo)

        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 1
        assert projects[0].releasable == "www"

    def test_add_with_releasable_false(self, mock_git_repo):
        """--releasable false writes releasable = false to workspace.toml."""
        self._setup_explicit_workspace(mock_git_repo)
        self._make_project_dir(mock_git_repo, "infra")

        with patch("rlsbl.commands.monorepo.commands.subprocess.run"):
            _cmd_add(["infra"], {
                "releasable": "false",
                "auto-commit": False,
            }, project_root=mock_git_repo)

        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 1
        assert projects[0].releasable is False

    def test_add_requires_releasable_in_explicit_mode(self, mock_git_repo):
        """In explicit mode, omitting --releasable is a hard error."""
        self._setup_explicit_workspace(mock_git_repo)
        self._make_project_dir(mock_git_repo, "app")

        with pytest.raises(SystemExit):
            _cmd_add(["app"], {
                "auto-commit": False,
            }, project_root=mock_git_repo)

    def test_add_validates_releasable_exists(self, mock_git_repo):
        """--releasable <name> errors if the name is not in [[releasables]]."""
        self._setup_explicit_workspace(mock_git_repo)
        self._make_project_dir(mock_git_repo, "app")

        with pytest.raises(SystemExit):
            _cmd_add(["app"], {
                "releasable": "nonexistent",
                "auto-commit": False,
            }, project_root=mock_git_repo)

    def test_add_without_releasable_flag(self, mock_git_repo):
        """--releasable is optional when adding a project."""
        from rlsbl.workspace import save_workspace
        save_workspace(str(mock_git_repo), [])
        self._make_project_dir(mock_git_repo, "lib")

        with patch("rlsbl.commands.monorepo.commands.subprocess.run"):
            _cmd_add(["lib"], {
                "auto-commit": False,
            }, project_root=mock_git_repo)

        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 1
        # releasable field should not be present
        assert projects[0].releasable is None
