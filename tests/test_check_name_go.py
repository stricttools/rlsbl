"""``check-name --target go``: an offline check of the Go package name a candidate implies.

The go target asks nothing of any network. It judges the identifier a caller
would type (``testsandbox.Run``), in three bands:

- invalid: not a Go identifier, a keyword, or the blank identifier -- the Go
  spec forbids each as a package name;
- taken: the name of a Go standard-library package, read from the committed
  ``go list std`` table, so every file importing both needs an alias;
- discouraged: legal, but against Effective Go (uppercase, underscores) or
  shadowing a predeclared identifier.

Everything else is available. Every test here runs with the HTTP layer, the
subprocess layer and raw sockets patched to fail, so a network or toolchain
call anywhere in the path is a test failure rather than a silent pass.
"""

import json
import socket
from unittest.mock import patch

import pytest

import rlsbl
from rlsbl.commands.check import (
    _check_single_name,
    _format_single_result,
    run_cmd,
)


def _forbidden(*_args, **_kwargs):
    raise AssertionError("the go check-name target must not contact any network or run any process")


@pytest.fixture(autouse=True)
def no_network():
    """Fail the test if the go check reaches HTTP, a subprocess, or a socket."""
    with patch("rlsbl.effects.urlopen", side_effect=_forbidden), \
         patch("rlsbl.commands.check._request_with_backoff", side_effect=_forbidden), \
         patch("rlsbl.effects.run", side_effect=_forbidden), \
         patch("subprocess.run", side_effect=_forbidden), \
         patch("urllib.request.urlopen", side_effect=_forbidden), \
         patch.object(socket.socket, "connect", _forbidden), \
         patch("rlsbl.commands.check.time.sleep", side_effect=_forbidden):
        yield


def _payload(stdout):
    return json.loads(stdout)["payload"]


class TestInvalidPackageName:
    """A name the Go spec does not accept as a package clause is invalid."""

    def test_dashed_name_is_invalid_and_suggests_nothing(self):
        result = _check_single_name("go-toml-edit", "go")
        assert result["status"] == "invalid"
        assert result["reason"] == "not-identifier"
        assert "package clause" in result["note"]
        assert "does not choose" in result["note"]
        assert result.get("structured_conflicts", []) == []

    @pytest.mark.parametrize("name", ["9lives", "has.dot", "has space", ""])
    def test_non_identifiers_are_invalid(self, name):
        result = _check_single_name(name, "go")
        assert result["status"] == "invalid"
        assert result["reason"] == "not-identifier"

    @pytest.mark.parametrize("name", ["func", "type", "range", "go", "package"])
    def test_keywords_are_invalid(self, name):
        result = _check_single_name(name, "go")
        assert result["status"] == "invalid"
        assert result["reason"] == "keyword"

    def test_blank_identifier_is_invalid(self):
        result = _check_single_name("_", "go")
        assert result["status"] == "invalid"
        assert result["reason"] == "blank"

    def test_invalid_exits_1_and_says_so(self, capsys):
        exit_code, payload = run_cmd("go", ["go-toml-edit"], {"delay": "200"})
        out = capsys.readouterr().out
        assert exit_code == 1
        assert payload[0]["status"] == "invalid"
        assert '"go-toml-edit" is not a valid Go package name.' in out


class TestStdlibCollision:
    """A name equal to a standard-library package's name is taken."""

    def test_testing_is_taken_by_the_stdlib(self):
        result = _check_single_name("testing", "go")
        assert result["status"] == "taken"
        assert result["reason"] == "stdlib"
        assert result["structured_conflicts"] == [{"name": "testing", "rule": "go-stdlib"}]

    def test_the_colliding_path_is_named(self):
        result = _check_single_name("json", "go")
        assert result["structured_conflicts"] == [{"name": "encoding/json", "rule": "go-stdlib"}]
        assert "encoding/json" in result["note"]

    def test_every_colliding_path_is_named(self):
        result = _check_single_name("template", "go")
        paths = [c["name"] for c in result["structured_conflicts"]]
        assert paths == ["html/template", "text/template"]

    def test_major_version_suffix_names_the_preceding_element(self):
        """math/rand/v2 declares package rand, not v2."""
        rand = [c["name"] for c in _check_single_name("rand", "go")["structured_conflicts"]]
        assert "math/rand/v2" in rand
        assert _check_single_name("v2", "go")["status"] == "available"

    @pytest.mark.parametrize("name", ["abi", "bytealg", "poll"])
    def test_internal_packages_do_not_collide(self, name):
        """Nothing outside the stdlib can import internal/..., so no alias is ever forced."""
        assert _check_single_name(name, "go")["status"] == "available"

    def test_stdlib_collision_exits_1(self, capsys):
        exit_code, _ = run_cmd("go", ["context"], {"delay": "200"})
        out = capsys.readouterr().out
        assert exit_code == 1
        assert '"context" is taken as a Go package name.' in out
        assert "context" in out


