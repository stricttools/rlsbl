"""Tests for PyPI, Go, and GitHub availability checks in rlsbl.commands.check."""

import subprocess
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock, call
from urllib.error import HTTPError, URLError

from conftest import FakeResponse
from rlsbl.commands.check import (
    _apply_ultranorm_check,
    _check_single_name,
    _check_stdlib_collision,
    _format_single_result,
    _format_table_row,
    _generate_ultranorm_variants,
    _normalize_npm_moniker,
    _request_with_backoff,
    _search_npm_similar,
    _ultranormalize,
    check_github_availability,
    check_go_availability,
    check_pypi_availability,
    get_pypi_variants,
    run_cmd,
)


class TestCheckPyPI(unittest.TestCase):
    """Tests for check_pypi_availability and get_pypi_variants."""

    @patch("urllib.request.urlopen")
    def test_pypi_available_on_404(self, mock_urlopen):
        """HTTPError with code 404 means the package name is available."""
        mock_urlopen.side_effect = HTTPError(
            "https://pypi.org/simple/nonexistent/", 404, "Not Found", {}, None
        )
        result = check_pypi_availability("nonexistent")
        self.assertEqual(result["status"], "available")

    @patch("urllib.request.urlopen")
    def test_pypi_taken_on_200(self, mock_urlopen):
        """A 200 response means the package name is taken."""
        mock_urlopen.return_value = FakeResponse({"info": {"name": "requests"}})
        result = check_pypi_availability("requests")
        self.assertEqual(result["status"], "taken")

    @patch("urllib.request.urlopen")
    def test_pypi_error_on_url_error(self, mock_urlopen):
        """A generic URLError (network failure) returns error status."""
        mock_urlopen.side_effect = URLError("Connection refused")
        result = check_pypi_availability("some-package")
        self.assertEqual(result["status"], "error")
        self.assertIn("message", result)

    @patch("urllib.request.urlopen")
    def test_pypi_registered_but_empty_is_taken(self, mock_urlopen):
        """A registered-but-empty package (no releases) should be 'taken'.

        The JSON API returns 404 for these, but the Simple API correctly
        returns 200. This test verifies we use the Simple API.
        """
        # Simple API returns 200 for registered packages even with no releases
        mock_urlopen.return_value = FakeResponse(b"<html></html>")
        result = check_pypi_availability("cost")
        self.assertEqual(result["status"], "taken")
        # Verify the URL uses the Simple API with normalized name
        called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("/simple/cost/", called_url)
        self.assertNotIn("/pypi/", called_url)

    @patch("urllib.request.urlopen")
    def test_pypi_uses_normalized_name_in_url(self, mock_urlopen):
        """The Simple API URL should use PEP 503 normalized names."""
        mock_urlopen.return_value = FakeResponse(b"<html></html>")
        check_pypi_availability("My_Package.Name")
        called_url = mock_urlopen.call_args[0][0].full_url
        # PEP 503: lowercase, runs of [-_.] replaced with single hyphen
        self.assertIn("/simple/my-package-name/", called_url)

    def test_pypi_variants(self):
        """get_pypi_variants generates PEP 503 normalized forms."""
        variants = get_pypi_variants("my-package")
        # Should include underscore and no-separator forms
        self.assertIn("my_package", variants)
        self.assertIn("mypackage", variants)
        # The normalized hyphen form is the same as input, so it should
        # be excluded (the function discards the original name)
        self.assertNotIn("my-package", variants)


