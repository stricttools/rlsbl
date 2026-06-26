"""Tests for per-releasable and per-package hook resolution, execution ordering,
env var construction, test aggregation, and the private-hook-stale check
in explicit releasable mode."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from rlsbl.commands.release.hooks import (
    build_hook_env,
    get_package_hook_path,
    get_releasable_hook_path,
    is_releasable_hook_customized,
    run_releasable_hooks,
    run_releasable_tests,
    run_releasable_lint,
    _run_per_package_hooks,
)
from rlsbl.commands.release.validate import HookError
from rlsbl.workspace import WORKSPACE_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_releasable_workspace(root, releasable_name, projects):
    """Create a workspace.toml with [[releasables]] and project entries.

    Each project dict should have: name, path, and optionally releasable, library.
    """
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "[[releasables]]",
        f'name = "{releasable_name}"',
        "",
    ]
    for proj in projects:
        lines.append("[[projects]]")
        lines.append(f'path = "{proj["path"]}"')
        lines.append(f'name = "{proj["name"]}"')
        if "releasable" in proj:
            val = proj["releasable"]
            if isinstance(val, str):
                lines.append(f'releasable = "{val}"')
            elif val is False:
                lines.append("releasable = false")
        if proj.get("library"):
            lines.append("library = true")
        lines.append("")

    (ws_dir / "workspace.toml").write_text("\n".join(lines))

    # Create releasable directory structure
    rel_dir = ws_dir / "releasables" / releasable_name
    rel_dir.mkdir(parents=True, exist_ok=True)
    (rel_dir / "hooks").mkdir(exist_ok=True)
    (rel_dir / "changes").mkdir(exist_ok=True)
    (rel_dir / "version").write_text("1.0.0\n")


def _make_hook(directory, hook_name, body="#!/bin/bash\necho ok\n"):
    """Create a hook script in the given directory."""
    hooks_dir = Path(directory)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / hook_name
    hook.write_text(body)
    hook.chmod(0o755)
    return str(hook)


# ---------------------------------------------------------------------------
# 7.1: get_releasable_hook_path resolution
# ---------------------------------------------------------------------------


class TestGetReleasableHookPath:
    """Tests for releasable-level hook path resolution."""

    def test_returns_correct_path(self, tmp_path):
        path = get_releasable_hook_path(str(tmp_path), "www", "pre-checks.sh")
        expected = os.path.join(
            str(tmp_path), ".rlsbl-monorepo", "releasables", "www", "hooks", "pre-checks.sh"
        )
        assert path == expected

    def test_different_hook_names(self, tmp_path):
        for hook in ("pre-checks.sh", "pre-release.sh", "post-release.sh"):
            path = get_releasable_hook_path(str(tmp_path), "core", hook)
            assert path.endswith(f"core/hooks/{hook}")

    def test_different_releasable_names(self, tmp_path):
        for name in ("core", "www", "api"):
            path = get_releasable_hook_path(str(tmp_path), name, "pre-checks.sh")
            assert f"/releasables/{name}/hooks/" in path

    def test_path_does_not_require_existence(self, tmp_path):
        """Path is returned even if the file/directory does not exist."""
        path = get_releasable_hook_path(str(tmp_path), "nonexistent", "pre-checks.sh")
        assert not os.path.exists(path)
        assert isinstance(path, str)


class TestGetPackageHookPath:
    """Tests for per-package hook path resolution."""

    def test_returns_correct_path(self, tmp_path):
        path = get_package_hook_path(str(tmp_path), "pre-checks.sh")
        expected = os.path.join(str(tmp_path), ".rlsbl", "hooks", "pre-checks.sh")
        assert path == expected


# ---------------------------------------------------------------------------
# 7.1: build_hook_env
# ---------------------------------------------------------------------------


class TestBuildHookEnv:
    """Tests for hook environment variable construction."""

    def test_base_env_is_copied(self):
        base = {"HOME": "/home/test", "PATH": "/usr/bin"}
        env = build_hook_env(base, "1.2.3")
        assert env["HOME"] == "/home/test"
        assert env["RLSBL_VERSION"] == "1.2.3"
        # Original dict is not mutated
        assert "RLSBL_VERSION" not in base

    def test_version_always_set(self):
        env = build_hook_env({}, "2.0.0")
        assert env["RLSBL_VERSION"] == "2.0.0"

    def test_optional_fields(self):
        env = build_hook_env(
            {},
            "1.0.0",
            bump_type="minor",
            prev_version="0.9.0",
            description="A release",
        )
        assert env["RLSBL_BUMP_TYPE"] == "minor"
        assert env["RLSBL_PREV_VERSION"] == "0.9.0"
        assert env["RLSBL_DESCRIPTION"] == "A release"

    def test_package_name_set_when_provided(self):
        env = build_hook_env({}, "1.0.0", package_name="my-package")
        assert env["RLSBL_PACKAGE"] == "my-package"

    def test_package_name_absent_when_not_provided(self):
        env = build_hook_env({}, "1.0.0")
        assert "RLSBL_PACKAGE" not in env

    def test_optional_fields_absent_when_none(self):
        env = build_hook_env({}, "1.0.0")
        assert "RLSBL_BUMP_TYPE" not in env
        assert "RLSBL_PREV_VERSION" not in env
        assert "RLSBL_DESCRIPTION" not in env


# ---------------------------------------------------------------------------
# 7.2: Per-releasable hook execution
# ---------------------------------------------------------------------------


class TestRunReleasableHooks:
    """Tests for combined releasable + per-package hook execution."""

    def test_pre_checks_releasable_first_then_packages(self, tmp_path):
        """pre-checks: releasable hook runs first, then per-package alphabetically."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
            {"name": "beta", "path": "beta", "releasable": "www"},
        ])

        # Create releasable hook
        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "pre-checks.sh", "#!/bin/bash\necho releasable-pre-checks\n")

        # Create per-package hooks
        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir(exist_ok=True)
        beta_dir.mkdir(exist_ok=True)
        _make_hook(alpha_dir / ".rlsbl" / "hooks", "pre-checks.sh", "#!/bin/bash\necho alpha-pre-checks\n")
        _make_hook(beta_dir / ".rlsbl" / "hooks", "pre-checks.sh", "#!/bin/bash\necho beta-pre-checks\n")

        execution_order = []

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            execution_order.append(hook_name)

        member_packages = [
            ("beta", str(beta_dir)),
            ("alpha", str(alpha_dir)),
        ]

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "pre-checks", str(tmp_path), "www",
                member_packages, {"RLSBL_VERSION": "1.0.0"}, None,
                print,
            )

        assert execution_order == [
            "releasable pre-checks",
            "pre-checks (alpha)",
            "pre-checks (beta)",
        ]

    def test_pre_release_packages_first_then_releasable(self, tmp_path):
        """pre-release: per-package hooks run first alphabetically, then releasable."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
            {"name": "beta", "path": "beta", "releasable": "www"},
        ])

        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "pre-release.sh")

        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir(exist_ok=True)
        beta_dir.mkdir(exist_ok=True)
        _make_hook(alpha_dir / ".rlsbl" / "hooks", "pre-release.sh")
        _make_hook(beta_dir / ".rlsbl" / "hooks", "pre-release.sh")

        execution_order = []

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            execution_order.append(hook_name)

        member_packages = [
            ("beta", str(beta_dir)),
            ("alpha", str(alpha_dir)),
        ]

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "pre-release", str(tmp_path), "www",
                member_packages, {"RLSBL_VERSION": "1.0.0"}, None,
                print,
            )

        assert execution_order == [
            "pre-release (alpha)",
            "pre-release (beta)",
            "releasable pre-release",
        ]

    def test_post_release_releasable_first_then_packages(self, tmp_path):
        """post-release: releasable first, then per-package alphabetically."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])

        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "post-release.sh")

        alpha_dir = tmp_path / "alpha"
        alpha_dir.mkdir(exist_ok=True)
        _make_hook(alpha_dir / ".rlsbl" / "hooks", "post-release.sh")

        execution_order = []

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            execution_order.append(hook_name)

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "post-release", str(tmp_path), "www",
                [("alpha", str(alpha_dir))], {"RLSBL_VERSION": "1.0.0"}, None,
                print,
            )

        assert execution_order == [
            "releasable post-release",
            "post-release (alpha)",
        ]

    def test_missing_hooks_are_skipped(self, tmp_path):
        """When hooks don't exist at either level, nothing runs."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        alpha_dir = tmp_path / "alpha"
        alpha_dir.mkdir(exist_ok=True)

        execution_order = []

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            execution_order.append(hook_name)

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "pre-checks", str(tmp_path), "www",
                [("alpha", str(alpha_dir))], {"RLSBL_VERSION": "1.0.0"}, None,
                print,
            )

        # run_release_hook should not be called since no hooks exist
        assert execution_order == []

    def test_only_releasable_hook_runs(self, tmp_path):
        """When only releasable hook exists, only it runs."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])

        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "pre-checks.sh")

        alpha_dir = tmp_path / "alpha"
        alpha_dir.mkdir(exist_ok=True)

        execution_order = []

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            execution_order.append(hook_name)

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "pre-checks", str(tmp_path), "www",
                [("alpha", str(alpha_dir))], {"RLSBL_VERSION": "1.0.0"}, None,
                print,
            )

        assert execution_order == ["releasable pre-checks"]

    def test_only_package_hooks_run(self, tmp_path):
        """When only per-package hooks exist, only they run."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
            {"name": "beta", "path": "beta", "releasable": "www"},
        ])

        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir(exist_ok=True)
        beta_dir.mkdir(exist_ok=True)
        _make_hook(alpha_dir / ".rlsbl" / "hooks", "pre-checks.sh")
        # beta has no hook

        execution_order = []

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            execution_order.append(hook_name)

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "pre-checks", str(tmp_path), "www",
                [("beta", str(beta_dir)), ("alpha", str(alpha_dir))],
                {"RLSBL_VERSION": "1.0.0"}, None, print,
            )

        assert execution_order == ["pre-checks (alpha)"]


# ---------------------------------------------------------------------------
# 7.2: Failure propagation
# ---------------------------------------------------------------------------


class TestHookFailurePropagation:
    """Verify that failure at any level aborts the release."""

    def test_releasable_hook_failure_blocks_release(self, tmp_path):
        """Failure in releasable pre-checks hook raises HookError."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])

        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "pre-checks.sh")

        alpha_dir = tmp_path / "alpha"
        alpha_dir.mkdir(exist_ok=True)

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            raise HookError(f"{hook_name} hook exited with code 1.")

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            with pytest.raises(HookError, match="releasable pre-checks"):
                run_releasable_hooks(
                    "pre-checks", str(tmp_path), "www",
                    [("alpha", str(alpha_dir))], {"RLSBL_VERSION": "1.0.0"}, None,
                    print,
                )

    def test_package_hook_failure_blocks_release(self, tmp_path):
        """Failure in per-package hook raises HookError and stops further packages."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
            {"name": "beta", "path": "beta", "releasable": "www"},
        ])

        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir(exist_ok=True)
        beta_dir.mkdir(exist_ok=True)
        _make_hook(alpha_dir / ".rlsbl" / "hooks", "pre-checks.sh")
        _make_hook(beta_dir / ".rlsbl" / "hooks", "pre-checks.sh")

        call_count = 0

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            nonlocal call_count
            call_count += 1
            if "alpha" in hook_name:
                raise HookError(f"{hook_name} hook exited with code 1.")

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            with pytest.raises(HookError, match="alpha"):
                run_releasable_hooks(
                    "pre-checks", str(tmp_path), "www",
                    [("alpha", str(alpha_dir)), ("beta", str(beta_dir))],
                    {"RLSBL_VERSION": "1.0.0"}, None, print,
                )

        # alpha failed, beta should not have run
        assert call_count == 1

    def test_pre_release_package_failure_blocks_releasable_hook(self, tmp_path):
        """In pre-release order, if a package hook fails, the releasable hook does not run."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])

        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "pre-release.sh")

        alpha_dir = tmp_path / "alpha"
        alpha_dir.mkdir(exist_ok=True)
        _make_hook(alpha_dir / ".rlsbl" / "hooks", "pre-release.sh")

        execution_order = []

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            execution_order.append(hook_name)
            if "alpha" in hook_name:
                raise HookError(f"{hook_name} hook exited with code 1.")

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            with pytest.raises(HookError, match="alpha"):
                run_releasable_hooks(
                    "pre-release", str(tmp_path), "www",
                    [("alpha", str(alpha_dir))], {"RLSBL_VERSION": "1.0.0"}, None,
                    print,
                )

        # Only alpha's hook ran; releasable hook did not
        assert execution_order == ["pre-release (alpha)"]


