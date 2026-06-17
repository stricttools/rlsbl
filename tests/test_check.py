"""Tests for PyPI, Go, and GitHub availability checks in rlsbl.commands.check."""

import subprocess
from io import StringIO
from unittest.mock import patch, MagicMock, call
from urllib.error import HTTPError, URLError

import pytest

from conftest import FakeResponse
from rlsbl.commands.check import (
    _apply_ultranorm_check,
    _check_single_name,
    _check_stdlib_collision,
    _check_variants,
    _format_single_result,
    _format_table_row,
    _generate_ultranorm_variants,
    _request_with_backoff,
    _search_npm_similar,
    _ultranormalize,
    check_github_availability,
    check_go_availability,
    check_npm_availability,
    check_pypi_availability,
    get_npm_variants,
    get_pypi_variants,
    run_cmd,
)
from rlsbl.targets.utils import normalize_npm


class TestCheckPyPI:
    """Tests for check_pypi_availability and get_pypi_variants."""

    @patch("urllib.request.urlopen")
    def test_pypi_available_on_404(self, mock_urlopen):
        """HTTPError with code 404 means the package name is available."""
        mock_urlopen.side_effect = HTTPError(
            "https://pypi.org/simple/nonexistent/", 404, "Not Found", {}, None
        )
        result = check_pypi_availability("nonexistent")
        assert result["status"] == "available"

    @patch("urllib.request.urlopen")
    def test_pypi_taken_on_200(self, mock_urlopen):
        """A 200 response means the package name is taken."""
        mock_urlopen.return_value = FakeResponse({"info": {"name": "requests"}})
        result = check_pypi_availability("requests")
        assert result["status"] == "taken"

    @patch("urllib.request.urlopen")
    def test_pypi_error_on_url_error(self, mock_urlopen):
        """A generic URLError (network failure) returns error status."""
        mock_urlopen.side_effect = URLError("Connection refused")
        result = check_pypi_availability("some-package")
        assert result["status"] == "error"
        assert "message" in result

    @patch("urllib.request.urlopen")
    def test_pypi_registered_but_empty_is_taken(self, mock_urlopen):
        """A registered-but-empty package (no releases) should be 'taken'.

        The JSON API returns 404 for these, but the Simple API correctly
        returns 200. This test verifies we use the Simple API.
        """
        # Simple API returns 200 for registered packages even with no releases
        mock_urlopen.return_value = FakeResponse(b"<html></html>")
        result = check_pypi_availability("cost")
        assert result["status"] == "taken"
        # Verify the URL uses the Simple API with normalized name
        called_url = mock_urlopen.call_args[0][0].full_url
        assert "/simple/cost/" in called_url
        assert "/pypi/" not in called_url

    @patch("urllib.request.urlopen")
    def test_pypi_uses_normalized_name_in_url(self, mock_urlopen):
        """The Simple API URL should use PEP 503 normalized names."""
        mock_urlopen.return_value = FakeResponse(b"<html></html>")
        check_pypi_availability("My_Package.Name")
        called_url = mock_urlopen.call_args[0][0].full_url
        # PEP 503: lowercase, runs of [-_.] replaced with single hyphen
        assert "/simple/my-package-name/" in called_url

    def test_pypi_variants(self):
        """get_pypi_variants generates PEP 503 normalized forms."""
        variants = get_pypi_variants("my-package")
        # Should include underscore and no-separator forms
        assert "my_package" in variants
        assert "mypackage" in variants
        # The normalized hyphen form is the same as input, so it should
        # be excluded (the function discards the original name)
        assert "my-package" not in variants


class TestCheckGo:
    """Tests for check_go_availability."""

    @patch("urllib.request.urlopen")
    def test_go_exists_on_200(self, mock_urlopen):
        """A 200 response means the Go module exists."""
        mock_urlopen.return_value = FakeResponse(b"<html>pkg page</html>")
        result = check_go_availability("github.com/gorilla/mux")
        assert result["status"] == "exists"

    @patch("urllib.request.urlopen")
    def test_go_not_found_on_404(self, mock_urlopen):
        """HTTPError with code 404 means the module is not found."""
        mock_urlopen.side_effect = HTTPError(
            "https://pkg.go.dev/github.com/fake/module", 404, "Not Found", {}, None
        )
        result = check_go_availability("github.com/fake/module")
        assert result["status"] == "not_found"
        assert "note" in result

    @patch("urllib.request.urlopen")
    def test_go_error_on_url_error(self, mock_urlopen):
        """A generic URLError (network failure) returns error status."""
        mock_urlopen.side_effect = URLError("DNS resolution failed")
        result = check_go_availability("github.com/some/module")
        assert result["status"] == "error"
        assert "message" in result


class TestCheckGitHub:
    """Tests for check_github_availability."""

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_github_available_on_zero_count(self, mock_urlopen):
        """Zero total_count means the name is unique on GitHub."""
        mock_urlopen.return_value = FakeResponse({"total_count": 0, "items": []})
        result = check_github_availability("some-unique-name")
        assert result["status"] == "available"
        assert result["count"] == 0

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_github_exists_on_nonzero_count(self, mock_urlopen):
        """Non-zero total_count means repos with this name exist."""
        mock_urlopen.return_value = FakeResponse({"total_count": 5, "items": []})
        result = check_github_availability("popular-name")
        assert result["status"] == "exists"
        assert result["count"] == 5
        assert "note" in result
        assert "5" in result["note"]

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_github_error_on_exception(self, mock_urlopen):
        """A network error returns error status."""
        mock_urlopen.side_effect = URLError("Connection refused")
        result = check_github_availability("some-name")
        assert result["status"] == "error"
        assert "message" in result


