"""Tests for batch release-init Flutter support, TOML comments/context, and shared validation."""

import json
import os
import tempfile

import pytest
import tomlkit

from conftest import make_workspace, run_git

from rlsbl.commands.monorepo import _cmd_batch_release_init
from rlsbl.release_file import (
    ReleaseConfig,
    _validate_release_config,
    get_batch_release_file_path,
    read_batch_release_file,
    read_release_file,
)


SAMPLE_FLUTTER_PUBSPEC = """\
name: my_flutter_app
description: A Flutter application.
version: 1.0.0+1

environment:
  sdk: ^3.0.0

flutter:
  uses-material-design: true
"""


# ---------------------------------------------------------------------------
# Task 1: Flutter target config sections in batch release-init
# ---------------------------------------------------------------------------


class TestBatchReleaseInitFlutter:
    """Batch release-init scaffolds Flutter target config sections."""

    def test_flutter_ios_target_section(self, mock_git_repo):
        """Flutter-ios project gets [packages.<name>.targets.flutter-ios] with mode=build."""
        make_workspace(mock_git_repo, [
            {"path": "app", "name": "app"},
        ])

        app_dir = mock_git_repo / "app"
        app_dir.mkdir()
        (app_dir / "pubspec.yaml").write_text(SAMPLE_FLUTTER_PUBSPEC)

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "app" in data["packages"]
        pkg = data["packages"]["app"]
        assert "targets" in pkg
        assert "flutter-ios" in pkg["targets"]
        assert pkg["targets"]["flutter-ios"]["mode"] == "build"

    def test_flutter_android_target_section(self, mock_git_repo):
        """Flutter-android project gets [packages.<name>.targets.flutter-android] with mode=build."""
        make_workspace(mock_git_repo, [
            {"path": "app", "name": "app"},
        ])

        app_dir = mock_git_repo / "app"
        app_dir.mkdir()
        (app_dir / "pubspec.yaml").write_text(SAMPLE_FLUTTER_PUBSPEC)

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        pkg = data["packages"]["app"]
        assert "flutter-android" in pkg["targets"]
        assert pkg["targets"]["flutter-android"]["mode"] == "build"

    def test_both_flutter_targets_scaffolded(self, mock_git_repo):
        """Both flutter-ios and flutter-android get target sections."""
        make_workspace(mock_git_repo, [
            {"path": "app", "name": "app"},
        ])

        app_dir = mock_git_repo / "app"
        app_dir.mkdir()
        (app_dir / "pubspec.yaml").write_text(SAMPLE_FLUTTER_PUBSPEC)

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        pkg = data["packages"]["app"]
        assert "flutter-ios" in pkg["targets"]
        assert "flutter-android" in pkg["targets"]
        assert pkg["targets"]["flutter-ios"]["mode"] == "build"
        assert pkg["targets"]["flutter-android"]["mode"] == "build"

    def test_non_flutter_project_no_targets(self, mock_git_repo):
        """Non-Flutter projects do not get a targets section."""
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

        assert "targets" not in data["packages"]["lib"]

    def test_mixed_flutter_and_non_flutter(self, mock_git_repo):
        """Mixed workspace: Flutter project gets targets, npm project does not."""
        make_workspace(mock_git_repo, [
            {"path": "app", "name": "app"},
            {"path": "lib", "name": "lib"},
        ])

        app_dir = mock_git_repo / "app"
        app_dir.mkdir()
        (app_dir / "pubspec.yaml").write_text(SAMPLE_FLUTTER_PUBSPEC)

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

        # Flutter project has targets
        assert "targets" in data["packages"]["app"]
        assert "flutter-ios" in data["packages"]["app"]["targets"]

        # npm project does not
        assert "targets" not in data["packages"]["lib"]

    def test_scaffolded_flutter_validates(self, mock_git_repo):
        """Scaffolded Flutter batch file passes read_batch_release_file when bump/description are filled."""
        make_workspace(mock_git_repo, [
            {"path": "app", "name": "app"},
        ])

        app_dir = mock_git_repo / "app"
        app_dir.mkdir()
        (app_dir / "pubspec.yaml").write_text(SAMPLE_FLUTTER_PUBSPEC)

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))

        # Edit the scaffolded file to set required fields
        data = tomlkit.loads(open(batch_path).read())
        data["packages"]["app"]["bump"] = "patch"
        data["packages"]["app"]["description"] = "test release"
        with open(batch_path, "w") as f:
            tomlkit.dump(data, f)

        # Should parse without error
        config = read_batch_release_file(batch_path)
        assert config.packages["app"].targets["flutter-ios"]["mode"] == "build"
        assert config.packages["app"].targets["flutter-android"]["mode"] == "build"


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
        assert "# Version bump type: patch, minor, or major" in raw

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
        assert data["packages"]["pkg"]["context"] == ""

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
            assert data["packages"][name]["context"] == ""

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
        cfg = _validate_release_config(data, prefix="[packages.mylib] ")
        assert cfg.bump == "minor"
        assert cfg.include == ["npm"]
        assert cfg.description == "new feature"
        assert cfg.context == "required for v2"

    def test_missing_bump_no_prefix(self):
        """Missing bump without prefix raises with no prefix in message."""
        data = {"include": [], "exclude": []}
        with pytest.raises(ValueError, match="^missing required field: bump$"):
            _validate_release_config(data)

    def test_missing_bump_with_prefix(self):
        """Missing bump with prefix includes prefix in message."""
        data = {"include": [], "exclude": []}
        with pytest.raises(
            ValueError, match=r"^\[packages\.foo\] missing required field: bump$"
        ):
            _validate_release_config(data, prefix="[packages.foo] ")

    def test_invalid_bump_no_prefix(self):
        """Invalid bump without prefix."""
        data = {"bump": "huge", "include": [], "exclude": []}
        with pytest.raises(ValueError, match="bump must be set.*invalid bump"):
            _validate_release_config(data)

    def test_invalid_bump_with_prefix(self):
        """Invalid bump with prefix."""
        data = {"bump": "huge", "include": [], "exclude": []}
        with pytest.raises(ValueError, match=r"\[packages\.bar\].*invalid bump"):
            _validate_release_config(data, prefix="[packages.bar] ")

    def test_flutter_mode_required_no_prefix(self):
        """Flutter mode validation works without prefix."""
        data = {
            "bump": "patch",
            "include": ["flutter-ios"],
            "exclude": [],
            "description": "test",
        }
        with pytest.raises(ValueError, match="requires.*mode"):
            _validate_release_config(data)

    def test_flutter_mode_required_with_prefix(self):
        """Flutter mode validation works with prefix."""
        data = {
            "bump": "patch",
            "include": ["flutter-ios"],
            "exclude": [],
            "description": "test",
        }
        with pytest.raises(ValueError, match=r"\[packages\.app\].*requires.*mode"):
            _validate_release_config(data, prefix="[packages.app] ")

    def test_flutter_same_mode_no_prefix(self):
        """Flutter same-mode check works without prefix."""
        data = {
            "bump": "patch",
            "include": ["flutter-ios", "flutter-android"],
            "exclude": [],
            "description": "test",
            "targets": {
                "flutter-ios": {"mode": "ota"},
                "flutter-android": {"mode": "build"},
            },
        }
        with pytest.raises(ValueError, match="same mode"):
            _validate_release_config(data)

    def test_flutter_same_mode_with_prefix(self):
        """Flutter same-mode check works with prefix."""
        data = {
            "bump": "patch",
            "include": ["flutter-ios", "flutter-android"],
            "exclude": [],
            "description": "test",
            "targets": {
                "flutter-ios": {"mode": "ota"},
                "flutter-android": {"mode": "build"},
            },
        }
        with pytest.raises(ValueError, match=r"\[packages\.myapp\].*same mode"):
            _validate_release_config(data, prefix="[packages.myapp] ")

    def test_include_exclude_overlap_no_prefix(self):
        data = {
            "bump": "patch",
            "include": ["pypi"],
            "exclude": ["pypi"],
            "description": "test",
        }
        with pytest.raises(ValueError, match="both include and exclude"):
            _validate_release_config(data)

    def test_include_exclude_overlap_with_prefix(self):
        data = {
            "bump": "patch",
            "include": ["pypi"],
            "exclude": ["pypi"],
            "description": "test",
        }
        with pytest.raises(
            ValueError, match=r"\[packages\.x\].*both include and exclude"
        ):
            _validate_release_config(data, prefix="[packages.x] ")

    def test_description_empty_no_prefix(self):
        data = {
            "bump": "patch",
            "include": [],
            "exclude": [],
            "description": "",
        }
        with pytest.raises(ValueError, match="description must be set"):
            _validate_release_config(data)

    def test_description_empty_with_prefix(self):
        data = {
            "bump": "patch",
            "include": [],
            "exclude": [],
            "description": "",
        }
        with pytest.raises(
            ValueError, match=r"\[packages\.z\].*description must be set"
        ):
            _validate_release_config(data, prefix="[packages.z] ")

    def test_context_not_string_no_prefix(self):
        data = {
            "bump": "patch",
            "include": [],
            "exclude": [],
            "description": "test",
            "context": 42,
        }
        with pytest.raises(ValueError, match="context must be a string"):
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
            ValueError, match=r"\[packages\.q\].*context must be a string"
        ):
            _validate_release_config(data, prefix="[packages.q] ")


