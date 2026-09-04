"""Tests for external check providers (config-declared subprocess checks)."""

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from strictcli import ErrorReporter

from rlsbl import external_checks, tool_checks
from rlsbl.external_checks import (
    ExternalCheckError,
    _make_external_check_fn,
    make_external_check_provider,
    validate_external_checks,
)

from conftest import workspace_toml


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
    """The RETIRED structured entry shape, for the migration-error tests."""
    entry = {
        "name": "mypy-strict",
        "kind": "structured",
        "tool": "mypy",
        "paths": ["claudewheel", "tests"],
        "tag": "preflight",
    }
    entry.update(overrides)
    return entry


class TestStructuredKindIsRetired:
    """`kind = "structured"` is a hard error that names its replacement.

    The path-list tool invocation moved into three built-in checks
    (``lint`` / ``format`` / ``type-check``) configured under the top-level
    ``checks`` key.  A config that still declares the old shape must not be
    silently ignored, and must not merely say "invalid kind" -- it must say
    where the setting went.
    """

    def test_structured_entry_is_rejected(self):
        with pytest.raises(ExternalCheckError, match='kind "structured" was'):
            validate_external_checks({"external_checks": [_structured()]})

    @pytest.mark.parametrize("tool,replacement", [
        ("mypy", "type-check"),
        ("ruff-check", "lint"),
        ("ruff-format", "format"),
    ])
    def test_the_error_names_the_replacing_check(self, tool, replacement):
        with pytest.raises(ExternalCheckError) as exc:
            validate_external_checks({
                "external_checks": [_structured(tool=tool)]
            })
        assert replacement in str(exc.value)
        assert '"checks"' in str(exc.value)

    def test_the_error_carries_the_declared_paths_into_the_example(self):
        with pytest.raises(ExternalCheckError) as exc:
            validate_external_checks({
                "external_checks": [_structured(paths=["pkg", "docs"])]
            })
        assert '"pkg", "docs"' in str(exc.value)

    def test_an_unknown_tool_still_reaches_the_migration_error(self):
        """The retirement is decided by `kind`, not by whether the tool
        happened to be one rlsbl knew."""
        with pytest.raises(ExternalCheckError, match='kind "structured" was'):
            validate_external_checks({
                "external_checks": [_structured(tool="pylint")]
            })

    def test_any_other_kind_points_at_the_same_place(self):
        with pytest.raises(ExternalCheckError) as exc:
            validate_external_checks({
                "external_checks": [_freeform(kind="weird")]
            })
        assert "only kind is 'freeform'" in str(exc.value)
        assert "type-check" in str(exc.value)

    def test_the_provider_surfaces_it_as_a_hard_error(self):
        provider = make_external_check_provider(
            lambda: {"external_checks": [_structured()]}
        )
        with pytest.raises(ValueError, match="external checks config error"):
            provider()


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
# Blank lines in tool output
#
# Real linters (ruff, mypy with a summary block, pytest) separate findings with
# blank lines.  Every line used to be handed to reporter.error() verbatim, and
# the reporter rejects an empty problem text -- so a failing linter aborted the
# whole check run with an internal ValueError and no attribution instead of
# showing its findings.  Blank lines are filtered before the reporter sees them.
# ---------------------------------------------------------------------------


