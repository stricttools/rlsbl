"""Validation helpers: tests, lint, selfdoc, scaffold conflicts, strictcli schema, blog body.

Also contains extracted validation steps from run_cmd: target validation, OTA mode,
config integrity, pipeline config, gh CLI, clean tree, branch/remote, monorepo context,
version/tag computation, and changelog state validation.
"""

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


VALID_BUMP_TYPES = ("patch", "minor", "major")


def validate_release_targets(release_config, project_root):
    """Validate include/exclude targets in the release config.

    Checks:
    - include list is non-empty
    - all named targets are known
    - include + exclude exhaustively covers detected targets

    Returns the primary registry name (first item in include).
    Raises ReleaseValidationError on failure.
    """
    from . import TARGETS, detect_targets

    if not release_config.include:
        raise ReleaseValidationError(
            "release file has an empty include list. Add at least one target."
        )
    registry = release_config.include[0]

    for t_name in release_config.include + release_config.exclude:
        if t_name not in TARGETS:
            raise ReleaseValidationError(
                f"unknown target '{t_name}' in release file. "
                f"Valid: {', '.join(TARGETS.keys())}"
            )

    detected = {entry.name for entry in detect_targets(str(project_root))}
    declared = set(release_config.include) | set(release_config.exclude)
    missing = detected - declared
    extra = declared - detected
    if missing:
        raise ReleaseValidationError(
            f"detected targets not in release file: {', '.join(sorted(missing))}. "
            "Add them to include or exclude."
        )
    if extra:
        print(
            f"Warning: release file lists targets not detected in project: {', '.join(sorted(extra))}",
            file=sys.stderr,
        )

    return registry


def validate_ota_mode(release_config, project_root, config):
    """Validate Flutter OTA mode: check for native changes since last build release.

    Raises ReleaseValidationError if OTA is requested but native files changed.
    """
    flutter_targets = [
        t for t in release_config.include if t.startswith("flutter-")
    ]
    if not flutter_targets:
        return

    flutter_cfg = release_config.targets
    ota_targets = [
        t for t in flutter_targets
        if flutter_cfg.get(t, {}).get("mode") == "ota"
    ]
    if not ota_targets:
        return

    from ...targets.native_changes import detect_native_changes

    _ota_root = str(project_root)
    last_build = config.get("last_build_release")
    if last_build:
        since_ref = f"v{last_build}"
        native_files = detect_native_changes(_ota_root, since_ref)
        if native_files:
            detail = "\n".join(f"  {nf}" for nf in native_files)
            raise ReleaseValidationError(
                f"OTA release requested but native files changed "
                f"since last build release ({last_build}):\n{detail}\n"
                'Use mode = "build" for a full release instead.'
            )


def validate_config_integrity(config):
    """Validate the 'private' key exists and private repos don't have local pipelines.

    Raises ReleaseValidationError on failure.
    """
    if "private" not in config:
        raise ReleaseValidationError(
            '"private" key missing from .rlsbl/config.json.\n'
            'Set "private": true for private repos or "private": false for public repos.\n'
            'Quick fix: rlsbl scaffold'
        )

    if config["private"]:
        pipelines_cfg = config.get("pipelines", {})
        if isinstance(pipelines_cfg, dict):
            for pipeline_name, pipeline_cfg in pipelines_cfg.items():
                if isinstance(pipeline_cfg, dict) and pipeline_cfg.get("local"):
                    raise ReleaseValidationError(
                        "private repo cannot publish to public registries.\n"
                        f'Remove pipelines.{pipeline_name}.local or set "private": false.'
                    )


def validate_pipeline_config(config):
    """Validate pipeline configuration and required env vars.

    Returns loaded pipelines dict.
    Raises ReleaseValidationError on failure.
    """
    from . import load_pipelines

    if config.get("publish") is not None:
        raise ReleaseValidationError(
            "the 'publish' key in .rlsbl/config.json is no longer recognized. "
            "Use 'pipelines' instead."
        )
    if config.get("pipelines") is None:
        raise ReleaseValidationError(
            "no 'pipelines' key in .rlsbl/config.json. "
            "Add a pipelines section or run 'rlsbl scaffold'."
        )

    pipelines = load_pipelines(config)
    missing_vars = []
    for pl_name, pl in pipelines.items():
        if pl.local:
            for var in pl.required_env_vars():
                if var not in os.environ:
                    missing_vars.append(f"  pipeline '{pl_name}' requires {var}")
    if missing_vars:
        raise ReleaseValidationError(
            "missing environment variables for local pipelines:\n"
            + "\n".join(missing_vars)
        )

    return pipelines