# ---------------------------------------------------------------------------
# 7.2: Per-package hook env vars
# ---------------------------------------------------------------------------


class TestPerPackageHookEnvVars:
    """Verify per-package hooks receive RLSBL_PACKAGE in their env."""

    def test_package_hooks_get_rlsbl_package(self, tmp_path):
        """Each per-package hook receives RLSBL_PACKAGE set to the package name."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
            {"name": "beta", "path": "beta", "releasable": "www"},
        ])

        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir(exist_ok=True)
        beta_dir.mkdir(exist_ok=True)
        _make_hook(alpha_dir / ".rlsbl" / "hooks", "pre-checks.sh")
        _make_hook(beta_dir / ".rlsbl" / "hooks", "pre-checks.sh")

        captured_envs = {}

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            captured_envs[hook_name] = dict(env)

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "pre-checks", str(tmp_path), "www",
                [("alpha", str(alpha_dir)), ("beta", str(beta_dir))],
                {"RLSBL_VERSION": "1.0.0"}, None, print,
            )

        assert captured_envs["pre-checks (alpha)"]["RLSBL_PACKAGE"] == "alpha"
        assert captured_envs["pre-checks (beta)"]["RLSBL_PACKAGE"] == "beta"

    def test_releasable_hook_does_not_get_rlsbl_package(self, tmp_path):
        """Releasable-level hooks do not receive RLSBL_PACKAGE."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])

        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "pre-checks.sh")

        alpha_dir = tmp_path / "alpha"
        alpha_dir.mkdir(exist_ok=True)

        captured_envs = {}

        def mock_run_release_hook(hook_name, hook_path, cwd, env, timeout, **kwargs):
            captured_envs[hook_name] = dict(env)

        base_env = {"RLSBL_VERSION": "1.0.0"}

        with patch("rlsbl.commands.release.hooks.run_release_hook", side_effect=mock_run_release_hook):
            run_releasable_hooks(
                "pre-checks", str(tmp_path), "www",
                [("alpha", str(alpha_dir))], base_env, None, print,
            )

        assert "RLSBL_PACKAGE" not in captured_envs["releasable pre-checks"]