class TestCheckGo(unittest.TestCase):
    """Tests for check_go_availability."""

    @patch("urllib.request.urlopen")
    def test_go_exists_on_200(self, mock_urlopen):
        """A 200 response means the Go module exists."""
        mock_urlopen.return_value = FakeResponse(b"<html>pkg page</html>")
        result = check_go_availability("github.com/gorilla/mux")
        self.assertEqual(result["status"], "exists")

    @patch("urllib.request.urlopen")
    def test_go_not_found_on_404(self, mock_urlopen):
        """HTTPError with code 404 means the module is not found."""
        mock_urlopen.side_effect = HTTPError(
            "https://pkg.go.dev/github.com/fake/module", 404, "Not Found", {}, None
        )
        result = check_go_availability("github.com/fake/module")
        self.assertEqual(result["status"], "not_found")
        self.assertIn("note", result)

    @patch("urllib.request.urlopen")
    def test_go_error_on_url_error(self, mock_urlopen):
        """A generic URLError (network failure) returns error status."""
        mock_urlopen.side_effect = URLError("DNS resolution failed")
        result = check_go_availability("github.com/some/module")
        self.assertEqual(result["status"], "error")
        self.assertIn("message", result)


class TestCheckGitHub(unittest.TestCase):
    """Tests for check_github_availability."""

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_github_available_on_zero_count(self, mock_urlopen):
        """Zero total_count means the name is unique on GitHub."""
        mock_urlopen.return_value = FakeResponse({"total_count": 0, "items": []})
        result = check_github_availability("some-unique-name")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["count"], 0)

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_github_exists_on_nonzero_count(self, mock_urlopen):
        """Non-zero total_count means repos with this name exist."""
        mock_urlopen.return_value = FakeResponse({"total_count": 5, "items": []})
        result = check_github_availability("popular-name")
        self.assertEqual(result["status"], "exists")
        self.assertEqual(result["count"], 5)
        self.assertIn("note", result)
        self.assertIn("5", result["note"])

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_github_error_on_exception(self, mock_urlopen):
        """A network error returns error status."""
        mock_urlopen.side_effect = URLError("Connection refused")
        result = check_github_availability("some-name")
        self.assertEqual(result["status"], "error")
        self.assertIn("message", result)