def validate_gh_cli():
    """Validate that gh CLI is installed and authenticated.

    Raises ReleaseValidationError on failure.
    """
    from . import check_gh_installed, check_gh_auth

    if not check_gh_installed():
        raise ReleaseValidationError(
            "gh CLI is not installed. Install it from https://cli.github.com"
        )
    if not check_gh_auth():
        raise ReleaseValidationError(
            'gh CLI is not authenticated. Run "gh auth login" first.'
        )


def validate_clean_tree(flags):
    """Validate working tree is clean (or record pre-existing dirty files).

    Returns set of pre-existing dirty file paths.
    Raises ReleaseValidationError if tree is dirty and --allow-dirty not set.
    """
    from . import run, is_clean_tree

    pre_existing_dirty = set()
    if flags.get("allow-dirty"):
        dirty_output = run("git", ["status", "--porcelain"])
        if dirty_output:
            pre_existing_dirty = parse_porcelain_paths(dirty_output)
    elif not is_clean_tree():
        raise ReleaseValidationError(
            "working tree is not clean. Commit your changes first."
        )
    return pre_existing_dirty


def validate_branch_and_remote(flags):
    """Validate branch is main/master and not behind origin.

    Returns the current branch name.
    Raises ReleaseValidationError if local branch is behind origin.
    """
    from . import run, get_current_branch, remote_branch_exists

    branch = get_current_branch()
    if branch not in ("main", "master"):
        print(f'Warning: you are on branch "{branch}", not main/master.', file=sys.stderr)

    try:
        run("git", ["fetch", "origin", "--quiet"])
    except Exception:
        print("Warning: could not fetch from origin. Skipping remote-ahead check.", file=sys.stderr)
        return branch

    if not remote_branch_exists(branch):
        print(
            f"Remote branch origin/{branch} does not exist yet. Skipping remote-ahead check.",
            file=sys.stderr,
        )
        return branch

    try:
        behind_count = int(run("git", ["rev-list", "--count", f"HEAD..origin/{branch}"]))
    except Exception as e:
        raise ReleaseValidationError(
            f"could not check if local branch is behind origin: {e}\n"
            "Cannot verify remote-ahead status. Aborting for safety."
        ) from e
    if behind_count > 0:
        raise ReleaseValidationError(
            f"local branch is {behind_count} commit(s) behind origin/{branch}. Pull before releasing."
        )

    return branch


def resolve_monorepo_context(monorepo_root, project_root, log):
    """Resolve monorepo project context if inside a monorepo.

    Returns (monorepo_name, monorepo_project_path, is_library, is_dev_node).
    All values are None/False when not in a monorepo.
    Raises ReleaseValidationError if inside a monorepo but not a recognized project,
    or if the project is a dev_node.
    """
    from . import resolve_project

    if not monorepo_root:
        return None, None, False, False

    project = resolve_project(monorepo_root, str(project_root))
    if project is None:
        raise ReleaseValidationError(
            "current directory is inside a monorepo but not inside any project.\n"
            "Run 'rlsbl monorepo status' to see registered projects."
        )
    monorepo_name = project["name"]
    monorepo_project_path = project["path"]
    is_library = bool(project.get("library"))
    is_dev_node = bool(project.get("dev_node"))
    log(f"Monorepo project: {monorepo_name} ({monorepo_project_path})")
    if is_dev_node:
        raise ReleaseValidationError(
            "dev_node projects cannot be released. Dev nodes are "
            "infrastructure projects that do not produce releases. Remove "
            "dev_node = true from workspace.toml if this project should be "
            "releasable."
        )

    return monorepo_name, monorepo_project_path, is_library, is_dev_node


