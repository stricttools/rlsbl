"""Tests for external check providers (config-declared subprocess checks)."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from strictcli import ErrorReporter

from rlsbl.external_checks import (
    ExternalCheckError,
    _make_external_check_fn,
    make_external_check_provider,
    validate_external_checks,
)


# ---------------------------------------------------------------------------
# Validation tests
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
                "external_checks": [{"command": "echo ok", "tag": "quality"}]
            })

    def test_missing_command_raises(self):
        with pytest.raises(ExternalCheckError, match="missing required key 'command'"):
            validate_external_checks({
                "external_checks": [{"name": "test", "tag": "quality"}]
            })

    def test_missing_tag_raises(self):
        with pytest.raises(ExternalCheckError, match="missing required key 'tag'"):
            validate_external_checks({
                "external_checks": [{"name": "test", "command": "echo ok"}]
            })

    def test_empty_name_raises(self):
        with pytest.raises(ExternalCheckError, match="must be a non-empty string"):
            validate_external_checks({
                "external_checks": [{"name": "", "command": "echo ok", "tag": "q"}]
            })

    def test_duplicate_name_raises(self):
        with pytest.raises(ExternalCheckError, match="duplicate name"):
            validate_external_checks({
                "external_checks": [
                    {"name": "dup", "command": "echo 1", "tag": "q"},
                    {"name": "dup", "command": "echo 2", "tag": "q"},
                ]
            })

    def test_depends_on_not_a_list_raises(self):
        with pytest.raises(ExternalCheckError, match="depends_on must be a list"):
            validate_external_checks({
                "external_checks": [{
                    "name": "test", "command": "echo ok",
                    "tag": "quality", "depends_on": "bad",
                }]
            })

    def test_depends_on_bad_entry_raises(self):
        with pytest.raises(ExternalCheckError, match="depends_on.*must be a non-empty string"):
            validate_external_checks({
                "external_checks": [{
                    "name": "test", "command": "echo ok",
                    "tag": "quality", "depends_on": [""],
                }]
            })

    def test_cwd_not_a_string_raises(self):
        with pytest.raises(ExternalCheckError, match="cwd must be a string"):
            validate_external_checks({
                "external_checks": [{
                    "name": "test", "command": "echo ok",
                    "tag": "quality", "cwd": 123,
                }]
            })

    def test_missing_command_binary_raises(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExternalCheckError, match="command binary not found"):
            validate_external_checks({
                "external_checks": [{
                    "name": "test",
                    "command": "nonexistent-binary --check",
                    "tag": "quality",
                }]
            })

    def test_valid_config_passes(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        result = validate_external_checks({
            "external_checks": [{
                "name": "my-check",
                "command": "mycheck --verify",
                "tag": "preflight",
                "depends_on": ["test-suite"],
                "cwd": "subdir",
            }]
        })
        assert len(result) == 1
        assert result[0]["name"] == "my-check"

    def test_leading_env_assignment_rejected(self, monkeypatch):
        """A `VAR=1 cmd` command prefix is rejected with env-prefix guidance."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(ExternalCheckError, match="must use the env prefix"):
            validate_external_checks({
                "external_checks": [{
                    "name": "envy",
                    "command": "VAR=1 mycheck --run",
                    "tag": "preflight",
                }]
            })

    def test_env_prefix_form_passes(self):
        """The `env VAR=1 cmd` form validates `env` on PATH and passes.

        `env` is a real binary on PATH, so no monkeypatch is needed -- this
        proves the accepted form resolves the binary correctly.
        """
        result = validate_external_checks({
            "external_checks": [{
                "name": "envy",
                "command": "env VAR=1 mycheck --run",
                "tag": "preflight",
            }]
        })
        assert len(result) == 1
        assert result[0]["name"] == "envy"

    @pytest.mark.parametrize("bad_name", [
        "test-*",     # trailing glob star -> would fnmatch-match test-suite
        "test?",      # single-char glob
        "test[a-z]",  # glob character class
        "Test",       # uppercase not allowed
        "test_suite", # underscore not allowed
        "1test",      # must start with a letter
        "-test",      # must start with a letter, not a hyphen
        "te st",      # whitespace not allowed
    ])
    def test_invalid_name_charset_rejected(self, monkeypatch, bad_name):
        """Names outside [a-z][a-z0-9-]* are hard errors at registration.

        Glob metacharacters (``*?[]``) are the security-relevant case: a name
        like ``test-*`` would pattern-match the built-in ``test-suite`` during
        name-based selection in the customized-hook path.
        """
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(ExternalCheckError, match=r"valid check name"):
            validate_external_checks({
                "external_checks": [{
                    "name": bad_name,
                    "command": "mycheck --run",
                    "tag": "preflight",
                }]
            })

    def test_valid_name_charset_passes(self, monkeypatch):
        """A conforming lowercase-hyphen name passes."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        result = validate_external_checks({
            "external_checks": [{
                "name": "my-ext-check-2",
                "command": "mycheck --run",
                "tag": "preflight",
            }]
        })
        assert len(result) == 1
        assert result[0]["name"] == "my-ext-check-2"


# ---------------------------------------------------------------------------
# Check function execution tests
# ---------------------------------------------------------------------------


class TestMakeExternalCheckFn:
    def test_passing_command(self, tmp_path):
        """A command that exits 0 produces a pass result."""
        fn = _make_external_check_fn("echo hello", None, "test-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"
        assert "hello" in result.message

    def test_failing_command(self, tmp_path):
        """A command that exits non-zero produces a fail result."""
        fn = _make_external_check_fn("false", None, "test-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert "test-check" in result.message

    def test_command_with_stderr(self, tmp_path):
        """A failing command captures stderr in the fail message."""
        fn = _make_external_check_fn(
            "bash -c 'echo bad-stuff >&2; exit 1'",
            None, "stderr-check",
        )

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert "bad-stuff" in result.message or any(
            "bad-stuff" in d for d in (p.text for p in result.problems)
        )

    def test_cwd_absolute(self, tmp_path):
        """When cwd is absolute, the command runs in that directory."""
        sub = tmp_path / "subdir"
        sub.mkdir()
        fn = _make_external_check_fn("pwd", str(sub), "cwd-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"
        assert str(sub) in result.message

    def test_cwd_relative(self, tmp_path):
        """When cwd is relative, it is resolved against project_root."""
        sub = tmp_path / "rel"
        sub.mkdir()
        fn = _make_external_check_fn("pwd", "rel", "cwd-check")

        class FakeCtx:
            project_root = tmp_path

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "pass"
        assert str(sub) in result.message


# ---------------------------------------------------------------------------
# Provider tests
# ---------------------------------------------------------------------------


class TestMakeExternalCheckProvider:
    def test_provider_returns_specs(self, monkeypatch):
        """Provider returns error_check_spec objects from config."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        config = {
            "external_checks": [{
                "name": "ext-quality-check",
                "command": "mycheck --verify",
                "tag": "quality",
                "depends_on": ["test-suite"],
            }]
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
        """No external_checks key -> empty list."""
        provider = make_external_check_provider(lambda: {})
        specs = provider()
        assert specs == []

    def test_provider_config_read_error(self):
        """Config reader raising -> empty list (graceful)."""
        def exploding_reader():
            raise FileNotFoundError("no config")

        provider = make_external_check_provider(exploding_reader)
        specs = provider()
        assert specs == []

    def test_provider_malformed_config_raises(self):
        """Malformed external_checks -> ValueError (hard error)."""
        provider = make_external_check_provider(
            lambda: {"external_checks": "bad"}
        )
        with pytest.raises(ValueError, match="external checks config error"):
            provider()


