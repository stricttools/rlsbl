"""Tests for check-name multi-target execution and --json output.

Two concerns:

1. Multi-target bug regression: ``rlsbl check-name --target npm --target pypi``
   must run EVERY target.  The old ``run_cmd`` called ``sys.exit`` internally,
   so the CLI handler's per-target loop only ever reached the first target.

2. ``--json`` structured output: single name+target yields one JSON object;
   multiple names and/or targets yield a JSON array.  Each object carries the
   unified ``structured_conflicts`` field with stable rule tokens.
"""

import json as _json
from unittest.mock import patch

import pytest

import rlsbl
from rlsbl.commands.check import (
    _RULE_TOKEN_NPM_MONIKER,
    _RULE_TOKEN_PYPI_SEPARATOR,
    _RULE_TOKEN_PYPI_ULTRANORM,
    _RULE_TOKEN_STDLIB,
    _RULE_TOKEN_SENTENCES,
    _result_to_json,
    run_cmd,
)


def _avail(name, registry):
    return {"name": name, "registry": registry, "status": "available",
            "variants": [], "reason": None}


class TestCheckNameMultiTargetRunsAllTargets:
    """Regression: the per-target loop must reach every target with real run_cmd.

    ``run_cmd`` returns ``(exit_code, payload)`` and never calls ``sys.exit`` --
    the handler owns process exit.  Before the fix, ``run_cmd`` exited on the
    first target, so the second target's checks never ran.
    """

    @patch("rlsbl._variadic_args", ["mypkg"])
    @patch("rlsbl.commands.check._apply_ultranorm_check")
    @patch("rlsbl.commands.check._check_single_name")
    def test_two_targets_both_checked(self, mock_check, _mock_ultra):
        mock_check.side_effect = lambda name, registry, delay_ms=200: _avail(name, registry)

        result = rlsbl.app.test(
            ["check-name", "--target", "npm", "--target", "pypi"]
        )

        registries_checked = {c.args[1] for c in mock_check.call_args_list}
        assert registries_checked == {"npm", "pypi"}
        assert result.exit_code == 0

    @patch("rlsbl._variadic_args", ["mypkg"])
    @patch("rlsbl.commands.check._apply_ultranorm_check")
    @patch("rlsbl.commands.check._check_single_name")
    def test_highest_exit_code_wins(self, mock_check, _mock_ultra):
        def side(name, registry, delay_ms=200):
            if registry == "pypi":
                return {"name": name, "registry": "pypi", "status": "taken",
                        "variants": None, "reason": "registered"}
            return _avail(name, registry)
        mock_check.side_effect = side

        result = rlsbl.app.test(
            ["check-name", "--target", "npm", "--target", "pypi"]
        )
        # npm available (0), pypi taken (1) -> overall exit 1
        assert result.exit_code == 1


class TestResultToJson:
    """_result_to_json projects a result dict onto the stable JSON surface."""

    def test_available_minimal_object(self):
        obj = _result_to_json(_avail("mypkg", "npm"), 0)
        assert obj == {
            "name": "mypkg", "target": "npm", "status": "available",
            "reason": None, "structured_conflicts": [],
            "rule_sentences": {}, "exit_code": 0,
        }

    @pytest.mark.parametrize("token", [
        _RULE_TOKEN_NPM_MONIKER,
        _RULE_TOKEN_PYPI_SEPARATOR,
        _RULE_TOKEN_PYPI_ULTRANORM,
        _RULE_TOKEN_STDLIB,
    ])
    def test_each_token_surfaces_sentence(self, token):
        """Every rule token carries its human sentence in rule_sentences."""
        result = {
            "name": "foo", "registry": "pypi", "status": "taken",
            "reason": "x", "variants": None,
            "structured_conflicts": [{"name": "foo-bar", "rule": token}],
        }
        obj = _result_to_json(result, 1)
        assert obj["structured_conflicts"] == [{"name": "foo-bar", "rule": token}]
        assert obj["rule_sentences"] == {token: _RULE_TOKEN_SENTENCES[token]}
        assert obj["exit_code"] == 1

    def test_error_surfaces_error_key(self):
        result = {
            "name": "x", "registry": "npm", "status": "error",
            "reason": None, "variants": None, "error": "boom",
        }
        obj = _result_to_json(result, 2)
        assert obj["error"] == "boom"
        assert obj["exit_code"] == 2

    def test_multiple_tokens_deduped_in_sentences(self):
        """A name colliding via two mechanisms lists both objects, both sentences."""
        result = {
            "name": "foo", "registry": "pypi", "status": "taken",
            "reason": "normalized", "variants": None,
            "structured_conflicts": [
                {"name": "foo-bar", "rule": _RULE_TOKEN_PYPI_SEPARATOR},
                {"name": "foo-bar", "rule": _RULE_TOKEN_PYPI_ULTRANORM},
            ],
        }
        obj = _result_to_json(result, 1)
        assert set(obj["rule_sentences"]) == {
            _RULE_TOKEN_PYPI_SEPARATOR, _RULE_TOKEN_PYPI_ULTRANORM
        }
        assert len(obj["structured_conflicts"]) == 2