def compute_release_version(target, primary_path, bump_arg, monorepo_name,
                            monorepo_project_path, log):
    """Compute current and new version, bump type, and tag.

    Returns (current_version, new_version, bump_type, tag).
    Raises ReleaseValidationError on invalid bump type or duplicate tag.
    """
    from . import run, bump_version

    current_version = target.read_version(primary_path)
    log(f"Current version: {current_version}")

    if monorepo_name:
        current_tag = target.monorepo_tag_format(
            monorepo_name, current_version, path=monorepo_project_path
        )
    else:
        current_tag = target.tag_format(current_version)
    current_tag_exists = len(run("git", ["tag", "-l", current_tag])) > 0

    if not current_tag_exists:
        new_version = current_version
        bump_type = None
        tag = current_tag
        if bump_arg:
            log(f"First release: releasing {new_version} as-is (bump type ignored)")
        else:
            log(f"First release: {new_version}")
    else:
        bump_type = bump_arg if bump_arg else "patch"
        if bump_type not in VALID_BUMP_TYPES:
            raise ReleaseValidationError(
                f'invalid bump type "{bump_type}". Use: {", ".join(VALID_BUMP_TYPES)}'
            )
        new_version = bump_version(current_version, bump_type)
        if monorepo_name:
            tag = target.monorepo_tag_format(
                monorepo_name, new_version, path=monorepo_project_path
            )
        else:
            tag = target.tag_format(new_version)
        log(f"New version: {new_version} ({bump_type})")

    # Check tag doesn't already exist
    tag_output = run("git", ["tag", "-l", tag])
    if len(tag_output) > 0:
        raise ReleaseValidationError(f'tag "{tag}" already exists.')

    return current_version, new_version, bump_type, tag


def validate_changelog_state(project_dir, target, monorepo_name,
                             monorepo_project_path, config, monorepo_project=None):
    """Validate JSONL changelog is set up and passes validation.

    Returns the changes_dir path.
    Raises ReleaseValidationError on failure.
    """
    from . import changes_dir_exists, get_changes_dir, validate_unreleased

    if not changes_dir_exists(project_dir):
        raise ReleaseValidationError(
            "JSONL changelog not set up. Run 'rlsbl scaffold' to create .rlsbl/changes/"
        )

    changes_dir = get_changes_dir(project_dir)
    tag_glob = (
        target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path)
        if monorepo_name
        else None
    )
    validation = validate_unreleased(
        changes_dir, tag_glob=tag_glob, project=monorepo_project, config=config
    )
    if not validation["passed"]:
        lines = []
        for check_name, (passed, details) in validation["checks"].items():
            if not passed:
                for detail in details:
                    lines.append(f"  {check_name}: {detail}")
        raise ReleaseValidationError(
            "JSONL changelog validation failed:\n" + "\n".join(lines)
        )

    return changes_dir


def print_dry_run_summary(log, registry, monorepo_name, monorepo_project_path,
                          bump_type, current_version, new_version, tag,
                          commit_msg, branch, target_paths, project_dir,
                          changelog_entry, monorepo_root=None):
    """Print dry-run summary and return (caller should exit after this)."""
    from . import TARGETS, load_workspace

    log("\n--- Dry run summary ---")
    log(f"Registry:  {registry}")
    if monorepo_name:
        log(f"Project:   {monorepo_name} ({monorepo_project_path})")
    if bump_type:
        log(f"Bump:      {current_version} -> {new_version} ({bump_type})")
    else:
        log(f"Version:   {new_version} (first release)")
    log(f"Tag:       {tag}")
    log(f"Commit:    {commit_msg}")
    log(f"Branch:    {branch}")
    # Show other version files that would be synced
    other_files = []
    for t_name, t_path in target_paths.items():
        if t_name == registry:
            continue
        other_reg = TARGETS.get(t_name)
        if other_reg and other_reg.check_project_exists(t_path):
            other_file = other_reg.version_file(t_path)
            if other_file:
                rel = os.path.relpath(os.path.join(t_path, other_file), project_dir)
                other_files.append(os.path.normpath(rel))
    if other_files:
        log(f"Sync to:   {', '.join(other_files)}")
    # Show subtree publishing info
    if monorepo_name:
        target = TARGETS[registry]
        try:
            projects = load_workspace(monorepo_root)
            proj_dict = next((p for p in projects if p["name"] == monorepo_name), None)
            subtree_remote = proj_dict.get("subtree_remote") if proj_dict else None
        except Exception as e:
            from ...utils import warn_exception
            warn_exception("could not load workspace for subtree info", e)
            subtree_remote = None
        if subtree_remote:
            plain_tag = target.tag_format(new_version)
            log(f"Subtree:   {subtree_remote} (tag: {plain_tag})")
    log(f"Changelog:\n{changelog_entry or '(none)'}")
    log("--- No changes made ---")


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