# ---------------------------------------------------------------------------
# Integration: external check runs via the app's built-in provider
# ---------------------------------------------------------------------------


class TestExternalCheckIntegration:
    """Integration tests exercise the real app by writing config files
    in the test repo's .rlsbl/config.json and resetting the provider
    cache so the built-in provider picks them up.
    """

    def test_external_check_in_preflight_tag(self, mock_git_repo, monkeypatch):
        """An external check tagged 'preflight' runs via run_checks."""
        import rlsbl

        monkeypatch.chdir(mock_git_repo)

        # Write config with an external check
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [{
                "name": "ext-test-pass",
                "command": "echo 'all good'",
                "tag": "preflight",
            }],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))

        # Reset provider cache so cwd change triggers re-materialization
        rlsbl.app.reset_check_provider_cache()

        try:
            from rlsbl.context import ProjectContext
            from pathlib import Path

            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None,
                config=config,
            )
            results, _impure, exit_code = rlsbl.app.run_checks(ctx, tag_expr="preflight")

            ext_results = [r for r in results if r.name == "ext-test-pass"]
            assert len(ext_results) == 1
            assert ext_results[0].status == "pass"
        finally:
            rlsbl.app.reset_check_provider_cache()

    def test_external_check_failure_aborts(self, mock_git_repo, monkeypatch):
        """A failing external check causes non-zero exit from run_checks."""
        import rlsbl

        monkeypatch.chdir(mock_git_repo)

        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [{
                "name": "ext-test-fail",
                "command": "false",
                "tag": "preflight",
            }],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))

        rlsbl.app.reset_check_provider_cache()

        try:
            from rlsbl.context import ProjectContext
            from pathlib import Path

            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None,
                config=config,
            )
            results, _impure, exit_code = rlsbl.app.run_checks(ctx, tag_expr="preflight")

            ext_results = [r for r in results if r.name == "ext-test-fail"]
            assert len(ext_results) == 1
            assert ext_results[0].status == "fail"
            assert exit_code != 0
        finally:
            rlsbl.app.reset_check_provider_cache()

    def test_depends_on_ordering(self, mock_git_repo, monkeypatch):
        """depends_on fields are preserved and used in ordering."""
        import rlsbl

        monkeypatch.chdir(mock_git_repo)

        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [
                {
                    "name": "ext-dep-target",
                    "command": "echo target",
                    "tag": "preflight",
                },
                {
                    "name": "ext-dep-dependent",
                    "command": "echo dependent",
                    "tag": "preflight",
                    "depends_on": ["ext-dep-target"],
                },
            ],
        }
        (rlsbl_dir / "config.json").write_text(json.dumps(config))

        rlsbl.app.reset_check_provider_cache()

        try:
            from rlsbl.context import ProjectContext
            from pathlib import Path

            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None,
                config=config,
            )
            results, _impure, exit_code = rlsbl.app.run_checks(ctx, tag_expr="preflight")

            names = [r.name for r in results]
            assert "ext-dep-target" in names
            assert "ext-dep-dependent" in names

            # Target must appear before dependent in results
            target_idx = names.index("ext-dep-target")
            dependent_idx = names.index("ext-dep-dependent")
            assert target_idx < dependent_idx
        finally:
            rlsbl.app.reset_check_provider_cache()

    def test_missing_command_hard_error_at_registration(self, monkeypatch):
        """A missing command binary errors at registration, not run time."""
        monkeypatch.setattr(shutil, "which", lambda name: None)

        with pytest.raises(ExternalCheckError, match="command binary not found"):
            validate_external_checks({
                "external_checks": [{
                    "name": "bad-cmd",
                    "command": "nonexistent --arg",
                    "tag": "quality",
                }]
            })


