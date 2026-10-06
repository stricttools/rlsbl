"""Tests for batch release-init sections, TOML comments/context, and shared validation."""

import json

import pytest
import tomlkit

from conftest import make_workspace, run_git

from rlsbl.commands.monorepo import _cmd_batch_release_init
from rlsbl.errors import ReleaseFileError
from rlsbl.release_file import (
    ReleaseConfig,
    _validate_release_config,
    get_batch_release_file_path,
    read_batch_release_file,
    read_release_file,
)


# ---------------------------------------------------------------------------
# Batch release-init writes no per-target sections
# ---------------------------------------------------------------------------


class TestBatchReleaseInitSections:
    """Batch release-init writes no per-target config section."""

    def test_a_project_gets_no_targets_section(self, mock_git_repo):
        """A releasable's section carries no targets table."""
        make_workspace(mock_git_repo, [
            {"path": "lib", "name": "lib"},
        ])

        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "targets" not in data["releasables"]["lib"]

# ---------------------------------------------------------------------------
# Task 2: TOML comments and context field
# ---------------------------------------------------------------------------


class TestBatchReleaseInitComments:
    """Batch release-init scaffolds TOML comments and context field."""

    def test_has_bump_comment(self, mock_git_repo):
        """Scaffolded file contains bump type comment."""
        make_workspace(mock_git_repo, [
            {"path": "pkg", "name": "pkg"},
        ])

        pkg_dir = mock_git_repo / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "package.json").write_text(
            json.dumps({"name": "pkg", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        raw = open(batch_path).read()
        assert "# Version bump type: patch, minor, major, infra, or prerelease" in raw

    def test_has_description_comment(self, mock_git_repo):
        """Scaffolded file contains description comment."""
        make_workspace(mock_git_repo, [
            {"path": "pkg", "name": "pkg"},
        ])

        pkg_dir = mock_git_repo / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "package.json").write_text(
            json.dumps({"name": "pkg", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        raw = open(batch_path).read()
        assert "# Short description of this release (required)" in raw

    def test_has_context_comment(self, mock_git_repo):
        """Scaffolded file contains context comment."""
        make_workspace(mock_git_repo, [
            {"path": "pkg", "name": "pkg"},
        ])

        pkg_dir = mock_git_repo / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "package.json").write_text(
            json.dumps({"name": "pkg", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        raw = open(batch_path).read()
        assert "# Optional context explaining why these changes were made" in raw

    def test_has_context_field(self, mock_git_repo):
        """Scaffolded file has context = "" field."""
        make_workspace(mock_git_repo, [
            {"path": "pkg", "name": "pkg"},
        ])

        pkg_dir = mock_git_repo / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "package.json").write_text(
            json.dumps({"name": "pkg", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())
        assert data["releasables"]["pkg"]["context"] == ""

    def test_all_packages_have_comments_and_context(self, mock_git_repo):
        """Every package section has comments and context field."""
        make_workspace(mock_git_repo, [
            {"path": "alpha", "name": "alpha"},
            {"path": "beta", "name": "beta"},
        ])

        for name in ("alpha", "beta"):
            d = mock_git_repo / name
            d.mkdir()
            (d / "package.json").write_text(
                json.dumps({"name": name, "version": "1.0.0"}) + "\n"
            )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())
        raw = open(batch_path).read()

        for name in ("alpha", "beta"):
            assert data["releasables"][name]["context"] == ""

        # Comments should appear at least twice (once per package)
        assert raw.count("# Version bump type:") >= 2
        assert raw.count("# Short description") >= 2
        assert raw.count("# Optional context") >= 2


# ---------------------------------------------------------------------------
# Task 3: Shared validation works identically for both paths
# ---------------------------------------------------------------------------


class TestSharedValidation:
    """_validate_release_config produces identical results for single and batch paths."""

    def test_valid_config_no_prefix(self):
        """No-prefix validation (single project) works."""
        data = {
            "bump": "patch",
            "include": ["pypi"],
            "exclude": [],
            "description": "test release",
        }
        cfg = _validate_release_config(data)
        assert isinstance(cfg, ReleaseConfig)
        assert cfg.bump == "patch"
        assert cfg.include == ["pypi"]
        assert cfg.description == "test release"
        assert cfg.context == ""

    def test_valid_config_with_prefix(self):
        """Prefixed validation (batch) works."""
        data = {
            "bump": "minor",
            "include": ["npm"],
            "exclude": [],
            "description": "new feature",
            "context": "required for v2",
        }
        cfg = _validate_release_config(data, prefix="[releasables.mylib] ")
        assert cfg.bump == "minor"
        assert cfg.include == ["npm"]
        assert cfg.description == "new feature"
        assert cfg.context == "required for v2"

    def test_missing_bump_no_prefix(self):
        """Missing bump without prefix raises with no prefix in message."""
        data = {"include": [], "exclude": []}
        with pytest.raises(ReleaseFileError, match="^missing required field: bump$"):
            _validate_release_config(data)

    def test_missing_bump_with_prefix(self):
        """Missing bump with prefix includes prefix in message."""
        data = {"include": [], "exclude": []}
        with pytest.raises(
            ReleaseFileError, match=r"^\[releasables\.foo\] missing required field: bump$"
        ):
            _validate_release_config(data, prefix="[releasables.foo] ")

    def test_invalid_bump_no_prefix(self):
        """Invalid bump without prefix."""
        data = {"bump": "huge", "include": [], "exclude": []}
        with pytest.raises(ReleaseFileError, match="bump must be set.*invalid bump"):
            _validate_release_config(data)

    def test_invalid_bump_with_prefix(self):
        """Invalid bump with prefix."""
        data = {"bump": "huge", "include": [], "exclude": []}
        with pytest.raises(ReleaseFileError, match=r"\[releasables\.bar\].*invalid bump"):
            _validate_release_config(data, prefix="[releasables.bar] ")

    def test_include_exclude_overlap_no_prefix(self):
        data = {
            "bump": "patch",
            "include": ["pypi"],
            "exclude": ["pypi"],
            "description": "test",
        }
        with pytest.raises(ReleaseFileError, match="both include and exclude"):
            _validate_release_config(data)

    def test_include_exclude_overlap_with_prefix(self):
        data = {
            "bump": "patch",
            "include": ["pypi"],
            "exclude": ["pypi"],
            "description": "test",
        }
        with pytest.raises(
            ReleaseFileError, match=r"\[releasables\.x\].*both include and exclude"
        ):
            _validate_release_config(data, prefix="[releasables.x] ")

    def test_description_empty_no_prefix(self):
        data = {
            "bump": "patch",
            "include": [],
            "exclude": [],
            "description": "",
        }
        with pytest.raises(ReleaseFileError, match="description must be set"):
            _validate_release_config(data)

    def test_description_empty_with_prefix(self):
        data = {
            "bump": "patch",
            "include": [],
            "exclude": [],
            "description": "",
        }
        with pytest.raises(
            ReleaseFileError, match=r"\[releasables\.z\].*description must be set"
        ):
            _validate_release_config(data, prefix="[releasables.z] ")

    def test_context_not_string_no_prefix(self):
        data = {
            "bump": "patch",
            "include": [],
            "exclude": [],
            "description": "test",
            "context": 42,
        }
        with pytest.raises(ReleaseFileError, match="context must be a string"):
            _validate_release_config(data)

    def test_context_not_string_with_prefix(self):
        data = {
            "bump": "patch",
            "include": [],
            "exclude": [],
            "description": "test",
            "context": True,
        }
        with pytest.raises(
            ReleaseFileError, match=r"\[releasables\.q\].*context must be a string"
        ):
            _validate_release_config(data, prefix="[releasables.q] ")


class TestSharedValidationViaPublicAPIs:
    """Verify read_release_file and read_batch_release_file both use shared validation."""

    def test_single_and_batch_same_valid_result(self, tmp_path):
        """Same config parsed via single and batch paths produces identical ReleaseConfig."""
        # Single-project file
        single = tmp_path / "single.toml"
        single.write_text(
            'format_version = 1\n'
            'bump = "minor"\n'
            'include = ["pypi", "npm"]\n'
            'exclude = ["go"]\n'
            'description = "New widget API"\n'
            'context = "Required for v2"\n'
        )
        single_cfg = read_release_file(str(single))

        # Batch file with the same config under a package name
        batch = tmp_path / "batch.toml"
        batch.write_text(
            '[releasables.mylib]\n'
            'bump = "minor"\n'
            'include = ["pypi", "npm"]\n'
            'exclude = ["go"]\n'
            'description = "New widget API"\n'
            'context = "Required for v2"\n'
        )
        batch_cfg = read_batch_release_file(str(batch))
        pkg_cfg = batch_cfg.packages["mylib"]

        assert single_cfg.bump == pkg_cfg.bump
        assert single_cfg.include == pkg_cfg.include
        assert single_cfg.exclude == pkg_cfg.exclude
        assert single_cfg.description == pkg_cfg.description
        assert single_cfg.context == pkg_cfg.context
