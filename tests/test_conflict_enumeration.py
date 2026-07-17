"""Tests for multi-conflict enumeration in check-name and claim-name.

Verifies that _check_single_name stores the FULL conflict list plus
the matched normalization rule on the result dict for all registries
(npm, pypi, crates), and that both check-name and claim-name render
every conflicting package and the rule in their text output.
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.check import (
    _check_single_name,
    _enumerate_conflicts,
    _format_single_result,
)


# ---------------------------------------------------------------------------
# _check_single_name: result dict stores full conflicts + rule
# ---------------------------------------------------------------------------

class TestCheckSingleNameConflictStorage:
    """_check_single_name must store conflicts list + conflict_rule on result."""

    @patch("rlsbl.commands.check._search_npm_similar", return_value=[])
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_multi_conflict_stored(self, mock_avail, mock_variants, mock_search):
        """npm: multiple hard collisions stored in conflicts + conflict_rule."""
        mock_avail.return_value = {"status": "available"}
        mock_variants.return_value = ["foo-bar", "foo.bar", "foo_bar"]

        result = _check_single_name("foobar", "npm")

        assert result["status"] == "taken"
        assert result["reason"] == "moniker"
        assert set(result["conflicts"]) == {"foo-bar", "foo.bar", "foo_bar"}
        assert result["conflict_rule"]  # non-empty rule string
        # note must list every conflict
        for pkg in ("foo-bar", "foo.bar", "foo_bar"):
            assert pkg in result["note"], f"'{pkg}' missing from note: {result['note']}"
        assert result["conflict_rule"] in result["note"]

    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_multi_conflict_stored(self, mock_avail, mock_variants):
        """pypi: multiple hard collisions stored in conflicts + conflict_rule."""
        mock_avail.return_value = {"status": "available"}
        # Simulate two variants that ultranormalize identically to the candidate
        mock_variants.return_value = ["foo-bar", "foo_bar"]

        with patch("rlsbl.commands.check._classify_variant_collisions",
                    return_value=(["foo-bar", "foo_bar"], [])):
            result = _check_single_name("foobar", "pypi")

        assert result["status"] == "taken"
        assert result["reason"] == "normalized"
        assert "conflicts" in result, "pypi result missing 'conflicts' key"
        assert set(result["conflicts"]) == {"foo-bar", "foo_bar"}
        assert "conflict_rule" in result, "pypi result missing 'conflict_rule' key"
        assert result["conflict_rule"]  # non-empty
        for pkg in ("foo-bar", "foo_bar"):
            assert pkg in result["note"], f"'{pkg}' missing from note: {result['note']}"
        assert result["conflict_rule"] in result["note"]

    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_crates_availability")
    def test_crates_multi_conflict_stored(self, mock_avail, mock_variants):
        """crates: multiple hard collisions stored in conflicts + conflict_rule."""
        mock_avail.return_value = {"status": "available"}
        mock_variants.return_value = ["foo-bar", "foo_bar"]

        with patch("rlsbl.commands.check._classify_variant_collisions",
                    return_value=(["foo-bar", "foo_bar"], [])):
            result = _check_single_name("foobar", "crates")

        assert result["status"] == "taken"
        assert result["reason"] == "normalized"
        assert "conflicts" in result, "crates result missing 'conflicts' key"
        assert set(result["conflicts"]) == {"foo-bar", "foo_bar"}
        assert "conflict_rule" in result, "crates result missing 'conflict_rule' key"
        assert result["conflict_rule"]  # non-empty
        for pkg in ("foo-bar", "foo_bar"):
            assert pkg in result["note"], f"'{pkg}' missing from note: {result['note']}"
        assert result["conflict_rule"] in result["note"]


# ---------------------------------------------------------------------------
# _format_single_result: text output enumerates every conflict + rule
# ---------------------------------------------------------------------------

class TestFormatSingleResultConflictEnumeration:
    """_format_single_result must render every conflict and the rule in output."""

    def test_npm_multi_conflict_rendered(self, capsys):
        rule = "npm strips dashes, dots, and underscores: these share one moniker"
        result = {
            "name": "foobar", "registry": "npm", "status": "taken",
            "variants": [], "reason": "moniker",
            "conflicts": ["foo-bar", "foo.bar", "foo_bar"],
            "conflict_rule": rule,
            "note": f"moniker collision with {_enumerate_conflicts(['foo-bar', 'foo.bar', 'foo_bar'])} — {rule}",
            "moniker_checked": True,
        }
        exit_code = _format_single_result(result)
        assert exit_code == 1
        out = capsys.readouterr().out
        for pkg in ("foo-bar", "foo.bar", "foo_bar"):
            assert pkg in out, f"'{pkg}' missing from check-name output"
        assert rule in out

    def test_pypi_multi_conflict_rendered(self, capsys):
        rule = "PyPI normalizes dashes, underscores, and dots to hyphens: these resolve identically"
        result = {
            "name": "foobar", "registry": "pypi", "status": "taken",
            "variants": [], "reason": "normalized",
            "conflicts": ["foo-bar", "foo_bar"],
            "conflict_rule": rule,
            "note": f"normalization collision with {_enumerate_conflicts(['foo-bar', 'foo_bar'])} — {rule}",
        }
        exit_code = _format_single_result(result)
        assert exit_code == 1
        out = capsys.readouterr().out
        for pkg in ("foo-bar", "foo_bar"):
            assert pkg in out, f"'{pkg}' missing from check-name output"
        assert rule in out

    def test_crates_multi_conflict_rendered(self, capsys):
        rule = "crates.io treats hyphens and underscores as equivalent"
        result = {
            "name": "foobar", "registry": "crates", "status": "taken",
            "variants": [], "reason": "normalized",
            "conflicts": ["foo-bar", "foo_bar"],
            "conflict_rule": rule,
            "note": f"normalization collision with {_enumerate_conflicts(['foo-bar', 'foo_bar'])} — {rule}",
        }
        exit_code = _format_single_result(result)
        assert exit_code == 1
        out = capsys.readouterr().out
        for pkg in ("foo-bar", "foo_bar"):
            assert pkg in out, f"'{pkg}' missing from check-name output"
        assert rule in out


# ---------------------------------------------------------------------------
# claim-name: text output enumerates every conflict + rule
# ---------------------------------------------------------------------------

class TestClaimNameConflictEnumeration:
    """claim-name stderr must show every conflict and the rule when taken."""

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_pypi_multi_conflict_rendered(self, mock_check, capsys):
        rule = "PyPI normalizes dashes, underscores, and dots to hyphens: these resolve identically"
        mock_check.return_value = {
            "name": "foobar", "registry": "pypi", "status": "taken",
            "variants": None, "reason": "normalized",
            "conflicts": ["foo-bar", "foo_bar"],
            "conflict_rule": rule,
            "note": f"normalization collision with {_enumerate_conflicts(['foo-bar', 'foo_bar'])} — {rule}",
        }
        from rlsbl.commands.claim_name import run_cmd as claim_run_cmd

        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("pypi", ["foobar"], {"yes": False})
        assert exc_info.value.code == 1

        err = capsys.readouterr().err
        for pkg in ("foo-bar", "foo_bar"):
            assert pkg in err, f"'{pkg}' missing from claim-name stderr"
        assert rule in err

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_crates_multi_conflict_rendered(self, mock_check, capsys):
        rule = "crates.io treats hyphens and underscores as equivalent"
        mock_check.return_value = {
            "name": "foobar", "registry": "crates", "status": "taken",
            "variants": None, "reason": "normalized",
            "conflicts": ["foo-bar", "foo_bar"],
            "conflict_rule": rule,
            "note": f"normalization collision with {_enumerate_conflicts(['foo-bar', 'foo_bar'])} — {rule}",
        }
        from rlsbl.commands.claim_name import run_cmd as claim_run_cmd

        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("crates", ["foobar"], {"yes": False})
        assert exc_info.value.code == 1

        err = capsys.readouterr().err
        for pkg in ("foo-bar", "foo_bar"):
            assert pkg in err, f"'{pkg}' missing from claim-name stderr"
        assert rule in err

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_npm_multi_conflict_rendered(self, mock_check, capsys):
        rule = "npm strips dashes, dots, and underscores: these share one moniker"
        mock_check.return_value = {
            "name": "foobar", "registry": "npm", "status": "taken",
            "variants": None, "reason": "moniker",
            "conflicts": ["foo-bar", "foo.bar", "foo_bar"],
            "conflict_rule": rule,
            "note": f"moniker collision with {_enumerate_conflicts(['foo-bar', 'foo.bar', 'foo_bar'])} — {rule}",
        }
        from rlsbl.commands.claim_name import run_cmd as claim_run_cmd

        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("npm", ["foobar"], {"yes": False})
        assert exc_info.value.code == 1

        err = capsys.readouterr().err
        for pkg in ("foo-bar", "foo.bar", "foo_bar"):
            assert pkg in err, f"'{pkg}' missing from claim-name stderr"
        assert rule in err
