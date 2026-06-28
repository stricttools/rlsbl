"""Tests for rlsbl.registry version query functions."""

import json
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from conftest import FakeResponse
from rlsbl.registry import (
    query_npm_version,
    query_pypi_version,
    query_go_version,
    query_crates_version,
    query_registry_version,
)


class TestQueryNpmVersion:
    @patch("urllib.request.urlopen")
    def test_found(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"dist-tags": {"latest": "3.2.1"}}
        )
        result = query_npm_version("some-package")
        assert result == {"status": "found", "version": "3.2.1"}

    @patch("urllib.request.urlopen")
    def test_not_found(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://registry.npmjs.org/nonexistent",
            404, "Not Found", {}, None,
        )
        result = query_npm_version("nonexistent")
        assert result == {"status": "not_found"}


class TestQueryPypiVersion:
    @patch("urllib.request.urlopen")
    def test_found(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"info": {"version": "1.0.5"}}
        )
        result = query_pypi_version("requests")
        assert result == {"status": "found", "version": "1.0.5"}

    @patch("urllib.request.urlopen")
    def test_not_found(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://pypi.org/pypi/nonexistent/json",
            404, "Not Found", {}, None,
        )
        result = query_pypi_version("nonexistent")
        assert result == {"status": "not_found"}


class TestQueryGoVersion:
    @patch("urllib.request.urlopen")
    def test_found_strips_v_prefix(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"Version": "v1.21.0", "Time": "2024-01-01T00:00:00Z"}
        )
        result = query_go_version("golang.org/x/net")
        assert result == {"status": "found", "version": "1.21.0"}


class TestQueryCratesVersion:
    @patch("urllib.request.urlopen")
    def test_found_with_user_agent(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"crate": {"max_version": "0.8.4"}}
        )
        result = query_crates_version("serde")
        assert result == {"status": "found", "version": "0.8.4"}
        # Verify the request was made with User-Agent header
        called_req = mock_urlopen.call_args[0][0]
        assert called_req.get_header("User-agent") == "rlsbl-cli"


class TestQueryRegistryVersion:
    @patch("urllib.request.urlopen")
    def test_dispatches_npm(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"dist-tags": {"latest": "2.0.0"}}
        )
        result = query_registry_version("pkg", "npm")
        assert result == {"status": "found", "version": "2.0.0"}

    @patch("urllib.request.urlopen")
    def test_dispatches_pypi(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"info": {"version": "3.0.0"}}
        )
        result = query_registry_version("pkg", "pypi")
        assert result == {"status": "found", "version": "3.0.0"}

    @patch("urllib.request.urlopen")
    def test_dispatches_go(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"Version": "v1.0.0"}
        )
        result = query_registry_version("example.com/mod", "go")
        assert result == {"status": "found", "version": "1.0.0"}

    @patch("urllib.request.urlopen")
    def test_dispatches_cargo(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"crate": {"max_version": "4.0.0"}}
        )
        result = query_registry_version("mycrate", "cargo")
        assert result == {"status": "found", "version": "4.0.0"}

    def test_unknown_registry(self):
        result = query_registry_version("pkg", "unknown")
        assert result["status"] == "error"
        assert "Unknown registry" in result["message"]


class TestNetworkErrors:
    @patch("urllib.request.urlopen")
    def test_url_error_returns_error_status(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("Connection refused")
        result = query_npm_version("some-package")
        assert result["status"] == "error"
        assert "Connection refused" in result["message"]

    @patch("urllib.request.urlopen")
    def test_timeout_returns_error_status(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        result = query_pypi_version("some-package")
        assert result["status"] == "error"
        assert "timed out" in result["message"]

    @patch("urllib.request.urlopen")
    def test_http_500_returns_error_status(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://example.com", 500, "Server Error", {}, None,
        )
        result = query_go_version("example.com/mod")
        assert result["status"] == "error"
        assert "500" in result["message"]

    @patch("urllib.request.urlopen")
    def test_invalid_json_returns_error_status(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(b"not valid json")
        result = query_crates_version("mycrate")
        assert result["status"] == "error"
        assert "Invalid JSON" in result["message"]
