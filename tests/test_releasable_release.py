"""Tests for Phase 6: release flow on releasable model.

Covers:
- ReleaseState construction with releasable fields
- Tag format from releasable (_format_releasable_tag, _releasable_tag_glob)
- compute_release_version with releasable_tag_fmt
- Release file parsing with [releasables.*] sections
- Batch release file scaffolding with releasable sections
- validate_changelog_state with releasable changes dir
- Batch release releasable ordering
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.release.execute import ReleaseState, _sync_member_package_versions
from rlsbl.commands.release.validate import (
    _format_releasable_tag,
    _releasable_tag_glob,
)
from rlsbl.errors import ConfigError, ReleaseFileError, VersionError
from rlsbl.release_file import (
    BatchReleaseConfig,
    read_batch_release_file,
)
from rlsbl.workspace import (
    Releasable,
    WorkspaceProject,
    save_workspace,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    write_releasable_version,
    get_releasable_changes_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(path, content):
    """Write a TOML string to a file, creating directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _write_workspace(tmp_path, content):
    """Write raw TOML content to workspace.toml in a temp directory."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


def _make_pypi_project(base_path, subdir, version="0.1.0"):
    """Create a minimal pypi project."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    content = f'[project]\nname = "{subdir}"\nversion = "{version}"\n'
    with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Tag format helpers
# ---------------------------------------------------------------------------


class TestFormatReleasableTag:

    def test_default_format(self):
        tag = _format_releasable_tag("{name}@v{version}", "www", "2.0.0")
        assert tag == "www@v2.0.0"

    def test_simple_format(self):
        tag = _format_releasable_tag("v{version}", "www", "1.0.0")
        assert tag == "v1.0.0"

    def test_custom_format(self):
        tag = _format_releasable_tag("{name}/v{version}", "core", "3.2.1")
        assert tag == "core/v3.2.1"


class TestReleasableTagGlob:

    def test_default_format(self):
        glob = _releasable_tag_glob("{name}@v{version}", "www")
        assert glob == "www@v*"

    def test_simple_format(self):
        glob = _releasable_tag_glob("v{version}", "www")
        assert glob == "v*"

    def test_custom_format(self):
        glob = _releasable_tag_glob("{name}/v{version}", "core")
        assert glob == "core/v*"


# ---------------------------------------------------------------------------
# ReleaseState with releasable fields
# ---------------------------------------------------------------------------


class TestReleaseStateReleasable:

    def test_defaults_are_none(self):
        """Releasable fields default to None when not set."""
        state = ReleaseState(
            registry="pypi",
            target=MagicMock(),
            new_version="1.0.0",
            current_version="0.9.0",
            bump_type="minor",
            tag="v1.0.0",
            branch="main",
        )
        assert state.releasable_name is None
        assert state.member_package_paths is None
        assert state.releasable_tag_format is None

    def test_explicit_mode_fields(self):
        """In explicit mode, releasable fields are populated."""
        state = ReleaseState(
            registry="pypi",
            target=MagicMock(),
            new_version="2.0.0",
            current_version="1.0.0",
            bump_type="major",
            tag="www@v2.0.0",
            branch="main",
            releasable_name="www",
            member_package_paths=["packages/api", "packages/web"],
            releasable_tag_format="{name}@v{version}",
        )
        assert state.releasable_name == "www"
        assert state.member_package_paths == ["packages/api", "packages/web"]
        assert state.releasable_tag_format == "{name}@v{version}"


# ---------------------------------------------------------------------------
# Release file: [releasables.*] sections
# ---------------------------------------------------------------------------


