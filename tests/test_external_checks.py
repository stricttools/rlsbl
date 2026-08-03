"""Tests for external check providers (config-declared subprocess checks)."""

import json
import shutil
import subprocess
from types import SimpleNamespace

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
# Timeout routing
#
# The budget is resolved per RUN from the live check context's config, never
# bound when the spec is built. The provider materializes specs from a fresh
# on-disk config read, so a bound budget could never see --check-timeout.
# ---------------------------------------------------------------------------


def _capture_timeout(monkeypatch, captured):
    """Patch subprocess.run in external_checks to record its timeout kwarg."""
    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        captured["argv"] = args[0] if args else None
        captured["shell"] = kwargs.get("shell")

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(external_checks.subprocess, "run", fake_run)


class TestTimeoutRouting:
    def test_freeform_honors_the_config_key(self, tmp_path, monkeypatch):
        captured = {}
        _capture_timeout(monkeypatch, captured)
        fn = _make_external_check_fn("echo hi", None, "c")

        class FakeCtx:
            project_root = tmp_path
            config = {"check_timeout": 77}

        fn(FakeCtx(), ErrorReporter())
        assert captured["timeout"] == 77

    def test_freeform_falls_back_to_shipped_default(self, tmp_path, monkeypatch):
        from rlsbl.utils import DEFAULT_CHECK_TIMEOUT

        captured = {}
        _capture_timeout(monkeypatch, captured)
        fn = _make_external_check_fn("echo hi", None, "c")

        class FakeCtx:
            project_root = tmp_path
            config = {}

        fn(FakeCtx(), ErrorReporter())
        assert captured["timeout"] == DEFAULT_CHECK_TIMEOUT

    def test_structured_honors_the_config_key(self, tmp_path, monkeypatch):
        captured = {}
        _capture_timeout(monkeypatch, captured)
        _write_pyproject(tmp_path, "[dependency-groups]\ndev = [\"mypy>=2.3.0\"]\n")
        fn = _make_structured_check_fn("mypy", ["src"], None, "c")

        class FakeCtx:
            project_root = tmp_path
            config = {"check_timeout": 1800}

        fn(FakeCtx(), ErrorReporter())
        assert captured["timeout"] == 1800
        assert captured["argv"] == ["uv", "run", "mypy", "src"]
        assert captured.get("shell") is None  # list argv, no shell


class TestCheckTimeoutFlagReachesExternalChecks:
    """``--check-timeout`` must govern config-declared external checks too.

    Regression: the provider was registered with a FRESH on-disk config read
    and bound each check's budget at materialization time, so the release's
    in-memory config -- the only place ``apply_timeout_overrides`` writes the
    flag -- was never consulted. An external check ran on the shipped default
    no matter what ``--check-timeout`` said.
    """

    def _run_spec(self, name, specs, ctx):
        """Invoke a provider-built spec the way strictcli does (impl takes ctx)."""
        spec = next(s for s in specs if s.name == name)
        return spec._impl(ctx)

    def test_freeform_external_check_honors_the_flag(self, tmp_path, monkeypatch):
        from rlsbl.commands.release.shared import (
            apply_timeout_overrides,
            build_release_flags,
        )

        captured = {}
        _capture_timeout(monkeypatch, captured)

        # What the provider sees on disk: no override, only the config key.
        on_disk = {"external_checks": [_freeform(command="echo ok")]}
        monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/echo")
        specs = make_external_check_provider(lambda: dict(on_disk))()

        # What the release holds in memory: the same config with --check-timeout
        # applied, handed to the check as ctx.config.
        live = dict(on_disk)
        apply_timeout_overrides(
            live,
            build_release_flags(False, True, False, False, check_timeout=13),
        )

        class ReleaseCtx:
            project_root = tmp_path
            config = live

        self._run_spec("my-check", specs, ReleaseCtx())
        assert captured["timeout"] == 13

    def test_structured_external_check_honors_the_flag(self, tmp_path, monkeypatch):
        from rlsbl.commands.release.shared import (
            apply_timeout_overrides,
            build_release_flags,
        )

        captured = {}
        _capture_timeout(monkeypatch, captured)
        _write_pyproject(tmp_path, "[dependency-groups]\ndev = [\"mypy>=2.3.0\"]\n")

        on_disk = {"external_checks": [_structured()]}
        monkeypatch.setattr(shutil, "which", lambda b: f"/usr/bin/{b}")
        specs = make_external_check_provider(lambda: dict(on_disk))()

        live = dict(on_disk)
        apply_timeout_overrides(
            live,
            build_release_flags(False, True, False, False, check_timeout=21),
        )

        class ReleaseCtx:
            project_root = tmp_path
            config = live

        self._run_spec("mypy-strict", specs, ReleaseCtx())
        assert captured["timeout"] == 21


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


# ---------------------------------------------------------------------------
# Release-context env injection (RLSBL_PROJECT_ROOT / LAST_TAG / RANGE)
# ---------------------------------------------------------------------------


def _env_probe_command():
    """A freeform command that prints the injected release-context vars."""
    return (
        'printf "root=%s tag=[%s] range=%s" '
        '"$RLSBL_PROJECT_ROOT" "$RLSBL_LAST_TAG" "$RLSBL_UNRELEASED_RANGE"'
    )


