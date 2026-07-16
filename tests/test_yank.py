"""Tests for rlsbl.commands.yank -- registry-aware removal with publication probing."""

import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.yank import run_cmd, _build_yank_notice
from rlsbl.publication_probe import PublicationProbeResult, PublicationStatus


MOD = "rlsbl.commands.yank"


class TestPublicationProbeResult:
    """Unit tests for the PublicationProbeResult data class."""

    def test_published_repr(self):
        r = PublicationProbeResult(
            PublicationStatus.PUBLISHED, "npm", "1.0.0", "found"
        )
        assert "PUBLISHED" in repr(r)
        assert r.status == PublicationStatus.PUBLISHED

    def test_unpublished(self):
        r = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "pypi", "0.5.0"
        )
        assert r.status == PublicationStatus.UNPUBLISHED
        assert r.message == ""

    def test_unprobeable(self):
        r = PublicationProbeResult(
            PublicationStatus.UNPROBEABLE, "plain", "1.0.0", "no API"
        )
        assert r.status == PublicationStatus.UNPROBEABLE


class TestPublicationProbeOnTargets:
    """Test publication_probe default and overrides on target classes."""

    def test_base_target_returns_unprobeable(self):
        from rlsbl.targets.base import BaseTarget
        t = BaseTarget()
        result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE

    def test_plain_target_returns_unprobeable(self):
        from rlsbl.targets.plain import PlainTarget
        t = PlainTarget()
        result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE

    @patch("rlsbl.targets.npm.NpmTarget.read_name", return_value=None)
    def test_npm_no_name_returns_unprobeable(self, _):
        from rlsbl.targets.npm import NpmTarget
        t = NpmTarget()
        result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE
        assert "no package name" in result.message

    @patch("rlsbl.targets.npm.NpmTarget.read_name", return_value="my-pkg")
    def test_npm_published(self, _):
        from rlsbl.targets.npm import NpmTarget
        import urllib.error

        t = NpmTarget()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"version": "1.0.0"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda s, *a: None

        with patch("rlsbl.commands.check._request_with_backoff", return_value=mock_resp):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.PUBLISHED

    @patch("rlsbl.targets.npm.NpmTarget.read_name", return_value="my-pkg")
    def test_npm_unpublished(self, _):
        from rlsbl.targets.npm import NpmTarget
        import urllib.error

        t = NpmTarget()
        with patch(
            "rlsbl.commands.check._request_with_backoff",
            side_effect=urllib.error.HTTPError(
                url="", code=404, msg="", hdrs=None, fp=None
            ),
        ):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPUBLISHED

    @patch("rlsbl.targets.npm.NpmTarget.read_name", return_value="my-pkg")
    def test_npm_api_error_returns_unprobeable(self, _):
        from rlsbl.targets.npm import NpmTarget
        import urllib.error

        t = NpmTarget()
        with patch(
            "rlsbl.commands.check._request_with_backoff",
            side_effect=urllib.error.HTTPError(
                url="", code=500, msg="", hdrs=None, fp=None
            ),
        ):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE

    @patch("rlsbl.targets.npm.NpmTarget.read_name", return_value="my-pkg")
    def test_npm_network_error_returns_unprobeable(self, _):
        from rlsbl.targets.npm import NpmTarget

        t = NpmTarget()
        with patch(
            "rlsbl.commands.check._request_with_backoff",
            side_effect=ConnectionError("timeout"),
        ):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE

    @patch("rlsbl.targets.pypi.PypiTarget.read_name", return_value="my-pkg")
    def test_pypi_published(self, _):
        from rlsbl.targets.pypi import PypiTarget

        t = PypiTarget()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"info": {"version": "1.0.0"}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda s, *a: None

        with patch("rlsbl.commands.check._request_with_backoff", return_value=mock_resp):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.PUBLISHED

    @patch("rlsbl.targets.pypi.PypiTarget.read_name", return_value="my-pkg")
    def test_pypi_unpublished(self, _):
        from rlsbl.targets.pypi import PypiTarget
        import urllib.error

        t = PypiTarget()
        with patch(
            "rlsbl.commands.check._request_with_backoff",
            side_effect=urllib.error.HTTPError(
                url="", code=404, msg="", hdrs=None, fp=None
            ),
        ):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPUBLISHED

    @patch("rlsbl.targets.pypi.PypiTarget.read_name", return_value=None)
    def test_pypi_no_name_returns_unprobeable(self, _):
        from rlsbl.targets.pypi import PypiTarget
        t = PypiTarget()
        result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE

    def test_go_published(self):
        from rlsbl.targets.go import GoTarget

        t = GoTarget()
        mock_result = MagicMock(
            returncode=0,
            stdout="abc123\trefs/tags/v1.0.0\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.PUBLISHED

    def test_go_unpublished(self):
        from rlsbl.targets.go import GoTarget

        t = GoTarget()
        mock_result = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPUBLISHED

    def test_go_no_module_returns_unprobeable(self):
        from rlsbl.targets.go import GoTarget
        t = GoTarget()
        result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE

    @patch("rlsbl.targets.cargo.CargoTarget.read_name", return_value="my-crate")
    def test_cargo_published(self, _):
        from rlsbl.targets.cargo import CargoTarget

        t = CargoTarget()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"version": {"num": "1.0.0"}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda s, *a: None

        with patch("rlsbl.commands.check._request_with_backoff", return_value=mock_resp):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.PUBLISHED

    @patch("rlsbl.targets.cargo.CargoTarget.read_name", return_value="my-crate")
    def test_cargo_unpublished(self, _):
        from rlsbl.targets.cargo import CargoTarget
        import urllib.error

        t = CargoTarget()
        with patch(
            "rlsbl.commands.check._request_with_backoff",
            side_effect=urllib.error.HTTPError(
                url="", code=404, msg="", hdrs=None, fp=None
            ),
        ):
            result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPUBLISHED

    @patch("rlsbl.targets.cargo.CargoTarget.read_name", return_value=None)
    def test_cargo_no_name_returns_unprobeable(self, _):
        from rlsbl.targets.cargo import CargoTarget
        t = CargoTarget()
        result = t.publication_probe("/fake", "1.0.0")
        assert result.status == PublicationStatus.UNPROBEABLE


class TestCapabilityConsistency:
    """Verify that targets declaring publication_probe capability have a working probe."""

    def test_probe_capable_targets_return_non_default(self):
        """Targets with publication_probe in capabilities must override the method."""
        from rlsbl.targets import TARGETS
        from rlsbl.targets.base import BaseTarget

        for name, target in TARGETS.items():
            if "publication_probe" in target.capabilities:
                # The method must be overridden (not the BaseTarget default)
                assert type(target).publication_probe is not BaseTarget.publication_probe, \
                    f"target '{name}' declares publication_probe capability but uses the default BaseTarget implementation"

    def test_non_probe_targets_use_default(self):
        """Targets without publication_probe capability should use the default."""
        from rlsbl.targets import TARGETS
        from rlsbl.targets.base import BaseTarget

        for name, target in TARGETS.items():
            if "publication_probe" not in target.capabilities:
                result = target.publication_probe("/fake", "1.0.0")
                assert result.status == PublicationStatus.UNPROBEABLE, \
                    f"target '{name}' lacks publication_probe capability but returned {result.status}"


class TestYankCommand:
    """Test the yank command's registry-aware removal flow."""

    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.run_gh")
    @patch(f"{MOD}.find_workspace_root", return_value=None)
    @patch(f"{MOD}.resolve_member_context")
    def test_yank_published_npm_dry_run(self, mock_member, _ws, mock_gh, _inst, _auth):
        """Dry run shows what would be yanked for a published npm package."""
        from rlsbl.targets import TargetEntry

        target = MagicMock()
        target.name = "npm"
        target.capabilities = frozenset({"publication_probe"})
        target.tag_format.return_value = "v1.0.0"
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.PUBLISHED, "npm", "1.0.0", "my-pkg@1.0.0 found"
        )
        target.read_name.return_value = "my-pkg"

        mock_member.return_value = MagicMock(targets=[TargetEntry("npm", ".")])

        mock_gh.side_effect = [
            "",         # release view
            "v2.0.0",  # release list (latest)
            "body",    # release view body
        ]

        with patch(f"rlsbl.commands.yank.TARGETS", {"npm": target}), \
             patch("sys.stdout", new_callable=StringIO) as out:
            run_cmd(["1.0.0"], {"dry-run": True, "yes": True}, project_root=".")

        output = out.getvalue()
        assert "npm" in output
        assert "my-pkg@1.0.0" in output
        assert "dry run" in output.lower() or "Dry run" in output

    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.run_gh")
    @patch(f"{MOD}.find_workspace_root", return_value=None)
    @patch(f"{MOD}.resolve_member_context")
    def test_yank_unprobeable_target_errors(self, mock_member, _ws, mock_gh, _inst, _auth):
        """Yank with an unprobeable target exits with error."""
        from rlsbl.targets import TargetEntry

        target = MagicMock()
        target.name = "plain"
        target.capabilities = frozenset()
        target.tag_format.return_value = "v1.0.0"

        mock_member.return_value = MagicMock(targets=[TargetEntry("plain", ".")])

        mock_gh.side_effect = [
            "",         # release view
            "v2.0.0",  # release list (latest)
        ]

        with patch(f"rlsbl.commands.yank.TARGETS", {"plain": target}), \
             patch("sys.stderr", new_callable=StringIO) as err:
            with pytest.raises(SystemExit) as exc:
                run_cmd(["1.0.0"], {"yes": True}, project_root=".")
        assert exc.value.code == 1
        assert "cannot determine publication status" in err.getvalue()

    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.run_gh")
    @patch(f"{MOD}.find_workspace_root", return_value=None)
    @patch(f"{MOD}.resolve_member_context")
    def test_yank_unpublished_skips(self, mock_member, _ws, mock_gh, _inst, _auth):
        """Unpublished targets are skipped gracefully."""
        from rlsbl.targets import TargetEntry

        target = MagicMock()
        target.name = "npm"
        target.capabilities = frozenset({"publication_probe"})
        target.tag_format.return_value = "v1.0.0"
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "npm", "1.0.0", "not found"
        )

        mock_member.return_value = MagicMock(targets=[TargetEntry("npm", ".")])

        mock_gh.side_effect = [
            "",         # release view
            "v2.0.0",  # release list (latest)
            "body",    # release view body
            "",         # release edit
        ]

        with patch(f"rlsbl.commands.yank.TARGETS", {"npm": target}), \
             patch("os.path.exists", return_value=True), \
             patch("os.unlink"), \
             patch("os.rename"), \
             patch("builtins.open", unittest.mock.mock_open()), \
             patch("sys.stdout", new_callable=StringIO) as out:
            run_cmd(["1.0.0"], {"yes": True}, project_root=".")

        output = out.getvalue()
        assert "skipping" in output.lower()

    def test_no_version_arg(self):
        with pytest.raises(SystemExit) as exc:
            run_cmd([], {}, project_root=Path("/fake"))
        assert exc.value.code == 1

    @patch(f"{MOD}.find_workspace_root", return_value=None)
    @patch(f"{MOD}.resolve_member_context", return_value=MagicMock(targets=[]))
    @patch(f"{MOD}.check_gh_installed", return_value=True)
    @patch(f"{MOD}.check_gh_auth", return_value=True)
    @patch(f"{MOD}.run_gh")
    def test_latest_release_blocked(self, mock_gh, *_):
        mock_gh.side_effect = [
            "",         # release view
            "v1.0.0",  # latest IS our target
        ]
        with pytest.raises(SystemExit) as exc:
            run_cmd(["1.0.0"], {"yes": True}, project_root=Path("/fake"))
        assert exc.value.code == 1


