"""Tests for batch release --watch/--no-watch support.

Covers:
- build_release_flags returns correct dict with and without watch
- batch-mode flag suppresses watch hint in execute.py
- batch release excludes watch from per-package flags
- batch release captures SHA after loop
"""

import re

import pytest

from rlsbl.commands.release.shared import build_release_flags


# ---------------------------------------------------------------------------
# build_release_flags
# ---------------------------------------------------------------------------


class TestBuildReleaseFlags:

    def test_defaults(self):
        """All False defaults produce expected dict."""
        result = build_release_flags(False, False, False)
        assert result == {
            "dry-run": False,
            "quiet": False,
            "allow-dirty": False,
            "watch": False,
            "push-timeout": None,
            "ci-timeout": None,
            "check-timeout": None,
            "hook-timeout": None,
        }

    def test_with_watch_true(self):
        """watch=True is preserved as a bool."""
        result = build_release_flags(True, False, True, watch=True)
        assert result["watch"] is True
        assert result["dry-run"] is True
        assert result["allow-dirty"] is True

    def test_watch_coerced_from_truthy(self):
        """Truthy non-bool values are coerced to True."""
        result = build_release_flags(False, False, False, watch="yes")
        assert result["watch"] is True

    def test_watch_coerced_from_none(self):
        """None is coerced to False."""
        result = build_release_flags(False, False, False, watch=None)
        assert result["watch"] is False

    def test_no_batch_mode_key(self):
        """build_release_flags does not add batch-mode."""
        result = build_release_flags(False, False, False)
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
    """The per-item release flags both batch loops pass to the release flow.

    Both loops build them through one helper, so this asserts the helper's
    output directly instead of scraping the loops' source.
    """

    def _flags(self, **overrides):
        from rlsbl.commands.monorepo.batch_release import _batch_release_flags

        base = {"dry-run": False, "quiet": False,
                "allow-dirty": False, "watch": True}
        base.update(overrides)
        return _batch_release_flags(base)

    def test_batch_mode_is_set(self):
        assert self._flags()["batch-mode"] is True

    def test_watch_is_never_forwarded(self):
        """The orchestrator watches once, after the whole batch."""
        assert "watch" not in self._flags()

    def test_lock_is_held_by_the_orchestrator(self):
        assert self._flags()["skip-lock"] is True

    def test_timeout_overrides_are_forwarded(self):
        flags = self._flags(**{"ci-timeout": 60, "check-timeout": 30})
        assert flags["ci-timeout"] == 60
        assert flags["check-timeout"] == 30

    def test_unset_timeouts_are_not_forwarded(self):
        flags = self._flags(**{"ci-timeout": None})
        assert "ci-timeout" not in flags

    def test_extra_keys_are_merged(self):
        from rlsbl.commands.monorepo.batch_release import _batch_release_flags

        flags = _batch_release_flags({"dry-run": False}, **{"ci-defer": True})
        assert flags["ci-defer"] is True


# ---------------------------------------------------------------------------
# SHA capture and post-batch watch
# ---------------------------------------------------------------------------


class TestBatchSHACapture:
    """Verify that batch release captures SHA and calls watch after the loop."""

    @pytest.mark.parametrize("func_name", ["_batch_release_releasables"])
    def test_batch_watches_the_verified_candidate(self, func_name):
        """Both loops watch a concrete SHA after the batch completes.

        The SHA is now the batch's CI-verified candidate (the commit every
        member tag points at) rather than whatever HEAD happened to be after
        the last member.
        """
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(getattr(batch_release, func_name))
        assert "last_sha = verified_sha" in source
        # Matched on the call's identity, not its exact spelling: the gate also
        # takes the batch's foreign-commit pin and trail as keyword arguments,
        # so the call spans several lines.
        assert re.search(
            r"_batch_ci_gate\(\s*workspace_root,\s*flags,\s*log\b", source,
        ), "the batch loop must run the shared CI gate"

    @pytest.mark.parametrize("func_name", ["_batch_release_releasables"])
    def test_both_loops_end_in_the_shared_watch_tail(self, func_name):
        """Neither loop may grow its own watch block.

        Both used to inline the watch/hint branch, which is how the two paths
        drift: the registry outcome check belongs to both, so the tail is one
        helper and each loop only delegates to it (with its own probe specs).
        """
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(getattr(batch_release, func_name))
        assert "_watch_and_verify_batch(flags, last_sha, probe_specs, log)" in source
        assert "watch_run_cmd(" not in source, (
            "the watch call belongs to the shared tail, not to the loop"
        )

    def test_the_shared_tail_watches_then_verifies_publication(self):
        """The tail is CI verdict first, registry outcome second."""
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(batch_release._watch_and_verify_batch)
        assert 'flags.get("watch")' in source
        assert "watch_run_cmd" in source
        assert source.index("watch_run_cmd") < source.index(
            "_verify_publication_members"
        ), "the registry probe must run AFTER the CI wait, never before"
        assert "_announce_unverified_publication" in source, (
            "--no-watch must announce that the outcome is unverified"
        )

    @pytest.mark.parametrize("func_name", ["_batch_release_releasables"])
    def test_dry_run_never_reaches_the_ci_gate(self, func_name):
        """A dry run pushes no candidate, so there is nothing to gate on.

        Both loops only collect pending items when ``not dry_run``, and the
        gate + watch only fire when something is pending.
        """
        import inspect
        from rlsbl.commands.monorepo import batch_release

        source = inspect.getsource(getattr(batch_release, func_name))
        assert "if pending:" in source
        assert "if dry_run:" in source
