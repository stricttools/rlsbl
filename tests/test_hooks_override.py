"""Tests for pre-release hook override of built-in tests and lint."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from rlsbl.commands.release import (
    _compute_content_hash,
    _get_pre_release_template_hashes,
    _is_hook_effectively_empty,
)
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


def _setup_project(tmp_path, hook_body=None):
    """Create a minimal project.  Optionally writes a pre-release hook."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release.\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (tmp_path / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False, "targets": ["npm"]}) + "\n"
    )
    if hook_body is not None:
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-release.sh"
        hook.write_text(hook_body)
        hook.chmod(0o755)


# The current scaffold template content (V2, after the comment update).
_CURRENT_TEMPLATE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "# Project-specific pre-release checks.\n"
    "# When this hook is customized (any change from the scaffold template),\n"
    "# built-in tests and lint are skipped -- the hook is expected to handle them.\n"
    "# Add custom validation here, e.g.:\n"
    "#   - Run tests and lint with project-specific flags\n"
    "#   - Check for uncommitted documentation\n"
    "#   - Verify external service connectivity\n"
    "#   - Run integration tests not covered by the test suite\n"
)

# The original scaffold template content (V1, before the comment update).
_V1_TEMPLATE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "# Project-specific pre-release checks.\n"
    "# Built-in checks (tests, lint) run automatically before this hook.\n"
    "# Add custom validation here, e.g.:\n"
    "#   - Check for uncommitted documentation\n"
    "#   - Verify external service connectivity\n"
    "#   - Run integration tests not covered by the test suite\n"
)


# ---------------------------------------------------------------------------
# _is_hook_effectively_empty tests
# ---------------------------------------------------------------------------

class TestIsHookEffectivelyEmpty:
    """Tests for the _is_hook_effectively_empty utility function."""

    def test_missing_hook_file_returns_true(self, tmp_path):
        """A nonexistent hook file is effectively empty."""
        hook_path = str(tmp_path / ".rlsbl" / "hooks" / "pre-release.sh")
        assert _is_hook_effectively_empty(hook_path) is True

    def test_current_template_returns_true(self, tmp_path):
        """A hook with the current scaffold template content is effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_CURRENT_TEMPLATE)
        assert _is_hook_effectively_empty(str(hook)) is True

    def test_v1_template_returns_true(self, tmp_path):
        """A hook with the V1 (old) scaffold template content is effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_V1_TEMPLATE)
        assert _is_hook_effectively_empty(str(hook)) is True

    def test_template_with_trailing_whitespace_returns_true(self, tmp_path):
        """Trailing whitespace differences are ignored (same as hook_hashes pattern)."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_CURRENT_TEMPLATE + "\n\n  \t  \n")
        assert _is_hook_effectively_empty(str(hook)) is True

    def test_customized_hook_returns_false(self, tmp_path):
        """A hook with custom lines appended is NOT effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text(_CURRENT_TEMPLATE + "uv run pytest\n")
        assert _is_hook_effectively_empty(str(hook)) is False

    def test_completely_different_hook_returns_false(self, tmp_path):
        """A fully custom hook is NOT effectively empty."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text("#!/bin/bash\nset -e\nmake test\nmake lint\n")
        assert _is_hook_effectively_empty(str(hook)) is False

    def test_empty_file_returns_false(self, tmp_path):
        """An empty file is NOT a template match -- it's customized (someone cleared it)."""
        hook = tmp_path / "pre-release.sh"
        hook.write_text("")
        assert _is_hook_effectively_empty(str(hook)) is False


