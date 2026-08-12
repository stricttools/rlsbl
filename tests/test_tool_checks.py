"""The path-capable built-in checks: lint, format and type-check.

Each runs one Python tool over a project-declared path list through the
project's own environment, and each is paired with a pure competing-scope
guard.  Configuration is the only input::

    "checks": {"lint": {"paths": ["pkg", "tests"]}}

These tests pin the invocation shape byte for byte, because the point of the
declared path list is that the tool's own config cannot silently change it.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rlsbl import app, tool_checks
from rlsbl.context import ProjectContext
from rlsbl.tool_checks import (
    TOOL_CHECKS,
    ToolCheckConfigError,
    compose_argv,
    guard_name,
    validate_tool_checks_config,
)


def _ctx(root, config):
    return ProjectContext(
        project_root=Path(str(root)), workspace_root=None, config=config,
    )


def _run(name, root, config):
    return app._check_defs[name].impl(_ctx(root, config))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_absent_key_declares_nothing(self):
        assert validate_tool_checks_config({}) == {}
        assert validate_tool_checks_config({"checks": None}) == {}

    def test_block_must_be_a_map(self):
        with pytest.raises(ToolCheckConfigError, match="must be a map"):
            validate_tool_checks_config({"checks": ["lint"]})

    def test_unknown_check_name_is_a_hard_error(self):
        with pytest.raises(ToolCheckConfigError, match="Valid names"):
            validate_tool_checks_config({"checks": {"typecheck": {"paths": ["a"]}}})

    def test_paths_are_required(self):
        with pytest.raises(ToolCheckConfigError, match="non-empty list"):
            validate_tool_checks_config({"checks": {"lint": {}}})

    def test_empty_paths_are_rejected(self):
        with pytest.raises(ToolCheckConfigError, match="non-empty list"):
            validate_tool_checks_config({"checks": {"lint": {"paths": []}}})

    def test_non_string_path_is_rejected(self):
        with pytest.raises(ToolCheckConfigError, match=r"paths\[1\]"):
            validate_tool_checks_config({"checks": {"lint": {"paths": ["a", 2]}}})

    def test_unknown_entry_key_is_rejected(self):
        with pytest.raises(ToolCheckConfigError, match="unknown key"):
            validate_tool_checks_config(
                {"checks": {"lint": {"paths": ["a"], "tool": "ruff"}}}
            )

    def test_cwd_must_be_a_string(self):
        with pytest.raises(ToolCheckConfigError, match="cwd must be a string"):
            validate_tool_checks_config(
                {"checks": {"lint": {"paths": ["a"], "cwd": 3}}}
            )

    def test_a_valid_block_round_trips(self):
        declared = validate_tool_checks_config({
            "checks": {
                "lint": {"paths": ["pkg", "tests"]},
                "type-check": {"paths": ["pkg"], "cwd": "sub"},
            }
        })
        assert sorted(declared) == ["lint", "type-check"]
        assert declared["lint"]["paths"] == ["pkg", "tests"]

    def test_the_config_schema_check_surfaces_a_bad_block(self, tmp_project):
        result = app._check_defs["config-schema"].impl(
            _ctx(tmp_project, {"publish_mode": "ci", "checks": {"nope": {}}})
        )
        assert result.status == "fail"
        assert any("Valid names" in p.text for p in result.problems)


# ---------------------------------------------------------------------------
# Invocation shape
# ---------------------------------------------------------------------------


class TestInvocationShape:
    """The argv is the adapter arm's, verbatim: uv run [group] tool [sub] paths."""

    @pytest.mark.parametrize("name,expected_tail", [
        ("lint", ["ruff", "check", "pkg", "tests"]),
        ("format", ["ruff", "format", "--check", "pkg", "tests"]),
        ("type-check", ["mypy", "pkg", "tests"]),
    ])
    def test_composed_argv(self, tmp_path, name, expected_tail):
        argv = compose_argv(name, ["pkg", "tests"], str(tmp_path))
        assert argv == ["uv", "run", *expected_tail]

    def test_a_non_default_dependency_group_adds_group_flags(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\nlint = ["ruff>=0.15.20"]\n'
        )
        argv = compose_argv("lint", ["pkg"], str(tmp_path))
        assert argv == ["uv", "run", "--group", "lint", "ruff", "check", "pkg"]

    def test_the_default_dev_group_stays_a_bare_uv_run(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["mypy"]\n'
        )
        argv = compose_argv("type-check", ["pkg"], str(tmp_path))
        assert argv == ["uv", "run", "mypy", "pkg"]

    def test_an_optional_extra_adds_extra_flags(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0.1.0"\n\n'
            '[project.optional-dependencies]\nqa = ["mypy"]\n'
        )
        argv = compose_argv("type-check", ["pkg"], str(tmp_path))
        assert argv == ["uv", "run", "--extra", "qa", "mypy", "pkg"]


