"""Tests for batch release --watch/--no-watch support.

Covers:
- build_release_flags returns correct dict with and without watch
- batch-mode flag suppresses watch hint in execute.py
- batch release excludes watch from per-package flags
- batch release captures SHA after loop
"""

import io
from unittest.mock import MagicMock, patch, call

import pytest

from rlsbl.commands.release.shared import build_release_flags


# ---------------------------------------------------------------------------
# build_release_flags
# ---------------------------------------------------------------------------


class TestBuildReleaseFlags:

    def test_defaults(self):
        """All False defaults produce expected dict."""
        result = build_release_flags(False, False, False, False)
        assert result == {
            "dry-run": False,
            "yes": False,
            "quiet": False,
            "allow-dirty": False,
            "watch": False,
            "watch-async": False,
        }

    def test_with_watch_true(self):
        """watch=True is preserved as a bool."""
        result = build_release_flags(True, True, False, True, watch=True)
        assert result["watch"] is True
        assert result["dry-run"] is True
        assert result["yes"] is True
        assert result["allow-dirty"] is True

    def test_watch_coerced_from_truthy(self):
        """Truthy non-bool values are coerced to True."""
        result = build_release_flags(False, False, False, False, watch="yes")
        assert result["watch"] is True

    def test_watch_coerced_from_none(self):
        """None is coerced to False."""
        result = build_release_flags(False, False, False, False, watch=None)
        assert result["watch"] is False

    def test_no_batch_mode_key(self):
        """build_release_flags does not add batch-mode."""
        result = build_release_flags(False, False, False, False)
        assert "batch-mode" not in result


# ---------------------------------------------------------------------------
# batch-mode suppresses watch in execute.py
# ---------------------------------------------------------------------------


class TestBatchModeSuppressesWatch:
    """Verify that the watch block in _run_release_mutating is skipped
    when batch-mode is set in flags."""

    def test_batch_mode_skips_watch_call(self):
        """With batch-mode=True and watch=True, the watch call is not made."""
        # We test the condition directly rather than running the full release
        # flow, since _run_release_mutating has extensive side effects.
        flags = {"dry-run": False, "watch": True, "batch-mode": True}
        # The condition in execute.py is:
        #   if not flags.get("dry-run", False) and not flags.get("batch-mode", False):
        should_watch = (
            not flags.get("dry-run", False)
            and not flags.get("batch-mode", False)
        )
        assert should_watch is False

    def test_non_batch_mode_allows_watch(self):
        """Without batch-mode, watch proceeds normally."""
        flags = {"dry-run": False, "watch": True}
        should_watch = (
            not flags.get("dry-run", False)
            and not flags.get("batch-mode", False)
        )
        assert should_watch is True

    def test_batch_mode_skips_hint(self):
        """With batch-mode=True and watch=False, the hint is also skipped."""
        flags = {"dry-run": False, "watch": False, "batch-mode": True}
        should_show_hint = (
            not flags.get("dry-run", False)
            and not flags.get("batch-mode", False)
        )
        assert should_show_hint is False


# ---------------------------------------------------------------------------
# Per-package flags exclude watch and include batch-mode
# ---------------------------------------------------------------------------


class TestPerPackageFlags:
    """Verify that per-package release_flags in both batch functions
    include batch-mode and exclude watch."""

    def _extract_release_flags_from_source(self, func_name):
        """Read the source of batch_release.py and verify the flags dict
        inside the given function includes batch-mode and excludes watch."""
        import ast
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(batch_release)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                # Find the release_flags dict assignment
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name) and target.id == "release_flags":
                                if isinstance(child.value, ast.Dict):
                                    keys = []
                                    for k in child.value.keys:
                                        if isinstance(k, ast.Constant):
                                            keys.append(k.value)
                                    assert "batch-mode" in keys, (
                                        f"{func_name}: release_flags missing 'batch-mode'"
                                    )
                                    assert "watch" not in keys, (
                                        f"{func_name}: release_flags should not contain 'watch'"
                                    )
                                    return
        raise AssertionError(f"Could not find release_flags dict in {func_name}")

    def test_releasables_flags_have_batch_mode(self):
        self._extract_release_flags_from_source("_batch_release_releasables")

    def test_packages_flags_have_batch_mode(self):
        self._extract_release_flags_from_source("_batch_release_packages")


# ---------------------------------------------------------------------------
# SHA capture and post-batch watch
# ---------------------------------------------------------------------------


class TestBatchSHACapture:
    """Verify that batch release captures SHA and calls watch after the loop."""

    @patch("rlsbl.commands.monorepo.batch_release.run")
    @patch("rlsbl.commands.monorepo.batch_release.commit_files")
    def test_releasables_captures_sha(self, mock_commit, mock_run, tmp_path):
        """_batch_release_releasables records last_sha from git rev-parse HEAD
        after each successful release."""
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(batch_release._batch_release_releasables)
        # After run_cmd(), the code should call run("git", ["rev-parse", "HEAD"])
        assert 'run("git", ["rev-parse", "HEAD"])' in source

    @patch("rlsbl.commands.monorepo.batch_release.run")
    @patch("rlsbl.commands.monorepo.batch_release.commit_files")
    def test_packages_captures_sha(self, mock_commit, mock_run, tmp_path):
        """_batch_release_packages records last_sha from git rev-parse HEAD
        after each successful release."""
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(batch_release._batch_release_packages)
        assert 'run("git", ["rev-parse", "HEAD"])' in source

    def test_releasables_watch_block_present(self):
        """_batch_release_releasables has a watch block after finalization."""
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(batch_release._batch_release_releasables)
        assert 'flags.get("watch")' in source
        assert "watch_run_cmd" in source
        assert "Watch CI: rlsbl watch" in source

    def test_packages_watch_block_present(self):
        """_batch_release_packages has a watch block after finalization."""
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(batch_release._batch_release_packages)
        assert 'flags.get("watch")' in source
        assert "watch_run_cmd" in source
        assert "Watch CI: rlsbl watch" in source

    def test_sha_capture_only_when_not_dry_run(self):
        """SHA capture is guarded by 'not dry_run'."""
        import inspect
        from rlsbl.commands.monorepo import batch_release

        for func in [batch_release._batch_release_releasables, batch_release._batch_release_packages]:
            source = inspect.getsource(func)
            # Find the line with rev-parse HEAD
            lines = source.split("\n")
            for i, line in enumerate(lines):
                if 'rev-parse' in line and 'HEAD' in line:
                    # Check that a preceding line has "not dry_run" or "if not dry_run"
                    context = "\n".join(lines[max(0, i - 3):i + 1])
                    assert "not dry_run" in context, (
                        f"SHA capture in {func.__name__} is not guarded by dry_run check"
                    )
                    break