class TestTemplateHashes:
    """Tests for _get_pre_release_template_hashes and _compute_content_hash."""

    def test_hash_set_contains_current_template(self):
        """The hash set includes the current template file."""
        hashes = _get_pre_release_template_hashes()
        current_hash = _compute_content_hash(_CURRENT_TEMPLATE)
        assert current_hash in hashes

    def test_hash_set_contains_v1_template(self):
        """The hash set includes the V1 template."""
        hashes = _get_pre_release_template_hashes()
        v1_hash = _compute_content_hash(_V1_TEMPLATE)
        assert v1_hash in hashes

    def test_hash_set_has_at_least_two_entries(self):
        """We track at least two template versions."""
        hashes = _get_pre_release_template_hashes()
        assert len(hashes) >= 2

    def test_compute_content_hash_strips_trailing_whitespace(self):
        """Trailing whitespace is stripped before hashing."""
        a = _compute_content_hash("hello\n")
        b = _compute_content_hash("hello\n\n  \t  ")
        assert a == b

    def test_compute_content_hash_different_content(self):
        """Different content produces different hashes."""
        a = _compute_content_hash("hello\n")
        b = _compute_content_hash("world\n")
        assert a != b


# ---------------------------------------------------------------------------
# Release flow integration tests
# ---------------------------------------------------------------------------

class TestBuiltinTestsSkippedWhenHookCustomized:
    """Tests that the release flow skips built-in tests/lint when the
    pre-release hook has been customized."""

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_skips_tests_and_lint_when_hook_customized(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _tag_local,
        _push,
        _remote_exists,
        tmp_project,
        capsys,
    ):
        """When the pre-release hook is customized, preflight checks
        are skipped."""
        _setup_project(
            tmp_project,
            hook_body=_V1_TEMPLATE + "uv run pytest\n",
        )
        mock_run.side_effect = ["", "0", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp, \
             patch("rlsbl.app.run_checks", return_value=([], 0)) as mock_checks:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        mock_checks.assert_not_called()

        captured = capsys.readouterr()
        # Under --dry-run the preflight gate is not executed; the honest
        # interim contract prints a pointer to the standalone check command.
        assert (
            "preflight checks not executed under --dry-run; run "
            "`rlsbl check --tag preflight` to evaluate the gate"
        ) in captured.out

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_skips_preflight_in_dry_run_when_hook_is_template(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _tag_local,
        _push,
        _remote_exists,
        tmp_project,
        capsys,
    ):
        """When the pre-release hook is the unmodified scaffold template,
        preflight checks are skipped in dry-run mode."""
        _setup_project(tmp_project, hook_body=_V1_TEMPLATE)
        mock_run.side_effect = ["", "0", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp, \
             patch("rlsbl.app.run_checks", return_value=([], 0)) as mock_checks:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        # In dry-run mode, preflight checks are not executed; the honest
        # interim contract prints a pointer to the standalone check command.
        mock_checks.assert_not_called()

        captured = capsys.readouterr()
        assert (
            "preflight checks not executed under --dry-run; run "
            "`rlsbl check --tag preflight` to evaluate the gate"
        ) in captured.out

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_skips_preflight_in_dry_run_when_hook_missing(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _tag_local,
        _push,
        _remote_exists,
        tmp_project,
    ):
        """When no pre-release hook exists, preflight checks are still
        skipped in dry-run mode."""
        _setup_project(tmp_project, hook_body=None)  # no hook
        mock_run.side_effect = ["", "0", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp, \
             patch("rlsbl.app.run_checks", return_value=([], 0)) as mock_checks:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "quiet": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        # In dry-run mode, preflight checks are skipped
        mock_checks.assert_not_called()

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_hook_still_runs_when_customized(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _tag_local,
        _push,
        _remote_exists,
        tmp_project,
    ):
        """Even when preflight checks are skipped, the pre-release hook itself
        still executes."""
        _setup_project(
            tmp_project,
            hook_body="#!/bin/bash\necho 'custom tests'\n",
        )
        mock_run.side_effect = ["", "0", "", ""]

        with patch("rlsbl.commands.release.subprocess") as mock_sp, \
             patch("rlsbl.app.run_checks", return_value=([], 0)) as mock_checks:
            mock_sp.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            mock_sp.CalledProcessError = subprocess.CalledProcessError
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            from rlsbl.commands.release import run_cmd

            run_cmd(
                _rc(), {"dry-run": True, "quiet": True, "yes": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"private": False, "pipelines": {}},
                ),
            )

        # Preflight checks skipped (hook is customized)
        mock_checks.assert_not_called()

        # The pre-release hook was still called via subprocess.run
        assert mock_sp.run.call_count >= 1
        hook_calls = [
            c for c in mock_sp.run.call_args_list
            if len(c[0][0]) > 1 and "pre-release.sh" in c[0][0][1]
        ]
        assert len(hook_calls) == 1


# ---------------------------------------------------------------------------
# External checks x hook-customization x dry-run interactions (Phase 1.8)
# ---------------------------------------------------------------------------

_FULL_FLOW_PATCHES = (
    patch("rlsbl.commands.release._run_release_mutating"),
    patch("rlsbl.commands.release.resolve_release_targets", return_value=[]),
    patch("rlsbl.commands.release.run", return_value=""),
    patch("rlsbl.commands.release.commit_files", return_value=True),
    patch("rlsbl.commands.release.generate_changelog"),
    patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}}),
    patch("rlsbl.commands.release.validate_release_targets", return_value="npm"),
    patch("rlsbl.commands.release.validate_pipeline_config"),
    patch("rlsbl.commands.release.validate_config_integrity"),
    patch("rlsbl.commands.release.validate_ota_mode"),
    patch("rlsbl.commands.release.validate_gh_cli"),
    patch("rlsbl.commands.release.validate_gh_push_access"),
    patch("rlsbl.commands.release.validate_clean_tree", return_value=set()),
    patch("rlsbl.commands.release.validate_branch_and_remote", return_value="main"),
    patch("rlsbl.commands.release.resolve_monorepo_context", return_value=(None, None, False, False, None)),
    patch("rlsbl.commands.release.validate_changelog_state", return_value=None),
    patch("rlsbl.commands.release.validate_blog_body", return_value=(None, None)),
    patch("rlsbl.commands.release._abort_on_scaffold_conflicts"),
    patch("rlsbl.commands.release.resolve_target_paths", return_value={}),
    patch("rlsbl.commands.release.compute_release_version", return_value=("1.0.0", "1.0.1", "patch", "v1.0.1")),
    patch("rlsbl.commands.release.extract_changelog_entry_from_text", return_value="- test"),
    patch("rlsbl.commands.release.parse_porcelain_paths", return_value=set()),
    patch("rlsbl.commands.release.build_hook_env", return_value={}),
    patch("rlsbl.commands.release.get_hook_timeout", return_value=30),
    patch("rlsbl.commands.release._run_strictcli_schema_dump"),
    patch("rlsbl.commands.release._run_selfdoc_gen"),
    patch("rlsbl.commands.release._run_selfdoc_check"),
    patch("rlsbl.commands.release._run_selfblog_post_generate"),
    patch("rlsbl.commands.release.commit_files_if_changed"),
)