class TestExecution:
    def test_an_unconfigured_check_skips(self, tmp_project):
        for name in TOOL_CHECKS:
            result = _run(name, tmp_project, {"publish_mode": "ci"})
            assert result.status == "skip", name

    def test_a_configured_check_runs_the_declared_paths(
        self, tmp_project, monkeypatch,
    ):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["cwd"] = kwargs.get("cwd")
            seen["env"] = kwargs.get("env")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(tool_checks.effects, "run", fake_run)
        config = {"publish_mode": "ci", "checks": {"lint": {"paths": ["a", "b"]}}}
        result = _run("lint", tmp_project, config)

        assert result.status == "pass"
        assert seen["argv"][-3:] == ["check", "a", "b"]
        assert seen["cwd"] == str(tmp_project)
        # Invoked through the project's environment, carrying the release
        # context every rlsbl-run check receives.
        assert seen["env"]["RLSBL_PROJECT_ROOT"] == str(tmp_project)

    def test_a_failing_tool_reports_its_own_output(self, tmp_project, monkeypatch):
        def fake_run(argv, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout="a.py:1:1: E501 line too long\n", stderr="",
            )

        monkeypatch.setattr(tool_checks.effects, "run", fake_run)
        result = _run(
            "lint", tmp_project,
            {"publish_mode": "ci", "checks": {"lint": {"paths": ["a"]}}},
        )
        assert result.status == "fail"
        assert any("E501" in p.text for p in result.problems)

    def test_cwd_override_resolves_against_the_project_root(
        self, tmp_project, monkeypatch,
    ):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["cwd"] = kwargs.get("cwd")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(tool_checks.effects, "run", fake_run)
        _run(
            "format", tmp_project,
            {"publish_mode": "ci",
             "checks": {"format": {"paths": ["a"], "cwd": "sub"}}},
        )
        assert seen["cwd"] == str(Path(tmp_project) / "sub")


# ---------------------------------------------------------------------------
# Competing-scope guards
# ---------------------------------------------------------------------------


class TestScopeGuards:
    def test_guard_skips_when_the_check_is_not_configured(self, tmp_project):
        for name in TOOL_CHECKS:
            result = _run(guard_name(name), tmp_project, {"publish_mode": "ci"})
            assert result.status == "skip", name

    def test_mypy_config_scope_fails_the_type_check_guard(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0.1.0"\n\n'
            '[tool.mypy]\nfiles = "src"\n'
        )
        result = _run(
            "type-check-scope-guard", tmp_project,
            {"publish_mode": "ci", "checks": {"type-check": {"paths": ["pkg"]}}},
        )
        assert result.status == "fail"
        assert any("'files'" in p.text for p in result.problems)

    @pytest.mark.parametrize("check_name", ["lint", "format"])
    def test_ruff_include_fails_the_ruff_guards(self, tmp_project, check_name):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0.1.0"\n\n'
            '[tool.ruff]\ninclude = ["src/**"]\n'
        )
        result = _run(
            guard_name(check_name), tmp_project,
            {"publish_mode": "ci", "checks": {check_name: {"paths": ["pkg"]}}},
        )
        assert result.status == "fail"
        assert any("include" in p.text for p in result.problems)

    def test_ruff_exclude_is_exempt(self, tmp_project):
        """An explicit path bypasses exclude -- loud over-inclusion, not
        silent under-scoping."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0.1.0"\n\n'
            '[tool.ruff]\nexclude = ["build"]\n'
        )
        result = _run(
            "lint-scope-guard", tmp_project,
            {"publish_mode": "ci", "checks": {"lint": {"paths": ["pkg"]}}},
        )
        assert result.status == "pass"

    def test_guards_are_declared_pure_and_the_tools_are_not(self):
        import tomllib

        with open(
            Path(__file__).resolve().parent.parent / "rlsbl" / "data" / "checks.toml",
            "rb",
        ) as f:
            defs = tomllib.load(f)["checks"]
        for name in TOOL_CHECKS:
            assert defs[name]["pure"] is False, name
            assert defs[guard_name(name)]["pure"] is True, name


# ---------------------------------------------------------------------------
# The consumer, reproduced by configuration alone
# ---------------------------------------------------------------------------


class TestReproducesTheAdapterConsumer:
    """The one project that used structured external checks, by config only.

    Its three structured entries named mypy / ruff-check / ruff-format with a
    path list each.  The built-ins must compose the identical argv and fire
    the identical scope guards.
    """

    CONSUMER = {
        "publish_mode": "ci",
        "checks": {
            "type-check": {"paths": ["claudewheel", "tests", "docs"]},
            "lint": {"paths": ["claudewheel", "tests", "scripts", "docs"]},
            "format": {"paths": ["claudewheel", "tests", "scripts", "docs"]},
        },
    }

    def test_argvs_match_the_adapter_arm(self, tmp_path):
        assert compose_argv(
            "type-check", ["claudewheel", "tests", "docs"], str(tmp_path),
        ) == ["uv", "run", "mypy", "claudewheel", "tests", "docs"]
        assert compose_argv(
            "lint", ["claudewheel", "tests", "scripts", "docs"], str(tmp_path),
        ) == ["uv", "run", "ruff", "check",
              "claudewheel", "tests", "scripts", "docs"]
        assert compose_argv(
            "format", ["claudewheel", "tests", "scripts", "docs"], str(tmp_path),
        ) == ["uv", "run", "ruff", "format", "--check",
              "claudewheel", "tests", "scripts", "docs"]

    def test_every_scope_guard_fires(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "claudewheel"\nversion = "0.1.0"\n\n'
            '[tool.mypy]\npackages = "claudewheel"\n\n'
            '[tool.ruff]\ninclude = ["claudewheel/**"]\n'
        )
        for name in ("type-check", "lint", "format"):
            result = _run(guard_name(name), tmp_project, self.CONSUMER)
            assert result.status == "fail", name

    def test_the_whole_block_validates(self):
        declared = validate_tool_checks_config(self.CONSUMER)
        assert sorted(declared) == ["format", "lint", "type-check"]

    def test_the_checks_carry_the_preflight_tag(self):
        import tomllib

        with open(
            Path(__file__).resolve().parent.parent / "rlsbl" / "data" / "checks.toml",
            "rb",
        ) as f:
            defs = tomllib.load(f)["checks"]
        for name in ("type-check", "lint", "format"):
            assert "preflight" in defs[name]["tags"], name
            assert "preflight" in defs[guard_name(name)]["tags"], name