class TestCheckNameJsonOutput:
    """--json end-to-end: single object vs array, no interleaved human output."""

    @patch("rlsbl._variadic_args", ["mypkg"])
    @patch("rlsbl.commands.check._apply_ultranorm_check")
    @patch("rlsbl.commands.check._check_single_name")
    def test_single_name_single_target_is_object(self, mock_check, _mock_ultra):
        mock_check.side_effect = lambda name, registry, delay_ms=200: _avail(name, registry)

        result = rlsbl.app.test(["check-name", "--target", "npm", "--json"])
        assert result.exit_code == 0
        data = _json.loads(result.stdout)
        assert isinstance(data, dict)
        assert data["name"] == "mypkg"
        assert data["target"] == "npm"
        assert data["status"] == "available"
        assert data["structured_conflicts"] == []
        # No human "Checking npm for..." lines leaked into json mode.
        assert "Checking" not in result.stdout

    @patch("rlsbl._variadic_args", ["a", "b"])
    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._apply_ultranorm_check")
    @patch("rlsbl.commands.check._check_single_name")
    def test_multi_name_single_target_is_array(self, mock_check, _mock_ultra, _mock_sleep):
        mock_check.side_effect = lambda name, registry, delay_ms=200: _avail(name, registry)

        result = rlsbl.app.test(["check-name", "--target", "npm", "--json"])
        data = _json.loads(result.stdout)
        assert isinstance(data, list)
        assert [o["name"] for o in data] == ["a", "b"]

    @patch("rlsbl._variadic_args", ["mypkg"])
    @patch("rlsbl.commands.check._apply_ultranorm_check")
    @patch("rlsbl.commands.check._check_single_name")
    def test_single_name_multi_target_is_array(self, mock_check, _mock_ultra):
        mock_check.side_effect = lambda name, registry, delay_ms=200: _avail(name, registry)

        result = rlsbl.app.test(
            ["check-name", "--target", "npm", "--target", "pypi", "--json"]
        )
        data = _json.loads(result.stdout)
        assert isinstance(data, list)
        assert {o["target"] for o in data} == {"npm", "pypi"}

    @patch("rlsbl._variadic_args", ["mypkg"])
    @patch("rlsbl.commands.check._apply_ultranorm_check")
    @patch("rlsbl.commands.check._check_single_name")
    def test_taken_sets_exit_1_in_json(self, mock_check, _mock_ultra):
        mock_check.side_effect = lambda name, registry, delay_ms=200: {
            "name": name, "registry": registry, "status": "taken",
            "variants": None, "reason": "registered",
        }
        result = rlsbl.app.test(["check-name", "--target", "npm", "--json"])
        assert result.exit_code == 1
        data = _json.loads(result.stdout)
        assert data["status"] == "taken"
        assert data["exit_code"] == 1

    @patch("rlsbl._variadic_args", ["foobar"])
    @patch("rlsbl.commands.check._apply_ultranorm_check")
    @patch("rlsbl.commands.check._check_single_name")
    def test_structured_conflicts_alphabetically_ordered(self, mock_check, _mock_ultra):
        # Provide conflicts already ordered by (name, rule) as produced at
        # construction; json must preserve that ordering.
        mock_check.side_effect = lambda name, registry, delay_ms=200: {
            "name": name, "registry": "npm", "status": "taken",
            "variants": None, "reason": "moniker",
            "structured_conflicts": [
                {"name": "foo-bar", "rule": _RULE_TOKEN_NPM_MONIKER},
                {"name": "foo.bar", "rule": _RULE_TOKEN_NPM_MONIKER},
                {"name": "foo_bar", "rule": _RULE_TOKEN_NPM_MONIKER},
            ],
        }
        result = rlsbl.app.test(["check-name", "--target", "npm", "--json"])
        data = _json.loads(result.stdout)
        names = [c["name"] for c in data["structured_conflicts"]]
        assert names == sorted(names)