class TestCheckSingleName(unittest.TestCase):
    """Tests for the _check_single_name structured result function."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_available_result(self, mock_npm, mock_gh):
        """Available npm name returns correct structured result."""
        mock_npm.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-new-pkg", "npm")
        self.assertEqual(result["name"], "my-new-pkg")
        self.assertEqual(result["registry"], "npm")
        self.assertEqual(result["status"], "available")
        self.assertIsInstance(result["variants"], list)
        self.assertEqual(result["github_count"], 0)

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_taken_result(self, mock_npm, mock_gh):
        """Taken npm name returns correct structured result; GitHub check skipped."""
        mock_npm.return_value = {"status": "taken"}
        mock_gh.return_value = {"status": "exists", "count": 5, "note": "5 repos"}

        result = _check_single_name("express", "npm")
        self.assertEqual(result["status"], "taken")
        self.assertIsNone(result["github_count"])
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_error_result(self, mock_npm, mock_gh):
        """Error checking npm returns error in result; GitHub skipped."""
        mock_npm.return_value = {"status": "error", "message": "npm CLI not found"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("some-pkg", "npm")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "npm CLI not found")
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_available_result(self, mock_pypi, mock_gh):
        """Available PyPI name returns correct structured result."""
        mock_pypi.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-new-pkg", "pypi")
        self.assertEqual(result["name"], "my-new-pkg")
        self.assertEqual(result["registry"], "pypi")
        self.assertEqual(result["status"], "available")

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
        self.assertEqual(result["status"], "not_found")
        self.assertIn("note", result)
        self.assertEqual(result["registry"], "go")

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_go_availability")
    def test_go_exists_result(self, mock_go, mock_gh):
        """Existing Go module returns 'exists' status; GitHub check skipped."""
        mock_go.return_value = {"status": "exists"}
        mock_gh.return_value = {"status": "exists", "count": 3, "note": "3 repos"}

        result = _check_single_name("github.com/gorilla/mux", "go")
        self.assertEqual(result["status"], "exists")
        self.assertIsNone(result["github_count"])
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_github_error_sets_count_none(self, mock_npm, mock_gh):
        """When GitHub check errors, github_count is None."""
        mock_npm.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "error", "message": "Connection refused"}

        result = _check_single_name("some-pkg", "npm")
        self.assertIsNone(result["github_count"])


class TestCheckTargetRequired(unittest.TestCase):
    """Tests verifying that --target is required for the check command."""

    def test_missing_target_prints_error(self):
        """Running 'rlsbl check-name <name>' without --target should exit with error."""
        from rlsbl import main

        with patch("sys.argv", ["rlsbl", "check-name", "some-name"]):
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 1)

    def test_missing_target_error_message(self):
        """Error message should mention --target is required."""
        from rlsbl import app

        result = app.test(["check-name"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("target", result.stderr)
        self.assertIn("required", result.stderr)


class TestRequestWithBackoff(unittest.TestCase):
    """Tests for the _request_with_backoff retry helper."""

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_successful_request_no_retry(self, mock_urlopen):
        """A successful request returns the response without retrying."""
        fake_resp = FakeResponse(b"OK")
        mock_urlopen.return_value = fake_resp
        result = _request_with_backoff("https://example.com/test")
        self.assertIs(result, fake_resp)
        self.assertEqual(mock_urlopen.call_count, 1)

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
        self.assertIs(result, fake_resp)
        self.assertEqual(mock_urlopen.call_count, 2)
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
        self.assertIs(result, fake_resp)
        self.assertEqual(mock_urlopen.call_count, 3)
        # attempt 0: 2^1 = 2, attempt 1: 2^2 = 4
        self.assertEqual(mock_sleep.call_args_list[0][0][0], 2)
        self.assertEqual(mock_sleep.call_args_list[1][0][0], 4)

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

        with self.assertRaises(HTTPError) as ctx:
            _request_with_backoff("https://example.com/test", max_retries=3)
        self.assertEqual(ctx.exception.code, 429)
        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 3)

    @patch("rlsbl.commands.check.time.sleep")
    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_non_429_http_error_not_retried(self, mock_urlopen, mock_sleep):
        """Non-429 HTTP errors are raised immediately without retrying."""
        error_500 = HTTPError(
            "https://example.com", 500, "Internal Server Error", {}, None
        )
        mock_urlopen.side_effect = error_500

        with self.assertRaises(HTTPError) as ctx:
            _request_with_backoff("https://example.com/test", max_retries=3)
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()


class TestDelayFlag(unittest.TestCase):
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
        self.assertEqual(result.exit_code, 0)
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
        self.assertEqual(result.exit_code, 0)
        # Default delay is 200ms = 0.2s between names
        mock_sleep.assert_called_once_with(0.2)


class TestMultiNameCheck(unittest.TestCase):
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
        self.assertEqual(len(lines), 7)
        self.assertIn("Name", lines[0])
        self.assertIn("Status", lines[0])
        self.assertIn("foo", lines[1])
        self.assertIn("available", lines[1])
        self.assertIn("bar", lines[2])
        self.assertIn("taken", lines[2])
        self.assertIn("baz", lines[3])
        self.assertIn("available", lines[3])

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
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_has_calls([call(0.5), call(0.5)])

    def test_empty_args_prints_error_and_exits(self):
        """No names should print an error to stderr and exit 1."""
        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd("npm", [], {})
            self.assertEqual(ctx.exception.code, 1)
        self.assertIn("missing package name", mock_stderr.getvalue())


class TestStdlibCollision(unittest.TestCase):
    """Tests for _check_stdlib_collision."""

    def test_queue_collides(self):
        """'queue' is a stdlib module and should be detected."""
        result = _check_stdlib_collision("queue")
        self.assertEqual(result, "queue")

    def test_json_collides(self):
        """'json' is a stdlib module and should be detected."""
        result = _check_stdlib_collision("json")
        self.assertEqual(result, "json")

    def test_unique_name_no_collision(self):
        """A name that is not a stdlib module returns None."""
        result = _check_stdlib_collision("myuniquepkg")
        self.assertIsNone(result)

    def test_os_path_no_collision(self):
        """'os-path' normalizes to 'os-path', not 'os' -- should NOT collide."""
        result = _check_stdlib_collision("os-path")
        self.assertIsNone(result)


class TestStdlibCollisionIntegration(unittest.TestCase):
    """Integration test: _check_single_name short-circuits for stdlib collisions."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_stdlib_name_skips_network(self, mock_pypi, mock_gh):
        """Checking 'queue' on pypi returns taken with stdlib note, no HTTP call."""
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("queue", "pypi")
        self.assertEqual(result["status"], "taken")
        self.assertIn("stdlib module", result["note"])
        self.assertIn("queue", result["note"])
        # PyPI availability check should NOT have been called
        mock_pypi.assert_not_called()
        # GitHub check should also be skipped for taken names
        mock_gh.assert_not_called()