class TestDiscouragedPackageName:
    """Legal names Effective Go advises against, or that shadow a predeclared identifier."""

    @pytest.mark.parametrize("name,reason", [
        ("testSandbox", "uppercase"),
        ("test_sandbox", "underscore"),
        ("len", "predeclared"),
        ("string", "predeclared"),
    ])
    def test_discouraged(self, name, reason):
        result = _check_single_name(name, "go")
        assert result["status"] == "discouraged"
        assert result["reason"] == reason

    def test_every_problem_is_listed_in_the_note(self):
        result = _check_single_name("Test_Sandbox", "go")
        assert result["reason"] == "uppercase"
        assert "uppercase" in result["note"]
        assert "underscore" in result["note"]


class TestAvailable:
    """A lowercase identifier that no stdlib package uses is available."""

    @pytest.mark.parametrize("name", ["testsandbox", "base64x", "rlsbl"])
    def test_clean_names_are_available(self, name):
        result = _check_single_name(name, "go")
        assert result["status"] == "available"
        assert result["reason"] is None

    def test_available_exits_0(self, capsys):
        exit_code, _ = run_cmd("go", ["testsandbox"], {"delay": "200"})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert '"testsandbox" is available as a Go package name.' in out
        assert "pkg.go.dev" not in out

    def test_human_output_names_the_offline_checks(self, capsys):
        _format_single_result(_check_single_name("testsandbox", "go"))
        out = capsys.readouterr().out
        checked = [line for line in out.splitlines() if line.startswith("Checked:")]
        assert checked == ["Checked: Go package-name rules, Go standard library (offline)"]


class TestBatchAndJson:
    """Several names, and machine mode, stay offline and share the npm/PyPI shape."""

    def test_batch_does_not_sleep_between_names(self, capsys):
        """The delay exists for rate-limited registries; the fixture fails any sleep."""
        exit_code, payload = run_cmd("go", ["testsandbox", "testing", "go-x"], {"delay": "200"})
        out = capsys.readouterr().out
        assert exit_code == 1
        assert [p["status"] for p in payload] == ["available", "taken", "invalid"]
        assert "Summary: 1 available, 1 taken, 1 invalid (3 total)" in out
        assert "delay" not in out

    @patch("rlsbl._variadic_args", ["json"])
    def test_json_payload_matches_the_shared_shape(self):
        result = rlsbl.app.test(["check-name", "--target", "go", "--json"])
        assert result.exit_code == 1
        data = _payload(result.stdout)
        assert set(data) == {
            "name", "target", "status", "reason", "structured_conflicts",
            "rule_sentences", "exit_code", "note",
        }
        assert data["target"] == "go"
        assert data["status"] == "taken"
        assert data["reason"] == "stdlib"
        assert data["structured_conflicts"] == [{"name": "encoding/json", "rule": "go-stdlib"}]
        assert set(data["rule_sentences"]) == {"go-stdlib"}
        assert data["exit_code"] == 1

    @patch("rlsbl._variadic_args", ["testsandbox"])
    def test_json_available_is_the_minimal_object(self):
        result = rlsbl.app.test(["check-name", "--target", "go", "--json"])
        assert result.exit_code == 0
        assert _payload(result.stdout) == {
            "name": "testsandbox", "target": "go", "status": "available",
            "reason": None, "structured_conflicts": [], "rule_sentences": {},
            "exit_code": 0,
        }


class TestGithubTargetRemoved:
    """GitHub repository names are owner-scoped: there is no github target."""

    @patch("rlsbl._variadic_args", ["x"])
    def test_check_name_refuses_github(self):
        result = rlsbl.app.test(["check-name", "--target", "github"])
        assert result.exit_code == 1
        assert "npm, pypi, go" in result.stderr
        assert "go, github" not in result.stderr

    def test_monorepo_check_names_refuses_github(self):
        result = rlsbl.app.test(["monorepo", "check-names", "--target", "github"])
        assert result.exit_code == 1
        assert "npm, pypi, go" in result.stderr
        assert "go, github" not in result.stderr

    def test_no_github_checker_remains(self):
        import rlsbl.commands.check as check

        assert not hasattr(check, "check_github_availability")
        assert not hasattr(check, "check_go_availability")
        assert not hasattr(check, "_NON_TARGET_REGISTRY_DISPLAY")


class TestDiscouragedExitCodeIsDocumented:
    """A discouraged Go name exits 1, like a taken one, and the help says so:
    an agent reads --help before invoking and must not read exit 1 on a legal
    name as a failure of the command itself."""

    def test_a_discouraged_name_exits_one(self):
        result = rlsbl.app.test(["check-name", "--target", "go", "My_Pkg"])
        assert result.exit_code == 1, result.stdout

    def test_the_help_states_the_exit_codes(self):
        help_text = rlsbl.app._commands["check-name"].help
        assert "Exits 0 when every name is available" in help_text
        assert "a discouraged Go name exits 1 even though Go accepts it" in help_text
        assert "2 when any check errored" in help_text