class TestReleaseContextEnv:
    """Both check kinds run with the release context in their environment."""

    def _tag(self, repo, tag):
        subprocess.run(["git", "tag", tag], cwd=str(repo), check=True)

    def test_untagged_repo_exports_empty_last_tag(self, mock_git_repo):
        """No tag yet -> LAST_TAG is the empty string, RANGE is HEAD."""
        from rlsbl.context import ProjectContext
        from pathlib import Path

        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)), workspace_root=None, config={},
        )
        fn = _make_external_check_fn(_env_probe_command(), None, "env-probe")
        reporter = ErrorReporter()
        result = fn(ctx, reporter)

        assert result.status == "pass"
        assert f"root={mock_git_repo}" in result.message
        assert "tag=[]" in result.message
        assert "range=HEAD" in result.message

    def test_tagged_repo_exports_tag_and_range(self, mock_git_repo):
        """A tagged standalone repo gets <tag> and <tag>..HEAD."""
        from rlsbl.context import ProjectContext
        from pathlib import Path

        self._tag(mock_git_repo, "v1.2.3")
        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)), workspace_root=None, config={},
        )
        fn = _make_external_check_fn(_env_probe_command(), None, "env-probe")
        result = fn(ctx, ErrorReporter())

        assert result.status == "pass"
        assert "tag=[v1.2.3]" in result.message
        assert "range=v1.2.3..HEAD" in result.message

    def test_structured_check_gets_the_same_env(self, mock_git_repo, monkeypatch):
        """The structured (shell-free) path injects the identical env."""
        from rlsbl.context import ProjectContext
        from pathlib import Path

        self._tag(mock_git_repo, "v0.9.0")
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        # Rebind the NAME in external_checks only -- patching the shared
        # effect chokepoint would also swallow the git calls the env resolver
        # itself makes.
        monkeypatch.setattr(
            external_checks, "effects", SimpleNamespace(run=fake_run),
        )
        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)), workspace_root=None, config={},
        )
        fn = _make_structured_check_fn("mypy", ["src"], None, "mypy-strict")
        fn(ctx, ErrorReporter())

        env = captured["env"]
        assert env["RLSBL_PROJECT_ROOT"] == str(mock_git_repo)
        assert env["RLSBL_LAST_TAG"] == "v0.9.0"
        assert env["RLSBL_UNRELEASED_RANGE"] == "v0.9.0..HEAD"

    def test_cwd_override_still_learns_the_project_root(self, mock_git_repo):
        """An entry with a cwd override has no other way to find the root."""
        from rlsbl.context import ProjectContext
        from pathlib import Path

        sub = mock_git_repo / "sub"
        sub.mkdir()
        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)), workspace_root=None, config={},
        )
        fn = _make_external_check_fn(_env_probe_command(), "sub", "env-probe")
        result = fn(ctx, ErrorReporter())

        assert result.status == "pass"
        assert f"root={mock_git_repo}" in result.message

    def test_resolved_once_per_run_not_once_per_check(self, mock_git_repo, monkeypatch):
        """N external checks must not mean N `git describe` calls."""
        from rlsbl.context import ProjectContext
        from pathlib import Path

        self._tag(mock_git_repo, "v2.0.0")
        calls = []
        real = external_checks.get_last_version_tag

        def counting(tag_glob="v*", **kwargs):
            calls.append(tag_glob)
            return real(tag_glob, **kwargs)

        monkeypatch.setattr(external_checks, "get_last_version_tag", counting)
        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)), workspace_root=None, config={},
        )
        for name in ("a", "b", "c"):
            fn = _make_external_check_fn("true", None, name)
            fn(ctx, ErrorReporter())

        assert len(calls) == 1, calls


class TestReleaseContextEnvWorkspace:
    """In a monorepo the tag glob is the project's own, not the bare `v*`."""

    def _workspace(self, repo):
        """An explicit-mode workspace with one releasable named `alpha`."""
        (repo / ".rlsbl-monorepo").mkdir(exist_ok=True)
        (repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            '[[projects]]\n'
            'name = "alpha"\n'
            'path = "alpha"\n'
            'releasable = "alpha"\n'
            '\n'
            '[[releasables]]\n'
            'name = "alpha"\n'
            'tag_format = "{name}@v{version}"\n'
        )
        changes = repo / ".rlsbl-monorepo" / "releasables" / "alpha" / "changes"
        changes.mkdir(parents=True, exist_ok=True)
        (changes / "unreleased.jsonl").write_text("")
        pkg = repo / "alpha"
        pkg.mkdir(exist_ok=True)
        (pkg / "package.json").write_text('{"name":"alpha","version":"1.0.0"}')
        return pkg

    def test_uses_the_releasable_tag_glob(self, mock_git_repo):
        """`alpha@v*` resolves alpha's own tag, ignoring an unrelated `v*`."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_releasables, load_workspace
        from pathlib import Path

        pkg = self._workspace(mock_git_repo)
        subprocess.run(["git", "tag", "v9.9.9"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)

        projects = load_workspace(str(mock_git_repo))
        releasables = load_releasables(str(mock_git_repo), projects)
        ctx = WorkspaceCheckContext(
            project_root=Path(str(pkg)),
            workspace_root=Path(str(mock_git_repo)),
            config={},
            projects=projects,
            graph=None,
            releasables=releasables,
        )
        fn = _make_external_check_fn(_env_probe_command(), None, "env-probe")
        result = fn(ctx, ErrorReporter())

        assert result.status == "pass"
        assert "tag=[alpha@v1.0.0]" in result.message
        assert "range=alpha@v1.0.0..HEAD" in result.message
        assert f"root={pkg}" in result.message
