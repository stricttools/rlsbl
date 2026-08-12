"""Tests for rlsbl.commands.discover.

The command reaches GitHub exclusively through ``gh api``: gh resolves and
applies the credential inside its own process, so no raw token ever transits
an rlsbl pipe (see :mod:`rlsbl.observe_allowlist` for the standard that
forbids it). These tests pin that shape -- the GET-pinned argv, gh's own
pagination, and the TSV projection the listing parses.
"""

import subprocess
from types import SimpleNamespace

import pytest

from rlsbl.commands.discover import (
    MAX_RESULTS,
    SEARCH_PATH,
    _fetch_all_repos,
    _gh_api,
    _relative_time,
    run_cmd,
)


def _tsv(*rows):
    return "".join("\t".join(row) + "\n" for row in rows)


class TestRelativeTime:
    """Tests for _relative_time."""

    def test_returns_empty_string_for_empty_input(self):
        assert _relative_time("") == ""

    def test_returns_days_ago_for_recent_timestamps(self):
        from datetime import datetime, timezone, timedelta

        ts = datetime.now(timezone.utc) - timedelta(days=3)
        iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _relative_time(iso) == "3d ago"


class TestNoTokenEverTransitsAPipe:
    """The command must never ask gh to print the credential."""

    def test_no_call_site_asks_for_the_raw_token(self):
        import inspect

        import rlsbl.commands.discover as discover

        source = inspect.getsource(discover)
        assert '"auth", "token"' not in source, (
            "`gh auth token` puts a live credential on stdout; let gh apply "
            "it itself instead"
        )


class TestGhApiShape:

    def test_every_read_is_get_pinned(self, monkeypatch):
        """--method GET is what makes the argv match the observe prefix."""
        seen = {}

        def fake_gh(args, **kwargs):
            seen["args"] = list(args)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        monkeypatch.setattr("rlsbl.commands.discover.effects.gh", fake_gh)
        _gh_api([SEARCH_PATH], timeout=5)
        assert seen["args"][:3] == ["api", "--method", "GET"]

    def test_a_recorded_run_reads_as_no_answer(self, monkeypatch):
        """Under a preview whose earlier steps recorded a mutation, gh's
        answer is a carrier, not text -- the caller must not parse it."""
        import strictcli

        def fake_gh(args, **kwargs):
            return strictcli.Unsettled("«stale»", None, "probe", True)

        monkeypatch.setattr("rlsbl.commands.discover.effects.gh", fake_gh)
        assert _gh_api(["user"], timeout=5) is None

    def test_timeout_reads_as_no_answer(self, monkeypatch):
        def fake_gh(args, **kwargs):
            raise subprocess.TimeoutExpired(["gh"], 5)

        monkeypatch.setattr("rlsbl.commands.discover.effects.gh", fake_gh)
        assert _gh_api(["user"], timeout=5) is None


class TestFetchAllRepos:

    def test_delegates_pagination_to_gh(self, monkeypatch):
        seen = {}

        def fake(args, timeout):
            seen["args"] = list(args)
            return ""

        monkeypatch.setattr("rlsbl.commands.discover._gh_api", fake)
        _fetch_all_repos()
        assert "--paginate" in seen["args"]
        assert SEARCH_PATH in seen["args"]

    def test_parses_the_tsv_projection(self, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api",
            lambda args, timeout: _tsv(
                ("a/one", "first", "2025-01-01T00:00:00Z", "a"),
                ("b/two", "", "2025-01-02T00:00:00Z", "b"),
            ),
        )
        assert _fetch_all_repos() == [
            ("a/one", "first", "2025-01-01T00:00:00Z", "a"),
            ("b/two", "", "2025-01-02T00:00:00Z", "b"),
        ]

    def test_stops_at_max_results(self, monkeypatch):
        rows = _tsv(*[
            (f"u/r{i}", "", "2025-01-01T00:00:00Z", "u")
            for i in range(MAX_RESULTS + 10)
        ])
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api", lambda args, timeout: rows,
        )
        assert len(_fetch_all_repos()) == MAX_RESULTS


class TestRunCmd:

    def test_prints_no_repos_found_when_gh_returns_nothing(
        self, monkeypatch, capsys,
    ):
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda: [],
        )
        run_cmd(None, [], {})
        assert "No rlsbl-tagged repositories found" in capsys.readouterr().out

    def test_exits_with_remedy_when_gh_cannot_answer(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda: None,
        )
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {})
        assert exc_info.value.code == 1
        assert "gh auth login" in capsys.readouterr().err