class TestBatchReleaseFileReleasables:

    def test_read_releasables_section(self, tmp_path):
        """Batch release file with [releasables.*] sections is parsed correctly."""
        toml_content = """\
[releasables.www]
bump = "minor"
description = "New features"
include = ["pypi"]
exclude = []

[releasables.core]
bump = "patch"
description = "Bug fixes"
include = ["pypi"]
exclude = []
"""
        path = tmp_path / "unreleased.toml"
        path.write_text(toml_content)

        config = read_batch_release_file(str(path))
        assert config.section_type == "releasables"
        assert "www" in config.packages
        assert "core" in config.packages
        assert config.packages["www"].bump == "minor"
        assert config.packages["core"].bump == "patch"

    def test_read_packages_section_backward_compat(self, tmp_path):
        """[packages.*] sections still work and report section_type='packages'."""
        toml_content = """\
[packages.mylib]
bump = "patch"
description = "Fixes"
include = ["npm"]
exclude = []
"""
        path = tmp_path / "unreleased.toml"
        path.write_text(toml_content)

        config = read_batch_release_file(str(path))
        assert config.section_type == "packages"
        assert "mylib" in config.packages

    def test_both_sections_error(self, tmp_path):
        """Having both [packages] and [releasables] is a hard error."""
        toml_content = """\
[packages.a]
bump = "patch"
description = "X"
include = ["pypi"]
exclude = []

[releasables.b]
bump = "patch"
description = "Y"
include = ["pypi"]
exclude = []
"""
        path = tmp_path / "unreleased.toml"
        path.write_text(toml_content)

        with pytest.raises(ReleaseFileError, match="both.*packages.*releasables"):
            read_batch_release_file(str(path))

    def test_missing_section_error(self, tmp_path):
        """File with no [packages] or [releasables] raises error."""
        toml_content = """\
[something_else]
x = 1
"""
        path = tmp_path / "unreleased.toml"
        path.write_text(toml_content)

        with pytest.raises(ReleaseFileError, match="missing required section"):
            read_batch_release_file(str(path))

    def test_empty_releasables_error(self, tmp_path):
        """Empty [releasables] section raises error."""
        toml_content = "[releasables]\n"
        path = tmp_path / "unreleased.toml"
        path.write_text(toml_content)

        with pytest.raises(ReleaseFileError, match="empty"):
            read_batch_release_file(str(path))

    def test_releasable_section_validation(self, tmp_path):
        """Releasable sections validate the same fields as package sections."""
        toml_content = """\
[releasables.www]
bump = "invalid"
description = "X"
include = ["pypi"]
exclude = []
"""
        path = tmp_path / "unreleased.toml"
        path.write_text(toml_content)

        with pytest.raises(ReleaseFileError, match="bump must be set"):
            read_batch_release_file(str(path))


# ---------------------------------------------------------------------------
# Batch release releasable ordering
# ---------------------------------------------------------------------------


class TestReleasableReleaseOrder:

    def test_ordering_by_max_member_position(self, tmp_path):
        """Releasable release order is determined by max member topological position."""
        from rlsbl.commands.monorepo.batch_release import _releasable_release_order
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_root = str(tmp_path)

        # Set up workspace: core has no deps, www depends on core
        projects = [
            WorkspaceProject({"name": "core-api", "path": "core-api", "releasable": "core"}),
            WorkspaceProject({"name": "core-lib", "path": "core-lib", "releasable": "core"}),
            WorkspaceProject({"name": "www-app", "path": "www-app", "releasable": "www", "depends_on": ["core-api"]}),
            WorkspaceProject({"name": "www-assets", "path": "www-assets", "releasable": "www"}),
        ]
        releasables = [
            Releasable(name="core"),
            Releasable(name="www"),
        ]

        # Write workspace so WorkspaceGraph can load
        save_workspace(ws_root, projects)

        graph = WorkspaceGraph(ws_root, projects)
        batch_names = {"core", "www"}

        order = _releasable_release_order(batch_names, releasables, projects, graph)
        assert order.index("core") < order.index("www")


# ---------------------------------------------------------------------------
# compute_release_version with releasable_tag_fmt
# ---------------------------------------------------------------------------


class TestComputeReleaseVersionReleasable:

    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.bump_version")
    def test_uses_releasable_tag_format(self, mock_bump, mock_run):
        """compute_release_version uses releasable tag format when provided."""
        from rlsbl.commands.release.validate import compute_release_version

        mock_target = MagicMock()
        mock_run.side_effect = [
            # git tag -l for current tag: exists
            "www@v1.0.0\n",
            # git tag -l for new tag: doesn't exist
            "",
        ]
        mock_bump.return_value = "1.1.0"

        # Patch read_releasable_version to return the version
        with patch("rlsbl.workspace.read_releasable_version", return_value="1.0.0"):
            _cur, _new, _bump, tag = compute_release_version(
                mock_target, "/some/path", "minor",
                "www-app", "www-app", lambda msg: None,
                workspace_root="/ws",
                releasable_name="www",
                releasable_tag_fmt="{name}@v{version}",
            )

        assert tag == "www@v1.1.0"
        # target.monorepo_tag_format should NOT have been called
        mock_target.monorepo_tag_format.assert_not_called()

    @patch("rlsbl.commands.release.run")
    def test_no_releasable_tag_fmt_uses_target_tag(self, mock_run):
        """Without releasable_tag_fmt, falls back to target tag format."""
        from rlsbl.commands.release.validate import compute_release_version

        mock_target = MagicMock()
        mock_target.read_version.return_value = "1.0.0"
        mock_target.tag_format.return_value = "v1.0.0"
        # First call: tag doesn't exist (first release)
        mock_run.side_effect = ["", ""]

        _cur, _new, _bump, tag = compute_release_version(
            mock_target, "/some/path", None,
            None, None, lambda msg: None,
        )

        assert tag == "v1.0.0"
        mock_target.tag_format.assert_called()


