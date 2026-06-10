"""Shared test-running logic for detected project types.

Extracted from the release pipeline so it can be reused by other commands
(e.g., pre-push checks, CI, standalone test invocations).
"""

import json
import os
import subprocess
import sys

from .utils import require_tool


def run_project_tests(
    target_name: str,
    *,
    project_dir: str | None = None,
    config: dict | None = None,
    dry_run: bool = False,
) -> bool:
    """Run tests for the given project target type.

    Args:
        target_name: registry/target identifier (e.g., "pypi", "go", "npm").
        project_dir: working directory for subprocess calls. None means cwd.
        config: project config dict. Used to read uv_sync_verbose for pypi.
        dry_run: if True, skip all subprocess execution and return True.

    Returns True if tests pass (or are skipped), False on failure.
    Does NOT call sys.exit -- that is the caller's responsibility.
    """
    if dry_run:
        return True

    print("Running tests...")

    if target_name == "pypi":
        return _run_pypi_tests(project_dir=project_dir, config=config or {})
    elif target_name == "go":
        return _run_go_tests(project_dir=project_dir)
    elif target_name == "npm":
        return _run_npm_tests(project_dir=project_dir)
    else:
        # Unknown target, skip tests
        return True


def _run_pypi_tests(*, project_dir: str | None, config: dict) -> bool:
    """Run Python tests via uv or bare pytest."""
    uv_verbose = config.get("uv_sync_verbose", False)
    if require_tool("uv", fatal=False):
        sync_cmd = ["uv", "sync", "--all-packages"]
        if not uv_verbose:
            sync_cmd.append("--quiet")
        result = subprocess.run(sync_cmd, cwd=project_dir)
        if result.returncode != 0:
            print("Error: uv sync failed.", file=sys.stderr)
            return False
        result = subprocess.run(["uv", "run", "pytest"], cwd=project_dir)
    elif require_tool("pytest", fatal=False):
        result = subprocess.run(["pytest"], cwd=project_dir)
    else:
        print("Warning: neither uv nor pytest found, skipping tests.", file=sys.stderr)
        return True

    return result.returncode == 0


def _run_go_tests(*, project_dir: str | None) -> bool:
    """Run Go tests."""
    result = subprocess.run(
        ["go", "test", "./...", "-race", "-short", "-count=1"], cwd=project_dir
    )
    return result.returncode == 0


def _run_npm_tests(*, project_dir: str | None) -> bool:
    """Run npm tests if a test script is defined in package.json."""
    pkg_path = os.path.join(project_dir, "package.json") if project_dir else "package.json"
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
            if pkg.get("scripts", {}).get("test"):
                result = subprocess.run(["npm", "test"], cwd=project_dir)
                return result.returncode == 0
            else:
                print("No test script in package.json, skipping tests.")
                return True
        except (json.JSONDecodeError, OSError):
            print("Warning: could not read package.json, skipping tests.", file=sys.stderr)
            return True
    else:
        return True
