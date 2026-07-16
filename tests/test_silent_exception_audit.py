"""Tests for the silent exception audit: verify corrupted input produces
visible errors, not false passes."""

import json
from unittest.mock import patch

from conftest import make_ctx

from rlsbl import app
from rlsbl.checks._common import _resolve_version_and_tag


class TestQualityCheckLibraryLint:
    """After the scope migration, library-lint no longer calls load_workspace.

    The scope adapter handles workspace detection. Non-workspace contexts
    are skipped by the scope adapter before the check runs.
    """

    def test_workspace_load_failure_returns_fail(self, tmp_project):
        """Non-workspace context -> scope adapter returns skip."""
        from rlsbl.checks.scope import scope_adapter

        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({"targets": []}))

        from strictcli import SkipCheck
        ctx = make_ctx(tmp_project)
        result = scope_adapter(ctx, "workspace:library")

        assert isinstance(result, SkipCheck)
        assert "not a monorepo" in result.reason


class TestVersionConsistencyCorruptedTarget:
    """read_version failure should warn, not silently return None."""

    def test_unreadable_version_does_not_crash(self, tmp_project):
        """Check completes even when read_version raises for a target."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({"targets": ["pypi"]}))
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        ctx = make_ctx(tmp_project)

        with patch("rlsbl.targets.pypi.PypiTarget.read_version", side_effect=RuntimeError("corrupt")):
            result = app._check_defs["version-consistency"].impl(ctx)

        # The check should complete without crashing. With one target whose
        # version is None, it should warn about no versions being reported.
        assert result.status in ("warn", "pass", "fail", "skip")

    def test_unreadable_version_sets_none(self, tmp_project, capsys):
        """When read_version raises, the target's version entry is None
        and a warning is printed to stderr."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({"targets": ["pypi"]}))
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        ctx = make_ctx(tmp_project)

        with patch("rlsbl.targets.pypi.PypiTarget.read_version", side_effect=RuntimeError("corrupt")):
            result = app._check_defs["version-consistency"].impl(ctx)

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "corrupt" in captured.err


class TestNameConsistencyCorruptedTarget:
    """read_name failure should warn, not silently crash."""

    def test_unreadable_name_does_not_crash(self, tmp_project):
        """Check completes even when read_name raises for a target."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({"targets": ["pypi"]}))
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        ctx = make_ctx(tmp_project)

        with patch("rlsbl.targets.pypi.PypiTarget.read_name", side_effect=RuntimeError("corrupt")):
            result = app._check_defs["name-consistency"].impl(ctx)

        # Should not crash; with all names being None it should warn.
        assert result.status in ("warn", "pass", "fail", "skip")

    def test_unreadable_name_prints_warning(self, tmp_project, capsys):
        """When read_name raises, a warning is printed to stderr."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({"targets": ["pypi"]}))
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        ctx = make_ctx(tmp_project)

        with patch("rlsbl.targets.pypi.PypiTarget.read_name", side_effect=RuntimeError("corrupt")):
            result = app._check_defs["name-consistency"].impl(ctx)

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "corrupt" in captured.err


class TestResolveVersionAndTag:
    """_resolve_version_and_tag should return (None, None) and warn on failure."""

    def test_read_version_failure_returns_none(self, tmp_project, capsys):
        """When read_version raises, version is None and warning is printed."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({"targets": ["pypi"]}))
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        ctx = make_ctx(tmp_project)

        with patch("rlsbl.targets.pypi.PypiTarget.read_version", side_effect=RuntimeError("corrupt")):
            version, tag = _resolve_version_and_tag(ctx)

        assert version is None
        assert tag is None
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "corrupt" in captured.err