class TestUltranormalize(unittest.TestCase):
    """Tests for _ultranormalize."""

    def test_cli(self):
        """l->1 and i->1."""
        self.assertEqual(_ultranormalize("cli"), "c11")

    def test_hello(self):
        """l->1 twice, o->0."""
        self.assertEqual(_ultranormalize("hello"), "he110")

    def test_foo_bar_with_dash(self):
        """Strips dash, o->0 twice."""
        self.assertEqual(_ultranormalize("foo-bar"), "f00bar")

    def test_no_ambiguous_chars(self):
        """No ambiguous chars, just lowercased."""
        self.assertEqual(_ultranormalize("MyPackage"), "mypackage")

    def test_empty_string(self):
        """Empty string returns empty string."""
        self.assertEqual(_ultranormalize(""), "")


class TestGenerateUltranormVariants(unittest.TestCase):
    """Tests for _generate_ultranorm_variants."""

    def test_cli_variants(self):
        """'cli' generates variants with l<->1 and i<->1, excluding itself."""
        variants, capped = _generate_ultranorm_variants("cli")
        self.assertFalse(capped)
        self.assertIn("cl1", variants)
        self.assertIn("c1i", variants)
        self.assertIn("c11", variants)
        self.assertNotIn("cli", variants)

    def test_hello_variants(self):
        """'hello' generates variants with l<->1 and o<->0 substitutions."""
        variants, capped = _generate_ultranorm_variants("hello")
        self.assertFalse(capped)
        # Some expected variants
        self.assertIn("he1lo", variants)
        self.assertIn("hel1o", variants)
        self.assertIn("hell0", variants)
        self.assertIn("he110", variants)
        self.assertNotIn("hello", variants)

    def test_no_ambiguous_chars(self):
        """'abc' has no ambiguous characters, returns empty list."""
        variants, capped = _generate_ultranorm_variants("abc")
        self.assertFalse(capped)
        self.assertEqual(variants, [])

    def test_cap_at_64(self):
        """Name with >6 ambiguous chars hits cap and reports capped=True."""
        # 7 ambiguous chars -> 2^7 = 128 combinations, minus original = 127
        name = "lllllll"
        variants, capped = _generate_ultranorm_variants(name)
        self.assertTrue(capped)
        self.assertLessEqual(len(variants), 64)


class TestNpmMonikerNormalize(unittest.TestCase):
    """Tests for _normalize_npm_moniker."""

    def test_strips_dashes(self):
        """'self-doc' normalizes to 'selfdoc'."""
        self.assertEqual(_normalize_npm_moniker("self-doc"), "selfdoc")

    def test_already_normalized(self):
        """'selfdoc' stays 'selfdoc'."""
        self.assertEqual(_normalize_npm_moniker("selfdoc"), "selfdoc")

    def test_strips_all_separators(self):
        """Dots, underscores, and dashes are all stripped."""
        self.assertEqual(_normalize_npm_moniker("my.package_name"), "mypackagename")

    def test_empty_string(self):
        """Empty string returns empty string."""
        self.assertEqual(_normalize_npm_moniker(""), "")