class TestBlankLineSeparatedOutput:
    """A tool whose findings are separated by blank lines is reported, not raised."""

    #: stdout of a fake linter: two findings separated by a blank line, plus a
    #: trailing blank line before the summary -- the shape ruff actually emits.
    FAKE_LINTER = (
        "bash -c '"
        "printf \"app.py:1:1: F401 unused import\\n\\n"
        "app.py:9:5: F841 unused variable\\n\\n"
        "Found 2 errors.\\n\"; exit 1'"
    )

    FAKE_LINTER_STDERR = (
        "bash -c '"
        "printf \"lib.py:3:1: E501 line too long\\n\\nFound 1 error.\\n\" >&2; exit 1'"
    )

    def test_freeform_blank_line_stdout_is_reported(self, tmp_path):
        fn = _make_external_check_fn(self.FAKE_LINTER, None, "fake-lint")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        texts = [p.text for p in result.problems]
        assert "app.py:1:1: F401 unused import" in texts
        assert "app.py:9:5: F841 unused variable" in texts
        assert "Found 2 errors." in texts
        assert "" not in texts
        assert "fake-lint" in result.message

    def test_freeform_blank_line_stderr_is_reported(self, tmp_path):
        fn = _make_external_check_fn(self.FAKE_LINTER_STDERR, None, "fake-lint-err")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        texts = [p.text for p in result.problems]
        assert "lib.py:3:1: E501 line too long" in texts
        assert "" not in texts


    def test_whitespace_only_output_still_attributes(self, tmp_path, monkeypatch):
        """Output that is nothing but blank lines still names the check."""
        def fake_run(*args, **kwargs):
            class R:
                returncode = 3
                stdout = "\n \n\n"
                stderr = ""
            return R()

        monkeypatch.setattr(external_checks.subprocess, "run", fake_run)
        fn = _make_external_check_fn("whatever", None, "blank-check")

        class FakeCtx:
            project_root = tmp_path
            config = {}

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert "blank-check" in result.message
        assert all(p.text.strip() for p in result.problems)


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
            build_release_flags(False, False, False, check_timeout=13),
        )

        class ReleaseCtx:
            project_root = tmp_path
            config = live

        self._run_spec("my-check", specs, ReleaseCtx())
        assert captured["timeout"] == 13



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
            results, _listed, exit_code = run_external_preflight_checks(
                rlsbl.app, ctx, config,
            )
            assert exit_code == 0
            assert ext_marker.exists()
            assert not builtin_marker.exists()
            names = [r.name for r in results]
            assert "ext-marker-check" in names
            assert "fake-builtin-preflight" not in names
        finally:
            rlsbl.app.reset_check_provider_cache()
            rlsbl.app._check_defs.pop("fake-builtin-preflight", None)


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
            results, _listed, exit_code = run_external_preflight_checks(
                rlsbl.app, ctx, config,
            )
            assert exit_code != 0
            assert any(
                r.name == "ext-fail-check" and r.status == "fail" for r in results
            )
        finally:
            rlsbl.app.reset_check_provider_cache()


class TestBothRehearsalBranchesPartitionTheSameWay:
    """The customized-hook branch previews like the other one, or not at all.

    Before this, a preview whose pre-release hook was customized LISTED every
    external check straight from the config and executed nothing -- so the
    checks that could safely have run under --dry-run (a structured entry's
    competing-scope guard is pure) were silently skipped in exactly the repos
    that had opted into owning their own testing.  Both branches now go
    through one call with the same purity partition.
    """

    def test_pure_only_is_forwarded_to_the_check_runner(self, mock_git_repo,
                                                        monkeypatch):
        import rlsbl
        from rlsbl.external_checks import run_external_preflight_checks
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)
        config = {
            "publish_mode": "ci",
            "external_checks": [_freeform(name="ext-partition", command="true")],
        }
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        seen = []
        real = rlsbl.app.run_checks

        def spy(ctx, **kwargs):
            seen.append(kwargs.get("pure_only"))
            return real(ctx, **kwargs)

        monkeypatch.setattr(rlsbl.app, "run_checks", spy)
        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None, config=config,
            )
            run_external_preflight_checks(
                rlsbl.app, ctx, config, pure_only=True,
            )
        finally:
            rlsbl.app.reset_check_provider_cache()

        assert seen and all(v is True for v in seen), (
            "the rehearsal's purity partition must reach the check runner, or "
            "a preview executes impure external checks for real"
        )

    def test_impure_external_check_is_listed_not_executed_under_pure_only(
        self, mock_git_repo, monkeypatch, tmp_path,
    ):
        import rlsbl
        from rlsbl.external_checks import run_external_preflight_checks
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)
        marker = tmp_path / "ext-ran-in-preview"
        config = {
            "publish_mode": "ci",
            "external_checks": [_freeform(
                name="ext-preview", command=f"touch {marker}",
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
            results, listed, exit_code = run_external_preflight_checks(
                rlsbl.app, ctx, config, pure_only=True,
            )
        finally:
            rlsbl.app.reset_check_provider_cache()

        assert "ext-preview" in listed
        assert not any(r.name == "ext-preview" for r in results)
        assert not marker.exists(), "a preview executed an impure external check"
        assert exit_code == 0

    def test_neither_branch_lists_from_raw_config(self):
        """The listing must come from the check runner's partition.

        Reading ``validate_external_checks`` and printing every entry is how
        the customized-hook branch used to preview: it named entries the
        runner would have SCOPED OUT, and it never executed the pure ones.
        """
        import inspect

        from rlsbl.commands import release as release_mod

        source = inspect.getsource(release_mod)
        assert "for entry in validate_external_checks(" not in source, (
            "the rehearsal is listing straight from config again instead of "
            "from the runner's purity partition"
        )


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

    def test_unreleased_repo_exports_empty_last_tag(self, mock_git_repo):
        """Nothing released yet -> LAST_TAG is empty, RANGE is HEAD."""
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

    def test_released_repo_exports_tag_and_range(self, mock_git_repo):
        """A released standalone repo gets its tag NAME and a commit range.

        The version is selected from the release record and translated into a tag for
        ``RLSBL_LAST_TAG``; the range is expressed as the released COMMIT, so
        a check receives a range that resolves even where the tag does not.
        """
        from rlsbl.context import ProjectContext
        from pathlib import Path

        from conftest import archive_release, git_head, release_record_dir

        self._tag(mock_git_repo, "v1.2.3")
        released = git_head(mock_git_repo)
        archive_release(release_record_dir(mock_git_repo), "1.2.3", released)
        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)), workspace_root=None, config={},
        )
        fn = _make_external_check_fn(_env_probe_command(), None, "env-probe")
        result = fn(ctx, ErrorReporter())

        assert result.status == "pass"
        assert "tag=[v1.2.3]" in result.message
        assert f"range={released}..HEAD" in result.message


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
        """N external checks must not mean N release record reads."""
        from rlsbl.context import ProjectContext
        from pathlib import Path

        from conftest import archive_release, git_head, release_record_dir
        import rlsbl.release_record as release_record_mod

        self._tag(mock_git_repo, "v2.0.0")
        archive_release(release_record_dir(mock_git_repo), "2.0.0", git_head(mock_git_repo))
        calls = []
        real = release_record_mod.range_anchor

        def counting(releases_dir, **kwargs):
            calls.append(releases_dir)
            return real(releases_dir, **kwargs)

        monkeypatch.setattr(release_record_mod, "range_anchor", counting)
        ctx = ProjectContext(
            project_root=Path(str(mock_git_repo)), workspace_root=None, config={},
        )
        for name in ("a", "b", "c"):
            fn = _make_external_check_fn("true", None, name)
            fn(ctx, ErrorReporter())

        assert len(calls) == 1, calls