# ---------------------------------------------------------------------------
# 7.3: Test/lint aggregation
# ---------------------------------------------------------------------------


class TestReleasableTestAggregation:
    """Tests for running tests across member packages."""

    def test_runs_tests_for_each_member(self, tmp_path):
        """run_releasable_tests calls _run_builtin_tests for each member."""
        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir()
        beta_dir.mkdir()

        tested_packages = []

        def mock_builtin_tests(registry, flags, *, project_dir=None, ctx):
            tested_packages.append(os.path.basename(project_dir))

        from rlsbl.targets import TargetEntry
        mock_targets = [TargetEntry(name="pypi", path=".")]

        with patch("rlsbl.targets.detect_targets", return_value=mock_targets), \
             patch("rlsbl.commands.release.validate._run_builtin_tests", side_effect=mock_builtin_tests):
            run_releasable_tests(
                [("beta", str(beta_dir)), ("alpha", str(alpha_dir))],
                {},
                ctx=MagicMock(),
                log=print,
                releasable_config_dir=None,
            )

        # Alphabetical order
        assert tested_packages == ["alpha", "beta"]

    def test_first_failure_aborts(self, tmp_path):
        """If a member's tests fail, HookError propagates and stops further testing."""
        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir()
        beta_dir.mkdir()

        call_count = 0

        def mock_builtin_tests(registry, flags, *, project_dir=None, ctx):
            nonlocal call_count
            call_count += 1
            if "alpha" in project_dir:
                raise HookError("Tests failed")

        from rlsbl.targets import TargetEntry
        mock_targets = [TargetEntry(name="pypi", path=".")]

        with patch("rlsbl.targets.detect_targets", return_value=mock_targets), \
             patch("rlsbl.commands.release.validate._run_builtin_tests", side_effect=mock_builtin_tests):
            with pytest.raises(HookError, match="Tests failed"):
                run_releasable_tests(
                    [("alpha", str(alpha_dir)), ("beta", str(beta_dir))],
                    {},
                    ctx=MagicMock(),
                    log=print,
                    releasable_config_dir=None,
                )

        assert call_count == 1

    def test_detects_target_per_member(self, tmp_path):
        """Each member gets its own target type detected, not the releasable-level registry."""
        pypi_dir = tmp_path / "pypi_pkg"
        go_dir = tmp_path / "go_pkg"
        pypi_dir.mkdir()
        go_dir.mkdir()

        # Create project files so detect_targets can identify targets
        (pypi_dir / "pyproject.toml").write_text('[project]\nname = "pypi_pkg"\nversion = "0.1.0"\n')
        (go_dir / "go.mod").write_text("module example.com/go_pkg\n\ngo 1.21\n")

        registries_per_call = []

        def mock_builtin_tests(registry, flags, *, project_dir=None, ctx):
            registries_per_call.append((os.path.basename(project_dir), registry))

        with patch("rlsbl.commands.release.validate._run_builtin_tests", side_effect=mock_builtin_tests):
            run_releasable_tests(
                [("go_pkg", str(go_dir)), ("pypi_pkg", str(pypi_dir))],
                {},
                ctx=MagicMock(),
                log=print,
            )

        # Sorted alphabetically, go_pkg comes first
        assert registries_per_call == [("go_pkg", "go"), ("pypi_pkg", "pypi")]