class TestCheckSingleName:
    """Tests for the _check_single_name structured result function."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_available_result(self, mock_npm, mock_gh):
        """Available npm name returns correct structured result."""
        mock_npm.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-new-pkg", "npm")
        assert result["name"] == "my-new-pkg"
        assert result["registry"] == "npm"
        assert result["status"] == "available"
        assert isinstance(result["variants"], list)
        assert result["github_count"] == 0

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_taken_result(self, mock_npm, mock_gh):
        """Taken npm name returns correct structured result; GitHub check skipped."""
        mock_npm.return_value = {"status": "taken"}
        mock_gh.return_value = {"status": "exists", "count": 5, "note": "5 repos"}

        result = _check_single_name("express", "npm")
        assert result["status"] == "taken"
        assert result["github_count"] is None
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_error_result(self, mock_npm, mock_gh):
        """Error checking npm returns error in result; GitHub skipped."""
        mock_npm.return_value = {"status": "error", "message": "npm CLI not found"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("some-pkg", "npm")
        assert result["status"] == "error"
        assert result["error"] == "npm CLI not found"
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_available_result(self, mock_pypi, mock_gh):
        """Available PyPI name returns correct structured result."""
        mock_pypi.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-new-pkg", "pypi")
        assert result["name"] == "my-new-pkg"
        assert result["registry"] == "pypi"
        assert result["status"] == "available"

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_go_availability")
    def test_go_not_found_result(self, mock_go, mock_gh):
        """Not-found Go module returns correct structured result with note."""
        mock_go.return_value = {
            "status": "not_found",
            "note": "Go modules use repository paths, not a central registry.",
        }
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("github.com/fake/module", "go")
        assert result["status"] == "not_found"
        assert "note" in result
        assert result["registry"] == "go"

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_go_availability")
    def test_go_exists_result(self, mock_go, mock_gh):
        """Existing Go module returns 'exists' status; GitHub check skipped."""
        mock_go.return_value = {"status": "exists"}
        mock_gh.return_value = {"status": "exists", "count": 3, "note": "3 repos"}

        result = _check_single_name("github.com/gorilla/mux", "go")
        assert result["status"] == "exists"
        assert result["github_count"] is None
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_github_error_sets_count_none(self, mock_npm, mock_gh):
        """When GitHub check errors, github_count is None."""
        mock_npm.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "error", "message": "Connection refused"}

        result = _check_single_name("some-pkg", "npm")
        assert result["github_count"] is None


class TestCheckTargetRequired:
    """Tests verifying that --target is required for the check command."""

    def test_missing_target_prints_error(self):
        """Running 'rlsbl check-name <name>' without --target should exit with error."""
        from rlsbl import main

        with patch("sys.argv", ["rlsbl", "check-name", "some-name"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_missing_target_error_message(self):
        """Error message should mention --target is required."""
        from rlsbl import app

        result = app.test(["check-name"])
        assert result.exit_code == 1
        assert "target" in result.stderr
        assert "required" in result.stderr


class TestRequestWithBackoff:
    """Tests for the _request_with_backoff retry helper."""

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_successful_request_no_retry(self, mock_urlopen):
        """A successful request returns the response without retrying."""
        fake_resp = FakeResponse(b"OK")
        mock_urlopen.return_value = fake_resp
        result = _request_with_backoff("https://example.com/test")
        assert result is fake_resp
        assert mock_urlopen.call_count == 1

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_429_with_retry_after_header(self, mock_urlopen, mock_sleep):
        """HTTP 429 with Retry-After header sleeps that many seconds then succeeds."""
        headers_429 = MagicMock()
        headers_429.get.return_value = "3"
        error_429 = HTTPError(
            "https://example.com", 429, "Too Many Requests", headers_429, None
        )
        fake_resp = FakeResponse(b"OK")
        mock_urlopen.side_effect = [error_429, fake_resp]

        result = _request_with_backoff("https://example.com/test", max_retries=3)
        assert result is fake_resp
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(3.0)

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_429_without_retry_after_uses_exponential_backoff(self, mock_urlopen, mock_sleep):
        """HTTP 429 without Retry-After uses exponential backoff (2, 4, ...)."""
        headers_429 = MagicMock()
        headers_429.get.return_value = None
        error_429_first = HTTPError(
            "https://example.com", 429, "Too Many Requests", headers_429, None
        )
        error_429_second = HTTPError(
            "https://example.com", 429, "Too Many Requests", headers_429, None
        )
        fake_resp = FakeResponse(b"OK")
        mock_urlopen.side_effect = [error_429_first, error_429_second, fake_resp]

        result = _request_with_backoff("https://example.com/test", max_retries=3)
        assert result is fake_resp
        assert mock_urlopen.call_count == 3
        # attempt 0: 2^1 = 2, attempt 1: 2^2 = 4
        assert mock_sleep.call_args_list[0][0][0] == 2
        assert mock_sleep.call_args_list[1][0][0] == 4

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_max_retries_exhausted_raises(self, mock_urlopen, mock_sleep):
        """After exhausting max_retries on 429, raises the last HTTPError."""
        headers_429 = MagicMock()
        headers_429.get.return_value = None
        errors = [
            HTTPError("https://example.com", 429, "Too Many Requests", headers_429, None)
            for _ in range(3)
        ]
        mock_urlopen.side_effect = errors

        with pytest.raises(HTTPError) as exc_info:
            _request_with_backoff("https://example.com/test", max_retries=3)
        assert exc_info.value.code == 429
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_count == 3

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_non_429_http_error_not_retried(self, mock_urlopen, mock_sleep):
        """Non-429 HTTP errors are raised immediately without retrying."""
        error_500 = HTTPError(
            "https://example.com", 500, "Internal Server Error", {}, None
        )
        mock_urlopen.side_effect = error_500

        with pytest.raises(HTTPError) as exc_info:
            _request_with_backoff("https://example.com/test", max_retries=3)
        assert exc_info.value.code == 500
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()


class TestDelayFlag:
    """Tests for the --delay value flag."""

    @patch("rlsbl._variadic_args", ["my-pkg"])
    @patch("rlsbl.commands.check._check_single_name")
    def test_delay_parsed_as_value_flag(self, mock_check):
        """--delay is recognized as a value flag and passed to run_cmd."""
        mock_check.return_value = {
            "name": "my-pkg", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0,
        }
        import rlsbl
        result = rlsbl.app.test(["check-name", "--target", "npm", "--delay", "500"])
        assert result.exit_code == 0
        mock_check.assert_called_once_with("my-pkg", "npm")

    @patch("rlsbl._variadic_args", ["a", "b"])
    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_delay_default_value(self, mock_check, mock_sleep):
        """When --delay is not provided, the default is 200ms."""
        mock_check.side_effect = [
            {"name": "a", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "b", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]
        import rlsbl
        result = rlsbl.app.test(["check-name", "--target", "npm"])
        assert result.exit_code == 0
        # Default delay is 200ms = 0.2s between names
        mock_sleep.assert_called_once_with(0.2)


class TestMultiNameCheck:
    """Tests for multi-name CLI behavior in run_cmd."""

    @patch("rlsbl.commands.check._format_single_result")
    @patch("rlsbl.commands.check._check_single_name")
    def test_single_name_uses_verbose_format(self, mock_check, mock_format):
        """A single name should use the verbose _format_single_result output."""
        mock_check.return_value = {
            "name": "foo", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0,
        }
        run_cmd("npm", ["foo"], {})
        mock_check.assert_called_once_with("foo", "npm")
        mock_format.assert_called_once_with(mock_check.return_value)

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_multiple_names_prints_table(self, mock_check, mock_sleep):
        """Multiple names should print a compact table with Name and Status columns."""
        mock_check.side_effect = [
            {"name": "foo", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "bar", "registry": "npm", "status": "taken",
             "variants": [], "github_count": 5},
            {"name": "baz", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd("npm", ["foo", "bar", "baz"], {})
        output = mock_stdout.getvalue()
        lines = output.strip().split("\n")
        # header + 3 rows + blank + summary + batch note = 7 lines
        assert len(lines) == 7
        assert "Name" in lines[0]
        assert "Status" in lines[0]
        assert "foo" in lines[1]
        assert "available" in lines[1]
        assert "bar" in lines[2]
        assert "taken" in lines[2]
        assert "baz" in lines[3]
        assert "available" in lines[3]

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_delay_applied_between_names(self, mock_check, mock_sleep):
        """Delay should be applied between names, not after the last one."""
        mock_check.side_effect = [
            {"name": "a", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "b", "registry": "npm", "status": "taken",
             "variants": [], "github_count": 0},
            {"name": "c", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", ["a", "b", "c"], {"delay": "500"})
        # 3 names -> 2 delays between them
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(0.5), call(0.5)])

    def test_empty_args_prints_error_and_exits(self):
        """No names should print an error to stderr and exit 1."""
        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", [], {})
            assert exc_info.value.code == 1
        assert "missing package name" in mock_stderr.getvalue()


class TestStdlibCollision:
    """Tests for _check_stdlib_collision."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # 'queue' is a stdlib module and should be detected
            ("queue", "queue"),
            # 'json' is a stdlib module and should be detected
            ("json", "json"),
            # A name that is not a stdlib module returns None
            ("myuniquepkg", None),
            # 'os-path' normalizes to 'os-path', not 'os' -- should NOT collide
            ("os-path", None),
        ],
    )
    def test_stdlib_collision(self, name, expected):
        """Stdlib module names are detected; non-stdlib names return None."""
        assert _check_stdlib_collision(name) == expected


