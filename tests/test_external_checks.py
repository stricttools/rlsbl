"""Tests for external check providers (config-declared subprocess checks)."""

import json
import shutil
import subprocess

import pytest

from strictcli import ErrorReporter

from rlsbl import external_checks
from rlsbl.external_checks import (
    ExternalCheckError,
    _compose_structured_argv,
    _guard_name,
    _make_external_check_fn,
    _make_scope_guard_fn,
    _make_structured_check_fn,
    _mypy_scope_conflicts,
    _ruff_scope_conflicts,
    make_external_check_provider,
    validate_external_checks,
)


def _freeform(**overrides):
    """A minimal valid freeform entry."""
    entry = {
        "name": "my-check",
        "kind": "freeform",
        "command": "echo ok",
        "tag": "preflight",
    }
    entry.update(overrides)
    return entry


def _structured(**overrides):
    """A minimal valid structured entry."""
    entry = {
        "name": "mypy-strict",
        "kind": "structured",
        "tool": "mypy",
        "paths": ["claudewheel", "tests"],
        "tag": "preflight",
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# Validation: shared / freeform
# ---------------------------------------------------------------------------


class TestValidateExternalChecks:
    def test_no_key_returns_empty(self):
        assert validate_external_checks({}) == []

    def test_none_value_returns_empty(self):
        assert validate_external_checks({"external_checks": None}) == []

    def test_not_a_list_raises(self):
        with pytest.raises(ExternalCheckError, match="must be a list"):
            validate_external_checks({"external_checks": "bad"})

    def test_entry_not_a_dict_raises(self):
        with pytest.raises(ExternalCheckError, match="must be a dict"):
            validate_external_checks({"external_checks": ["bad"]})

    def test_missing_name_raises(self):
        with pytest.raises(ExternalCheckError, match="missing required key 'name'"):
            validate_external_checks({
                "external_checks": [{
                    "kind": "freeform", "command": "echo ok", "tag": "quality",
                }]
            })

    def test_missing_tag_raises(self):
        with pytest.raises(ExternalCheckError, match="missing required key 'tag'"):
            validate_external_checks({
                "external_checks": [{
                    "name": "test", "kind": "freeform", "command": "echo ok",
                }]
            })

    def test_missing_kind_raises(self):
        with pytest.raises(ExternalCheckError, match="missing required key 'kind'"):
            validate_external_checks({
                "external_checks": [{
                    "name": "test", "command": "echo ok", "tag": "quality",
                }]
            })

    def test_unknown_kind_raises(self):
        with pytest.raises(ExternalCheckError, match="kind 'weird' is invalid"):
            validate_external_checks({
                "external_checks": [_freeform(kind="weird")]
            })

    def test_freeform_missing_command_raises(self):
        with pytest.raises(ExternalCheckError, match="require a non-empty 'command'"):
            validate_external_checks({
                "external_checks": [{
                    "name": "test", "kind": "freeform", "tag": "quality",
                }]
            })

    def test_empty_name_raises(self):
        with pytest.raises(ExternalCheckError, match="must be a non-empty string"):
            validate_external_checks({
                "external_checks": [_freeform(name="")]
            })

    def test_duplicate_name_raises(self):
        with pytest.raises(ExternalCheckError, match="duplicate name"):
            validate_external_checks({
                "external_checks": [
                    _freeform(name="dup", command="echo 1"),
                    _freeform(name="dup", command="echo 2"),
                ]
            })

    def test_depends_on_not_a_list_raises(self):
        with pytest.raises(ExternalCheckError, match="depends_on must be a list"):
            validate_external_checks({
                "external_checks": [_freeform(depends_on="bad")]
            })

    def test_depends_on_bad_entry_raises(self):
        with pytest.raises(ExternalCheckError, match="depends_on.*must be a non-empty string"):
            validate_external_checks({
                "external_checks": [_freeform(depends_on=[""])]
            })

    def test_cwd_not_a_string_raises(self):
        with pytest.raises(ExternalCheckError, match="cwd must be a string"):
            validate_external_checks({
                "external_checks": [_freeform(cwd=123)]
            })

    def test_unknown_key_raises(self):
        with pytest.raises(ExternalCheckError, match="unknown key"):
            validate_external_checks({
                "external_checks": [_freeform(bogus="x")]
            })

    def test_command_on_structured_is_unknown_key(self):
        """'command' is not allowed on structured entries -> unknown key."""
        with pytest.raises(ExternalCheckError, match="unknown key"):
            validate_external_checks({
                "external_checks": [_structured(command="echo ok")]
            })

    def test_tool_on_freeform_is_unknown_key(self):
        with pytest.raises(ExternalCheckError, match="unknown key"):
            validate_external_checks({
                "external_checks": [_freeform(tool="mypy")]
            })

    def test_missing_command_binary_raises(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExternalCheckError, match="command binary not found"):
            validate_external_checks({
                "external_checks": [_freeform(command="nonexistent-binary --check")]
            })

    def test_valid_freeform_passes(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        result = validate_external_checks({
            "external_checks": [_freeform(
                command="mycheck --verify",
                depends_on=["test-suite"],
                cwd="subdir",
            )]
        })
        assert len(result) == 1
        assert result[0]["name"] == "my-check"

    def test_leading_env_assignment_rejected(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(ExternalCheckError, match="must use the env prefix"):
            validate_external_checks({
                "external_checks": [_freeform(command="VAR=1 mycheck --run")]
            })

    def test_env_prefix_form_passes(self):
        result = validate_external_checks({
            "external_checks": [_freeform(command="env VAR=1 mycheck --run")]
        })
        assert len(result) == 1

    @pytest.mark.parametrize("bad_name", [
        "test-*", "test?", "test[a-z]", "Test", "test_suite",
        "1test", "-test", "te st",
    ])
    def test_invalid_name_charset_rejected(self, monkeypatch, bad_name):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(ExternalCheckError, match=r"valid check name"):
            validate_external_checks({
                "external_checks": [_freeform(name=bad_name)]
            })

    def test_valid_name_charset_passes(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        result = validate_external_checks({
            "external_checks": [_freeform(name="my-ext-check-2")]
        })
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Validation: structured
# ---------------------------------------------------------------------------


class TestValidateStructured:
    def test_valid_structured_passes(self):
        result = validate_external_checks({
            "external_checks": [_structured()]
        })
        assert len(result) == 1
        assert result[0]["tool"] == "mypy"

    @pytest.mark.parametrize("tool", ["mypy", "ruff-check", "ruff-format"])
    def test_all_valid_tools(self, tool):
        result = validate_external_checks({
            "external_checks": [_structured(name=f"t-{tool}".replace("_", "-"), tool=tool)]
        })
        assert result[0]["tool"] == tool

    def test_unknown_tool_raises(self):
        with pytest.raises(ExternalCheckError, match="unknown tool 'pylint'"):
            validate_external_checks({
                "external_checks": [_structured(tool="pylint")]
            })

    def test_unknown_tool_message_mentions_freeform(self):
        with pytest.raises(ExternalCheckError, match="freeform"):
            validate_external_checks({
                "external_checks": [_structured(tool="pylint")]
            })

    def test_missing_tool_raises(self):
        entry = _structured()
        del entry["tool"]
        with pytest.raises(ExternalCheckError, match="require a non-empty 'tool'"):
            validate_external_checks({"external_checks": [entry]})

    def test_missing_paths_raises(self):
        entry = _structured()
        del entry["paths"]
        with pytest.raises(ExternalCheckError, match="require a non-empty 'paths'"):
            validate_external_checks({"external_checks": [entry]})

    def test_empty_paths_raises(self):
        with pytest.raises(ExternalCheckError, match="require a non-empty 'paths'"):
            validate_external_checks({
                "external_checks": [_structured(paths=[])]
            })

    def test_paths_bad_element_raises(self):
        with pytest.raises(ExternalCheckError, match=r"paths\[1\] must be"):
            validate_external_checks({
                "external_checks": [_structured(paths=["ok", ""])]
            })

    def test_missing_uv_raises(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExternalCheckError, match="'uv' not found on PATH"):
            validate_external_checks({
                "external_checks": [_structured()]
            })


# ---------------------------------------------------------------------------
# Structured argv composition (byte-for-byte canonical invocations)
# ---------------------------------------------------------------------------


def _write_pyproject(dir_path, body):
    (dir_path / "pyproject.toml").write_text(body)


class TestComposeStructuredArgv:
    def test_mypy_default_dev_group(self, tmp_path):
        _write_pyproject(tmp_path, (
            "[dependency-groups]\ndev = [\"mypy>=2.3.0\", \"ruff>=0.15.20\"]\n"
        ))
        argv = _compose_structured_argv("mypy", ["claudewheel", "tests"], str(tmp_path))
        assert argv == ["uv", "run", "mypy", "claudewheel", "tests"]

    def test_ruff_check_default_dev_group(self, tmp_path):
        _write_pyproject(tmp_path, (
            "[dependency-groups]\ndev = [\"ruff>=0.15.20\"]\n"
        ))
        argv = _compose_structured_argv(
            "ruff-check", ["claudewheel", "tests", "scripts", "docs"], str(tmp_path)
        )
        assert argv == ["uv", "run", "ruff", "check", "claudewheel", "tests", "scripts", "docs"]

    def test_ruff_format_default_dev_group(self, tmp_path):
        _write_pyproject(tmp_path, (
            "[dependency-groups]\ndev = [\"ruff>=0.15.20\"]\n"
        ))
        argv = _compose_structured_argv(
            "ruff-format", ["claudewheel", "tests", "scripts", "docs"], str(tmp_path)
        )
        assert argv == [
            "uv", "run", "ruff", "format", "--check",
            "claudewheel", "tests", "scripts", "docs",
        ]

    def test_no_pyproject_degrades_to_plain(self, tmp_path):
        argv = _compose_structured_argv("mypy", ["src"], str(tmp_path))
        assert argv == ["uv", "run", "mypy", "src"]

    def test_non_default_group_adds_group_flag(self, tmp_path):
        _write_pyproject(tmp_path, (
            "[dependency-groups]\nlint = [\"ruff>=0.15.20\"]\n"
        ))
        argv = _compose_structured_argv("ruff-check", ["src"], str(tmp_path))
        assert argv == ["uv", "run", "--group", "lint", "ruff", "check", "src"]

    def test_optional_dependency_adds_extra_flag(self, tmp_path):
        _write_pyproject(tmp_path, (
            "[project.optional-dependencies]\ntyping = [\"mypy>=2.3.0\"]\n"
        ))
        argv = _compose_structured_argv("mypy", ["src"], str(tmp_path))
        assert argv == ["uv", "run", "--extra", "typing", "mypy", "src"]


# ---------------------------------------------------------------------------
# Competing-scope guards
# ---------------------------------------------------------------------------


class TestMypyScopeConflicts:
    def test_clean_pyproject_passes(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.mypy]\nstrict = true\n")
        assert _mypy_scope_conflicts(str(tmp_path)) == []

    def test_pyproject_files_key_conflicts(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.mypy]\nfiles = \"src\"\n")
        conflicts = _mypy_scope_conflicts(str(tmp_path))
        assert ("pyproject.toml [tool.mypy]", "files") in conflicts

    @pytest.mark.parametrize("key", ["files", "packages", "modules"])
    def test_all_scope_keys_conflict(self, tmp_path, key):
        _write_pyproject(tmp_path, f"[tool.mypy]\n{key} = \"src\"\n")
        conflicts = _mypy_scope_conflicts(str(tmp_path))
        assert any(k == key for _, k in conflicts)

    def test_mypy_ini_files_conflicts(self, tmp_path):
        (tmp_path / "mypy.ini").write_text("[mypy]\nfiles = src\n")
        conflicts = _mypy_scope_conflicts(str(tmp_path))
        assert ("mypy.ini [mypy]", "files") in conflicts

    def test_setup_cfg_mypy_conflicts(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[mypy]\nfiles = src\n")
        conflicts = _mypy_scope_conflicts(str(tmp_path))
        assert ("setup.cfg [mypy]", "files") in conflicts

    def test_no_config_passes(self, tmp_path):
        assert _mypy_scope_conflicts(str(tmp_path)) == []


class TestRuffScopeConflicts:
    def test_clean_pyproject_passes(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.ruff.lint]\nselect = [\"E\"]\n")
        assert _ruff_scope_conflicts(str(tmp_path)) == []

    def test_include_conflicts(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.ruff]\ninclude = [\"src/**\"]\n")
        conflicts = _ruff_scope_conflicts(str(tmp_path))
        assert ("pyproject.toml [tool.ruff]", "include") in conflicts

    def test_extend_include_conflicts(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.ruff]\nextend-include = [\"*.pyi\"]\n")
        conflicts = _ruff_scope_conflicts(str(tmp_path))
        assert ("pyproject.toml [tool.ruff]", "extend-include") in conflicts

    def test_exclude_is_exempt(self, tmp_path):
        _write_pyproject(tmp_path, (
            "[tool.ruff]\nexclude = [\"build\"]\nextend-exclude = [\"x\"]\n"
            "force-exclude = true\n"
        ))
        assert _ruff_scope_conflicts(str(tmp_path)) == []

    def test_ruff_toml_include_conflicts(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("include = [\"src/**\"]\n")
        conflicts = _ruff_scope_conflicts(str(tmp_path))
        assert ("ruff.toml", "include") in conflicts


class TestScopeGuardFn:
    def test_mypy_guard_passes_clean(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.mypy]\nstrict = true\n")
        fn = _make_scope_guard_fn("mypy", None, "mypy-strict")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"

    def test_mypy_guard_fails_on_files(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.mypy]\nfiles = \"src\"\n")
        fn = _make_scope_guard_fn("mypy", None, "mypy-strict")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert "OVERRIDE" in result.message

    def test_ruff_guard_fails_on_include(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.ruff]\ninclude = [\"src/**\"]\n")
        fn = _make_scope_guard_fn("ruff-check", None, "ruff-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert "NARROWS" in result.message

    def test_ruff_guard_passes_clean(self, tmp_path):
        _write_pyproject(tmp_path, "[tool.ruff.lint]\nselect = [\"E\"]\n")
        fn = _make_scope_guard_fn("ruff-format", None, "ruff-format")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# Check function execution (freeform)
# ---------------------------------------------------------------------------


class TestMakeExternalCheckFn:
    def test_passing_command(self, tmp_path):
        fn = _make_external_check_fn("echo hello", None, "test-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"
        assert "hello" in result.message

    def test_failing_command(self, tmp_path):
        fn = _make_external_check_fn("false", None, "test-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert "test-check" in result.message

    def test_command_with_stderr(self, tmp_path):
        fn = _make_external_check_fn(
            "bash -c 'echo bad-stuff >&2; exit 1'", None, "stderr-check",
        )

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert "bad-stuff" in result.message or any(
            "bad-stuff" in d for d in (p.text for p in result.problems)
        )

    def test_cwd_absolute(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        fn = _make_external_check_fn("pwd", str(sub), "cwd-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"
        assert str(sub) in result.message

    def test_cwd_relative(self, tmp_path):
        sub = tmp_path / "rel"
        sub.mkdir()
        fn = _make_external_check_fn("pwd", "rel", "cwd-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"
        assert str(sub) in result.message


# ---------------------------------------------------------------------------
# Timeout routing (live-bug fix: hardcoded 300 -> configured budget)
# ---------------------------------------------------------------------------


class TestTimeoutRouting:
    def test_freeform_honors_env_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLSBL_CHECK_TIMEOUT", "77")
        captured = {}

        def fake_run(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

            class R:
                returncode = 0
                stdout = "ok"
                stderr = ""
            return R()

        monkeypatch.setattr(external_checks.subprocess, "run", fake_run)
        fn = _make_external_check_fn("echo hi", None, "c")

        class FakeCtx:
            project_root = tmp_path

        fn(FakeCtx(), ErrorReporter())
        assert captured["timeout"] == 77

    def test_structured_honors_explicit_timeout(self, tmp_path, monkeypatch):
        captured = {}

        def fake_run(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            captured["argv"] = args[0]

            class R:
                returncode = 0
                stdout = "ok"
                stderr = ""
            return R()

        monkeypatch.setattr(external_checks.subprocess, "run", fake_run)
        _write_pyproject(tmp_path, "[dependency-groups]\ndev = [\"mypy>=2.3.0\"]\n")
        fn = _make_structured_check_fn("mypy", ["src"], None, "c", 1800)

        class FakeCtx:
            project_root = tmp_path

        fn(FakeCtx(), ErrorReporter())
        assert captured["timeout"] == 1800
        assert captured["argv"] == ["uv", "run", "mypy", "src"]
        assert captured.get("shell") is None  # list argv, no shell


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class TestMakeExternalCheckProvider:
    def test_freeform_provider_returns_specs(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        config = {
            "external_checks": [_freeform(
                name="ext-quality-check", command="mycheck --verify",
                tag="quality", depends_on=["test-suite"],
            )]
        }
        provider = make_external_check_provider(lambda: config)
        specs = provider()
        assert len(specs) == 1
        assert specs[0].name == "ext-quality-check"
        assert specs[0].tags == ["quality"]
        assert specs[0].depends_on == ["test-suite"]
        assert specs[0].severity == "error"
        assert specs[0].pure is False

    def test_structured_provider_emits_tool_and_guard(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        config = {"external_checks": [_structured(name="mypy-strict")]}
        provider = make_external_check_provider(lambda: config)
        specs = provider()
        names = {s.name for s in specs}
        assert names == {"mypy-strict", "mypy-strict-scope-guard"}
        guard = next(s for s in specs if s.name == _guard_name("mypy-strict"))
        assert guard.pure is True
        assert guard.fast is True
        assert guard.depends_on == []
        tool = next(s for s in specs if s.name == "mypy-strict")
        assert tool.pure is False

    def test_provider_empty_config(self):
        provider = make_external_check_provider(lambda: {})
        assert provider() == []

    def test_provider_config_read_error(self):
        def exploding_reader():
            raise FileNotFoundError("no config")

        provider = make_external_check_provider(exploding_reader)
        assert provider() == []

    def test_provider_malformed_config_raises(self):
        provider = make_external_check_provider(lambda: {"external_checks": "bad"})
        with pytest.raises(ValueError, match="external checks config error"):
            provider()


# ---------------------------------------------------------------------------
# Integration: external check runs via the app's built-in provider
# ---------------------------------------------------------------------------


class TestExternalCheckIntegration:
    def test_external_check_in_preflight_tag(self, mock_git_repo, monkeypatch):
        import rlsbl

        monkeypatch.chdir(mock_git_repo)
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [_freeform(
                name="ext-test-pass", command="echo 'all good'",
            )],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            from rlsbl.context import ProjectContext
            from pathlib import Path

            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, _impure, exit_code = rlsbl.app.run_checks(ctx, tag_expr="preflight")
            ext_results = [r for r in results if r.name == "ext-test-pass"]
            assert len(ext_results) == 1
            assert ext_results[0].status == "pass"
        finally:
            rlsbl.app.reset_check_provider_cache()

    def test_external_check_failure_aborts(self, mock_git_repo, monkeypatch):
        import rlsbl

        monkeypatch.chdir(mock_git_repo)
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [_freeform(name="ext-test-fail", command="false")],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            from rlsbl.context import ProjectContext
            from pathlib import Path

            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, _impure, exit_code = rlsbl.app.run_checks(ctx, tag_expr="preflight")
            ext_results = [r for r in results if r.name == "ext-test-fail"]
            assert len(ext_results) == 1
            assert ext_results[0].status == "fail"
            assert exit_code != 0
        finally:
            rlsbl.app.reset_check_provider_cache()

    def test_depends_on_ordering(self, mock_git_repo, monkeypatch):
        import rlsbl

        monkeypatch.chdir(mock_git_repo)
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [
                _freeform(name="ext-dep-target", command="echo target"),
                _freeform(
                    name="ext-dep-dependent", command="echo dependent",
                    depends_on=["ext-dep-target"],
                ),
            ],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            from rlsbl.context import ProjectContext
            from pathlib import Path

            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, _impure, exit_code = rlsbl.app.run_checks(ctx, tag_expr="preflight")
            names = [r.name for r in results]
            assert "ext-dep-target" in names
            assert "ext-dep-dependent" in names
            assert names.index("ext-dep-target") < names.index("ext-dep-dependent")
        finally:
            rlsbl.app.reset_check_provider_cache()


# ---------------------------------------------------------------------------
# run_external_preflight_checks
# ---------------------------------------------------------------------------


class TestRunExternalPreflightChecks:
    def test_runs_only_external_checks_not_builtins(self, mock_git_repo, monkeypatch, tmp_path):
        from strictcli import _CheckDef

        import rlsbl
        from rlsbl.external_checks import run_external_preflight_checks
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)
        ext_marker = tmp_path / "ext-ran"
        builtin_marker = tmp_path / "builtin-ran"
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [_freeform(
                name="ext-marker-check", command=f"touch {ext_marker}",
            )],
        }
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        def _builtin_impl(ctx):
            builtin_marker.write_text("ran")
            return ErrorReporter().passed("ok")

        rlsbl.app._check_defs["fake-builtin-preflight"] = _CheckDef(
            name="fake-builtin-preflight", tags=["preflight"], severity="error",
            fast=False, pure=False, needs_network=False, depends_on=[],
            scope="", impl=_builtin_impl,
        )

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, exit_code = run_external_preflight_checks(rlsbl.app, ctx, config)
            assert exit_code == 0
            assert ext_marker.exists()
            assert not builtin_marker.exists()
            names = [r.name for r in results]
            assert "ext-marker-check" in names
            assert "fake-builtin-preflight" not in names
        finally:
            rlsbl.app.reset_check_provider_cache()
            rlsbl.app._check_defs.pop("fake-builtin-preflight", None)

    def test_structured_guard_runs_in_preflight(self, mock_git_repo, monkeypatch):
        """A structured entry's scope guard runs in the customized-hook path."""
        import rlsbl
        from rlsbl.external_checks import run_external_preflight_checks
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        # mypy config carries competing scope -> guard must fail.
        (mock_git_repo / "pyproject.toml").write_text(
            "[tool.mypy]\nfiles = \"src\"\n"
        )
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [_structured(name="mypy-strict", paths=["src"])],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, exit_code = run_external_preflight_checks(rlsbl.app, ctx, config)
            names = [r.name for r in results]
            assert "mypy-strict-scope-guard" in names
            guard = next(r for r in results if r.name == "mypy-strict-scope-guard")
            assert guard.status == "fail"
            assert exit_code != 0
        finally:
            rlsbl.app.reset_check_provider_cache()

    def test_failing_external_check_returns_nonzero(self, mock_git_repo, monkeypatch):
        import rlsbl
        from rlsbl.external_checks import run_external_preflight_checks
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)
        config = {
            "publish_mode": "ci",
            "coverage_unit": "commit",
            "external_checks": [_freeform(name="ext-fail-check", command="false")],
        }
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, exit_code = run_external_preflight_checks(rlsbl.app, ctx, config)
            assert exit_code != 0
            assert any(
                r.name == "ext-fail-check" and r.status == "fail" for r in results
            )
        finally:
            rlsbl.app.reset_check_provider_cache()


# ---------------------------------------------------------------------------
# Provider error handling
# ---------------------------------------------------------------------------


class TestProviderHardError:
    def test_malformed_config_raises_value_error(self):
        provider = make_external_check_provider(lambda: {"external_checks": "bad"})
        with pytest.raises(ValueError, match="external checks config error"):
            provider()


# ---------------------------------------------------------------------------
# Partition wiring
# ---------------------------------------------------------------------------


class TestPartitionWiring:
    """Freeform/structured tool checks are impure (listed, not run under
    pure_only). Structured scope guards are pure/fast (they execute)."""

    def test_external_check_listed_not_executed_under_pure_only(
        self, mock_git_repo, monkeypatch, tmp_path,
    ):
        import rlsbl
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)
        ext_marker = tmp_path / "ext-ran"
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [_freeform(
                name="ext-pure-test", command=f"touch {ext_marker}",
            )],
        }
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, impure_listed, exit_code = rlsbl.app.run_checks(
                ctx, tag_expr="preflight", pure_only=True,
            )
            assert "ext-pure-test" in impure_listed
            assert not ext_marker.exists()
            assert len([r for r in results if r.name == "ext-pure-test"]) == 0
        finally:
            rlsbl.app.reset_check_provider_cache()

    def test_structured_guard_is_pure_and_runs_under_pure_only(
        self, mock_git_repo, monkeypatch,
    ):
        """The scope guard is pure: it executes under pure_only while the
        structured tool check is listed impure."""
        import rlsbl
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [_structured(name="mypy-strict", paths=["src"])],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            results, impure_listed, exit_code = rlsbl.app.run_checks(
                ctx, tag_expr="preflight", pure_only=True,
            )
            # Tool check is impure -> listed, not run.
            assert "mypy-strict" in impure_listed
            # Guard is pure -> runs and appears in results (passes: no
            # competing config in the mock repo).
            guard_results = [
                r for r in results if r.name == "mypy-strict-scope-guard"
            ]
            assert len(guard_results) == 1
            assert guard_results[0].status == "pass"
        finally:
            rlsbl.app.reset_check_provider_cache()