class TestReleasableLintAggregation:
    """Tests for running lint across library members."""

    def test_only_library_members_linted(self, tmp_path):
        """run_releasable_lint only runs lint on members with library=true."""
        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        gamma_dir = tmp_path / "gamma"
        alpha_dir.mkdir()
        beta_dir.mkdir()
        gamma_dir.mkdir()

        linted = []

        def mock_builtin_lint(flags, is_library=False, project_dir=None, check_timeout=None, allowed_imports=None):
            linted.append(os.path.basename(project_dir))

        # Create mock workspace projects
        from rlsbl.workspace import WorkspaceProject
        ws_projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha", "library": True, "releasable": "www"}),
            WorkspaceProject({"name": "beta", "path": "beta", "releasable": "www"}),
            WorkspaceProject({"name": "gamma", "path": "gamma", "library": True, "releasable": "www"}),
        ]

        with patch("rlsbl.commands.release.validate._run_builtin_lint", side_effect=mock_builtin_lint):
            run_releasable_lint(
                [
                    ("gamma", str(gamma_dir)),
                    ("alpha", str(alpha_dir)),
                    ("beta", str(beta_dir)),
                ],
                {},
                ws_projects=ws_projects,
                log=print,
            )

        # Only library members, in alphabetical order
        assert linted == ["alpha", "gamma"]