class TestSharedValidationViaPublicAPIs:
    """Verify read_release_file and read_batch_release_file both use shared validation.

    These tests check that both paths handle Flutter validation identically --
    previously batch was missing Flutter mode requirement and same-mode checks.
    """

    def test_batch_flutter_requires_mode(self, tmp_path):
        """Batch path now rejects Flutter targets without mode (was previously missing)."""
        f = tmp_path / "batch.toml"
        f.write_text(
            '[packages.myapp]\n'
            'bump = "patch"\n'
            'description = "test"\n'
            'include = ["flutter-ios"]\n'
            'exclude = []\n'
        )
        with pytest.raises(ValueError, match="requires.*mode"):
            read_batch_release_file(str(f))

    def test_batch_flutter_same_mode_required(self, tmp_path):
        """Batch path now rejects Flutter targets with different modes."""
        f = tmp_path / "batch.toml"
        f.write_text(
            '[packages.myapp]\n'
            'bump = "patch"\n'
            'description = "test"\n'
            'include = ["flutter-ios", "flutter-android"]\n'
            'exclude = []\n'
            '\n'
            '[packages.myapp.targets.flutter-ios]\n'
            'mode = "ota"\n'
            '\n'
            '[packages.myapp.targets.flutter-android]\n'
            'mode = "build"\n'
        )
        with pytest.raises(ValueError, match="same mode"):
            read_batch_release_file(str(f))

    def test_batch_flutter_valid(self, tmp_path):
        """Batch path accepts Flutter targets with valid mode config."""
        f = tmp_path / "batch.toml"
        f.write_text(
            '[packages.myapp]\n'
            'bump = "minor"\n'
            'description = "test release"\n'
            'include = ["flutter-ios", "flutter-android"]\n'
            'exclude = []\n'
            '\n'
            '[packages.myapp.targets.flutter-ios]\n'
            'mode = "build"\n'
            '\n'
            '[packages.myapp.targets.flutter-android]\n'
            'mode = "build"\n'
        )
        config = read_batch_release_file(str(f))
        assert config.packages["myapp"].targets["flutter-ios"]["mode"] == "build"
        assert config.packages["myapp"].targets["flutter-android"]["mode"] == "build"

    def test_single_and_batch_same_valid_result(self, tmp_path):
        """Same config parsed via single and batch paths produces identical ReleaseConfig."""
        # Single-project file
        single = tmp_path / "single.toml"
        single.write_text(
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
            '[packages.mylib]\n'
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
        assert single_cfg.targets == pkg_cfg.targets
        assert single_cfg.description == pkg_cfg.description
        assert single_cfg.context == pkg_cfg.context