class TestStdlibCollisionIntegration:
    """Integration test: _check_single_name short-circuits for stdlib collisions."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_stdlib_name_skips_network(self, mock_pypi, mock_gh):
        """Checking 'queue' on pypi returns taken with stdlib note, no HTTP call."""
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("queue", "pypi")
        assert result["status"] == "taken"
        assert "stdlib module" in result["note"]
        assert "queue" in result["note"]
        # PyPI availability check should NOT have been called
        mock_pypi.assert_not_called()
        # GitHub check should also be skipped for taken names
        mock_gh.assert_not_called()


class TestUltranormalize:
    """Tests for _ultranormalize."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # l->1 and i->1
            ("cli", "c11"),
            # l->1 twice, o->0
            ("hello", "he110"),
            # Strips dash, o->0 twice
            ("foo-bar", "f00bar"),
            # No ambiguous chars, just lowercased
            ("MyPackage", "mypackage"),
            # Empty string returns empty string
            ("", ""),
        ],
    )
    def test_ultranormalize(self, name, expected):
        """Ambiguous characters are normalized; separators stripped; lowercased."""
        assert _ultranormalize(name) == expected


class TestGenerateUltranormVariants:
    """Tests for _generate_ultranorm_variants."""

    def test_cli_variants(self):
        """'cli' generates variants with l<->1 and i<->1, excluding itself."""
        variants, capped = _generate_ultranorm_variants("cli")
        assert not capped
        assert "cl1" in variants
        assert "c1i" in variants
        assert "c11" in variants
        assert "cli" not in variants

    def test_hello_variants(self):
        """'hello' generates variants with l<->1 and o<->0 substitutions."""
        variants, capped = _generate_ultranorm_variants("hello")
        assert not capped
        # Some expected variants
        assert "he1lo" in variants
        assert "hel1o" in variants
        assert "hell0" in variants
        assert "he110" in variants
        assert "hello" not in variants

    def test_no_ambiguous_chars(self):
        """'abc' has no ambiguous characters, returns empty list."""
        variants, capped = _generate_ultranorm_variants("abc")
        assert not capped
        assert variants == []

    def test_cap_at_64(self):
        """Name with >6 ambiguous chars hits cap and reports capped=True."""
        # 7 ambiguous chars -> 2^7 = 128 combinations, minus original = 127
        name = "lllllll"
        variants, capped = _generate_ultranorm_variants(name)
        assert capped
        assert len(variants) <= 64


