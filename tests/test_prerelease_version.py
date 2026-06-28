"""Tests for pre-release version support in bump_version."""

import pytest

from rlsbl.errors import VersionError
from rlsbl.utils import bump_version


class TestStandardBumpWithPreid:
    """Standard bumps (patch/minor/major) with a preid produce a pre-release version."""

    @pytest.mark.parametrize("version, bump_type, preid, expected", [
        # minor + alpha
        ("0.42.0", "minor", "alpha", "0.43.0-alpha.0"),
        ("1.0.0", "minor", "alpha", "1.1.0-alpha.0"),
        # patch + beta
        ("0.42.0", "patch", "beta", "0.42.1-beta.0"),
        ("3.2.1", "patch", "beta", "3.2.2-beta.0"),
        # major + rc
        ("0.42.0", "major", "rc", "1.0.0-rc.0"),
        ("2.5.3", "major", "rc", "3.0.0-rc.0"),
        # minor + beta
        ("1.2.3", "minor", "beta", "1.3.0-beta.0"),
        # patch + alpha
        ("0.1.0", "patch", "alpha", "0.1.1-alpha.0"),
        # major + alpha
        ("0.9.9", "major", "alpha", "1.0.0-alpha.0"),
    ])
    def test_standard_bump_with_preid(self, version, bump_type, preid, expected):
        assert bump_version(version, bump_type, preid=preid) == expected

    def test_standard_bump_with_preid_on_prerelease_version(self):
        """When bumping a pre-release version with a preid, the suffix is
        stripped first, then the bump is applied with the new preid."""
        assert bump_version("1.0.0-beta.1", "minor", preid="alpha") == "1.1.0-alpha.0"
        assert bump_version("2.3.0-rc.2", "patch", preid="beta") == "2.3.1-beta.0"


class TestPrereleaseIncrement:
    """bump_type='prerelease' without preid (or matching preid) increments the counter."""

    def test_alpha_increment(self):
        assert bump_version("0.43.0-alpha.0", "prerelease") == "0.43.0-alpha.1"

    def test_alpha_increment_higher(self):
        assert bump_version("0.43.0-alpha.5", "prerelease") == "0.43.0-alpha.6"

    def test_beta_increment(self):
        assert bump_version("1.0.0-beta.0", "prerelease") == "1.0.0-beta.1"

    def test_rc_increment(self):
        assert bump_version("2.1.0-rc.3", "prerelease") == "2.1.0-rc.4"

    def test_matching_preid_increments(self):
        """Explicitly passing the same preid also increments."""
        assert bump_version("0.43.0-alpha.0", "prerelease", preid="alpha") == "0.43.0-alpha.1"
        assert bump_version("1.0.0-beta.2", "prerelease", preid="beta") == "1.0.0-beta.3"


class TestPrereleasePromotion:
    """bump_type='prerelease' with a higher preid promotes to that preid."""

    def test_alpha_to_beta(self):
        assert bump_version("0.43.0-alpha.3", "prerelease", preid="beta") == "0.43.0-beta.0"

    def test_alpha_to_rc(self):
        assert bump_version("0.43.0-alpha.1", "prerelease", preid="rc") == "0.43.0-rc.0"

    def test_beta_to_rc(self):
        assert bump_version("1.0.0-beta.5", "prerelease", preid="rc") == "1.0.0-rc.0"


class TestPrereleaseToStable:
    """bump_type='prerelease' with preid='stable' strips the suffix."""

    def test_alpha_to_stable(self):
        assert bump_version("0.43.0-alpha.3", "prerelease", preid="stable") == "0.43.0"

    def test_beta_to_stable(self):
        assert bump_version("1.0.0-beta.0", "prerelease", preid="stable") == "1.0.0"

    def test_rc_to_stable(self):
        assert bump_version("2.1.0-rc.7", "prerelease", preid="stable") == "2.1.0"