# ---------------------------------------------------------------------------
# validate_changelog_state with releasable changes dir
# ---------------------------------------------------------------------------


class TestValidateChangelogStateReleasable:

    def test_uses_releasable_changes_dir(self, tmp_path):
        """In explicit mode, resolves the releasable's changes dir."""
        from rlsbl.commands.release.validate import validate_changelog_state

        ws_root = str(tmp_path)
        releasable_name = "www"

        # Create releasable changes dir with unreleased.jsonl
        changes_dir = get_releasable_changes_dir(ws_root, releasable_name)
        os.makedirs(changes_dir, exist_ok=True)
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            pass

        # validate_changelog_state now just resolves the changes dir path
        # (changelog validation moved to preflight-changelog checks)
        result = validate_changelog_state(
            "/some/project", MagicMock(), "www-app", "www-app",
            {}, releasable_name="www",
            releasable_tag_fmt="{name}@v{version}",
            workspace_root=ws_root,
        )

        assert result == changes_dir

    def test_missing_releasable_changes_dir_error(self, tmp_path):
        """Missing releasable changes dir raises ReleaseValidationError."""
        from rlsbl.commands.release.validate import (
            validate_changelog_state,
            ReleaseValidationError,
        )

        ws_root = str(tmp_path)

        with pytest.raises(ReleaseValidationError, match="not set up.*www"):
            validate_changelog_state(
                "/some/project", MagicMock(), "www-app", "www-app",
                {}, releasable_name="www",
                releasable_tag_fmt="{name}@v{version}",
                workspace_root=ws_root,
            )

    def test_no_releasable_uses_project_changes_dir(self, tmp_path):
        """Without releasable_name, uses per-project .rlsbl/changes/."""
        from rlsbl.commands.release.validate import validate_changelog_state

        project_dir = str(tmp_path)
        changes_dir = os.path.join(project_dir, ".rlsbl", "changes")
        os.makedirs(changes_dir, exist_ok=True)
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            pass

        # validate_changelog_state now just resolves the changes dir path
        result = validate_changelog_state(
            project_dir, MagicMock(), None, None, {},
        )

        assert result == changes_dir


# ---------------------------------------------------------------------------
# _sync_member_package_versions
# ---------------------------------------------------------------------------