class TestSearchNpmSimilar(unittest.TestCase):
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
        self.assertEqual(result, ["self-doc"])

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_no_similar_results(self, mock_urlopen):
        """When API returns no results, returns empty list."""
        mock_urlopen.return_value = FakeResponse({"objects": []})
        result = _search_npm_similar("selfdoc")
        self.assertEqual(result, [])

    @patch("rlsbl.commands.check.urllib.request.urlopen")
    def test_network_error_returns_empty(self, mock_urlopen):
        """Network errors degrade gracefully to empty list."""
        mock_urlopen.side_effect = URLError("Connection refused")
        result = _search_npm_similar("selfdoc")
        self.assertEqual(result, [])

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
        self.assertEqual(result, [])


class TestNpmMonikerIntegration(unittest.TestCase):
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
        self.assertEqual(result["status"], "taken")
        self.assertIn("moniker conflict", result["note"])
        self.assertIn("self-doc", result["note"])
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
        self.assertEqual(result["status"], "available")
        self.assertNotIn("note", result)

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
        self.assertEqual(result["status"], "taken")
        mock_similar.assert_not_called()
        mock_gh.assert_not_called()


class TestUltranormIntegration(unittest.TestCase):
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
        self.assertIn("ultranorm_conflicts", result)
        self.assertIn("cl1", result["ultranorm_conflicts"])
        self.assertTrue(result.get("ultranorm_caveat"))

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
        self.assertNotIn("ultranorm_conflicts", result)
        self.assertTrue(result.get("ultranorm_caveat"))

    def test_no_flag_skips_ultranorm(self):
        """Without the flag, no ultranorm checking occurs at all."""
        result = {
            "name": "cli", "registry": "pypi", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "pypi", False, 200)
        self.assertNotIn("ultranorm_conflicts", result)
        self.assertNotIn("ultranorm_caveat", result)

    def test_flag_with_non_pypi_registry_skips(self):
        """Flag with non-pypi registry does no ultranorm checking."""
        result = {
            "name": "cli", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0,
        }
        _apply_ultranorm_check(result, "npm", True, 200)
        self.assertNotIn("ultranorm_conflicts", result)
        self.assertNotIn("ultranorm_caveat", result)


class TestRetryVisibility(unittest.TestCase):
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
        self.assertIn("Rate limited, retrying in", mock_stderr.getvalue())

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
        self.assertIn("Rate limited, retrying in", mock_stderr.getvalue())