# ---------------------------------------------------------------------------
# 7.3: Effectively empty hook check at releasable level
# ---------------------------------------------------------------------------


class TestIsReleasableHookCustomized:
    """Tests for the releasable-level hook customization check."""

    def test_nonexistent_hook_is_not_customized(self, tmp_path):
        """Missing releasable pre-release hook means not customized."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        assert not is_releasable_hook_customized(str(tmp_path), "www")

    def test_customized_hook_is_detected(self, tmp_path):
        """A releasable pre-release hook with custom content is detected."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        _make_hook(rel_hook_dir, "pre-release.sh", "#!/bin/bash\nset -euo pipefail\nnpm run build\n")
        assert is_releasable_hook_customized(str(tmp_path), "www")

    def test_scaffold_template_is_not_customized(self, tmp_path):
        """A releasable pre-release hook matching the scaffold template is not customized."""
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "alpha", "path": "alpha", "releasable": "www"},
        ])
        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        # Use the scaffold template content
        template_content = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "# Project-specific pre-release checks.\n"
            "# Built-in checks (tests, lint) run automatically before this hook.\n"
            "# Add custom validation here, e.g.:\n"
            "#   - Check for uncommitted documentation\n"
            "#   - Verify external service connectivity\n"
            "#   - Run integration tests not covered by the test suite\n"
        )
        _make_hook(rel_hook_dir, "pre-release.sh", template_content)
        assert not is_releasable_hook_customized(str(tmp_path), "www")


