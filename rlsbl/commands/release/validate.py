"""Validation helpers: tests, lint, selfdoc, scaffold conflicts, strictcli schema, blog body."""

import os
import sys

from ...strictcli_detect import detect_strictcli
from ...testing import run_project_tests


class ReleaseValidationError(Exception):
    """Raised when a pre-release validation check fails."""
    pass


class HookError(Exception):
    """Raised when a built-in hook (tests, lint, selfdoc) fails."""
    pass


_SCHEMA_DUMP_TIMEOUT = 30


def parse_porcelain_paths(porcelain_output):
    """Parse file paths from `git status --porcelain` output.

    Handles the case where run() strips stdout, potentially removing a
    leading space from the first line. Uses lstrip().split(None, 1) to
    robustly extract the status code and path regardless.

    Returns a set of file paths found in the output.
    """
    dirty_files = set()
    for line in porcelain_output.splitlines():
        parts = line.lstrip().split(None, 1)
        if len(parts) < 2:
            continue
        # Handle rename notation: "R old -> new"
        file_path = parts[1].split(" -> ")[-1]
        dirty_files.add(file_path)
    return dirty_files


def _run_builtin_tests(registry, flags, *, project_dir=None, ctx):
    """Run built-in tests for the detected project type.

    Delegates to run_project_tests() for the actual test execution.
    Raises HookError on failure.
    Returns True if tests pass.
    """
    success = run_project_tests(
        registry,
        project_dir=project_dir,
        config=ctx.config,
        dry_run=flags.get("dry-run", False),
    )
    if not success:
        print("Error: tests failed.", file=sys.stderr)
        raise HookError("Tests failed")
    return True


def _run_builtin_lint(flags, is_library=False, project_dir=None):
    """Run built-in library lint.

    Counts errors and warnings from lint results. Raises HookError on errors.
    Only runs on library projects (monorepo projects with library = true).
    When project_dir is set (monorepo mode), lint runs against that directory.
    """
    if flags.get("dry-run"):
        return True

    if not is_library:
        print("Skipping lint (not a library project)")
        return True

    print("Running lint...")

    from ...lint import lint_library

    results = lint_library(project_dir if project_dir else ".")

    errors = [r for r in results if r.severity == "error"]
    warnings = [r for r in results if r.severity == "warning"]

    if errors:
        for r in errors:
            print(f"  {r.file}:{r.line}: {r.rule}: {r.message}", file=sys.stderr)
        print(f"Error: library lint found {len(errors)} error(s).", file=sys.stderr)
        raise HookError("Lint errors found")

    if warnings:
        for r in warnings:
            print(f"  {r.file}:{r.line}: {r.rule}: {r.message}", file=sys.stderr)
        print(f"Library lint: {len(warnings)} warning(s).")
    else:
        print("Library lint: clean.")

    return True


def _run_selfdoc_gen(flags, project_dir=None):
    """Run selfdoc gen if selfdoc.json exists in the project directory.

    Regenerates documentation pages from source before the selfdoc check step,
    ensuring the check validates fresh content rather than stale pages.
    """
    from . import require_tool, subprocess as _subprocess

    check_dir = project_dir if project_dir else "."
    selfdoc_config = os.path.join(check_dir, "selfdoc.json")
    if not os.path.exists(selfdoc_config):
        return True

    if flags.get("dry-run"):
        print("Would run: selfdoc gen --no-commit")
        return True

    if not require_tool("selfdoc", fatal=False):
        print(
            "Note: selfdoc.json found but selfdoc is not installed. Skipping docs generation."
        )
        return True

    print("Running selfdoc gen...")
    try:
        _subprocess.run(["selfdoc", "gen", "--no-commit"], cwd=project_dir, check=True)
    except _subprocess.CalledProcessError as e:
        print(
            f"Error: selfdoc gen failed (exit code {e.returncode}).",
            file=sys.stderr,
        )
        raise HookError("selfdoc gen failed")
    return True


