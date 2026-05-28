"""Tests for the release-init command."""

import os
from unittest.mock import patch

import pytest
import tomlkit

from rlsbl.targets import TargetEntry


def _read_scaffolded_toml(path):
    """Read a scaffolded TOML file and return the raw tomlkit document.

    Unlike read_release_file(), this does not validate field values,
    so it works with the empty decision fields that scaffolding produces.
    """
    with open(path, "r", encoding="utf-8") as f:
        return tomlkit.load(f)


def _run_release_init(tmp_path, target_entries):
    """Helper: run cmd_release_init in tmp_path with mocked detect_targets."""
    # Create .rlsbl/ so _require_project_root succeeds
    rlsbl_dir = tmp_path / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)

    original_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        with patch("rlsbl.targets.detect_targets", return_value=target_entries):
            # Import here so the patched detect_targets is used
            from rlsbl import cmd_release_init
            cmd_release_init()
    finally:
        os.chdir(original_cwd)


class TestReleaseInitSingleTarget:
    """Single target detected -> correct file created."""

    def test_pypi_target(self, tmp_path):
        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        assert release_path.exists()

        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["pypi"]
        assert list(data["exclude"]) == []
        assert "targets" not in data

        # Verify the comment is present in the raw file
        raw = release_path.read_text()
        assert "# Version bump type: patch, minor, or major" in raw

    def test_npm_target(self, tmp_path):
        entries = [TargetEntry(name="npm", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["npm"]


class TestReleaseInitMultipleTargets:
    """Multiple targets detected -> all listed in include."""

    def test_two_targets(self, tmp_path):
        entries = [
            TargetEntry(name="pypi", path=str(tmp_path)),
            TargetEntry(name="npm", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["pypi", "npm"]
        assert list(data["exclude"]) == []
        assert "targets" not in data

    def test_three_targets(self, tmp_path):
        entries = [
            TargetEntry(name="pypi", path=str(tmp_path)),
            TargetEntry(name="npm", path=str(tmp_path)),
            TargetEntry(name="go", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["pypi", "npm", "go"]


class TestReleaseInitFlutterTargets:
    """Flutter targets -> [targets] section with mode = 'build'."""

    def test_flutter_ios(self, tmp_path):
        entries = [TargetEntry(name="flutter-ios", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["flutter-ios"]
        assert data["targets"]["flutter-ios"]["mode"] == "build"

    def test_flutter_android(self, tmp_path):
        entries = [TargetEntry(name="flutter-android", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert data["targets"]["flutter-android"]["mode"] == "build"

    def test_mixed_flutter_and_regular(self, tmp_path):
        entries = [
            TargetEntry(name="pypi", path=str(tmp_path)),
            TargetEntry(name="flutter-ios", path=str(tmp_path)),
            TargetEntry(name="flutter-android", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["pypi", "flutter-ios", "flutter-android"]
        assert list(data["exclude"]) == []
        assert data["targets"]["flutter-ios"]["mode"] == "build"
        assert data["targets"]["flutter-android"]["mode"] == "build"
        # pypi should NOT have a targets section
        assert "pypi" not in data["targets"]


class TestReleaseInitAlreadyExists:
    """File already exists -> error."""

    def test_errors_if_file_exists(self, tmp_path):
        # Create the file first
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
        )

        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        with pytest.raises(SystemExit):
            _run_release_init(tmp_path, entries)


class TestReleaseInitEmptyFileAllowed:
    """Empty or whitespace-only file does not block release-init."""

    def test_empty_file_allows_overwrite(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text("")

        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        assert release_path.exists()
        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["pypi"]

    def test_whitespace_only_file_allows_overwrite(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text("   \n\n  \n")

        entries = [TargetEntry(name="npm", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["npm"]

    def test_nonempty_file_still_blocks(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text('bump = "minor"\n')

        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        with pytest.raises(SystemExit):
            _run_release_init(tmp_path, entries)


class TestReleaseInitRoundTrip:
    """Verify the generated file structure and that it requires editing before use."""

    def test_scaffolded_file_has_correct_structure(self, tmp_path):
        entries = [TargetEntry(name="cargo", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert isinstance(data["bump"], str)
        assert isinstance(data["include"], list)
        assert isinstance(data["exclude"], list)

    def test_scaffolded_file_rejected_without_editing(self, tmp_path):
        """read_release_file rejects the scaffolded file because bump is empty."""
        from rlsbl.release_file import read_release_file

        entries = [TargetEntry(name="cargo", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        with pytest.raises(ValueError, match="bump must be set"):
            read_release_file(str(release_path))

    def test_roundtrip_with_flutter(self, tmp_path):
        entries = [
            TargetEntry(name="flutter-ios", path=str(tmp_path)),
            TargetEntry(name="flutter-android", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["flutter-ios", "flutter-android"]
        assert list(data["exclude"]) == []
        assert "flutter-ios" in data["targets"]
        assert "flutter-android" in data["targets"]
        assert data["targets"]["flutter-ios"]["mode"] == "build"
        assert data["targets"]["flutter-android"]["mode"] == "build"


class TestReleaseInitPrintsPath:
    """The command prints the path of the created file."""

    def test_prints_path(self, tmp_path, capsys):
        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries)

        captured = capsys.readouterr()
        assert "unreleased.toml" in captured.out
        assert ".rlsbl/releases/unreleased.toml" in captured.out


class TestReleaseInitNoTargets:
    """No targets detected -> error."""

    def test_errors_with_no_targets(self, tmp_path):
        with pytest.raises(SystemExit):
            _run_release_init(tmp_path, [])


class TestReleaseInitMonorepo:
    """In monorepo mode, release-init creates the file in the package directory."""

    def test_creates_file_in_package_dir(self, tmp_path):
        """When inside a monorepo package, release file goes in the package dir."""
        from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE

        # Create monorepo structure
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "python"\nname = "mylib"\n'
        )

        # Create package directory with .rlsbl/ (scaffold creates this)
        pkg_dir = tmp_path / "python"
        pkg_dir.mkdir()
        (pkg_dir / ".rlsbl").mkdir()

        entries = [TargetEntry(name="pypi", path=str(pkg_dir))]

        original_cwd = os.getcwd()
        os.chdir(str(pkg_dir))
        try:
            with patch("rlsbl.targets.detect_targets", return_value=entries):
                from rlsbl import cmd_release_init
                cmd_release_init()
        finally:
            os.chdir(original_cwd)

        # The release file should be in the package's directory, not the workspace root
        release_path = pkg_dir / ".rlsbl" / "releases" / "unreleased.toml"
        assert release_path.exists()

        # Workspace root should NOT have a release file
        root_release = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        assert not root_release.exists()

        # Verify the content has correct structure
        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["pypi"]