class TestSyncMemberPackageVersions:

    def test_skips_private_packages(self, tmp_path):
        """Private packages (private: true) are not synced."""
        ws_root = str(tmp_path)
        pkg_path = "packages/internal"
        abs_pkg = os.path.join(ws_root, pkg_path)
        os.makedirs(abs_pkg, exist_ok=True)

        # Create a private config
        rlsbl_dir = os.path.join(abs_pkg, ".rlsbl")
        os.makedirs(rlsbl_dir, exist_ok=True)
        with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
            json.dump({"private": True, "targets": ["pypi"]}, f)

        # Create a pyproject.toml to detect as pypi
        with open(os.path.join(abs_pkg, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "internal"\nversion = "0.1.0"\n')

        files = []
        _sync_member_package_versions(
            [pkg_path], ws_root, "1.0.0",
            files, ws_root, lambda msg: None, MagicMock(),
        )
        assert files == []

    def test_skips_excluded_path(self, tmp_path):
        """The exclude_path is skipped."""
        ws_root = str(tmp_path)
        pkg_path = "packages/api"
        abs_pkg = os.path.join(ws_root, pkg_path)
        os.makedirs(abs_pkg, exist_ok=True)

        files = []
        _sync_member_package_versions(
            [pkg_path], ws_root, "1.0.0",
            files, ws_root, lambda msg: None, MagicMock(),
            exclude_path=pkg_path,
        )
        assert files == []

    def test_syncs_public_package(self, tmp_path):
        """Public packages with detected targets get version synced."""
        ws_root = str(tmp_path)
        pkg_path = "packages/api"
        abs_pkg = os.path.join(ws_root, pkg_path)
        os.makedirs(abs_pkg, exist_ok=True)

        # Create a public config
        rlsbl_dir = os.path.join(abs_pkg, ".rlsbl")
        os.makedirs(rlsbl_dir, exist_ok=True)
        with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
            json.dump({"private": False, "targets": ["pypi"]}, f)

        # Create a pyproject.toml
        with open(os.path.join(abs_pkg, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "api"\nversion = "0.1.0"\n')

        files = []
        _sync_member_package_versions(
            [pkg_path], ws_root, "1.0.0",
            files, ws_root, lambda msg: None, MagicMock(),
        )
        # Should have written the version -- files list should be non-empty
        assert len(files) > 0

    def test_write_version_error_propagates(self, tmp_path):
        """VersionError from write_version must propagate, not be swallowed."""
        ws_root = str(tmp_path)
        pkg_path = "packages/api"
        abs_pkg = os.path.join(ws_root, pkg_path)
        os.makedirs(abs_pkg, exist_ok=True)

        # Create a public config with pypi target
        rlsbl_dir = os.path.join(abs_pkg, ".rlsbl")
        os.makedirs(rlsbl_dir, exist_ok=True)
        with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
            json.dump({"private": False, "targets": ["pypi"]}, f)

        # Create a pyproject.toml so pypi target is detected
        with open(os.path.join(abs_pkg, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "api"\nversion = "0.1.0"\n')

        files = []
        with patch("rlsbl.targets.pypi.PypiTarget.write_version",
                   side_effect=VersionError("test error")):
            with pytest.raises(VersionError, match="test error"):
                _sync_member_package_versions(
                    [pkg_path], ws_root, "1.0.0",
                    files, ws_root, lambda msg: None, MagicMock(),
                )

    def test_config_error_propagates(self, tmp_path):
        """ConfigError from read_project_config must propagate, not be swallowed."""
        ws_root = str(tmp_path)
        pkg_path = "packages/broken"
        abs_pkg = os.path.join(ws_root, pkg_path)
        os.makedirs(abs_pkg, exist_ok=True)

        files = []
        with patch("rlsbl.config.read_project_config",
                   side_effect=ConfigError("malformed JSON")):
            with pytest.raises(ConfigError, match="malformed JSON"):
                _sync_member_package_versions(
                    [pkg_path], ws_root, "1.0.0",
                    files, ws_root, lambda msg: None, MagicMock(),
                )


# ---------------------------------------------------------------------------
# Batch release init scaffolding
# ---------------------------------------------------------------------------


class TestBatchReleaseInitReleasable:

    def test_scaffold_package_sections(self, tmp_path):
        """scaffold produces [packages.*] sections for package mode."""
        from rlsbl.commands.monorepo.batch_release_init import _scaffold_package_sections

        ws_root = str(tmp_path)
        _make_pypi_project(tmp_path, "lib-a")

        projects = [
            WorkspaceProject({"name": "lib-a", "path": "lib-a"}),
        ]
        save_workspace(ws_root, projects)

        batch_path = os.path.join(ws_root, ".rlsbl-monorepo", "releases", "unreleased.toml")

        with patch("rlsbl.commands.monorepo.batch_release_init._get_unreleased_commit_count", return_value=(5, None)):
            _scaffold_package_sections(ws_root, projects, batch_path, None)

        content = open(batch_path).read()
        assert "[packages.lib-a]" in content
        assert "[releasables." not in content

    def test_scaffold_explicit_mode_uses_releasables(self, tmp_path):
        """In explicit mode, scaffold produces [releasables.*] sections."""
        from rlsbl.commands.monorepo.batch_release_init import _scaffold_releasable_sections

        ws_root = str(tmp_path)
        _make_pypi_project(tmp_path, "api")
        _make_pypi_project(tmp_path, "web")

        # Write workspace with releasables
        _write_workspace(tmp_path, """\
[[releasables]]
name = "www"

[[projects]]
path = "api"
name = "api"
releasable = "www"

[[projects]]
path = "web"
name = "web"
releasable = "www"
""")

        projects = [
            WorkspaceProject({"name": "api", "path": "api", "releasable": "www"}),
            WorkspaceProject({"name": "web", "path": "web", "releasable": "www"}),
        ]

        batch_path = os.path.join(ws_root, ".rlsbl-monorepo", "releases", "unreleased.toml")

        with patch("rlsbl.commands.monorepo.batch_release_init._get_unreleased_commit_count", return_value=(3, None)):
            _scaffold_releasable_sections(ws_root, projects, batch_path, None)

        content = open(batch_path).read()
        assert "[releasables.www]" in content
        assert "[packages." not in content