def _run_selfdoc_check(flags, project_dir=None):
    """Run selfdoc check if selfdoc.json exists in the project directory.

    Checks documentation consistency before releasing. Non-fatal if selfdoc
    is not installed; fatal if it is installed and the check fails.
    When project_dir is set (monorepo mode), checks are resolved relative to it.
    """
    from . import require_tool, subprocess as _subprocess

    if flags.get("dry-run"):
        return True

    check_dir = project_dir if project_dir else "."
    selfdoc_config = os.path.join(check_dir, "selfdoc.json")
    if not os.path.exists(selfdoc_config):
        return True

    if not require_tool("selfdoc", fatal=False):
        print("Note: selfdoc.json found but selfdoc is not installed. Skipping docs check.")
        return True

    print("Running selfdoc check...")
    try:
        _subprocess.run(["selfdoc", "check"], cwd=project_dir, check=True)
    except _subprocess.CalledProcessError as e:
        print(
            f"Error: selfdoc check failed (exit code {e.returncode}).",
            file=sys.stderr,
        )
        raise HookError("selfdoc check failed")
    return True


def _abort_on_scaffold_conflicts(project_dir):
    """Abort the release if scaffold-managed files contain unresolved merge
    conflict markers.

    Scaffold's three-way merge (git merge-file) intentionally leaves
    conflict markers for manual resolution; releasing with them would
    publish corrupted workflows/hooks. Runs PRE-MUTATION: nothing has
    been modified yet when this aborts.
    """
    from ...checks.project import find_conflicted_scaffold_files

    conflicted = find_conflicted_scaffold_files(project_dir)
    if conflicted:
        print(
            "Error: unresolved merge conflict markers in scaffold-managed file(s):",
            file=sys.stderr,
        )
        for path, line in conflicted:
            print(f"  {path}:{line}", file=sys.stderr)
        print(
            "Resolve the conflicts and commit before releasing.",
            file=sys.stderr,
        )
        raise ReleaseValidationError("Unresolved scaffold conflict markers")


def _run_strictcli_schema_dump(flags, log, project_dir="."):
    """Run --dump-schema for strictcli projects to regenerate .strictcli/schema.json.

    Detects strictcli usage via pyproject.toml, runs the entry point with
    --dump-schema, and logs the result. The generated file is picked up by
    the hook-generated file mechanism (pre/post hook dirty snapshots).

    Non-fatal: a failing dump command prints a warning but does not abort.
    """
    from . import subprocess as _subprocess

    if flags.get("dry-run"):
        check_dir = project_dir
        result = detect_strictcli(check_dir)
        if result:
            entry_point, _ = result
            log(f"Would run: uv run {entry_point} --dump-schema")
        return

    check_dir = project_dir
    result = detect_strictcli(check_dir)
    if not result:
        return

    entry_point, lang = result
    log(f"Dumping strictcli schema ({entry_point})...")

    try:
        _subprocess.run(
            ["uv", "run", entry_point, "--dump-schema"],
            cwd=project_dir,
            timeout=_SCHEMA_DUMP_TIMEOUT,
        )
    except _subprocess.TimeoutExpired:
        print(
            f"Warning: strictcli schema dump timed out after {_SCHEMA_DUMP_TIMEOUT}s.",
            file=sys.stderr,
        )
    except (_subprocess.CalledProcessError, OSError) as e:
        print(f"Warning: strictcli schema dump failed: {e}", file=sys.stderr)


def validate_blog_body(project_dir, blog_enabled):
    """Validate the blog body file for a release.

    Returns (body_path, warning_message) where body_path is the path if it exists
    and warning_message is set if the file is missing.
    Raises ReleaseValidationError if blog_enabled and file is empty.
    """
    if not blog_enabled:
        return None, None
    blog_body_path = os.path.join(project_dir, ".rlsbl", "releases", "unreleased.md")
    if os.path.exists(blog_body_path):
        with open(blog_body_path, "r", encoding="utf-8") as f:
            body_content = f.read()
        if not body_content.strip():
            print(
                "Error: blog body file at .rlsbl/releases/unreleased.md exists but is empty.",
                file=sys.stderr,
            )
            raise ReleaseValidationError("Blog body validation failed")
        return blog_body_path, None
    return None, "blog = true but no body file at .rlsbl/releases/unreleased.md (post will be changelog-only)"
