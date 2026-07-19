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
    _RULE_TOKEN_CRATES_SEPARATOR,
    _RULE_TOKEN_NPM_MONIKER,
    _RULE_TOKEN_PYPI_SEPARATOR,
    _RULE_TOKEN_PYPI_ULTRANORM,
    _RULE_TOKEN_STDLIB,
    _add_structured_conflicts,
    _apply_ultranorm_check,
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
# Unified structured_conflicts field: {name, rule} objects per mechanism
# ---------------------------------------------------------------------------

class TestStructuredConflictsPerMechanism:
    """Every collision mechanism folds into result['structured_conflicts']."""

    @patch("rlsbl.commands.check._search_npm_similar", return_value=[])
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_moniker_hard_token(self, mock_avail, mock_variants, mock_search):
        mock_avail.return_value = {"status": "available"}
        mock_variants.return_value = ["foo-bar", "foo.bar"]

        result = _check_single_name("foobar", "npm")

        assert result["structured_conflicts"] == [
            {"name": "foo-bar", "rule": _RULE_TOKEN_NPM_MONIKER},
            {"name": "foo.bar", "rule": _RULE_TOKEN_NPM_MONIKER},
        ]

    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants", return_value=[])
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_moniker_search_token(self, mock_avail, mock_variants, mock_search):
        mock_avail.return_value = {"status": "available"}
        mock_search.return_value = ["foo-bar", "foo.bar"]

        result = _check_single_name("foobar", "npm")

        assert result["structured_conflicts"] == [
            {"name": "foo-bar", "rule": _RULE_TOKEN_NPM_MONIKER},
            {"name": "foo.bar", "rule": _RULE_TOKEN_NPM_MONIKER},
        ]

    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_separator_token(self, mock_avail, mock_variants):
        mock_avail.return_value = {"status": "available"}
        mock_variants.return_value = ["foo-bar", "foo_bar"]
        with patch("rlsbl.commands.check._classify_variant_collisions",
                    return_value=(["foo-bar", "foo_bar"], [])):
            result = _check_single_name("foobar", "pypi")

        assert result["structured_conflicts"] == [
            {"name": "foo-bar", "rule": _RULE_TOKEN_PYPI_SEPARATOR},
            {"name": "foo_bar", "rule": _RULE_TOKEN_PYPI_SEPARATOR},
        ]

    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_crates_availability")
    def test_crates_separator_token(self, mock_avail, mock_variants):
        mock_avail.return_value = {"status": "available"}
        mock_variants.return_value = ["foo-bar", "foo_bar"]
        with patch("rlsbl.commands.check._classify_variant_collisions",
                    return_value=(["foo-bar", "foo_bar"], [])):
            result = _check_single_name("foobar", "crates")

        assert result["structured_conflicts"] == [
            {"name": "foo-bar", "rule": _RULE_TOKEN_CRATES_SEPARATOR},
            {"name": "foo_bar", "rule": _RULE_TOKEN_CRATES_SEPARATOR},
        ]

    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_stdlib_token(self, mock_avail):
        # 'queue' collides with the stdlib module 'queue' before any network call.
        result = _check_single_name("queue", "pypi")
        assert result["structured_conflicts"] == [
            {"name": "queue", "rule": _RULE_TOKEN_STDLIB},
        ]
        mock_avail.assert_not_called()

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_ultranorm_token(self, mock_pypi, mock_sleep):
        result = {"name": "cli", "registry": "pypi", "status": "available",
                  "variants": []}

        def side(name):
            return {"status": "taken"} if name == "cl1" else {"status": "available"}
        mock_pypi.side_effect = side

        _apply_ultranorm_check(result, "pypi", 200)
        assert {"name": "cl1", "rule": _RULE_TOKEN_PYPI_ULTRANORM} in result["structured_conflicts"]
        assert all(c["rule"] == _RULE_TOKEN_PYPI_ULTRANORM for c in result["structured_conflicts"])

    def test_simultaneous_separator_and_ultranorm(self):
        """A PyPI name colliding via BOTH mechanisms lists both objects, sorted.

        The unified field accumulates across attach sites; a single conflicting
        name that trips separator normalization AND visual-ambiguity contributes
        one object per mechanism.
        """
        result = {"name": "foobar", "registry": "pypi", "status": "taken"}
        _add_structured_conflicts(result, ["foo_bar"], _RULE_TOKEN_PYPI_SEPARATOR)
        _add_structured_conflicts(result, ["foo_bar"], _RULE_TOKEN_PYPI_ULTRANORM)

        assert result["structured_conflicts"] == [
            {"name": "foo_bar", "rule": _RULE_TOKEN_PYPI_SEPARATOR},
            {"name": "foo_bar", "rule": _RULE_TOKEN_PYPI_ULTRANORM},
        ]

    def test_ordering_is_by_name_then_rule(self):
        """Regardless of insertion order, the list sorts by (name, rule)."""
        result = {"name": "x", "registry": "npm", "status": "taken"}
        _add_structured_conflicts(result, ["zeta", "alpha"], _RULE_TOKEN_NPM_MONIKER)
        _add_structured_conflicts(result, ["alpha"], _RULE_TOKEN_CRATES_SEPARATOR)
        names_rules = [(c["name"], c["rule"]) for c in result["structured_conflicts"]]
        assert names_rules == sorted(names_rules)
        # 'alpha' appears twice with different rules, ordered by rule.
        assert names_rules[0] == ("alpha", _RULE_TOKEN_CRATES_SEPARATOR)
        assert names_rules[1] == ("alpha", _RULE_TOKEN_NPM_MONIKER)


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
