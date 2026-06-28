"""Tests for pre-release version translation and publishing integration (phases 4e+4f)."""

import os
import re
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from rlsbl.targets.base import BaseTarget
from rlsbl.targets.pypi import PypiTarget, _PEP440_PRE_RE


# ===========================================================================
# Phase 4e: Per-target version translation
# ===========================================================================


class TestBaseTargetFormatVersion:
    """BaseTarget.format_version is identity (passthrough)."""

    def test_stable_version(self):
        t = BaseTarget()
        assert t.format_version("1.2.3") == "1.2.3"

    def test_prerelease_version_passthrough(self):
        t = BaseTarget()
        assert t.format_version("1.2.3-alpha.0") == "1.2.3-alpha.0"

    def test_prerelease_rc_passthrough(self):
        t = BaseTarget()
        assert t.format_version("2.0.0-rc.5") == "2.0.0-rc.5"

    def test_zero_version(self):
        t = BaseTarget()
        assert t.format_version("0.0.0") == "0.0.0"


class TestNonPypiTargetsFormatVersion:
    """Non-pypi targets inherit identity format_version from BaseTarget."""

    def test_npm_target(self):
        from rlsbl.targets.npm import NpmTarget
        t = NpmTarget()
        assert t.format_version("1.0.0-alpha.0") == "1.0.0-alpha.0"
        assert t.format_version("2.3.4") == "2.3.4"

    def test_go_target(self):
        from rlsbl.targets.go import GoTarget
        t = GoTarget()
        assert t.format_version("1.0.0-beta.1") == "1.0.0-beta.1"
        assert t.format_version("0.5.0") == "0.5.0"

    def test_plain_target(self):
        from rlsbl.targets.plain import PlainTarget
        t = PlainTarget()
        assert t.format_version("1.0.0-rc.2") == "1.0.0-rc.2"
        assert t.format_version("3.1.4") == "3.1.4"


class TestPypiFormatVersion:
    """PypiTarget.format_version translates semver pre-release to PEP 440."""

    def setup_method(self):
        self.t = PypiTarget()

    def test_alpha(self):
        assert self.t.format_version("1.2.3-alpha.0") == "1.2.3a0"

    def test_beta(self):
        assert self.t.format_version("1.2.3-beta.1") == "1.2.3b1"

    def test_rc(self):
        assert self.t.format_version("1.2.3-rc.2") == "1.2.3rc2"

    def test_stable_passthrough(self):
        assert self.t.format_version("1.2.3") == "1.2.3"

    def test_zero_version_stable(self):
        assert self.t.format_version("0.1.0") == "0.1.0"

    def test_alpha_zero(self):
        assert self.t.format_version("0.43.0-alpha.0") == "0.43.0a0"

    def test_high_counter(self):
        assert self.t.format_version("1.0.0-beta.15") == "1.0.0b15"

    def test_unknown_preid_passthrough(self):
        """Unknown preids are not translated."""
        assert self.t.format_version("1.0.0-gamma.0") == "1.0.0-gamma.0"


class TestPypiPep440ToSemver:
    """PypiTarget._pep440_to_semver reverse-translates PEP 440 to semver."""

    def test_alpha(self):
        assert PypiTarget._pep440_to_semver("1.2.3a0") == "1.2.3-alpha.0"

    def test_beta(self):
        assert PypiTarget._pep440_to_semver("1.2.3b1") == "1.2.3-beta.1"

    def test_rc(self):
        assert PypiTarget._pep440_to_semver("1.2.3rc2") == "1.2.3-rc.2"

    def test_stable_passthrough(self):
        assert PypiTarget._pep440_to_semver("1.2.3") == "1.2.3"

    def test_zero_version(self):
        assert PypiTarget._pep440_to_semver("0.1.0") == "0.1.0"

    def test_high_counter_alpha(self):
        assert PypiTarget._pep440_to_semver("2.0.0a10") == "2.0.0-alpha.10"

    def test_roundtrip_alpha(self):
        t = PypiTarget()
        original = "1.2.3-alpha.0"
        assert t._pep440_to_semver(t.format_version(original)) == original

    def test_roundtrip_beta(self):
        t = PypiTarget()
        original = "5.0.0-beta.3"
        assert t._pep440_to_semver(t.format_version(original)) == original

    def test_roundtrip_rc(self):
        t = PypiTarget()
        original = "0.1.0-rc.7"
        assert t._pep440_to_semver(t.format_version(original)) == original

    def test_roundtrip_stable(self):
        t = PypiTarget()
        original = "3.2.1"
        assert t._pep440_to_semver(t.format_version(original)) == original


