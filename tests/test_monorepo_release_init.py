"""Tests for monorepo release-init command (batch release file scaffolding)."""

import json
import os

import pytest
import tomlkit

from conftest import make_workspace, run_git

from rlsbl.commands.monorepo import _cmd_batch_release_init
from rlsbl.release_file import get_batch_release_file_path


class TestBatchReleaseInit:
    """Tests for _cmd_batch_release_init."""

    def test_scaffolds_correct_toml_structure(self, mock_git_repo):
        """Creates unreleased.toml with [packages.<name>] sections and detected targets."""
        # Set up a workspace with two projects (npm and pypi)
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
            {"path": "pkg-b", "name": "pkg-b"},
        ])

        # Create project dirs with target files
        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        pkg_b = mock_git_repo / "pkg-b"
        pkg_b.mkdir()
        (pkg_b / "pyproject.toml").write_text(
            '[project]\nname = "pkg-b"\nversion = "0.1.0"\n'
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        assert os.path.exists(batch_path)

        data = tomlkit.loads(open(batch_path).read())
        assert "packages" in data

        # pkg-a should have npm target
        assert "pkg-a" in data["packages"]
        assert data["packages"]["pkg-a"]["bump"] == ""
        assert data["packages"]["pkg-a"]["description"] == ""
        assert "npm" in data["packages"]["pkg-a"]["include"]
        assert data["packages"]["pkg-a"]["exclude"] == []

        # pkg-b should have pypi target
        assert "pkg-b" in data["packages"]
        assert data["packages"]["pkg-b"]["bump"] == ""
        assert data["packages"]["pkg-b"]["description"] == ""
        assert "pypi" in data["packages"]["pkg-b"]["include"]
        assert data["packages"]["pkg-b"]["exclude"] == []

    def test_errors_on_existing_non_empty_file(self, mock_git_repo):
        """Exits with error if unreleased.toml already exists and is non-empty."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Create the file with content
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)
        with open(batch_path, "w") as f:
            f.write("[packages.pkg-a]\nbump = \"patch\"\n")

        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(project_root=mock_git_repo)
        assert exc_info.value.code == 1

    def test_overwrites_empty_existing_file(self, mock_git_repo):
        """Succeeds if unreleased.toml exists but is empty."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Create an empty file
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)
        with open(batch_path, "w") as f:
            pass

        # Should not raise
        _cmd_batch_release_init(project_root=mock_git_repo)

        data = tomlkit.loads(open(batch_path).read())
        assert "pkg-a" in data["packages"]

    def test_skips_dev_node_projects(self, mock_git_repo):
        """Dev-node projects are excluded from the batch release file."""
        make_workspace(mock_git_repo, [
            {"path": "lib", "name": "lib"},
            {"path": "test-infra", "name": "test-infra", "dev_node": True},
        ])

        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "1.0.0"}) + "\n"
        )

        infra_dir = mock_git_repo / "test-infra"
        infra_dir.mkdir()
        (infra_dir / "package.json").write_text(
            json.dumps({"name": "test-infra", "version": "0.1.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "lib" in data["packages"]
        assert "test-infra" not in data["packages"]

    def test_creates_releases_directory(self, mock_git_repo):
        """Creates the releases/ directory if it doesn't exist."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        releases_dir = mock_git_repo / ".rlsbl-monorepo" / "releases"
        assert not releases_dir.exists()

        _cmd_batch_release_init(project_root=mock_git_repo)

        assert releases_dir.exists()
        assert (releases_dir / "unreleased.toml").exists()

    def test_errors_without_workspace(self, mock_git_repo):
        """Exits with error when no workspace.toml exists."""
        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(project_root=mock_git_repo)
        assert exc_info.value.code == 1

    def test_skips_projects_without_targets(self, mock_git_repo, capsys):
        """Projects with no detectable targets are skipped with a warning."""
        make_workspace(mock_git_repo, [
            {"path": "has-target", "name": "has-target"},
            {"path": "no-target", "name": "no-target"},
        ])

        has_dir = mock_git_repo / "has-target"
        has_dir.mkdir()
        (has_dir / "package.json").write_text(
            json.dumps({"name": "has-target", "version": "1.0.0"}) + "\n"
        )

        no_dir = mock_git_repo / "no-target"
        no_dir.mkdir()
        # No target files -- just an empty dir

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "has-target" in data["packages"]
        assert "no-target" not in data["packages"]

        captured = capsys.readouterr()
        assert "no targets detected for no-target" in captured.err