# ---------------------------------------------------------------------------
# run_external_preflight_checks: runs ONLY config-declared external checks
# ---------------------------------------------------------------------------


class TestRunExternalPreflightChecks:
    """The helper used in the customized-hook case: it must run config-declared
    external checks but never run built-in preflight-tagged checks."""

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
            "external_checks": [{
                "name": "ext-marker-check",
                "command": f"touch {ext_marker}",
                "tag": "preflight",
            }],
        }

        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        # A fake built-in preflight-tagged check that must NOT run.
        def _builtin_impl(ctx):
            builtin_marker.write_text("ran")
            return ErrorReporter().passed("ok")

        rlsbl.app._check_defs["fake-builtin-preflight"] = _CheckDef(
            name="fake-builtin-preflight",
            tags=["preflight"],
            severity="error",
            fast=False,
            pure=False,
            needs_network=False,
            depends_on=[],
            scope="",
            impl=_builtin_impl,
        )

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None,
                config=config,
            )
            results, exit_code = run_external_preflight_checks(
                rlsbl.app, ctx, config,
            )

            assert exit_code == 0
            assert ext_marker.exists(), "external check should have executed"
            assert not builtin_marker.exists(), (
                "built-in preflight check must NOT run in customized-hook mode"
            )
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
            "coverage_unit": "commit",
            "external_checks": [{
                "name": "ext-fail-check",
                "command": "false",
                "tag": "preflight",
            }],
        }

        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None,
                config=config,
            )
            results, exit_code = run_external_preflight_checks(
                rlsbl.app, ctx, config,
            )
            assert exit_code != 0
            assert any(
                r.name == "ext-fail-check" and r.status == "fail"
                for r in results
            )
        finally:
            rlsbl.app.reset_check_provider_cache()


# ---------------------------------------------------------------------------
# Provider error handling: malformed config is a hard error
# ---------------------------------------------------------------------------


class TestProviderHardError:
    def test_malformed_config_raises_value_error(self):
        """A malformed external_checks section raises ValueError from provider."""
        provider = make_external_check_provider(
            lambda: {"external_checks": "bad"}
        )
        with pytest.raises(ValueError, match="external checks config error"):
            provider()


# ---------------------------------------------------------------------------
# Partition wiring: external checks (pure=false) listed under pure_only
# ---------------------------------------------------------------------------


class TestPartitionWiring:
    """External checks are hardcoded pure=false. Under pure_only=True they
    appear in the impure_listed set and never execute."""

    def test_external_check_listed_not_executed_under_pure_only(
        self, mock_git_repo, monkeypatch, tmp_path,
    ):
        """pure_only=True lists external checks as impure, does not execute them."""
        import rlsbl
        from rlsbl.context import ProjectContext
        from pathlib import Path

        monkeypatch.chdir(mock_git_repo)

        ext_marker = tmp_path / "ext-ran"
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "coverage_unit": "commit",
            "external_checks": [{
                "name": "ext-pure-test",
                "command": f"touch {ext_marker}",
                "tag": "preflight",
            }],
        }

        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(config))
        rlsbl.app.reset_check_provider_cache()

        try:
            ctx = ProjectContext(
                project_root=Path(str(mock_git_repo)),
                workspace_root=None,
                config=config,
            )
            results, impure_listed, exit_code = rlsbl.app.run_checks(
                ctx, tag_expr="preflight", pure_only=True,
            )

            # External check must appear in impure_listed
            assert "ext-pure-test" in impure_listed

            # External check must NOT have executed
            assert not ext_marker.exists(), (
                "external check should not execute under pure_only=True"
            )

            # External check must NOT appear in results
            ext_results = [r for r in results if r.name == "ext-pure-test"]
            assert len(ext_results) == 0
        finally:
            rlsbl.app.reset_check_provider_cache()