class TestNpmMonikerNormalize:
    """Tests for normalize_npm."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # 'self-doc' normalizes to 'selfdoc'
            ("self-doc", "selfdoc"),
            # 'selfdoc' stays 'selfdoc'
            ("selfdoc", "selfdoc"),
            # Dots, underscores, and dashes are all stripped
            ("my.package_name", "mypackagename"),
            # Empty string returns empty string
            ("", ""),
        ],
    )
    def test_normalize_npm(self, name, expected):
        """Separators (dashes, dots, underscores) are stripped."""
        assert normalize_npm(name) == expected


class TestSearchNpmSimilar:
    """Tests for _search_npm_similar."""

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_finds_moniker_conflict(self, mock_urlopen):
        """When API returns a package with matching moniker, it is reported."""
        mock_urlopen.return_value = FakeResponse({
            "objects": [
                {"package": {"name": "self-doc"}},
                {"package": {"name": "unrelated-pkg"}},
            ]
        })
        result = _search_npm_similar("selfdoc")
        assert result == ["self-doc"]

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_no_similar_results(self, mock_urlopen):
        """When API returns no results, returns empty list."""
        mock_urlopen.return_value = FakeResponse({"objects": []})
        result = _search_npm_similar("selfdoc")
        assert result == []

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_network_error_returns_empty(self, mock_urlopen):
        """Network errors degrade gracefully to empty list."""
        mock_urlopen.side_effect = URLError("Connection refused")
        result = _search_npm_similar("selfdoc")
        assert result == []

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_no_moniker_match(self, mock_urlopen):
        """When results exist but none have matching monikers, returns empty."""
        mock_urlopen.return_value = FakeResponse({
            "objects": [
                {"package": {"name": "completely-different"}},
                {"package": {"name": "also-unrelated"}},
            ]
        })
        result = _search_npm_similar("selfdoc")
        assert result == []


class TestNpmMonikerIntegration:
    """Integration tests: moniker similarity wired into _check_single_name for npm."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_available_with_moniker_conflict_becomes_taken(
        self, mock_npm, mock_variants, mock_similar, mock_gh
    ):
        """Available name with a moniker conflict is marked taken with note; GitHub skipped."""
        mock_npm.return_value = {"status": "available"}
        mock_variants.return_value = []
        mock_similar.return_value = ["self-doc"]
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("selfdoc", "npm")
        assert result["status"] == "taken"
        assert "moniker conflict" in result["note"]
        assert "self-doc" in result["note"]
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_available_without_moniker_conflict_stays_available(
        self, mock_npm, mock_variants, mock_similar, mock_gh
    ):
        """Available name with no moniker conflicts stays available."""
        mock_npm.return_value = {"status": "available"}
        mock_variants.return_value = []
        mock_similar.return_value = []
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("uniquepkg", "npm")
        assert result["status"] == "available"
        assert "note" not in result

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_taken_name_skips_moniker_search(
        self, mock_npm, mock_variants, mock_similar, mock_gh
    ):
        """Already-taken name does not trigger moniker search or GitHub check."""
        mock_npm.return_value = {"status": "taken"}
        mock_variants.return_value = []
        mock_gh.return_value = {"status": "exists", "count": 5, "note": "5 repos"}

        result = _check_single_name("express", "npm")
        assert result["status"] == "taken"
        mock_similar.assert_not_called()
        mock_gh.assert_not_called()


