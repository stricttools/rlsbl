"""Tests for the fleet sweep script scripts/sweep_dead_coverage_unit.py.

The sweep's summary is the only thing an operator reads. It has to be honest:
a run that swept nothing because every candidate repo was dirty must NOT look
like a run that found nothing to sweep.
"""

import importlib.util
from pathlib import Path

import pytest


def _load_sweep():
    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "sweep_dead_coverage_unit.py")
    spec = importlib.util.spec_from_file_location("sweep_dead_coverage_unit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sweep():
    return _load_sweep()


def _text(lines):
    return "\n".join(lines)


class TestSummaryHonesty:
    def test_nothing_at_all_says_nothing_sweepable(self, sweep):
        out = _text(sweep.summarize([], [], [], dry_run=True))
        assert "would sweep: 0 repo(s)" in out
        assert "nothing sweepable" in out
        assert "NOT SWEPT" not in out

    def test_zero_swept_but_dirty_skips_is_not_reported_as_clean(self, sweep):
        skipped = [("/p/alpha", [".rlsbl/config.json"], ["src/x.py"])]
        out = _text(sweep.summarize([], skipped, [], dry_run=True))

        # The misleading "0 repo(s)" headline is still there (it is true), but
        # it can no longer be read as "the key is gone".
        assert "nothing sweepable" not in out
        assert "NOT SWEPT" in out
        assert "1 repo(s) skipped (dirty tree)" in out
        assert sweep.KEY in out
        # The skipped repos are named, with what blocked them.
        assert "/p/alpha" in out
        assert ".rlsbl/config.json" in out
        assert "src/x.py" in out
        assert "not gone yet" in out

    def test_swept_with_no_skips_declares_the_key_gone(self, sweep):
        swept = [("/p/alpha", [".rlsbl/config.json"])]
        out = _text(sweep.summarize(swept, [], [], dry_run=False))
        assert "swept: 1 repo(s)" in out
        assert f"no repo left carrying {sweep.KEY}" in out
        assert "NOT SWEPT" not in out

    def test_swept_and_skipped_reports_both(self, sweep):
        swept = [("/p/alpha", [".rlsbl/config.json"])]
        skipped = [("/p/beta", [".rlsbl/config.json"], ["README.md"])]
        out = _text(sweep.summarize(swept, skipped, [], dry_run=False))
        assert "swept: 1 repo(s)" in out
        assert "/p/alpha" in out
        assert "NOT SWEPT" in out
        assert "/p/beta" in out
        assert "no repo left carrying" not in out

    def test_failures_are_listed(self, sweep):
        failed = [("/p/gamma", [".rlsbl/config.json"], "rlsbl commit failed")]
        out = _text(sweep.summarize([], [], failed, dry_run=False))
        assert "FAILED: 1 repo(s)" in out
        assert "/p/gamma" in out
        assert "rlsbl commit failed" in out

    def test_foreign_change_list_is_truncated_to_five(self, sweep):
        foreign = [f"f{i}.py" for i in range(9)]
        skipped = [("/p/alpha", [".rlsbl/config.json"], foreign)]
        out = _text(sweep.summarize([], skipped, [], dry_run=True))
        assert "f4.py" in out
        assert "f5.py" not in out