# ---------------------------------------------------------------------------
# 7.4: private-hook-stale check in explicit mode
# ---------------------------------------------------------------------------


class TestPrivateHookStaleCheck:
    """Tests for the updated private-hook-stale check that covers both levels."""

    def _make_check_ctx(self, tmp_path, *, workspace_root=None):
        """Build a minimal ctx object for the private-hook-stale check."""
        from rlsbl.context import ProjectContext
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text('{"private": false}')
        return ProjectContext(
            project_root=tmp_path,
            workspace_root=Path(workspace_root) if workspace_root else None,
            config={"private": False},
        )

    def _run_check(self, ctx):
        """Run the private-hook-stale check and return its CheckResult."""
        from rlsbl import app
        check_fn = app._check_defs["private-hook-stale"].impl
        return check_fn(ctx)

    def test_per_package_stale_detected(self, tmp_path):
        """Legacy content in per-package hook is detected."""
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "post-release.sh").write_text(
            "#!/bin/bash\n# Post-release hook for private repositories\n"
        )

        ctx = self._make_check_ctx(tmp_path)
        result = self._run_check(ctx)
        assert result.status == "fail"
        assert "legacy" in result.message.lower()

    def test_releasable_level_stale_detected(self, tmp_path):
        """Legacy content in releasable-level hook is detected."""
        # Set up workspace with explicit releasable
        _make_releasable_workspace(tmp_path, "www", [
            {"name": "myapp", "path": ".", "releasable": "www"},
        ])

        # Add legacy content to releasable hook
        rel_hook_dir = tmp_path / WORKSPACE_DIR / "releasables" / "www" / "hooks"
        (rel_hook_dir / "post-release.sh").write_text(
            "#!/bin/bash\n# Post-release hook for private repositories\n"
        )

        ctx = self._make_check_ctx(tmp_path, workspace_root=str(tmp_path))
        result = self._run_check(ctx)
        assert result.status == "fail"
        assert "legacy" in result.message.lower()

    def test_clean_hooks_pass(self, tmp_path):
        """Hooks without legacy content pass the check."""
        hooks_dir = tmp_path / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "post-release.sh").write_text(
            "#!/bin/bash\necho 'deploy'\n"
        )

        ctx = self._make_check_ctx(tmp_path)
        result = self._run_check(ctx)
        assert result.status == "pass"

    def test_no_hooks_pass(self, tmp_path):
        """No hooks at all passes the check."""
        (tmp_path / ".rlsbl").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".rlsbl" / "config.json").write_text('{"private": false}')

        ctx = self._make_check_ctx(tmp_path)
        result = self._run_check(ctx)
        assert result.status == "pass"
        assert "no post-release hook" in result.message