class TestPrereleaseDemotionError:
    """Demoting a pre-release (higher -> lower preid) is an error."""

    def test_beta_to_alpha(self):
        with pytest.raises(VersionError, match="Cannot demote"):
            bump_version("0.43.0-beta.0", "prerelease", preid="alpha")

    def test_rc_to_alpha(self):
        with pytest.raises(VersionError, match="Cannot demote"):
            bump_version("1.0.0-rc.1", "prerelease", preid="alpha")

    def test_rc_to_beta(self):
        with pytest.raises(VersionError, match="Cannot demote"):
            bump_version("1.0.0-rc.1", "prerelease", preid="beta")

    def test_same_preid_is_not_demotion(self):
        """Same preid is an increment, not a demotion -- should not error."""
        result = bump_version("0.43.0-beta.2", "prerelease", preid="beta")
        assert result == "0.43.0-beta.3"


class TestPrereleaseOnStableVersionError:
    """bump_type='prerelease' on a version without a pre-release suffix is an error."""

    def test_stable_version(self):
        with pytest.raises(VersionError, match="no pre-release suffix"):
            bump_version("1.0.0", "prerelease")

    def test_stable_zero_version(self):
        with pytest.raises(VersionError, match="no pre-release suffix"):
            bump_version("0.42.0", "prerelease")

    def test_stable_version_with_preid(self):
        with pytest.raises(VersionError, match="no pre-release suffix"):
            bump_version("1.0.0", "prerelease", preid="alpha")


class TestHotfixWithPreidError:
    """Hotfix with preid is a hard error."""

    def test_hotfix_with_alpha(self):
        with pytest.raises(VersionError, match="hotfix releases cannot be pre-releases"):
            bump_version("1.0.0", "hotfix", preid="alpha")

    def test_hotfix_with_beta(self):
        with pytest.raises(VersionError, match="hotfix releases cannot be pre-releases"):
            bump_version("1.0.0", "hotfix", preid="beta")

    def test_hotfix_with_rc(self):
        with pytest.raises(VersionError, match="hotfix releases cannot be pre-releases"):
            bump_version("0.5.0", "hotfix", preid="rc")

    def test_hotfix_without_preid_still_works(self):
        """Hotfix without preid should still work as before."""
        assert bump_version("1.2.3", "hotfix") == "1.2.4"


class TestStandardBumpWithoutPreid:
    """Existing behavior: standard bumps without preid produce clean versions."""

    @pytest.mark.parametrize("version, bump_type, expected", [
        ("1.2.3", "patch", "1.2.4"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "major", "2.0.0"),
        ("0.1.0", "patch", "0.1.1"),
        ("0.1.0", "minor", "0.2.0"),
        ("0.1.0", "major", "1.0.0"),
        ("3.2.1", "patch", "3.2.2"),
        ("3.2.1", "minor", "3.3.0"),
        ("3.2.1", "major", "4.0.0"),
    ])
    def test_standard_bump_no_preid(self, version, bump_type, expected):
        assert bump_version(version, bump_type) == expected


class TestStandardBumpOnPrereleaseWithoutPreid:
    """Existing behavior: standard bumps on pre-release versions strip the suffix."""

    @pytest.mark.parametrize("version, bump_type, expected", [
        ("1.0.0-beta.1", "patch", "1.0.1"),
        ("1.0.0-beta.1", "minor", "1.1.0"),
        ("1.0.0-beta.1", "major", "2.0.0"),
        ("2.3.0-rc.2", "patch", "2.3.1"),
        ("2.3.0-rc.2", "minor", "2.4.0"),
        ("2.3.0-rc.2", "major", "3.0.0"),
        ("0.5.0-alpha.3", "patch", "0.5.1"),
    ])
    def test_strip_suffix_and_bump(self, version, bump_type, expected):
        assert bump_version(version, bump_type) == expected


class TestUnknownPreid:
    """Unknown preid values produce errors."""

    def test_unknown_preid_with_standard_bump(self):
        with pytest.raises(VersionError, match="Unknown preid"):
            bump_version("1.0.0", "minor", preid="gamma")

    def test_unknown_preid_with_prerelease_bump(self):
        with pytest.raises(VersionError, match="Unknown preid"):
            bump_version("1.0.0-alpha.0", "prerelease", preid="gamma")