class TestReleaseContextEnvFailureIsHard:
    """A release record read that cannot answer must not become "no release".

    ``RLSBL_LAST_TAG=""`` is a SIGNAL everything rests on: it means "this
    project has never been released", and a check reading it takes the
    first-release branch. Producing it from a swallowed exception makes an
    interrogable repo look brand-new -- so a coverage or diff check silently
    validates the whole history instead of the unreleased range.
    """

    def _ctx(self, repo):
        from pathlib import Path

        from rlsbl.context import ProjectContext

        return ProjectContext(
            project_root=Path(str(repo)), workspace_root=None, config={},
        )

    def test_an_undecidable_ancestry_is_a_hard_error(self, mock_git_repo):
        """A truncated history cannot answer, so nothing is exported at all."""
        from conftest import archive_release, release_record_dir
        from rlsbl.errors import ReleaseRecordError

        # An anchor whose object this repository does not have: git answers
        # "I cannot say", which is not "not released".
        archive_release(release_record_dir(mock_git_repo), "1.0.0", "c" * 40)
        with pytest.raises(ReleaseRecordError) as exc:
            tool_checks.release_context_env(self._ctx(mock_git_repo))
        assert "unshallow" in str(exc.value)

    def test_an_unexpected_failure_propagates(self, mock_git_repo, monkeypatch):
        import rlsbl.release_record as release_record_mod

        def boom(*args, **kwargs):
            raise RuntimeError("git went sideways")

        monkeypatch.setattr(release_record_mod, "range_anchor", boom)
        with pytest.raises(RuntimeError, match="sideways"):
            tool_checks.release_context_env(self._ctx(mock_git_repo))

    def test_a_genuinely_unreleased_repo_still_reports_empty(self, mock_git_repo):
        """The no-release case does not raise -- it returns None -- and stays ""."""
        env = tool_checks.release_context_env(self._ctx(mock_git_repo))
        assert env["RLSBL_LAST_TAG"] == ""
        assert env["RLSBL_UNRELEASED_RANGE"] == "HEAD"


class TestReleaseContextEnvWorkspace:
    """In a monorepo the release record and the tag glob are the project's own."""

    def _workspace(self, repo):
        """An explicit-mode workspace with one releasable named `alpha`."""
        (repo / ".rlsbl-monorepo").mkdir(exist_ok=True)
        (repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            workspace_toml('[[projects]]\n'
            'name = "alpha"\n'
            'path = "alpha"\n'
            'releasable = "alpha"\n'
            '\n'
            '[[releasables]]\n'
            'name = "alpha"\n'
            'tag_format = "{name}@v{version}"\n')
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

        from conftest import archive_release, git_head, release_record_dir

        pkg = self._workspace(mock_git_repo)
        subprocess.run(["git", "tag", "v9.9.9"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)
        released = git_head(mock_git_repo)
        archive_release(
            release_record_dir(None, releasable_dir=(
                mock_git_repo / ".rlsbl-monorepo" / "releasables" / "alpha"
            )),
            "1.0.0", released,
        )

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
        assert f"range={released}..HEAD" in result.message
        assert f"root={pkg}" in result.message
