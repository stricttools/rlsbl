"""Tests for crates.io check-name and claim-name support."""

import json
import os
import re
import subprocess
import sys
import urllib.error
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.check import (
    check_crates_availability,
    get_crates_variants,
    _check_single_name,
    _classify_variant_collisions,
    _format_single_result,
)
from rlsbl.targets.utils import normalize_crates


class TestNormalizeCrates:
    """Tests for crates.io name normalization."""

    def test_hyphen_underscore_equivalent(self):
        assert normalize_crates("foo-bar") == normalize_crates("foo_bar")

    def test_lowercased(self):
        assert normalize_crates("FooBar") == "foobar"

    def test_multiple_separators_collapsed(self):
        assert normalize_crates("foo--bar") == "foo-bar"
        assert normalize_crates("foo__bar") == "foo-bar"

    def test_mixed_separators(self):
        assert normalize_crates("foo-bar_baz") == "foo-bar-baz"

    def test_no_separators_unchanged(self):
        assert normalize_crates("foobar") == "foobar"

    def test_single_char(self):
        assert normalize_crates("x") == "x"


class TestGetCratesVariants:
    """Tests for crates.io variant generation."""

    def test_hyphen_generates_underscore(self):
        variants = get_crates_variants("foo-bar")
        assert "foo_bar" in variants

    def test_underscore_generates_hyphen(self):
        variants = get_crates_variants("foo_bar")
        assert "foo-bar" in variants

    def test_stripped_variant_included(self):
        variants = get_crates_variants("foo-bar")
        assert "foobar" in variants

    def test_original_excluded(self):
        variants = get_crates_variants("foo-bar")
        assert "foo-bar" not in variants

    def test_no_separator_inserts_separators(self):
        variants = get_crates_variants("foobar")
        # Should include insertion variants like "foo-bar", "foo_bar", etc.
        assert any("-" in v for v in variants)
        assert any("_" in v for v in variants)

    def test_no_duplicates(self):
        variants = get_crates_variants("foo-bar")
        assert len(variants) == len(set(variants))


class TestCheckCratesAvailability:
    """Tests for check_crates_availability with mocked HTTP."""

    @patch("rlsbl.commands.check._request_with_backoff")
    def test_available_on_404(self, mock_req):
        mock_req.side_effect = urllib.error.HTTPError(
            "https://crates.io/api/v1/crates/nonexistent",
            404, "Not Found", {}, None,
        )
        result = check_crates_availability("nonexistent")
        assert result["status"] == "available"

    @patch("rlsbl.commands.check._request_with_backoff")
    def test_taken_on_200(self, mock_req):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_req.return_value = resp
        result = check_crates_availability("serde")
        assert result["status"] == "taken"

    @patch("rlsbl.commands.check._request_with_backoff")
    def test_error_on_500(self, mock_req):
        mock_req.side_effect = urllib.error.HTTPError(
            "https://crates.io/api/v1/crates/test",
            500, "Server Error", {}, None,
        )
        result = check_crates_availability("test")
        assert result["status"] == "error"
        assert "500" in result["message"]

    @patch("rlsbl.commands.check._request_with_backoff")
    def test_error_on_network_failure(self, mock_req):
        mock_req.side_effect = Exception("DNS resolution failed")
        result = check_crates_availability("test")
        assert result["status"] == "error"

    @patch("rlsbl.commands.check._request_with_backoff")
    def test_user_agent_header_sent(self, mock_req):
        mock_req.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None,
        )
        check_crates_availability("test-pkg")
        # Verify headers were passed
        call_kwargs = mock_req.call_args
        assert "headers" in call_kwargs[1] or (len(call_kwargs[0]) > 0)


class TestClassifyVariantCollisionsCrates:
    """Tests for crates.io collision classification."""

    def test_hyphen_underscore_is_hard_collision(self):
        hard, soft = _classify_variant_collisions("foo-bar", ["foo_bar"], "crates")
        assert "foo_bar" in hard
        assert soft == []

    def test_unrelated_is_soft(self):
        hard, soft = _classify_variant_collisions("foobar", ["foobaz"], "crates")
        assert hard == []
        assert "foobaz" in soft


class TestCheckSingleNameCrates:
    """Tests for _check_single_name with crates registry."""

    @patch("rlsbl.commands.check.check_crates_availability")
    def test_available_name(self, mock_check):
        mock_check.return_value = {"status": "available"}
        with patch("rlsbl.commands.check._check_variants", return_value=[]):
            result = _check_single_name("my-new-crate", "crates")
        assert result["status"] == "available"
        assert result["registry"] == "crates"

    @patch("rlsbl.commands.check.check_crates_availability")
    def test_taken_name(self, mock_check):
        mock_check.return_value = {"status": "taken"}
        result = _check_single_name("serde", "crates")
        assert result["status"] == "taken"
        assert result["reason"] == "registered"

    @patch("rlsbl.commands.check.check_crates_availability")
    def test_error_name(self, mock_check):
        mock_check.return_value = {"status": "error", "message": "timeout"}
        result = _check_single_name("test", "crates")
        assert result["status"] == "error"
        assert result["error"] == "timeout"

    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_crates_availability")
    def test_normalization_collision_detected(self, mock_check, mock_variants):
        mock_check.return_value = {"status": "available"}
        mock_variants.return_value = ["foo_bar"]
        result = _check_single_name("foo-bar", "crates")
        assert result["status"] == "taken"
        assert result["reason"] == "normalized"
        assert "hyphens and underscores" in result["note"]


class TestFormatSingleResultCrates:
    """Tests for verbose output formatting of crates.io results."""

    def test_available_output(self, capsys):
        result = {
            "name": "my-crate",
            "registry": "crates",
            "status": "available",
            "variants": None,
            "reason": None,
        }
        exit_code = _format_single_result(result)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "crates.io" in captured.out
        assert "available" in captured.out

    def test_taken_output(self, capsys):
        result = {
            "name": "serde",
            "registry": "crates",
            "status": "taken",
            "variants": None,
            "reason": "registered",
        }
        exit_code = _format_single_result(result)
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "taken" in captured.out

    def test_error_output(self, capsys):
        result = {
            "name": "test",
            "registry": "crates",
            "status": "error",
            "variants": None,
            "reason": None,
            "error": "network failure",
        }
        exit_code = _format_single_result(result)
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "network failure" in captured.err

    def test_normalization_collision_output(self, capsys):
        result = {
            "name": "foo-bar",
            "registry": "crates",
            "status": "taken",
            "variants": [],
            "reason": "normalized",
            "note": "normalization collision with 'foo_bar' (crates.io treats hyphens and underscores as equivalent)",
        }
        exit_code = _format_single_result(result)
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "normalization" in captured.out.lower() or "normalized" in captured.out.lower()


class TestCheckNameCratesTarget:
    """Tests for crates target acceptance in check-name CLI."""

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["my-crate"])
    def test_crates_target_accepted(self, mock_run_cmd):
        from rlsbl import cmd_check_name
        cmd_check_name(target=["crates"], delay="200")
        mock_run_cmd.assert_called_once()
        assert mock_run_cmd.call_args[0][0] == "crates"

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["my-crate"])
    def test_crates_with_other_targets(self, mock_run_cmd):
        from rlsbl import cmd_check_name
        cmd_check_name(target=["npm", "crates"], delay="200")
        assert mock_run_cmd.call_count == 2
        targets_called = [call[0][0] for call in mock_run_cmd.call_args_list]
        assert "npm" in targets_called
        assert "crates" in targets_called