class TestUltranormIntegration:
    """Integration tests: ultranormalization variant checking wired into run_cmd."""

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_flag_with_variant_exists_adds_conflicts(self, mock_pypi, mock_sleep):
        """Available name with flag + existing variant adds ultranorm_conflicts."""
        # "cli" generates variants: "cl1", "c1i", "c11"
        result = {
            "name": "cli", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        def pypi_side_effect(name):
            if name == "cl1":
                return {"status": "taken"}
            return {"status": "available"}
        mock_pypi.side_effect = pypi_side_effect

        _apply_ultranorm_check(result, "pypi", True, 200)
        assert "ultranorm_conflicts" in result
        assert "cl1" in result["ultranorm_conflicts"]
        assert result.get("ultranorm_caveat")

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_flag_no_variant_exists_no_conflicts_but_caveat(self, mock_pypi, mock_sleep):
        """Available name with flag + no existing variants has caveat but no conflicts."""
        result = {
            "name": "cli", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        mock_pypi.return_value = {"status": "available"}

        _apply_ultranorm_check(result, "pypi", True, 200)
        assert "ultranorm_conflicts" not in result
        assert result.get("ultranorm_caveat")

    def test_no_flag_skips_ultranorm(self):
        """Without the flag, no ultranorm checking occurs at all."""
        result = {
            "name": "cli", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "pypi", False, 200)
        assert "ultranorm_conflicts" not in result
        assert "ultranorm_caveat" not in result

    def test_flag_with_non_pypi_registry_skips(self):
        """Flag with non-pypi registry does no ultranorm checking."""
        result = {
            "name": "cli", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "npm", True, 200)
        assert "ultranorm_conflicts" not in result
        assert "ultranorm_caveat" not in result


class TestRetryVisibility:
    """Tests for retry visibility: _request_with_backoff prints to stderr on 429."""

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_429_with_retry_after_prints_to_stderr(self, mock_urlopen, mock_sleep):
        """HTTP 429 with Retry-After header prints rate-limit message to stderr."""
        headers_429 = MagicMock()
        headers_429.get.return_value = "3"
        error_429 = HTTPError(
            "https://example.com", 429, "Too Many Requests", headers_429, None
        )
        fake_resp = FakeResponse(b"OK")
        mock_urlopen.side_effect = [error_429, fake_resp]

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            _request_with_backoff("https://example.com/test", max_retries=3)
        assert "Rate limited, retrying in" in mock_stderr.getvalue()

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_429_without_retry_after_prints_to_stderr(self, mock_urlopen, mock_sleep):
        """HTTP 429 without Retry-After header prints rate-limit message to stderr."""
        headers_429 = MagicMock()
        headers_429.get.return_value = None
        error_429 = HTTPError(
            "https://example.com", 429, "Too Many Requests", headers_429, None
        )
        fake_resp = FakeResponse(b"OK")
        mock_urlopen.side_effect = [error_429, fake_resp]

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            _request_with_backoff("https://example.com/test", max_retries=3)
        assert "Rate limited, retrying in" in mock_stderr.getvalue()


class TestReasonField:
    """Tests for the reason field on check result dicts."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_stdlib_collision_reason(self, mock_pypi, mock_gh):
        """PyPI stdlib collision sets reason='stdlib'; GitHub skipped."""
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("queue", "pypi")
        assert result["status"] == "taken"
        assert result["reason"] == "stdlib"
        mock_pypi.assert_not_called()
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_registered_reason(self, mock_pypi, mock_gh):
        """PyPI registered package sets reason='registered'; GitHub skipped."""
        mock_pypi.return_value = {"status": "taken"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("requests", "pypi")
        assert result["status"] == "taken"
        assert result["reason"] == "registered"
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_available_reason_none(self, mock_pypi, mock_gh):
        """PyPI available package has reason=None."""
        mock_pypi.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-unique-pkg-xyz", "pypi")
        assert result["status"] == "available"
        assert result["reason"] is None

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_registered_reason(self, mock_npm, mock_gh):
        """npm registered package sets reason='registered'; GitHub skipped."""
        mock_npm.return_value = {"status": "taken"}
        mock_gh.return_value = {"status": "exists", "count": 5, "note": "5 repos"}

        result = _check_single_name("express", "npm")
        assert result["status"] == "taken"
        assert result["reason"] == "registered"
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_moniker_conflict_reason(self, mock_npm, mock_variants, mock_similar, mock_gh):
        """npm moniker conflict sets reason='moniker'; GitHub skipped."""
        mock_npm.return_value = {"status": "available"}
        mock_variants.return_value = []
        mock_similar.return_value = ["self-doc"]
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("selfdoc", "npm")
        assert result["status"] == "taken"
        assert result["reason"] == "moniker"
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_available_no_conflict_reason_none(self, mock_npm, mock_variants, mock_similar, mock_gh):
        """npm available with no moniker conflict has reason=None."""
        mock_npm.return_value = {"status": "available"}
        mock_variants.return_value = []
        mock_similar.return_value = []
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("uniquepkg", "npm")
        assert result["status"] == "available"
        assert result["reason"] is None

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_go_availability")
    def test_go_exists_reason(self, mock_go, mock_gh):
        """Go existing module sets reason='registered'; GitHub skipped."""
        mock_go.return_value = {"status": "exists"}
        mock_gh.return_value = {"status": "exists", "count": 3, "note": "3 repos"}

        result = _check_single_name("github.com/gorilla/mux", "go")
        assert result["status"] == "exists"
        assert result["reason"] == "registered"
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_ultranorm_conflict_reason(self, mock_pypi, mock_sleep):
        """Ultranorm conflict sets reason='ultranorm' and status='taken'."""
        result = {
            "name": "cli", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
        }
        def pypi_side_effect(name):
            if name == "cl1":
                return {"status": "taken"}
            return {"status": "available"}
        mock_pypi.side_effect = pypi_side_effect

        _apply_ultranorm_check(result, "pypi", True, 200)
        assert result["status"] == "taken"
        assert result["reason"] == "ultranorm"
        assert "cl1" in result["ultranorm_conflicts"]


class TestReasonExplanations:
    """Tests for reason-specific explanations in verbose output."""

    def test_pypi_stdlib_explanation(self):
        """PyPI stdlib reason prints standard library explanation."""
        result = {
            "name": "queue", "registry": "pypi", "status": "taken",
            "variants": [], "github_count": None, "reason": "stdlib",
            "note": "conflicts with Python stdlib module 'queue'",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        assert "standard library modules" in mock_stdout.getvalue()

    def test_npm_moniker_explanation(self):
        """npm moniker reason prints punctuation-stripping explanation."""
        result = {
            "name": "selfdoc", "registry": "npm", "status": "taken",
            "variants": [], "github_count": None, "reason": "moniker",
            "note": "moniker conflict with 'self-doc' (npm strips punctuation)",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        assert "removing dashes, dots, and underscores" in mock_stdout.getvalue()

    def test_pypi_ultranorm_explanation(self):
        """PyPI ultranorm reason prints visual similarity explanation."""
        result = {
            "name": "cli", "registry": "pypi", "status": "taken",
            "variants": [], "github_count": None, "reason": "ultranorm",
            "ultranorm_conflicts": ["cl1"],
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        assert "visually similar" in mock_stdout.getvalue()

    def test_pypi_registered_no_explanation(self):
        """PyPI registered reason does NOT print any reason explanation."""
        result = {
            "name": "requests", "registry": "pypi", "status": "taken",
            "variants": [], "github_count": None, "reason": "registered",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        assert "standard library modules" not in output
        assert "removing dashes, dots, and underscores" not in output
        assert "visually similar" not in output

    def test_npm_available_no_explanation(self):
        """npm available result does NOT print any reason explanation."""
        result = {
            "name": "my-unique-pkg", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        assert "standard library modules" not in output
        assert "removing dashes, dots, and underscores" not in output
        assert "visually similar" not in output


class TestShortCircuit:
    """Tests for short-circuit behavior: skip variants and GitHub when taken."""

    # -- 1A: Skip variants when taken --

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_taken_skips_variants(self, mock_npm, mock_variants, mock_gh):
        """npm taken name does not call _check_variants."""
        mock_npm.return_value = {"status": "taken"}

        result = _check_single_name("express", "npm")
        assert result["status"] == "taken"
        mock_variants.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_available_calls_variants(self, mock_npm, mock_variants, mock_similar, mock_gh):
        """npm available name calls _check_variants."""
        mock_npm.return_value = {"status": "available"}
        mock_variants.return_value = []
        mock_similar.return_value = []
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-unique-pkg", "npm")
        assert result["status"] == "available"
        mock_variants.assert_called_once()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_taken_skips_variants(self, mock_pypi, mock_variants, mock_gh):
        """PyPI taken name does not call _check_variants."""
        mock_pypi.return_value = {"status": "taken"}

        result = _check_single_name("requests", "pypi")
        assert result["status"] == "taken"
        mock_variants.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_stdlib_skips_variants_and_github(self, mock_pypi, mock_variants, mock_gh):
        """PyPI stdlib collision skips both _check_variants and GitHub check."""
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("queue", "pypi")
        assert result["status"] == "taken"
        assert result["reason"] == "stdlib"
        mock_variants.assert_not_called()
        mock_pypi.assert_not_called()
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_available_calls_variants_and_github(self, mock_pypi, mock_variants, mock_gh):
        """PyPI available name calls both _check_variants and GitHub check."""
        mock_pypi.return_value = {"status": "available"}
        mock_variants.return_value = []
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-unique-pkg", "pypi")
        assert result["status"] == "available"
        mock_variants.assert_called_once()
        mock_gh.assert_called_once()

    # -- 1B: Skip GitHub when taken --

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_taken_skips_github(self, mock_npm, mock_gh):
        """npm taken name does not call check_github_availability."""
        mock_npm.return_value = {"status": "taken"}

        result = _check_single_name("express", "npm")
        assert result["status"] == "taken"
        assert result["github_count"] is None
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._search_npm_similar")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_available_calls_github(self, mock_npm, mock_variants, mock_similar, mock_gh):
        """npm available name calls check_github_availability."""
        mock_npm.return_value = {"status": "available"}
        mock_variants.return_value = []
        mock_similar.return_value = []
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-unique-pkg", "npm")
        assert result["status"] == "available"
        assert result["github_count"] == 0
        mock_gh.assert_called_once()


class TestUltranormEarlyExit:
    """Tests for early exit in ultranorm variant checking."""

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._generate_ultranorm_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_first_variant_taken_stops_checking(self, mock_pypi, mock_variants, mock_sleep):
        """When the first variant is taken, only 1 check_pypi_availability call is made."""
        mock_variants.return_value = (["var1", "var2", "var3"], False)
        mock_pypi.return_value = {"status": "taken"}

        result = {
            "name": "test-pkg", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "pypi", True, 200)

        assert mock_pypi.call_count == 1
        mock_pypi.assert_called_once_with("var1")
        assert "ultranorm_conflicts" in result
        assert result["ultranorm_conflicts"] == ["var1"]

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._generate_ultranorm_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_no_variants_taken_checks_all(self, mock_pypi, mock_variants, mock_sleep):
        """When no variants are taken, all 3 check_pypi_availability calls are made."""
        mock_variants.return_value = (["var1", "var2", "var3"], False)
        mock_pypi.return_value = {"status": "available"}

        result = {
            "name": "test-pkg", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "pypi", True, 200)

        assert mock_pypi.call_count == 3
        assert "ultranorm_conflicts" not in result

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._generate_ultranorm_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_single_conflict_reported(self, mock_pypi, mock_variants, mock_sleep):
        """The single conflict found via early exit is reported in ultranorm_conflicts."""
        mock_variants.return_value = (["var1", "var2", "var3"], False)
        def pypi_side_effect(name):
            if name == "var2":
                return {"status": "taken"}
            return {"status": "available"}
        mock_pypi.side_effect = pypi_side_effect

        result = {
            "name": "test-pkg", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "pypi", True, 200)

        # var1 available, var2 taken -> break, var3 never checked
        assert mock_pypi.call_count == 2
        assert result["ultranorm_conflicts"] == ["var2"]
        assert result["status"] == "taken"
        assert result["reason"] == "ultranorm"

    @patch("rlsbl.commands.check._generate_ultranorm_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_capped_variants_is_hard_error(self, mock_pypi, mock_variants):
        """When variant generation is capped, result is set to error without checking PyPI."""
        mock_variants.return_value = (["var1", "var2"], True)

        result = {
            "name": "lllllll", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "pypi", True, 200)

        assert result["status"] == "error"
        assert "capped at 64" in result["error"]
        assert "Too many ambiguous characters" in result["error"]
        # PyPI should never be queried when capped
        mock_pypi.assert_not_called()


class TestPyPICaveats:
    """Tests for PyPI-specific caveats in _format_single_result output."""

    def test_pypi_available_no_flag_shows_prohibited_note_and_tip(self):
        """PyPI available without --ultranormalized-variants shows prohibited names note and tip."""
        result = {
            "name": "my-new-pkg", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        assert "PyPI may reject names on its prohibited names list" in output
        assert "--ultranormalized-variants" in output

    def test_pypi_available_with_flag_shows_ultranorm_caveat_no_duplicate(self):
        """PyPI available with --ultranormalized-variants shows ultranorm caveat, no duplicate."""
        result = {
            "name": "my-new-pkg", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
            "ultranorm_caveat": True,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        assert "PyPI may also reject names on its prohibited names list" in output
        # Should NOT contain the tip to use the flag (it was already used)
        assert "--ultranormalized-variants" not in output
        # Should not print the prohibited names note twice
        count = output.count("prohibited names list")
        assert count == 1

    def test_pypi_taken_no_caveats(self):
        """PyPI taken name does not show prohibited names note or ultranorm tip."""
        result = {
            "name": "requests", "registry": "pypi", "status": "taken",
            "variants": [], "github_count": None, "reason": "registered",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        assert "prohibited names list" not in output
        assert "--ultranormalized-variants" not in output

    def test_npm_available_no_pypi_caveats(self):
        """npm available name does not show PyPI-specific caveats."""
        result = {
            "name": "my-new-pkg", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        assert "prohibited names list" not in output
        assert "--ultranormalized-variants" not in output


class TestStepsSummary:
    """Tests for the steps-run summary line in verbose output."""

    def test_pypi_available_summary(self):
        """PyPI available result includes PyPI, stdlib, variants, GitHub repos."""
        result = {
            "name": "my-new-pkg", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        # Find the Checked: line
        checked_line = [l for l in output.split("\n") if l.startswith("Checked:")][0]
        assert "PyPI" in checked_line
        assert "stdlib" in checked_line
        assert "variants" in checked_line
        assert "GitHub repos" in checked_line

    def test_pypi_taken_by_stdlib_summary(self):
        """PyPI taken by stdlib includes only PyPI and stdlib."""
        result = {
            "name": "queue", "registry": "pypi", "status": "taken",
            "variants": None, "github_count": None, "reason": "stdlib",
            "note": "conflicts with Python stdlib module 'queue'",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        checked_line = [l for l in output.split("\n") if l.startswith("Checked:")][0]
        assert "PyPI" in checked_line
        assert "stdlib" in checked_line
        assert "variants" not in checked_line
        assert "GitHub repos" not in checked_line

    def test_npm_available_summary(self):
        """npm available result includes npm, variants, moniker similarity, GitHub repos."""
        result = {
            "name": "my-new-pkg", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
            "moniker_checked": True,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        checked_line = [l for l in output.split("\n") if l.startswith("Checked:")][0]
        assert "npm" in checked_line
        assert "variants" in checked_line
        assert "moniker similarity" in checked_line
        assert "GitHub repos" in checked_line
        assert "stdlib" not in checked_line

    def test_npm_taken_summary(self):
        """npm taken result includes only npm."""
        result = {
            "name": "express", "registry": "npm", "status": "taken",
            "variants": None, "github_count": None, "reason": "registered",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        checked_line = [l for l in output.split("\n") if l.startswith("Checked:")][0]
        assert checked_line == "Checked: npm"

    def test_pypi_with_ultranorm_flag_summary(self):
        """PyPI available with ultranorm flag includes ultranormalization."""
        result = {
            "name": "my-new-pkg", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
            "ultranorm_caveat": True,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        checked_line = [l for l in output.split("\n") if l.startswith("Checked:")][0]
        assert "PyPI" in checked_line
        assert "stdlib" in checked_line
        assert "variants" in checked_line
        assert "GitHub repos" in checked_line
        assert "ultranormalization" in checked_line


class TestMultiNameSummary:
    """Tests for multi-name summary line and batch context note."""

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_two_available_one_taken(self, mock_check, mock_sleep):
        """3 names: 2 available, 1 taken -> summary says '2 available, 1 taken (3 total)'."""
        mock_check.side_effect = [
            {"name": "foo", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "bar", "registry": "npm", "status": "taken",
             "variants": [], "github_count": None},
            {"name": "baz", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd("npm", ["foo", "bar", "baz"], {})
        output = mock_stdout.getvalue()
        assert "Summary: 2 available, 1 taken (3 total)" in output
        # No error count in summary
        assert "error(s)" not in output

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_summary_includes_error_count(self, mock_check, mock_sleep):
        """3 names: 1 error -> summary includes error count."""
        mock_check.side_effect = [
            {"name": "foo", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "bar", "registry": "npm", "status": "error",
             "variants": [], "github_count": None, "error": "timeout"},
            {"name": "baz", "registry": "npm", "status": "taken",
             "variants": [], "github_count": None},
        ]
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd("npm", ["foo", "bar", "baz"], {})
        output = mock_stdout.getvalue()
        assert "Summary: 1 available, 1 taken, 1 error(s) (3 total)" in output

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_all_available_no_error_in_summary(self, mock_check, mock_sleep):
        """3 names: all available -> no error in summary."""
        mock_check.side_effect = [
            {"name": "foo", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "bar", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "baz", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd("npm", ["foo", "bar", "baz"], {})
        output = mock_stdout.getvalue()
        assert "Summary: 3 available, 0 taken (3 total)" in output
        assert "error(s)" not in output

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_default_delay_shows_increase_tip(self, mock_check, mock_sleep):
        """Default delay -> output contains 'Increase --delay if rate limited'."""
        mock_check.side_effect = [
            {"name": "foo", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "bar", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd("npm", ["foo", "bar"], {})
        output = mock_stdout.getvalue()
        assert "Checked with 200ms delay between names." in output
        assert "Increase --delay if rate limited." in output

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_custom_delay_no_increase_tip(self, mock_check, mock_sleep):
        """Custom delay -> output does NOT contain the increase tip."""
        mock_check.side_effect = [
            {"name": "foo", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "bar", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd("npm", ["foo", "bar"], {"delay": "500"})
        output = mock_stdout.getvalue()
        assert "Checked with 500ms delay between names." in output
        assert "Increase --delay if rate limited." not in output


class TestPyPIVariantsInsertions:
    """Tests for separator-insertion variants in get_pypi_variants."""

    def test_pypi_variants_separator_free_generates_insertions(self):
        """get_pypi_variants for a separator-free name generates insertion variants."""
        variants = get_pypi_variants("llmloop")
        # Should include insertion variants like llm-loop, ll-mloop, etc.
        assert "llm-loop" in variants
        assert "ll-mloop" in variants
        assert "l-lmloop" in variants
        # The original name should NOT be in the set
        assert "llmloop" not in variants
        # Should have more than just the stripped form (which equals original)
        assert len(variants) > 0

    def test_pypi_variants_with_separators_no_insertions(self):
        """get_pypi_variants for a name with separators does NOT generate insertion variants."""
        variants = get_pypi_variants("my-pkg")
        # Should have swap variants (underscore, no-separator)
        assert "my_pkg" in variants
        assert "mypkg" in variants
        # Should NOT have insertion variants like m-ypkg, my-p-kg, etc.
        # Insertion variants are only for separator-free names
        insertion_candidates = [v for v in variants if v.count("-") > 1 or v.count("_") > 1]
        assert len(insertion_candidates) == 0

    def test_pypi_variants_insertion_cap(self):
        """For a very long separator-free name, insertion count is capped at 30."""
        # A 50-char name would generate 49 * 3 = 147 insertion variants without cap
        long_name = "a" * 50
        variants = get_pypi_variants(long_name)
        # Count only insertion variants (those with a separator in them)
        insertion_variants = [v for v in variants if "-" in v or "_" in v or "." in v]
        assert len(insertion_variants) <= 30


class TestNpmVariants:
    """Tests for get_npm_variants."""

    def test_npm_variants_with_separators(self):
        """Separator-swap variants are generated for names with separators."""
        variants = get_npm_variants("my-pkg")
        # Should include underscore and dot swaps, and stripped form
        assert "my_pkg" in variants
        assert "my.pkg" in variants
        assert "mypkg" in variants

    def test_npm_variants_separator_free_generates_insertions(self):
        """Insertion variants are generated for separator-free names."""
        variants = get_npm_variants("llmloop")
        assert "llm-loop" in variants
        assert "llm_loop" in variants
        assert "llm.loop" in variants
        assert "l-lmloop" in variants

    def test_npm_variants_excludes_original(self):
        """The original name is excluded from variants."""
        variants = get_npm_variants("mypackage")
        assert "mypackage" not in variants

        variants2 = get_npm_variants("my-pkg")
        assert "my-pkg" not in variants2


class TestCheckVariantsDirectCoverage:
    """Direct tests for _check_variants without going through _check_single_name."""

    def test_sequential_path(self):
        """When _HAS_THREADS is False, sequential execution works."""
        call_log = []

        def fake_check(name):
            call_log.append(name)
            if name == "b-variant":
                return {"status": "taken"}
            return {"status": "available"}

        def fake_variants(name):
            return ["a-variant", "b-variant", "c-variant"]

        with patch("rlsbl.commands.check._HAS_THREADS", False):
            result = _check_variants("orig", fake_check, fake_variants)

        assert result == ["b-variant"]
        assert call_log == ["a-variant", "b-variant", "c-variant"]

    def test_threaded_path(self):
        """Threaded execution returns correct results."""
        def fake_check(name):
            if name in ("taken1", "taken2"):
                return {"status": "taken"}
            return {"status": "available"}

        def fake_variants(name):
            return ["taken1", "avail1", "taken2"]

        with patch("rlsbl.commands.check._HAS_THREADS", True):
            result = _check_variants("orig", fake_check, fake_variants)

        assert sorted(result) == ["taken1", "taken2"]

    def test_excludes_input_name(self):
        """The input name is filtered from variants even if get_variants returns it."""
        def fake_check(name):
            return {"status": "taken"}

        def fake_variants(name):
            return ["orig", "other"]

        with patch("rlsbl.commands.check._HAS_THREADS", False):
            result = _check_variants("orig", fake_check, fake_variants)

        assert "orig" not in result
        assert "other" in result

    def test_exception_in_future_skipped(self):
        """An exception in one variant check does not crash the whole operation."""
        call_count = [0]

        def fake_check(name):
            call_count[0] += 1
            if name == "bad":
                raise RuntimeError("simulated failure")
            if name == "taken":
                return {"status": "taken"}
            return {"status": "available"}

        def fake_variants(name):
            return ["bad", "taken", "avail"]

        with patch("rlsbl.commands.check._HAS_THREADS", True):
            result = _check_variants("orig", fake_check, fake_variants)

        assert "taken" in result
        assert "bad" not in result