class TestExternalChecksVsHookCustomization:
    """The core Phase 1.8 fix: config-declared external checks run in the
    preflight step regardless of pre-release hook customization, and are not
    double-run when the hook is not customized."""

    def _setup_with_external_check(self, tmp_project, hook_body):
        """Minimal npm project with one config-declared external check and a
        pre-release hook. Returns the config dict (passed via ProjectContext)."""
        _setup_project(tmp_project, hook_body=hook_body)
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "pre-checks.sh").write_text("#!/bin/bash\nexit 0\n")
        (hooks_dir / "pre-checks.sh").chmod(0o755)
        return {
            "private": False,
            "pipelines": {},
            "external_checks": [{
                "name": "ext-preflight-check",
                "command": "true",
                "tag": "preflight",
            }],
        }

    def _run_and_track(self, tmp_project, config, hook_customized):
        """Run run_cmd (non-dry-run) tracking every run_checks (tag, name_glob).

        Cleans up the registered external check afterward so it does not leak
        into rlsbl.app._check_defs for other tests.
        """
        import rlsbl

        calls = []

        def tracking_run_checks(ctx, *, tag_expr=None, name_glob=None, **kw):
            calls.append((tag_expr, name_glob))
            return ([], 0)

        started = [p.start() for p in _FULL_FLOW_PATCHES]
        try:
            with (
                patch("rlsbl.commands.release.is_hook_customized", return_value=hook_customized),
                patch("rlsbl.app.run_checks", side_effect=tracking_run_checks),
            ):
                from rlsbl.commands.release import run_cmd

                run_cmd(
                    _rc(),
                    {"quiet": True, "yes": True},
                    ctx=ProjectContext(
                        project_root=Path(str(tmp_project)),
                        workspace_root=None,
                        config=config,
                    ),
                )
        finally:
            for p in _FULL_FLOW_PATCHES:
                p.stop()
            rlsbl.app._check_defs.pop("ext-preflight-check", None)
        return calls

    def test_external_check_runs_when_hook_customized(self, tmp_project):
        """With a customized pre-release hook, the config-declared external
        check is still selected (by name), while the built-in preflight run
        (bare tag_expr='preflight') does NOT happen -- test-suite stays skipped."""
        config = self._setup_with_external_check(
            tmp_project, hook_body="#!/bin/bash\necho custom tests\n"
        )
        calls = self._run_and_track(tmp_project, config, hook_customized=True)

        # (ii) external check selected during preflight WITH customized hook
        assert ("preflight", "ext-preflight-check") in calls, (
            "external check must run in customized-hook mode"
        )
        # (iii) built-in test-suite still skipped: no wholesale preflight run
        assert ("preflight", None) not in calls, (
            "built-in preflight checks must be skipped when hook customized"
        )
        # changelog preflight still runs unconditionally (non-dry-run)
        assert ("preflight-changelog", None) in calls

    def test_external_check_not_double_run_when_hook_not_customized(self, tmp_project):
        """With a non-customized (template) hook, exactly one wholesale preflight
        run covers built-ins + externals -- externals are NOT run again by name."""
        config = self._setup_with_external_check(
            tmp_project, hook_body=_CURRENT_TEMPLATE
        )
        calls = self._run_and_track(tmp_project, config, hook_customized=False)

        wholesale = [c for c in calls if c == ("preflight", None)]
        by_name = [c for c in calls if c[0] == "preflight" and c[1] is not None]
        assert len(wholesale) == 1, (
            "exactly one wholesale preflight run (built-ins + externals)"
        )
        assert by_name == [], (
            "externals must not be run again by name when hook not customized"
        )

    def test_dry_run_executes_zero_checks_and_prints_pointers(self, tmp_project, capsys):
        """Under --dry-run, no run_checks call happens at any of the three
        sites, and each site prints the honest pointer message."""
        config = self._setup_with_external_check(
            tmp_project, hook_body="#!/bin/bash\necho custom tests\n"
        )

        started = [p.start() for p in _FULL_FLOW_PATCHES]
        try:
            with (
                patch("rlsbl.commands.release.is_hook_customized", return_value=True),
                patch("rlsbl.app.run_checks", return_value=([], 0)) as mock_checks,
            ):
                from rlsbl.commands.release import run_cmd

                run_cmd(
                    _rc(),
                    {"dry-run": True, "yes": True},
                    ctx=ProjectContext(
                        project_root=Path(str(tmp_project)),
                        workspace_root=None,
                        config=config,
                    ),
                )
        finally:
            for p in _FULL_FLOW_PATCHES:
                p.stop()
            import rlsbl
            rlsbl.app._check_defs.pop("ext-preflight-check", None)

        mock_checks.assert_not_called()
        out = capsys.readouterr().out
        assert (
            "preflight checks not executed under --dry-run; run "
            "`rlsbl check --tag preflight-changelog` to evaluate the gate"
        ) in out
        assert (
            "preflight checks not executed under --dry-run; run "
            "`rlsbl check --tag preflight` to evaluate the gate"
        ) in out
