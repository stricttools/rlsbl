"""Tests for the release-init command."""

import os
import subprocess
from unittest.mock import patch

import pytest
import tomlkit

from rlsbl.errors import ReleaseFileError
from rlsbl.targets import TargetEntry


def _read_scaffolded_toml(path):
    """Read a scaffolded TOML file and return the raw tomlkit document.

    Unlike read_release_file(), this does not validate field values,
    so it works with the empty decision fields that scaffolding produces.
    """
    with open(path, "r", encoding="utf-8") as f:
        return tomlkit.load(f)


def _run_release_init(tmp_path, target_entries, monkeypatch):
    """Helper: run cmd_release_init in tmp_path with mocked detect_targets."""
    # Create .rlsbl/ so _require_project_root succeeds
    rlsbl_dir = tmp_path / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)

    monkeypatch.chdir(str(tmp_path))
    with patch("rlsbl.targets.detect_targets", return_value=target_entries):
        # Import here so the patched detect_targets is used
        from rlsbl import cmd_release_init
        cmd_release_init()


class TestReleaseInitSingleTarget:
    """Single target detected -> correct file created."""

    def test_pypi_target(self, tmp_path, monkeypatch):
        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        assert release_path.exists()

        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["pypi"]
        assert list(data["exclude"]) == []
        assert "targets" not in data

        # Verify the comment is present in the raw file
        raw = release_path.read_text()
        assert "# Version bump type: patch, minor, major, hotfix, or prerelease" in raw

    def test_npm_target(self, tmp_path, monkeypatch):
        entries = [TargetEntry(name="npm", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["npm"]


class TestReleaseInitMultipleTargets:
    """Multiple targets detected -> all listed in include."""

    def test_two_targets(self, tmp_path, monkeypatch):
        entries = [
            TargetEntry(name="pypi", path=str(tmp_path)),
            TargetEntry(name="npm", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["pypi", "npm"]
        assert list(data["exclude"]) == []
        assert "targets" not in data

    def test_three_targets(self, tmp_path, monkeypatch):
        entries = [
            TargetEntry(name="pypi", path=str(tmp_path)),
            TargetEntry(name="npm", path=str(tmp_path)),
            TargetEntry(name="go", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["pypi", "npm", "go"]


class TestReleaseInitFlutterTargets:
    """Flutter target -> [targets] section with mode = 'build'."""

    def test_flutter(self, tmp_path, monkeypatch):
        entries = [TargetEntry(name="flutter", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["flutter"]
        assert data["targets"]["flutter"]["mode"] == "build"

    def test_mixed_flutter_and_regular(self, tmp_path, monkeypatch):
        entries = [
            TargetEntry(name="pypi", path=str(tmp_path)),
            TargetEntry(name="flutter", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert list(data["include"]) == ["pypi", "flutter"]
        assert list(data["exclude"]) == []
        assert data["targets"]["flutter"]["mode"] == "build"
        # pypi should NOT have a targets section
        assert "pypi" not in data["targets"]


class TestReleaseInitAlreadyExists:
    """File already exists -> error."""

    def test_errors_if_file_exists(self, tmp_path, monkeypatch):
        # Create the file first
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
        )

        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        with pytest.raises(SystemExit):
            _run_release_init(tmp_path, entries, monkeypatch)


class TestReleaseInitEmptyFileAllowed:
    """Empty or whitespace-only file does not block release-init."""

    def test_empty_file_allows_overwrite(self, tmp_path, monkeypatch):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text("")

        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        assert release_path.exists()
        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["pypi"]

    def test_whitespace_only_file_allows_overwrite(self, tmp_path, monkeypatch):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text("   \n\n  \n")

        entries = [TargetEntry(name="npm", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["npm"]

    def test_nonempty_file_still_blocks(self, tmp_path, monkeypatch):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text('bump = "minor"\n')

        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        with pytest.raises(SystemExit):
            _run_release_init(tmp_path, entries, monkeypatch)


class TestReleaseInitRoundTrip:
    """Verify the generated file structure and that it requires editing before use."""

    def test_scaffolded_file_has_correct_structure(self, tmp_path, monkeypatch):
        entries = [TargetEntry(name="cargo", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert isinstance(data["bump"], str)
        assert isinstance(data["include"], list)
        assert isinstance(data["exclude"], list)

    def test_scaffolded_file_rejected_without_editing(self, tmp_path, monkeypatch):
        """read_release_file rejects the scaffolded file because bump is empty."""
        from rlsbl.release_file import read_release_file

        entries = [TargetEntry(name="cargo", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        with pytest.raises(ReleaseFileError, match="bump must be set"):
            read_release_file(str(release_path))

    def test_roundtrip_with_flutter(self, tmp_path, monkeypatch):
        entries = [
            TargetEntry(name="flutter", path=str(tmp_path)),
        ]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert data["bump"] == ""
        assert list(data["include"]) == ["flutter"]
        assert list(data["exclude"]) == []
        assert "flutter" in data["targets"]
        assert data["targets"]["flutter"]["mode"] == "build"


class TestReleaseInitDescriptionContext:
    """Scaffolded file includes description and context fields."""

    def test_scaffolded_file_has_description_and_context(self, tmp_path, monkeypatch):
        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        data = _read_scaffolded_toml(str(release_path))
        assert data["description"] == ""
        assert data["context"] == ""

    def test_scaffolded_file_has_description_comment(self, tmp_path, monkeypatch):
        entries = [TargetEntry(name="npm", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        raw = release_path.read_text()
        assert "# Short description of this release" in raw
        assert "# Optional context" in raw


class TestReleaseInitPrintsPath:
    """The command prints the path of the created file."""

    def test_prints_path(self, tmp_path, monkeypatch, capsys):
        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        captured = capsys.readouterr()
        assert "unreleased.toml" in captured.out
        assert ".rlsbl/releases/unreleased.toml" in captured.out


class TestReleaseInitNoTargets:
    """No targets detected -> error."""

    def test_errors_with_no_targets(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            _run_release_init(tmp_path, [], monkeypatch)


class TestReleaseInitAutoCommit:
    """release init auto-commits the scaffolded file."""

    def _init_git_repo(self, path):
        """Initialize a git repo with an initial commit."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
        readme = path / "README.md"
        readme.write_text("# test\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(path), check=True)

    def _is_tracked(self, repo_path, file_path):
        """Check if a file is tracked (committed) in git."""
        result = subprocess.run(
            ["git", "ls-files", str(file_path)],
            cwd=str(repo_path),
            capture_output=True, text=True,
        )
        return bool(result.stdout.strip())

    def test_standalone_auto_commits(self, tmp_path, monkeypatch):
        """After release init, the scaffolded file is committed to git."""
        self._init_git_repo(tmp_path)

        entries = [TargetEntry(name="pypi", path=str(tmp_path))]
        _run_release_init(tmp_path, entries, monkeypatch)

        release_path = tmp_path / ".rlsbl" / "releases" / "unreleased.toml"
        assert release_path.exists()

        # The file must be tracked (committed), not just on disk
        assert self._is_tracked(tmp_path, release_path), \
            "release init should auto-commit the scaffolded file"

        # Working tree should be clean for this file
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", str(release_path)],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert status.stdout.strip() == "", \
            f"release file should be clean after auto-commit, got: {status.stdout}"


class TestReleaseInitMonorepo:
    """In monorepo mode, release-init creates the file in the package directory."""

    def test_creates_file_in_package_dir(self, tmp_path, monkeypatch):
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

        monkeypatch.chdir(str(pkg_dir))
        with patch("rlsbl.targets.detect_targets", return_value=entries):
            from rlsbl import cmd_release_init
            cmd_release_init()

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