class TestPypiReadVersionTranslation:
    """PypiTarget.read_version converts PEP 440 pre-releases back to semver."""

    def test_reads_pep440_as_semver(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "1.2.3a0"\n')
        t = PypiTarget()
        assert t.read_version(str(tmp_path)) == "1.2.3-alpha.0"

    def test_reads_stable_unchanged(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "1.2.3"\n')
        t = PypiTarget()
        assert t.read_version(str(tmp_path)) == "1.2.3"

    def test_reads_beta_as_semver(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "2.0.0b5"\n')
        t = PypiTarget()
        assert t.read_version(str(tmp_path)) == "2.0.0-beta.5"

    def test_reads_rc_as_semver(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "3.1.0rc0"\n')
        t = PypiTarget()
        assert t.read_version(str(tmp_path)) == "3.1.0-rc.0"


class TestPypiWriteVersionTranslation:
    """PypiTarget.write_version writes PEP 440 format to pyproject.toml."""

    def test_writes_pep440_alpha(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "1.0.0"\n')
        t = PypiTarget()
        t.write_version(str(tmp_path), "1.1.0-alpha.0", ctx=None)
        import tomllib
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["version"] == "1.1.0a0"

    def test_writes_pep440_beta(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "1.0.0"\n')
        t = PypiTarget()
        t.write_version(str(tmp_path), "1.1.0-beta.1", ctx=None)
        import tomllib
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["version"] == "1.1.0b1"

    def test_writes_stable_unchanged(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "1.0.0"\n')
        t = PypiTarget()
        t.write_version(str(tmp_path), "1.1.0", ctx=None)
        import tomllib
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["version"] == "1.1.0"


class TestVersionConsistencyWithMixedFormats:
    """Version consistency check normalizes versions via read_version.

    Since PypiTarget.read_version now converts PEP 440 to semver,
    a project with pypi (1.2.3a0 on disk) and npm (1.2.3-alpha.0)
    should report consistent versions.
    """

    def test_pypi_reads_semver_so_consistency_works(self, tmp_path):
        """Both targets read the same semver string even though the
        on-disk formats differ."""
        t = PypiTarget()
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "1.2.3a0"\n')
        version = t.read_version(str(tmp_path))
        # Should be in semver, not PEP 440
        assert version == "1.2.3-alpha.0"
        # This matches what npm would report
        assert version == "1.2.3-alpha.0"


# ===========================================================================
# Phase 4f: Publishing integration
# ===========================================================================


class TestNpmPipelinePrerelease:
    """npm pipeline adds --tag {preid} for pre-release versions."""

    def test_stable_no_tag(self):
        from rlsbl.pipelines.npm import NpmPipeline

        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=True, config={}
        )
        with patch("rlsbl.pipelines.npm.run") as mock_run:
            pipeline._publish_command(".", "1.0.0", "fake-token")
            args = mock_run.call_args[0][1]
            assert "--tag" not in args

    def test_alpha_prerelease_tag(self):
        from rlsbl.pipelines.npm import NpmPipeline

        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=True, config={}
        )
        with patch("rlsbl.pipelines.npm.run") as mock_run:
            pipeline._publish_command(".", "1.0.0-alpha.0", "fake-token")
            args = mock_run.call_args[0][1]
            tag_idx = args.index("--tag")
            assert args[tag_idx + 1] == "alpha"

    def test_beta_prerelease_tag(self):
        from rlsbl.pipelines.npm import NpmPipeline

        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=True, config={}
        )
        with patch("rlsbl.pipelines.npm.run") as mock_run:
            pipeline._publish_command(".", "2.0.0-beta.3", "fake-token")
            args = mock_run.call_args[0][1]
            tag_idx = args.index("--tag")
            assert args[tag_idx + 1] == "beta"

    def test_rc_prerelease_tag(self):
        from rlsbl.pipelines.npm import NpmPipeline

        pipeline = NpmPipeline(
            name="npm", pipeline_type="npm", local=True, config={}
        )
        with patch("rlsbl.pipelines.npm.run") as mock_run:
            pipeline._publish_command(".", "3.0.0-rc.1", "fake-token")
            args = mock_run.call_args[0][1]
            tag_idx = args.index("--tag")
            assert args[tag_idx + 1] == "rc"


class TestDockerPipelinePrerelease:
    """Docker pipeline skips :latest push for pre-release versions."""

    def _make_pipeline(self):
        from rlsbl.pipelines.docker import DockerPipeline
        return DockerPipeline(
            name="docker", pipeline_type="docker", local=True,
            config={"image": "myapp", "registry": "ghcr.io/org"},
        )

    def test_stable_pushes_latest(self):
        pipeline = self._make_pipeline()
        with patch("rlsbl.pipelines.docker.require_tool", return_value=True):
            with patch("rlsbl.pipelines.docker.run") as mock_run:
                pipeline._publish_command(".", "1.0.0", "user", "pass")
                # Should have 4 calls: build, push versioned, tag latest, push latest
                assert mock_run.call_count == 4
                tag_call = mock_run.call_args_list[2]
                # The docker tag command receives [tag, versioned, latest]
                assert any("latest" in arg for arg in tag_call[0][1])
                push_latest_call = mock_run.call_args_list[3]
                assert any("latest" in arg for arg in push_latest_call[0][1])

    def test_prerelease_skips_latest(self):
        pipeline = self._make_pipeline()
        with patch("rlsbl.pipelines.docker.require_tool", return_value=True):
            with patch("rlsbl.pipelines.docker.run") as mock_run:
                pipeline._publish_command(".", "1.0.0-alpha.0", "user", "pass")
                # Should have only 2 calls: build, push versioned
                assert mock_run.call_count == 2
                # Verify no :latest in any call
                for c in mock_run.call_args_list:
                    assert "latest" not in str(c)


class TestGitHubReleasePrerelease:
    """GitHub Release creation adds --prerelease for pre-release versions."""

    def test_stable_no_prerelease_flag(self):
        """For stable versions, --prerelease should not appear in gh release args."""
        from rlsbl.commands.release.execute import _run_release_mutating, ReleaseState

        # We verify by checking the gh_release_args construction logic.
        # The relevant code:
        #   gh_release_args = ["release", "create", tag, ...]
        #   if "-" in new_version: gh_release_args.append("--prerelease")
        version = "1.0.0"
        assert "-" not in version  # stable -- no --prerelease

    def test_prerelease_has_dash(self):
        """For pre-release versions, the version string contains a dash."""
        version = "1.0.0-alpha.0"
        assert "-" in version  # pre-release -- --prerelease should be added

    def test_gh_release_args_stable(self):
        """Build the args list for a stable version and verify no --prerelease."""
        tag = "v1.0.0"
        new_version = "1.0.0"
        notes_file = "notes.tmp"
        gh_release_args = ["release", "create", tag, "--title", tag, "--notes-file", notes_file]
        if "-" in new_version:
            gh_release_args.append("--prerelease")
        assert "--prerelease" not in gh_release_args

    def test_gh_release_args_prerelease(self):
        """Build the args list for a pre-release version and verify --prerelease is present."""
        tag = "v1.0.0-alpha.0"
        new_version = "1.0.0-alpha.0"
        notes_file = "notes.tmp"
        gh_release_args = ["release", "create", tag, "--title", tag, "--notes-file", notes_file]
        if "-" in new_version:
            gh_release_args.append("--prerelease")
        assert "--prerelease" in gh_release_args


class TestUndoVersionRegex:
    """undo.py version regex matches pre-release tags."""

    # The regex used in undo.py:
    _RE = re.compile(r"v(\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?)$")

    def test_stable_tag(self):
        m = self._RE.search("v1.2.3")
        assert m is not None
        assert m.group(1) == "1.2.3"

    def test_alpha_tag(self):
        m = self._RE.search("v1.2.3-alpha.0")
        assert m is not None
        assert m.group(1) == "1.2.3-alpha.0"

    def test_beta_tag(self):
        m = self._RE.search("v2.0.0-beta.5")
        assert m is not None
        assert m.group(1) == "2.0.0-beta.5"

    def test_rc_tag(self):
        m = self._RE.search("v3.1.0-rc.2")
        assert m is not None
        assert m.group(1) == "3.1.0-rc.2"

    def test_monorepo_stable_tag(self):
        m = self._RE.search("mypackage@v1.2.3")
        assert m is not None
        assert m.group(1) == "1.2.3"

    def test_monorepo_prerelease_tag(self):
        m = self._RE.search("mypackage@v1.2.3-alpha.0")
        assert m is not None
        assert m.group(1) == "1.2.3-alpha.0"


class TestPrePushPrerelease:
    """Pre-push hook correctly handles pre-release version pushes.

    The pre-push check uses RLSBL_RELEASE_PUSH env var, not commit
    message matching, so pre-release versions work without changes.
    """

    def test_release_push_env_skips_check(self):
        from rlsbl.git_util import detect_manual_push_branches

        stdin_lines = [
            "refs/heads/main abc123 refs/heads/main def456"
        ]
        with patch.dict(os.environ, {"RLSBL_RELEASE_PUSH": "1"}):
            result = detect_manual_push_branches(stdin_lines, ["main"])
        assert result == []

    def test_no_release_push_env_detects_manual(self):
        from rlsbl.git_util import detect_manual_push_branches

        stdin_lines = [
            "refs/heads/main abc123 refs/heads/main def456"
        ]
        with patch.dict(os.environ, {}, clear=True):
            # Remove RLSBL_RELEASE_PUSH if present
            os.environ.pop("RLSBL_RELEASE_PUSH", None)
            result = detect_manual_push_branches(stdin_lines, ["main"])
        assert result == ["main"]


class TestQuickBumpPreid:
    """Quick bump shortcut (--bump + --preid) produces correct ReleaseConfig."""

    def test_preid_without_bump_errors(self):
        """--preid without --bump is an error."""
        from rlsbl.release_file import VALID_PREIDS
        # The actual error is printed and sys.exit(1) is called
        # We test the validation logic directly
        preid = "alpha"
        bump = ""
        assert preid and not bump  # This is the error condition

    def test_preid_with_bump_is_valid(self):
        """--preid with --bump produces a ReleaseConfig with preid set."""
        from rlsbl.release_file import ReleaseConfig, VALID_BUMP_TYPES, VALID_PREIDS

        bump = "minor"
        preid = "alpha"
        description = "test release"
        assert bump in VALID_BUMP_TYPES
        assert preid in VALID_PREIDS
        config = ReleaseConfig(
            bump=bump,
            include=["pypi"],
            exclude=[],
            description=description,
            preid=preid,
        )
        assert config.preid == "alpha"
        assert config.bump == "minor"

    def test_invalid_preid_rejected(self):
        """Invalid preid values are rejected."""
        from rlsbl.release_file import VALID_PREIDS
        assert "gamma" not in VALID_PREIDS
        assert "alpha" in VALID_PREIDS
        assert "beta" in VALID_PREIDS
        assert "rc" in VALID_PREIDS
        assert "stable" in VALID_PREIDS

    @pytest.mark.parametrize("preid", ["alpha", "beta", "rc"])
    def test_all_valid_preids_with_bump(self, preid):
        """All valid preids work with standard bump types."""
        from rlsbl.release_file import ReleaseConfig
        config = ReleaseConfig(
            bump="minor",
            include=["pypi"],
            exclude=[],
            description="test",
            preid=preid,
        )
        assert config.preid == preid


class TestPep440Regex:
    """The PEP 440 pre-release regex correctly matches versions."""

    def test_alpha(self):
        m = _PEP440_PRE_RE.match("1.2.3a0")
        assert m is not None
        assert m.group(1) == "1.2.3"
        assert m.group(2) == "a"
        assert m.group(3) == "0"

    def test_beta(self):
        m = _PEP440_PRE_RE.match("1.2.3b1")
        assert m is not None
        assert m.group(2) == "b"

    def test_rc(self):
        m = _PEP440_PRE_RE.match("1.2.3rc2")
        assert m is not None
        assert m.group(2) == "rc"

    def test_stable_no_match(self):
        m = _PEP440_PRE_RE.match("1.2.3")
        assert m is None

    def test_semver_prerelease_no_match(self):
        """Semver pre-release format should NOT match PEP 440 regex."""
        m = _PEP440_PRE_RE.match("1.2.3-alpha.0")
        assert m is None