class TestBuildYankNotice:
    """Unit tests for the yank notice builder."""

    def test_no_reason_no_use(self):
        result = _build_yank_notice(None, None)
        assert result == "> **Yanked.**"

    def test_reason_only(self):
        result = _build_yank_notice("security issue", None)
        assert result == "> **Yanked:** security issue."

    def test_use_only(self):
        result = _build_yank_notice(None, "0.9.2")
        assert result == "> **Yanked:** Use v0.9.2 instead."

    def test_reason_and_use(self):
        result = _build_yank_notice("security issue", "0.9.2")
        assert result == "> **Yanked:** security issue. Use v0.9.2 instead."

    def test_use_with_v_prefix(self):
        result = _build_yank_notice(None, "v0.9.2")
        assert result == "> **Yanked:** Use v0.9.2 instead."


class TestCmdReleaseYankDelegation:
    """Verify the CLI handler delegates correctly to the new yank module."""

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.yank.run_cmd")
    def test_delegates(self, mock_run, _):
        import rlsbl
        rlsbl.cmd_release_yank(
            reason="security", use="1.2.4",
            dry_run=True, yes=True, version="1.2.3",
        )
        mock_run.assert_called_once()
        flags = mock_run.call_args[0][1]
        assert flags["reason"] == "security"
        assert flags["use"] == "1.2.4"
        # No 'hard' key
        assert "hard" not in flags


if __name__ == "__main__":
    unittest.main()
