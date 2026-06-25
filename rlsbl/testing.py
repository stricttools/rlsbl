"""Shared test-running logic that auto-detects project types and invokes the correct test runner (pytest, go test, npm test) for releases and checks.

Extracted from the release pipeline so it can be reused by other commands
(e.g., pre-push checks, CI, standalone test invocations).
"""

import json
import os
import subprocess
import sys

from .utils import require_tool


def sync_workspace(workspace_root: str, *, verbose: bool = False) -> bool:
    """Run uv sync --all-packages at the workspace root.

    Returns True on success, False on failure.
    """
    if not require_tool("uv", fatal=False):
        return True

    if not os.path.exists(os.path.join(workspace_root, "pyproject.toml")):
        return True

    sync_cmd = ["uv", "sync", "--all-packages"]
    if not verbose:
        sync_cmd.append("--quiet")
    try:
        result = subprocess.run(sync_cmd, cwd=workspace_root, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"Error: command timed out after 120s: {sync_cmd}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print("Error: uv sync failed.", file=sys.stderr)
        return False
    return True


def run_project_tests(
    target_name: str,
    *,
    project_dir: str | None = None,
    workspace_root: str | None = None,
    skip_sync: bool = False,
    config: dict | None = None,
    dry_run: bool = False,
) -> bool:
    """Run tests for the given project target type.

    Args:
        target_name: registry/target identifier (e.g., "pypi", "go", "npm").
        project_dir: working directory for subprocess calls. None means cwd.
        workspace_root: uv workspace root for monorepos. When set, uv sync
            runs here instead of at project_dir.
        skip_sync: if True, skip the uv sync step (caller already synced).
        config: project config dict. Used to read uv_sync_verbose for pypi.
        dry_run: if True, skip all subprocess execution and return True.

    Returns True if tests pass (or are skipped), False on failure.
    Does NOT call sys.exit -- that is the caller's responsibility.
    """
    if dry_run:
        return True

    print("Running tests...")

    if target_name == "pypi":
        return _run_pypi_tests(
            project_dir=project_dir,
            workspace_root=workspace_root,
            skip_sync=skip_sync,
            config=config or {},
        )
    elif target_name == "go":
        return _run_go_tests(project_dir=project_dir)
    elif target_name == "npm":
        return _run_npm_tests(project_dir=project_dir)
    elif target_name == "maven":
        return _run_maven_tests(project_dir=project_dir)
    else:
        # Unknown target, skip tests
        return True


def _run_pypi_tests(
    *,
    project_dir: str | None,
    workspace_root: str | None = None,
    skip_sync: bool = False,
    config: dict,
) -> bool:
    """Run Python tests via uv or bare pytest."""
    uv_verbose = config.get("uv_sync_verbose", False)
    if require_tool("uv", fatal=False):
        if not skip_sync:
            sync_cwd = workspace_root if workspace_root else project_dir
            if not sync_workspace(sync_cwd, verbose=uv_verbose):
                return False
        try:
            result = subprocess.run(["uv", "run", "pytest"], cwd=project_dir, timeout=120)
        except subprocess.TimeoutExpired:
            print("Error: command timed out after 120s: ['uv', 'run', 'pytest']", file=sys.stderr)
            return False
    elif require_tool("pytest", fatal=False):
        try:
            result = subprocess.run(["pytest"], cwd=project_dir, timeout=120)
        except subprocess.TimeoutExpired:
            print("Error: command timed out after 120s: ['pytest']", file=sys.stderr)
            return False
    else:
        print("Warning: neither uv nor pytest found, skipping tests.", file=sys.stderr)
        return True

    return result.returncode == 0


def _run_go_tests(*, project_dir: str | None) -> bool:
    """Run Go tests."""
    cmd = ["go", "test", "./...", "-race", "-short", "-count=1"]
    try:
        result = subprocess.run(cmd, cwd=project_dir, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"Error: command timed out after 120s: {cmd}", file=sys.stderr)
        return False
    return result.returncode == 0


def _run_maven_tests(*, project_dir: str | None) -> bool:
    """Run Maven/Gradle tests.

    Prefers ./gradlew test if gradlew exists, otherwise falls back to mvn test
    if pom.xml exists.
    """
    effective_dir = project_dir or "."
    gradlew = os.path.join(effective_dir, "gradlew")
    if os.path.exists(gradlew):
        cmd = ["./gradlew", "test"]
        try:
            result = subprocess.run(cmd, cwd=project_dir, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"Error: command timed out after 120s: {cmd}", file=sys.stderr)
            return False
        return result.returncode == 0

    pom_path = os.path.join(effective_dir, "pom.xml")
    if os.path.exists(pom_path):
        cmd = ["mvn", "test"]
        try:
            result = subprocess.run(cmd, cwd=project_dir, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"Error: command timed out after 120s: {cmd}", file=sys.stderr)
            return False
        return result.returncode == 0

    print("Warning: no gradlew or pom.xml found, skipping maven tests.", file=sys.stderr)
    return True


def _run_npm_tests(*, project_dir: str | None) -> bool:
    """Run npm tests if a test script is defined in package.json."""
    pkg_path = os.path.join(project_dir, "package.json") if project_dir else "package.json"
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
            if pkg.get("scripts", {}).get("test"):
                try:
                    result = subprocess.run(["npm", "test"], cwd=project_dir, timeout=120)
                except subprocess.TimeoutExpired:
                    print("Error: command timed out after 120s: ['npm', 'test']", file=sys.stderr)
                    return False
                return result.returncode == 0
            else:
                print("No test script in package.json, skipping tests.")
                return True
        except (json.JSONDecodeError, OSError):
            print("Warning: could not read package.json, skipping tests.", file=sys.stderr)
            return True
    else:
        return True
