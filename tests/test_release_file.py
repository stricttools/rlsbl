"""Tests for rlsbl.release_file."""

import os

import pytest

from rlsbl.release_file import (
    ReleaseConfig,
    VALID_BUMP_TYPES,
    get_release_file_path,
    read_release_file,
)


class TestGetReleaseFilePath:
    def test_default_project_dir(self):
        path = get_release_file_path()
        assert path == os.path.join(".", ".rlsbl", "releases", "unreleased.toml")

    def test_custom_project_dir(self):
        path = get_release_file_path("/some/project")
        assert path == "/some/project/.rlsbl/releases/unreleased.toml"


class TestReadReleaseFileValid:
    def test_minimal_file(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = ["pypi"]\nexclude = ["npm"]\ndescription = "test release"\n')
        cfg = read_release_file(str(f))
        assert cfg.bump == "patch"
        assert cfg.include == ["pypi"]
        assert cfg.exclude == ["npm"]
        assert cfg.targets == {}

    def test_empty_include_and_exclude(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "minor"\ninclude = []\nexclude = []\ndescription = "test release"\n')
        cfg = read_release_file(str(f))
        assert cfg.bump == "minor"
        assert cfg.include == []
        assert cfg.exclude == []
        assert cfg.targets == {}

    def test_with_targets_section(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "minor"\n'
            'include = ["flutter"]\n'
            'exclude = ["npm"]\n'
            'description = "test release"\n'
            "\n"
            "[targets.flutter]\n"
            'mode = "ota"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.bump == "minor"
        assert cfg.include == ["flutter"]
        assert cfg.exclude == ["npm"]
        assert cfg.targets == {
            "flutter": {"mode": "ota"},
        }

    def test_returns_dataclass(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "major"\ninclude = []\nexclude = []\ndescription = "test release"\n')
        cfg = read_release_file(str(f))
        assert isinstance(cfg, ReleaseConfig)

    def test_all_bump_types(self, tmp_path):
        for bump in VALID_BUMP_TYPES:
            f = tmp_path / f"release_{bump}.toml"
            f.write_text(f'bump = "{bump}"\ninclude = []\nexclude = []\ndescription = "test release"\n')
            cfg = read_release_file(str(f))
            assert cfg.bump == bump


class TestReadReleaseFileErrors:
    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_release_file(str(tmp_path / "nonexistent.toml"))

    def test_missing_bump(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('include = ["pypi"]\nexclude = []\n')
        with pytest.raises(ValueError, match="bump"):
            read_release_file(str(f))

    def test_invalid_bump_value(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "huge"\ninclude = []\nexclude = []\n')
        with pytest.raises(ValueError, match="bump"):
            read_release_file(str(f))

    def test_missing_include(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\nexclude = []\n')
        with pytest.raises(ValueError, match="include"):
            read_release_file(str(f))

    def test_missing_exclude(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\n')
        with pytest.raises(ValueError, match="exclude"):
            read_release_file(str(f))

    def test_include_not_list_of_strings(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = [1, 2]\nexclude = []\n')
        with pytest.raises(ValueError, match="include"):
            read_release_file(str(f))

    def test_exclude_not_list_of_strings(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = [1]\n')
        with pytest.raises(ValueError, match="exclude"):
            read_release_file(str(f))

    def test_target_in_both_include_and_exclude(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = ["pypi"]\nexclude = ["pypi"]\n')
        with pytest.raises(ValueError, match="both include and exclude"):
            read_release_file(str(f))

    def test_target_config_for_excluded_target(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["pypi"]\n'
            'exclude = ["npm"]\n'
            "\n"
            "[targets.npm]\n"
            'mode = "ota"\n'
        )
        with pytest.raises(ValueError, match="not in include"):
            read_release_file(str(f))

    def test_target_config_for_unlisted_target(self, tmp_path):
        """A target in [targets] that isn't in include at all."""
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
            "\n"
            "[targets.flutter]\n"
            'mode = "ota"\n'
        )
        with pytest.raises(ValueError, match="not in include"):
            read_release_file(str(f))

    def test_invalid_target_mode(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["flutter"]\n'
            'exclude = []\n'
            "\n"
            "[targets.flutter]\n"
            'mode = "deploy"\n'
        )
        with pytest.raises(ValueError, match="invalid mode"):
            read_release_file(str(f))

    def test_unknown_target_field(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["flutter"]\n'
            'exclude = []\n'
            "\n"
            "[targets.flutter]\n"
            'flavor = "production"\n'
        )
        with pytest.raises(ValueError, match="unknown field"):
            read_release_file(str(f))

    def test_description_not_string(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\ndescription = 42\n')
        with pytest.raises(ValueError, match="description must be a string"):
            read_release_file(str(f))

    def test_context_not_string(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\ndescription = "test release"\ncontext = true\n')
        with pytest.raises(ValueError, match="context must be a string"):
            read_release_file(str(f))


class TestReadReleaseFileDescriptionContext:
    """Tests for description and context fields in release files."""

    def test_description_required(self, tmp_path):
        """Omitting description raises ValueError."""
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\n')
        with pytest.raises(ValueError, match="description"):
            read_release_file(str(f))

    def test_empty_description_rejected(self, tmp_path):
        """An empty description string is rejected."""
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\ndescription = ""\n')
        with pytest.raises(ValueError, match="description"):
            read_release_file(str(f))

    def test_context_defaults_empty(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\ndescription = "test release"\n')
        cfg = read_release_file(str(f))
        assert cfg.context == ""

    def test_description_read(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "Fix critical startup crash"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.description == "Fix critical startup crash"

    def test_context_read(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "test release"\n'
            'context = "Users reported crash on iOS 17"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.context == "Users reported crash on iOS 17"

    def test_description_and_context_together(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "minor"\ninclude = ["pypi"]\nexclude = []\n'
            'description = "Add widget API"\n'
            'context = "Required for dashboard v2"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.description == "Add widget API"
        assert cfg.context == "Required for dashboard v2"

    def test_description_stripped(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "  whitespace around  "\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.description == "whitespace around"

    def test_context_stripped(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\ninclude = []\nexclude = []\n'
            'description = "test release"\n'
            'context = "  padded context  "\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.context == "padded context"
