"""Tests for rlsbl.release_file."""

import os
import stat

import pytest

from rlsbl.errors import ReleaseFileError
from rlsbl.release_file import (
    ReleaseConfig,
    VALID_BUMP_TYPES,
    VALID_PREIDS,
    get_release_file_path,
    read_release_file,
    unfinalize_release_file,
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
        with pytest.raises(ReleaseFileError, match="bump"):
            read_release_file(str(f))

    def test_invalid_bump_value(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "huge"\ninclude = []\nexclude = []\n')
        with pytest.raises(ReleaseFileError, match="bump"):
            read_release_file(str(f))

    def test_missing_include(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\nexclude = []\n')
        with pytest.raises(ReleaseFileError, match="include"):
            read_release_file(str(f))

    def test_missing_exclude(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\n')
        with pytest.raises(ReleaseFileError, match="exclude"):
            read_release_file(str(f))

    def test_include_not_list_of_strings(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = [1, 2]\nexclude = []\n')
        with pytest.raises(ReleaseFileError, match="include"):
            read_release_file(str(f))

    def test_exclude_not_list_of_strings(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = [1]\n')
        with pytest.raises(ReleaseFileError, match="exclude"):
            read_release_file(str(f))

    def test_target_in_both_include_and_exclude(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = ["pypi"]\nexclude = ["pypi"]\n')
        with pytest.raises(ReleaseFileError, match="both include and exclude"):
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
        with pytest.raises(ReleaseFileError, match="not in include"):
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
        with pytest.raises(ReleaseFileError, match="not in include"):
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
        with pytest.raises(ReleaseFileError, match="invalid mode"):
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
        with pytest.raises(ReleaseFileError, match="unknown field"):
            read_release_file(str(f))

    def test_description_not_string(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\ndescription = 42\n')
        with pytest.raises(ReleaseFileError, match="description must be a string"):
            read_release_file(str(f))

    def test_context_not_string(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\ndescription = "test release"\ncontext = true\n')
        with pytest.raises(ReleaseFileError, match="context must be a string"):
            read_release_file(str(f))


class TestReadReleaseFileDescriptionContext:
    """Tests for description and context fields in release files."""

    def test_description_required(self, tmp_path):
        """Omitting description raises ReleaseFileError."""
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\n')
        with pytest.raises(ReleaseFileError, match="description"):
            read_release_file(str(f))

    def test_empty_description_rejected(self, tmp_path):
        """An empty description string is rejected."""
        f = tmp_path / "release.toml"
        f.write_text('bump = "patch"\ninclude = []\nexclude = []\ndescription = ""\n')
        with pytest.raises(ReleaseFileError, match="description"):
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


class TestUnfinalizeReleaseFile:
    """Tests for unfinalize_release_file (inverse of release-file finalization).

    Release finalization renames unreleased.toml to vX.Y.Z.toml, chmods it
    read-only (0o444), and creates a fresh empty unreleased.toml. These tests
    simulate that post-release state directly on disk.
    """

    CONTENT = (
        'bump = "minor"\n'
        'include = ["pypi"]\n'
        'exclude = []\n'
        'description = "my release"\n'
        'context = "why these changes were made"\n'
    )

    def _finalized_state(self, tmp_path, version="1.2.3"):
        """Create the on-disk state left behind by a release finalization."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        versioned = releases_dir / f"v{version}.toml"
        versioned.write_text(self.CONTENT)
        os.chmod(str(versioned), 0o444)
        unreleased = releases_dir / "unreleased.toml"
        unreleased.write_text("")  # finalization writes an empty file
        return releases_dir, versioned, unreleased

    def test_restores_finalized_release_file(self, tmp_path):
        releases_dir, versioned, unreleased = self._finalized_state(tmp_path)

        changed = unfinalize_release_file(str(releases_dir), "1.2.3")

        # unreleased.toml is back with the original content and is writable
        assert unreleased.read_text() == self.CONTENT
        assert os.stat(str(unreleased)).st_mode & stat.S_IWUSR
        # the versioned file is gone
        assert not versioned.exists()
        # both changed paths are reported (for committing)
        assert set(changed) == {str(unreleased), str(versioned)}

    def test_preserves_user_modified_unreleased(self, tmp_path, capsys):
        """If unreleased.toml has user content that differs from the
        finalized file, it must not be deleted; warn and skip instead."""
        releases_dir, versioned, unreleased = self._finalized_state(tmp_path)
        user_content = 'bump = "patch"\ndescription = "new work in progress"\n'
        unreleased.write_text(user_content)

        changed = unfinalize_release_file(str(releases_dir), "1.2.3")

        assert changed == []
        # user content untouched
        assert unreleased.read_text() == user_content
        # versioned file left in place (and still read-only)
        assert versioned.exists()
        assert versioned.read_text() == self.CONTENT
        assert not (os.stat(str(versioned)).st_mode & stat.S_IWUSR)
        # a warning was reported
        assert "warning" in capsys.readouterr().err.lower()

    def test_restores_when_unreleased_identical_to_versioned(self, tmp_path):
        """unreleased.toml identical to the versioned file carries no user
        information; restoring loses nothing."""
        releases_dir, versioned, unreleased = self._finalized_state(tmp_path)
        unreleased.write_text(self.CONTENT)

        changed = unfinalize_release_file(str(releases_dir), "1.2.3")

        assert unreleased.read_text() == self.CONTENT
        assert not versioned.exists()
        assert set(changed) == {str(unreleased), str(versioned)}

    def test_restores_when_fresh_unreleased_missing(self, tmp_path):
        """Restoration proceeds even if the fresh unreleased.toml was removed."""
        releases_dir, versioned, unreleased = self._finalized_state(tmp_path)
        os.unlink(str(unreleased))

        changed = unfinalize_release_file(str(releases_dir), "1.2.3")

        assert unreleased.read_text() == self.CONTENT
        assert not versioned.exists()
        assert set(changed) == {str(unreleased), str(versioned)}

    def test_noop_when_versioned_file_missing(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "unreleased.toml").write_text("")

        assert unfinalize_release_file(str(releases_dir), "9.9.9") == []


class TestValidBumpTypesIncludesPrerelease:
    def test_prerelease_in_valid_bump_types(self):
        assert "prerelease" in VALID_BUMP_TYPES

    def test_valid_preids_tuple(self):
        assert VALID_PREIDS == ("alpha", "beta", "rc", "stable")


class TestReleaseFilePreid:
    """Tests for preid field in release file validation."""

    BASE = 'bump = "{bump}"\ninclude = []\nexclude = []\ndescription = "test"\n'

    def test_preid_defaults_empty(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="patch"))
        cfg = read_release_file(str(f))
        assert cfg.preid == ""

    def test_preid_alpha(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="minor") + 'preid = "alpha"\n')
        cfg = read_release_file(str(f))
        assert cfg.preid == "alpha"

    def test_preid_beta(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="minor") + 'preid = "beta"\n')
        cfg = read_release_file(str(f))
        assert cfg.preid == "beta"

    def test_preid_rc(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="minor") + 'preid = "rc"\n')
        cfg = read_release_file(str(f))
        assert cfg.preid == "rc"

    def test_preid_stable_with_prerelease_bump(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="prerelease") + 'preid = "stable"\n')
        cfg = read_release_file(str(f))
        assert cfg.preid == "stable"
        assert cfg.bump == "prerelease"

    def test_prerelease_bump_without_preid(self, tmp_path):
        """prerelease bump with no preid is valid (increments current preid counter)."""
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="prerelease"))
        cfg = read_release_file(str(f))
        assert cfg.bump == "prerelease"
        assert cfg.preid == ""

    def test_invalid_preid_rejected(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="minor") + 'preid = "gamma"\n')
        with pytest.raises(ReleaseFileError, match="invalid preid"):
            read_release_file(str(f))

    def test_preid_not_string_rejected(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="minor") + "preid = 42\n")
        with pytest.raises(ReleaseFileError, match="preid must be a string"):
            read_release_file(str(f))

    def test_preid_with_hotfix_rejected(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="hotfix") + 'preid = "alpha"\n')
        with pytest.raises(ReleaseFileError, match="hotfix releases cannot"):
            read_release_file(str(f))

    def test_preid_stable_with_non_prerelease_bump_rejected(self, tmp_path):
        """preid='stable' only makes sense with bump='prerelease'."""
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="minor") + 'preid = "stable"\n')
        with pytest.raises(ReleaseFileError, match='preid "stable" is only valid'):
            read_release_file(str(f))

    def test_preid_stable_with_patch_rejected(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="patch") + 'preid = "stable"\n')
        with pytest.raises(ReleaseFileError, match='preid "stable" is only valid'):
            read_release_file(str(f))

    def test_preid_stable_with_major_rejected(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="major") + 'preid = "stable"\n')
        with pytest.raises(ReleaseFileError, match='preid "stable" is only valid'):
            read_release_file(str(f))

    def test_preid_stripped(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="minor") + 'preid = "  alpha  "\n')
        cfg = read_release_file(str(f))
        assert cfg.preid == "alpha"

    def test_empty_preid_string_ok(self, tmp_path):
        """Explicitly setting preid = '' is the same as omitting it."""
        f = tmp_path / "release.toml"
        f.write_text(self.BASE.format(bump="patch") + 'preid = ""\n')
        cfg = read_release_file(str(f))
        assert cfg.preid == ""
