"""Tests for the consolidated tag->version parser in rlsbl.tag_glob.

``parse_version_tag`` replaces seven divergent ad-hoc regexes across four
files. These tests define its contract in both modes and pin the accept/reject
behavior each former call site relied on for realistic tags.
"""

import pytest

from rlsbl.tag_glob import TagMode, TagVersion, parse_version_tag


FINAL = TagMode.FINAL_ONLY
PRE = TagMode.PRERELEASE_INCLUSIVE


class TestSchemes:
    """All three tag schemes are recognized and reported."""

    def test_standalone(self):
        r = parse_version_tag("v1.2.3", mode=FINAL)
        assert r == TagVersion(version="1.2.3", scheme="standalone")

    def test_monorepo(self):
        r = parse_version_tag("mylib@v0.5.0", mode=FINAL)
        assert r == TagVersion(version="0.5.0", scheme="monorepo")

    def test_path_style(self):
        r = parse_version_tag("packages/core/v1.0.0", mode=FINAL)
        assert r == TagVersion(version="1.0.0", scheme="path")

    def test_go_cmd_path(self):
        r = parse_version_tag("cmd/tool/v1.2.3", mode=FINAL)
        assert r == TagVersion(version="1.2.3", scheme="path")

    def test_monorepo_name_with_slash_is_monorepo(self):
        # A name containing '/' but ending in '@v' is a monorepo tag, not path.
        r = parse_version_tag("group/pkg@v2.0.0", mode=FINAL)
        assert r == TagVersion(version="2.0.0", scheme="monorepo")


class TestFinalOnlyMode:
    """FINAL_ONLY rejects any prerelease suffix across all schemes."""

    @pytest.mark.parametrize("tag", [
        "v1.0.0-rc.1",
        "mylib@v1.2.3-rc.1",
        "v1.2.3-alpha",
        "v1.2.3-alpha.2",
        "www@v1.2.3-beta.1",
    ])
    def test_prerelease_rejected(self, tag):
        assert parse_version_tag(tag, mode=FINAL) is None

    @pytest.mark.parametrize("tag,version", [
        ("v1.2.3", "1.2.3"),
        ("v0.0.0", "0.0.0"),
        ("mylib@v0.5.0", "0.5.0"),
        ("packages/core/v1.0.0", "1.0.0"),
    ])
    def test_final_accepted(self, tag, version):
        r = parse_version_tag(tag, mode=FINAL)
        assert r is not None and r.version == version


class TestPrereleaseInclusiveMode:
    """PRERELEASE_INCLUSIVE accepts both final and prerelease tags."""

    @pytest.mark.parametrize("tag,version", [
        ("v1.2.3", "1.2.3"),
        ("v0.0.0", "0.0.0"),
        ("mylib@v0.5.0", "0.5.0"),
        ("packages/core/v1.0.0", "1.0.0"),
        ("v1.0.0-rc.1", "1.0.0-rc.1"),
        ("mylib@v1.2.3-rc.1", "1.2.3-rc.1"),
        ("v1.2.3-alpha.2", "1.2.3-alpha.2"),
        ("www@v1.2.3-beta.1", "1.2.3-beta.1"),
        # Prerelease without a numeric component: accepted (semver-valid).
        ("v1.2.3-alpha", "1.2.3-alpha"),
    ])
    def test_accepted(self, tag, version):
        r = parse_version_tag(tag, mode=PRE)
        assert r is not None and r.version == version


class TestStrictShapeRejections:
    """Non-version and malformed tags are rejected in both modes."""

    @pytest.mark.parametrize("tag", [
        "release-candidate",
        "latest",
        "milestone-3",
        "1.2.3",            # missing 'v' prefix
        "v1.2",             # partial version
        "v1.2.3+build",     # build metadata not supported
        "garbagev1.2.3",    # no scheme separator before 'v'
        "",
    ])
    @pytest.mark.parametrize("mode", [FINAL, PRE])
    def test_rejected(self, tag, mode):
        assert parse_version_tag(tag, mode=mode) is None


class TestModeIsRequired:
    """mode is keyword-only with no default and must be a TagMode."""

    def test_missing_mode_raises(self):
        with pytest.raises(TypeError):
            parse_version_tag("v1.2.3")  # type: ignore[call-arg]

    def test_invalid_mode_raises(self):
        with pytest.raises(TypeError):
            parse_version_tag("v1.2.3", mode="final")  # type: ignore[arg-type]