class TestReasonField(unittest.TestCase):
    """Tests for the reason field on check result dicts."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_stdlib_collision_reason(self, mock_pypi, mock_gh):
        """PyPI stdlib collision sets reason='stdlib'; GitHub skipped."""
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("queue", "pypi")
        self.assertEqual(result["status"], "taken")
        self.assertEqual(result["reason"], "stdlib")
        mock_pypi.assert_not_called()
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_registered_reason(self, mock_pypi, mock_gh):
        """PyPI registered package sets reason='registered'; GitHub skipped."""
        mock_pypi.return_value = {"status": "taken"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("requests", "pypi")
        self.assertEqual(result["status"], "taken")
        self.assertEqual(result["reason"], "registered")
        mock_gh.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_available_reason_none(self, mock_pypi, mock_gh):
        """PyPI available package has reason=None."""
        mock_pypi.return_value = {"status": "available"}
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("my-unique-pkg-xyz", "pypi")
        self.assertEqual(result["status"], "available")
        self.assertIsNone(result["reason"])

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_registered_reason(self, mock_npm, mock_gh):
        """npm registered package sets reason='registered'; GitHub skipped."""
        mock_npm.return_value = {"status": "taken"}
        mock_gh.return_value = {"status": "exists", "count": 5, "note": "5 repos"}

        result = _check_single_name("express", "npm")
        self.assertEqual(result["status"], "taken")
        self.assertEqual(result["reason"], "registered")
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
        self.assertEqual(result["status"], "taken")
        self.assertEqual(result["reason"], "moniker")
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
        self.assertEqual(result["status"], "available")
        self.assertIsNone(result["reason"])

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_go_availability")
    def test_go_exists_reason(self, mock_go, mock_gh):
        """Go existing module sets reason='registered'; GitHub skipped."""
        mock_go.return_value = {"status": "exists"}
        mock_gh.return_value = {"status": "exists", "count": 3, "note": "3 repos"}

        result = _check_single_name("github.com/gorilla/mux", "go")
        self.assertEqual(result["status"], "exists")
        self.assertEqual(result["reason"], "registered")
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
        self.assertEqual(result["status"], "taken")
        self.assertEqual(result["reason"], "ultranorm")
        self.assertIn("cl1", result["ultranorm_conflicts"])


class TestReasonExplanations(unittest.TestCase):
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
        self.assertIn("standard library modules", mock_stdout.getvalue())

    def test_npm_moniker_explanation(self):
        """npm moniker reason prints punctuation-stripping explanation."""
        result = {
            "name": "selfdoc", "registry": "npm", "status": "taken",
            "variants": [], "github_count": None, "reason": "moniker",
            "note": "moniker conflict with 'self-doc' (npm strips punctuation)",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        self.assertIn("removing dashes, dots, and underscores", mock_stdout.getvalue())

    def test_pypi_ultranorm_explanation(self):
        """PyPI ultranorm reason prints visual similarity explanation."""
        result = {
            "name": "cli", "registry": "pypi", "status": "taken",
            "variants": [], "github_count": None, "reason": "ultranorm",
            "ultranorm_conflicts": ["cl1"],
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        self.assertIn("visually similar", mock_stdout.getvalue())

    def test_pypi_registered_no_explanation(self):
        """PyPI registered reason does NOT print any reason explanation."""
        result = {
            "name": "requests", "registry": "pypi", "status": "taken",
            "variants": [], "github_count": None, "reason": "registered",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        self.assertNotIn("standard library modules", output)
        self.assertNotIn("removing dashes, dots, and underscores", output)
        self.assertNotIn("visually similar", output)

    def test_npm_available_no_explanation(self):
        """npm available result does NOT print any reason explanation."""
        result = {
            "name": "my-unique-pkg", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        self.assertNotIn("standard library modules", output)
        self.assertNotIn("removing dashes, dots, and underscores", output)
        self.assertNotIn("visually similar", output)


class TestShortCircuit(unittest.TestCase):
    """Tests for short-circuit behavior: skip variants and GitHub when taken."""

    # -- 1A: Skip variants when taken --

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_taken_skips_variants(self, mock_npm, mock_variants, mock_gh):
        """npm taken name does not call _check_variants."""
        mock_npm.return_value = {"status": "taken"}

        result = _check_single_name("express", "npm")
        self.assertEqual(result["status"], "taken")
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
        self.assertEqual(result["status"], "available")
        mock_variants.assert_called_once()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_taken_skips_variants(self, mock_pypi, mock_variants, mock_gh):
        """PyPI taken name does not call _check_variants."""
        mock_pypi.return_value = {"status": "taken"}

        result = _check_single_name("requests", "pypi")
        self.assertEqual(result["status"], "taken")
        mock_variants.assert_not_called()

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check._check_variants")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_stdlib_skips_variants_and_github(self, mock_pypi, mock_variants, mock_gh):
        """PyPI stdlib collision skips both _check_variants and GitHub check."""
        mock_gh.return_value = {"status": "available", "count": 0}

        result = _check_single_name("queue", "pypi")
        self.assertEqual(result["status"], "taken")
        self.assertEqual(result["reason"], "stdlib")
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
        self.assertEqual(result["status"], "available")
        mock_variants.assert_called_once()
        mock_gh.assert_called_once()

    # -- 1B: Skip GitHub when taken --

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    def test_npm_taken_skips_github(self, mock_npm, mock_gh):
        """npm taken name does not call check_github_availability."""
        mock_npm.return_value = {"status": "taken"}

        result = _check_single_name("express", "npm")
        self.assertEqual(result["status"], "taken")
        self.assertIsNone(result["github_count"])
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
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["github_count"], 0)
        mock_gh.assert_called_once()


class TestUltranormEarlyExit(unittest.TestCase):
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

        self.assertEqual(mock_pypi.call_count, 1)
        mock_pypi.assert_called_once_with("var1")
        self.assertIn("ultranorm_conflicts", result)
        self.assertEqual(result["ultranorm_conflicts"], ["var1"])

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

        self.assertEqual(mock_pypi.call_count, 3)
        self.assertNotIn("ultranorm_conflicts", result)

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
        self.assertEqual(mock_pypi.call_count, 2)
        self.assertEqual(result["ultranorm_conflicts"], ["var2"])
        self.assertEqual(result["status"], "taken")
        self.assertEqual(result["reason"], "ultranorm")

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

        self.assertEqual(result["status"], "error")
        self.assertIn("capped at 64", result["error"])
        self.assertIn("Too many ambiguous characters", result["error"])
        # PyPI should never be queried when capped
        mock_pypi.assert_not_called()


class TestPyPICaveats(unittest.TestCase):
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
        self.assertIn("PyPI may reject names on its prohibited names list", output)
        self.assertIn("--ultranormalized-variants", output)

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
        self.assertIn("PyPI may also reject names on its prohibited names list", output)
        # Should NOT contain the tip to use the flag (it was already used)
        self.assertNotIn("--ultranormalized-variants", output)
        # Should not print the prohibited names note twice
        count = output.count("prohibited names list")
        self.assertEqual(count, 1)

    def test_pypi_taken_no_caveats(self):
        """PyPI taken name does not show prohibited names note or ultranorm tip."""
        result = {
            "name": "requests", "registry": "pypi", "status": "taken",
            "variants": [], "github_count": None, "reason": "registered",
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        self.assertNotIn("prohibited names list", output)
        self.assertNotIn("--ultranormalized-variants", output)

    def test_npm_available_no_pypi_caveats(self):
        """npm available name does not show PyPI-specific caveats."""
        result = {
            "name": "my-new-pkg", "registry": "npm", "status": "available",
            "variants": [], "github_count": 0, "reason": None,
        }
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _format_single_result(result)
        output = mock_stdout.getvalue()
        self.assertNotIn("prohibited names list", output)
        self.assertNotIn("--ultranormalized-variants", output)


class TestStepsSummary(unittest.TestCase):
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
        self.assertIn("PyPI", checked_line)
        self.assertIn("stdlib", checked_line)
        self.assertIn("variants", checked_line)
        self.assertIn("GitHub repos", checked_line)

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
        self.assertIn("PyPI", checked_line)
        self.assertIn("stdlib", checked_line)
        self.assertNotIn("variants", checked_line)
        self.assertNotIn("GitHub repos", checked_line)

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
        self.assertIn("npm", checked_line)
        self.assertIn("variants", checked_line)
        self.assertIn("moniker similarity", checked_line)
        self.assertIn("GitHub repos", checked_line)
        self.assertNotIn("stdlib", checked_line)

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
        self.assertEqual(checked_line, "Checked: npm")

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
        self.assertIn("PyPI", checked_line)
        self.assertIn("stdlib", checked_line)
        self.assertIn("variants", checked_line)
        self.assertIn("GitHub repos", checked_line)
        self.assertIn("ultranormalization", checked_line)


class TestMultiNameSummary(unittest.TestCase):
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
        self.assertIn("Summary: 2 available, 1 taken (3 total)", output)
        # No error count in summary
        self.assertNotIn("error(s)", output)

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
        self.assertIn("Summary: 1 available, 1 taken, 1 error(s) (3 total)", output)

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
        self.assertIn("Summary: 3 available, 0 taken (3 total)", output)
        self.assertNotIn("error(s)", output)

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
        self.assertIn("Checked with 200ms delay between names.", output)
        self.assertIn("Increase --delay if rate limited.", output)

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
        self.assertIn("Checked with 500ms delay between names.", output)
        self.assertNotIn("Increase --delay if rate limited.", output)


if __name__ == "__main__":
    unittest.main()
