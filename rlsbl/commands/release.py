"""Release command that bumps the project version, commits the change, tags the commit, pushes to remote, and creates a GitHub Release."""

import json
import os
import subprocess
import sys
import time

from ..changelog import (
    changes_dir_exists,
    finalize_version,
    generate_changelog,
    get_changes_dir,
    validate_unreleased,
)
from ..config import read_deploy_config, read_json_config, read_project_config, should_tag
from ..deploy import deploy_target
from ..lock import acquire_lock, release_lock
from ..targets import TARGETS, detect_targets, _parse_target_entry
from ..tagging import ensure_github_topic, ensure_npm_keyword, ensure_pypi_keyword
from ..workspace import find_workspace_root, load_workspace, resolve_project
from ..utils import (
    bump_version,
    check_gh_auth,
    check_gh_installed,
    commit_files,
    extract_changelog_entry,
    extract_changelog_entry_from_text,
    get_current_branch,
    get_hook_timeout,
    get_push_timeout,
    is_clean_tree,
    push_if_needed,
    require_tool,
    run,
)

VALID_BUMP_TYPES = ("patch", "minor", "major")


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


def resolve_target_paths(version_dir="."):
    """Build a dict mapping target names to their resolved paths.

    Uses detect_targets() which reads .rlsbl/config.json "targets" (supporting
    both plain strings and dicts with "name"/"path") and falls back to
    auto-detection.

    Returns dict[str, str] mapping target name -> resolved directory path.
    """
    entries = detect_targets(version_dir)
    return {e.name: e.path for e in entries}


def resolve_release_targets(primary, flags, version_dir="."):
    """Compute the effective set of secondary targets for this release.

    Reads the baseline from .rlsbl/config.json "release_targets" list.
    If absent, falls back to auto-detect (all targets that detect(".")).
    Entries can be plain strings or dicts with "name" and optional "path".

    The primary target is always excluded from the secondary set
    (it's handled separately by the main release flow).

    Returns a dict mapping target name -> resolved directory path.
    """
    from ..targets import TARGETS as ALL_TARGETS

    config = read_json_config(os.path.join(version_dir, ".rlsbl", "config.json"))
    configured = config.get("release_targets")

    # Build baseline: dict of name -> path
    if configured is not None:
        baseline = {}
        for entry in configured:
            try:
                te = _parse_target_entry(entry, version_dir)
            except (ValueError, TypeError):
                # Unparseable entry -- skip
                continue
            if te.name in ALL_TARGETS:
                baseline[te.name] = te.path
    else:
        # Auto-detect: use detect_targets which handles config and fallback
        baseline = resolve_target_paths(version_dir)

    # Never include the primary target in the secondary set
    baseline.pop(primary, None)

    return baseline


def _run_builtin_tests(registry, flags, project_dir=None):
    """Run built-in tests for the detected project type.

    Detects the project type from registry and runs the appropriate test command.
    When project_dir is set (monorepo mode), subprocess calls use it as cwd and
    filesystem checks are resolved relative to it.
    Returns True if tests pass, calls sys.exit(1) on failure.
    """
    if flags.get("skip-tests") or flags.get("dry-run"):
        return True

    print("Running tests...")

    if registry == "pypi":
        config = read_project_config()
        uv_verbose = config.get("uv_sync_verbose", False)
        if require_tool("uv", fatal=False):
            sync_cmd = ["uv", "sync"]
            if not uv_verbose:
                sync_cmd.append("--quiet")
            result = subprocess.run(sync_cmd, cwd=project_dir)
            if result.returncode != 0:
                print("Error: uv sync failed.", file=sys.stderr)
                sys.exit(1)
            result = subprocess.run(["uv", "run", "pytest"], cwd=project_dir)
        elif require_tool("pytest", fatal=False):
            result = subprocess.run(["pytest"], cwd=project_dir)
        else:
            print("Warning: neither uv nor pytest found, skipping tests.", file=sys.stderr)
            return True
    elif registry == "go":
        result = subprocess.run(["go", "test", "./...", "-race", "-short", "-count=1"], cwd=project_dir)
    elif registry == "npm":
        pkg_path = os.path.join(project_dir, "package.json") if project_dir else "package.json"
        if os.path.exists(pkg_path):
            try:
                with open(pkg_path, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                if pkg.get("scripts", {}).get("test"):
                    result = subprocess.run(["npm", "test"], cwd=project_dir)
                else:
                    print("No test script in package.json, skipping tests.")
                    return True
            except (json.JSONDecodeError, OSError):
                print("Warning: could not read package.json, skipping tests.", file=sys.stderr)
                return True
        else:
            return True
    else:
        # Unknown registry, skip tests
        return True

    if result.returncode != 0:
        print("Error: tests failed.", file=sys.stderr)
        sys.exit(1)

    return True


def _run_builtin_lint(flags, is_library=False, project_dir=None):
    """Run built-in library lint.

    Counts errors and warnings from lint results. Exits on errors.
    Only runs on library projects (monorepo projects with library = true).
    When project_dir is set (monorepo mode), lint runs against that directory.
    """
    if flags.get("skip-lint") or flags.get("dry-run"):
        return True

    if not is_library:
        print("Skipping lint (not a library project)")
        return True

    print("Running lint...")

    from ..lint import lint_library

    results = lint_library(project_dir if project_dir else ".")

    errors = [r for r in results if r.severity == "error"]
    warnings = [r for r in results if r.severity == "warning"]

    if errors:
        for r in errors:
            print(f"  {r.file}:{r.line}: {r.rule}: {r.message}", file=sys.stderr)
        print(f"Error: library lint found {len(errors)} error(s).", file=sys.stderr)
        sys.exit(1)

    if warnings:
        for r in warnings:
            print(f"  {r.file}:{r.line}: {r.rule}: {r.message}", file=sys.stderr)
        print(f"Library lint: {len(warnings)} warning(s).")
    else:
        print("Library lint: clean.")

    return True


def _run_selfdoc_check(flags, project_dir=None):
    """Run selfdoc check if selfdoc.json exists in the project directory.

    Checks documentation consistency before releasing. Non-fatal if selfdoc
    is not installed; fatal if it is installed and the check fails.
    When project_dir is set (monorepo mode), checks are resolved relative to it.
    """
    if flags.get("skip-docs") or flags.get("dry-run"):
        if flags.get("skip-docs"):
            print("Skipping selfdoc check")
        return True

    check_dir = project_dir if project_dir else "."
    selfdoc_config = os.path.join(check_dir, "selfdoc.json")
    if not os.path.exists(selfdoc_config):
        return True

    if not require_tool("selfdoc", fatal=False):
        print("Note: selfdoc.json found but selfdoc is not installed. Skipping docs check.")
        return True

    print("Running selfdoc check...")
    subprocess.run(["selfdoc", "check"], cwd=project_dir, check=True)
    return True


def run_cmd(registry, args, flags):
    """Release command handler.

    Bumps version, commits, pushes, and creates a GitHub Release.
    """
    quiet = flags.get("quiet", False)

    def log(msg):
        if not quiet:
            print(msg)

    # Load env file if configured
    config = read_project_config()
    env_file = config.get("env_file")
    if env_file:
        from ..config import load_env_file
        load_env_file(env_file)
        if "CF_ACCOUNT_ID" in os.environ and "CLOUDFLARE_ACCOUNT_ID" not in os.environ:
            os.environ["CLOUDFLARE_ACCOUNT_ID"] = os.environ["CF_ACCOUNT_ID"]

    reg = TARGETS[registry]

    # Check prerequisites
    if not check_gh_installed():
        print("Error: gh CLI is not installed. Install it from https://cli.github.com", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print('Error: gh CLI is not authenticated. Run "gh auth login" first.', file=sys.stderr)
        sys.exit(1)

    # Clean working tree
    pre_existing_dirty = set()
    if flags.get("allow-dirty"):
        # Record which files are already dirty so the re-check guard inside
        # _run_release_mutating can distinguish pre-existing dirt from genuinely
        # unexpected modifications that appeared during the release.
        dirty_output = run("git", ["status", "--porcelain"])
        if dirty_output:
            pre_existing_dirty = parse_porcelain_paths(dirty_output)
    elif not is_clean_tree():
        print("Error: working tree is not clean. Commit your changes first.", file=sys.stderr)
        sys.exit(1)

    # Branch check
    branch = get_current_branch()
    if branch not in ("main", "master"):
        print(f'Warning: you are on branch "{branch}", not main/master.', file=sys.stderr)

    # Remote-ahead check: abort if local branch is behind origin
    if not flags.get("skip-remote-check"):
        try:
            run("git", ["fetch", "origin", "--quiet"])
        except Exception:
            # Network failure or no remote — warn but don't block the release
            print("Warning: could not fetch from origin. Skipping remote-ahead check.", file=sys.stderr)
        else:
            try:
                behind_count = int(run("git", ["rev-list", "--count", f"HEAD..origin/{branch}"]))
            except Exception:
                # Remote branch may not exist yet — not an error
                behind_count = 0
            if behind_count > 0:
                print(
                    f"Error: local branch is {behind_count} commit(s) behind origin/{branch}. Pull before releasing.",
                    file=sys.stderr,
                )
                sys.exit(1)

    # Monorepo context detection
    monorepo_root = find_workspace_root(".")
    monorepo_name = None
    monorepo_project_path = None
    is_library = False

    if monorepo_root:
        project = resolve_project(monorepo_root, ".")
        if project is None:
            print("Error: current directory is inside a monorepo but not inside any project.", file=sys.stderr)
            print("Run 'rlsbl monorepo status' to see registered projects.", file=sys.stderr)
            sys.exit(1)
        monorepo_name = project["name"]
        monorepo_project_path = project["path"]
        is_library = bool(project.get("library"))
        # Change to monorepo root so all paths (git and filesystem) are
        # relative to the repo root, matching git's expectations.
        os.chdir(monorepo_root)
        log(f"Monorepo project: {monorepo_name} ({monorepo_project_path})")

    # Scoped version directory: project subdir in monorepo, repo root otherwise
    version_dir = monorepo_project_path if monorepo_name else "."

    # Get target instance for tag_format/build/publish
    target = TARGETS[registry]

    # Resolve per-target paths from config (supports subdirectory targets)
    target_paths = resolve_target_paths(version_dir)

    # Primary target's path: from config if available, else version_dir
    primary_path = target_paths.get(registry, version_dir)

    # Current version
    current_version = reg.read_version(primary_path)
    log(f"Current version: {current_version}")

    # If the current version has never been tagged, release it as-is (bootstrap)
    if monorepo_name:
        current_tag = target.monorepo_tag_format(monorepo_name, current_version, path=monorepo_project_path)
    else:
        current_tag = target.tag_format(current_version)
    current_tag_exists = len(run("git", ["tag", "-l", current_tag])) > 0

    if not current_tag_exists:
        new_version = current_version
        bump_type = None
        tag = current_tag
        if args:
            log(f"First release: releasing {new_version} as-is (bump type ignored)")
        else:
            log(f"First release: {new_version}")
    else:
        bump_type = args[0] if args else "patch"
        if bump_type not in VALID_BUMP_TYPES:
            print(
                f'Error: invalid bump type "{bump_type}". Use: {", ".join(VALID_BUMP_TYPES)}',
                file=sys.stderr,
            )
            sys.exit(1)

        new_version = bump_version(current_version, bump_type)
        if monorepo_name:
            tag = target.monorepo_tag_format(monorepo_name, new_version, path=monorepo_project_path)
        else:
            tag = target.tag_format(new_version)
        log(f"New version: {new_version} ({bump_type})")

    # Check tag doesn't already exist
    tag_output = run("git", ["tag", "-l", tag])
    if len(tag_output) > 0:
        print(f'Error: tag "{tag}" already exists.', file=sys.stderr)
        sys.exit(1)

    # Validate JSONL changelog
    if not changes_dir_exists(version_dir):
        print(
            "Error: JSONL changelog not set up. Run 'rlsbl scaffold --update' to create .rlsbl/changes/",
            file=sys.stderr,
        )
        sys.exit(1)

    changes_dir = get_changes_dir(version_dir)
    tag_glob = target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path) if monorepo_name else None
    validation = validate_unreleased(changes_dir, tag_glob=tag_glob)
    if not validation["passed"]:
        print("Error: JSONL changelog validation failed:", file=sys.stderr)
        for check_name, (passed, details) in validation["checks"].items():
            if not passed:
                for detail in details:
                    print(f"  {check_name}: {detail}", file=sys.stderr)
        sys.exit(1)
    # Compute the changelog content in memory only. We defer writing CHANGELOG.md
    # (and per-version .md files) to disk until after pre-release checks pass,
    # so that an aborted release leaves the working tree exactly as it was.
    # The actual write to disk happens just after acquire_lock() below.
    changelog_content = generate_changelog(version_dir, write_to_disk=False)
    log("Generated CHANGELOG.md from JSONL entries (in-memory preview)")

    if isinstance(changelog_content, str):
        changelog_entry = extract_changelog_entry_from_text(changelog_content, new_version)
    else:
        # Mocked in tests (returns MagicMock). Fall back to the on-disk file,
        # which test fixtures pre-populate with a known entry.
        changelog_path = os.path.join(version_dir, "CHANGELOG.md")
        if not os.path.exists(changelog_path):
            print(
                "Error: CHANGELOG.md not found after generation.",
                file=sys.stderr,
            )
            sys.exit(1)
        changelog_entry = extract_changelog_entry(changelog_path, new_version)

    # In monorepo mode, hooks/tests/lint must run from the project subdirectory
    # (not the monorepo root that os.chdir switched to).
    abs_project_dir = os.path.join(monorepo_root, monorepo_project_path) if monorepo_root else None

    # Run pre-checks hook if present
    pre_checks_script = os.path.join(version_dir, ".rlsbl", "hooks", "pre-checks.sh")
    if os.path.exists(pre_checks_script):
        pre_checks_script = os.path.abspath(pre_checks_script)
        log("Running pre-checks hook...")
        hook_timeout = get_hook_timeout()
        try:
            env = os.environ.copy()
            env["RLSBL_VERSION"] = new_version
            subprocess.run(["bash", pre_checks_script], env=env, check=True, timeout=hook_timeout, cwd=abs_project_dir)
        except subprocess.CalledProcessError as e:
            print(f"Error: pre-checks hook exited with code {e.returncode}.", file=sys.stderr)
            sys.exit(1)
        except subprocess.TimeoutExpired:
            print(f"Error: pre-checks hook timed out after {hook_timeout}s.", file=sys.stderr)
            sys.exit(1)

    # Built-in test runner
    _run_builtin_tests(registry, flags, project_dir=abs_project_dir)

    # Built-in lint runner
    _run_builtin_lint(flags, is_library=is_library, project_dir=abs_project_dir)

    # Built-in selfdoc check
    _run_selfdoc_check(flags, project_dir=abs_project_dir)

    # Run pre-release hook if present
    pre_release_script = os.path.join(version_dir, ".rlsbl", "hooks", "pre-release.sh")
    if os.path.exists(pre_release_script):
        pre_release_script = os.path.abspath(pre_release_script)
        log("Running pre-release hook...")
        hook_timeout = get_hook_timeout()
        try:
            env = os.environ.copy()
            env["RLSBL_VERSION"] = new_version
            subprocess.run(["bash", pre_release_script], env=env, check=True, timeout=hook_timeout, cwd=abs_project_dir)
        except subprocess.CalledProcessError as e:
            print(f"Error: pre-release hook exited with code {e.returncode}.", file=sys.stderr)
            sys.exit(1)
        except subprocess.TimeoutExpired:
            print(f"Error: pre-release hook timed out after {hook_timeout}s.", file=sys.stderr)
            sys.exit(1)

    # Commit message: scoped in monorepo mode, plain tag otherwise
    commit_msg = f"{monorepo_name}: release v{new_version}" if monorepo_name else tag

    # Dry run: print summary and return
    if flags.get("dry-run", False):
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
        # Show other version files that would be synced (with per-target paths)
        other_files = []
        for t_name, t_path in target_paths.items():
            if t_name == registry:
                continue
            other_reg = TARGETS.get(t_name)
            if other_reg and other_reg.check_project_exists(t_path):
                other_file = other_reg.version_file()
                if other_file:
                    rel = os.path.relpath(os.path.join(t_path, other_file), version_dir)
                    other_files.append(os.path.normpath(rel))
        if other_files:
            log(f"Sync to:   {', '.join(other_files)}")
        # Show subtree publishing info in dry-run
        if monorepo_name:
            try:
                projects = load_workspace(".")
                proj_dict = next((p for p in projects if p["name"] == monorepo_name), None)
                subtree_remote = proj_dict.get("subtree_remote") if proj_dict else None
            except Exception:
                subtree_remote = None
            if subtree_remote:
                plain_tag = target.tag_format(new_version)
                log(f"Subtree:   {subtree_remote} (tag: {plain_tag})")
        log(f"Changelog:\n{changelog_entry}")
        log("--- No changes made ---")
        return

    # Resolve which secondary targets participate in this release
    secondary_targets = resolve_release_targets(registry, flags, version_dir=version_dir)

    # Acquire advisory lock to prevent concurrent rlsbl operations.
    # In monorepo mode the lock goes in .rlsbl-monorepo/ (the workspace
    # config dir) instead of .rlsbl/ to avoid creating a spurious directory.
    lock_dir = ".rlsbl-monorepo" if monorepo_name else ".rlsbl"
    acquire_lock(lock_dir=lock_dir)

    # Pre-release checks all passed; now safe to materialize CHANGELOG.md and
    # per-version .md files on disk. The version-bump commit below includes
    # CHANGELOG.md, so it must exist before commit_files() runs.
    generate_changelog(version_dir)

    try:
        _run_release_mutating(
            registry, reg, flags, quiet, log, new_version, current_version,
            bump_type, tag, branch, changelog_entry, target,
            secondary_targets=secondary_targets,
            monorepo_name=monorepo_name,
            monorepo_project_path=monorepo_project_path,
            version_dir=version_dir,
            commit_msg=commit_msg,
            primary_path=primary_path,
            target_paths=target_paths,
            lock_dir=lock_dir,
            pre_existing_dirty=pre_existing_dirty,
            abs_project_dir=abs_project_dir,
        )
    finally:
        release_lock()


def _run_release_mutating(registry, reg, flags, quiet, log, new_version, current_version,
                          bump_type, tag, branch, changelog_entry, target,
                          secondary_targets=None, monorepo_name=None,
                          monorepo_project_path=None,
                          version_dir=".", commit_msg=None,
                          primary_path=None, target_paths=None,
                          lock_dir=".rlsbl",
                          pre_existing_dirty=None,
                          abs_project_dir=None):
    """Inner release logic that runs under the advisory lock (mutating phase)."""
    if commit_msg is None:
        commit_msg = tag
    if primary_path is None:
        primary_path = version_dir
    if target_paths is None:
        target_paths = resolve_target_paths(version_dir)

    def vpath(filename):
        """Join filename with version_dir and normalize (e.g. './x' -> 'x')."""
        return os.path.normpath(os.path.join(version_dir, filename))

    def target_vpath(t_path, filename):
        """Join filename with a target's resolved path, normalized.

        Target paths from detect_targets() are already resolved relative to
        the repo root, so we just join and normalize.
        """
        return os.path.normpath(os.path.join(t_path, filename))

    # Pre-compute expected version files for the confirmation prompt display.
    # The actual files_to_commit list is built from write_version() return
    # values below, which may include additional files (e.g. __init__.py).
    version_file = reg.version_file()
    preview_files = []
    if version_file:
        preview_files.append(target_vpath(primary_path, version_file))
    for t_name, t_path in target_paths.items():
        if t_name == registry:
            continue
        other_reg = TARGETS.get(t_name)
        if other_reg and other_reg.check_project_exists(t_path):
            other_file = other_reg.version_file()
            if other_file:
                preview_files.append(target_vpath(t_path, other_file))

    # Confirmation prompt (skip with --yes)
    if not flags.get("yes"):
        bump_label = f" ({bump_type})" if bump_type else ""
        print(f"\nAbout to release {new_version}{bump_label} on {branch}")
        print(f"  Tag: {tag}")
        if preview_files:
            print(f"  Files: {', '.join(preview_files)}")
        else:
            print("  Files: (none -- version is the git tag)")
        if should_tag(flags):
            print("  Will add 'rlsbl' keyword to project manifests")
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # Write new version to version files (skip if version didn't change, e.g. first release)
    # Build files_to_commit from the paths actually modified by write_version().
    files_to_commit = []
    if new_version != current_version:
        modified = reg.write_version(primary_path, new_version)
        for rel in modified:
            files_to_commit.append(target_vpath(primary_path, rel))
        if modified:
            log(f"Updated version in {', '.join(target_vpath(primary_path, r) for r in modified)}")

        # Sync version to other configured/detected targets (per-target paths)
        for t_name, t_path in target_paths.items():
            if t_name == registry:
                continue
            other_reg = TARGETS.get(t_name)
            if other_reg and other_reg.check_project_exists(t_path):
                other_modified = other_reg.write_version(t_path, new_version)
                for rel in other_modified:
                    files_to_commit.append(target_vpath(t_path, rel))
                if other_modified:
                    log(f"Synced version to {', '.join(target_vpath(t_path, r) for r in other_modified)}")

    # Ecosystem tagging: add keyword to manifests if enabled
    if should_tag(flags):
        npm_path = target_paths.get("npm", version_dir)
        try:
            if TARGETS["npm"].check_project_exists(npm_path):
                if ensure_npm_keyword(npm_path, quiet=quiet):
                    pkg_path = target_vpath(npm_path, "package.json")
                    if pkg_path not in files_to_commit:
                        files_to_commit.append(pkg_path)
        except Exception:
            pass
        pypi_path = target_paths.get("pypi", version_dir)
        try:
            if TARGETS["pypi"].check_project_exists(pypi_path):
                if ensure_pypi_keyword(pypi_path, quiet=quiet):
                    pyproject_path = target_vpath(pypi_path, "pyproject.toml")
                    if pyproject_path not in files_to_commit:
                        files_to_commit.append(pyproject_path)
        except Exception:
            pass

    # Update .rlsbl/version marker so it's included in the release commit
    rlsbl_version_marker = vpath(os.path.join(".rlsbl", "version"))
    if os.path.exists(os.path.dirname(rlsbl_version_marker)):
        try:
            from .. import __version__ as rlsbl_ver
            with open(rlsbl_version_marker, "w") as f:
                f.write(rlsbl_ver + "\n")
            if rlsbl_version_marker not in files_to_commit:
                files_to_commit.append(rlsbl_version_marker)
        except Exception:
            pass

    # Include the generated CHANGELOG.md in the commit
    changelog_file = vpath("CHANGELOG.md")
    if changelog_file not in files_to_commit:
        files_to_commit.append(changelog_file)

    # Build step (no-op for npm/pypi/go targets)
    try:
        target.build(primary_path, new_version)
    except Exception as e:
        print(f"Warning: target build step failed: {e}", file=sys.stderr)

    # Re-check working tree: abort if files outside our expected set were modified
    # (guards against concurrent processes dirtying the tree after our initial check)
    dirty_output = run("git", ["status", "--porcelain"])
    if dirty_output:
        dirty_files = parse_porcelain_paths(dirty_output)
        expected_files = set(files_to_commit)
        expected_files.add(os.path.join(lock_dir, "lock"))
        # The .validated cache is written by changelog validation earlier in the
        # release flow.  It may be tracked (dirty) or gitignored (invisible to
        # git status).  Either way it is not a concurrent-change signal.
        validated_file = os.path.normpath(
            os.path.join(get_changes_dir(version_dir), ".validated")
        )
        expected_files.add(validated_file)
        # When --allow-dirty was used, files that were already dirty before the
        # release started are not "unexpected" -- only genuinely new modifications
        # (from e.g. concurrent processes) should trigger the abort.
        if pre_existing_dirty:
            expected_files |= pre_existing_dirty
        unexpected = dirty_files - expected_files
        if unexpected:
            unexpected_list = ", ".join(sorted(unexpected))
            print(
                f"Unexpected modified files detected (possible concurrent change): {unexpected_list}. Aborting release.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Commit if any of the files we track actually have changes.
    # Don't use is_clean_tree() as a proxy — the advisory lock file (.rlsbl/lock)
    # makes the tree appear dirty even when no release-relevant files changed.
    def has_staged_or_modified(paths):
        """Check if any of the given paths have actual changes vs the index."""
        for p in paths:
            diff = run("git", ["diff", "--name-only", "--", p]) if os.path.exists(p) else ""
            status = run("git", ["status", "--porcelain", "--", p])
            if diff or status:
                return True
        return False

    # Capture HEAD before any git mutations so we can roll back on failure
    pre_release_sha = run("git", ["rev-parse", "HEAD"])

    needs_commit = new_version != current_version or has_staged_or_modified(files_to_commit)
    try:
        if files_to_commit and needs_commit:
            commit_files(commit_msg, files_to_commit)
            log(f"Committed: {commit_msg}")
        elif not needs_commit:
            log("No changes to commit")

        # Finalize JSONL changelog: rename unreleased.jsonl to x.y.z.jsonl
        changes_dir = get_changes_dir(version_dir)
        finalize_version(changes_dir, new_version)
        log(f"Finalized JSONL changelog for {new_version}")
        # Regenerate CHANGELOG.md so version headings reflect the finalized
        # version (e.g. "## 0.25.0") instead of "## Unreleased"
        generate_changelog(version_dir)
        changelog_path = os.path.join(version_dir, "CHANGELOG.md")
        changelog_entry = extract_changelog_entry(changelog_path, new_version)
        log("Regenerated CHANGELOG.md with version heading")
        # Commit the finalized JSONL file and the new empty unreleased.jsonl
        jsonl_finalized = os.path.normpath(os.path.join(changes_dir, f"{new_version}.jsonl"))
        jsonl_unreleased = os.path.normpath(os.path.join(changes_dir, "unreleased.jsonl"))
        # Also commit the generated per-version .md file if it exists
        jsonl_md = os.path.normpath(os.path.join(changes_dir, f"{new_version}.md"))
        changelog_file = vpath("CHANGELOG.md")
        finalize_files = [jsonl_finalized, jsonl_unreleased, changelog_file]
        if os.path.exists(jsonl_md):
            finalize_files.append(jsonl_md)
        commit_files(f"chore: finalize changelog for {new_version}", finalize_files)
        log(f"Committed finalized changelog files")

        # Create local git tag
        run("git", ["tag", tag])
        log(f"Tagged: {tag}")

        # Push commits and tag
        push_timeout = get_push_timeout()
        if push_timeout != 120:
            log(f"Push timeout: {push_timeout}s (from RLSBL_PUSH_TIMEOUT)")
        # Mark pushes as release-authorized so the pre-push hook skips its
        # "manual push" warning. The hook still runs JSONL coverage checks.
        push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
        push_if_needed(branch, env=push_env)
        run("git", ["push", "origin", tag], timeout=push_timeout, env=push_env)
        log(f"Pushed to origin/{branch}")
    except Exception:
        # Roll back local mutations: delete tag (may not exist yet) and
        # reset commits so the working tree looks like it did before the
        # release attempt.
        try:
            run("git", ["tag", "-d", tag])
        except Exception:
            pass
        run("git", ["reset", "--hard", pre_release_sha])
        print(
            f"Error: release failed. Local state has been rolled back to {pre_release_sha[:10]}.",
            file=sys.stderr,
        )
        print(
            "If the branch was partially pushed, you may need to run:\n"
            f"  git push --force-with-lease origin {branch}",
            file=sys.stderr,
        )
        raise

    # Capture the pushed commit SHA now, before any post-release hooks that
    # might create new commits and move HEAD past the release commit.
    pushed_sha = run("git", ["rev-parse", "HEAD"])

    # Create GitHub Release using a temp notes file
    # Notes file cleanup is deferred until after subtree publishing (which reuses it)
    notes_file = f".rlsbl-notes-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    try:
        with open(writing_file, "w", encoding="utf-8") as f:
            f.write(changelog_entry)
        os.rename(writing_file, notes_file)
        run("gh", ["release", "create", tag, "--title", tag, "--notes-file", notes_file])
        log(f"Created GitHub Release: {tag}")

        # Subtree publishing for monorepo projects with subtree_remote configured
        if monorepo_name and monorepo_project_path:
            try:
                projects = load_workspace(".")
                proj_dict = None
                for p in projects:
                    if p["name"] == monorepo_name:
                        proj_dict = p
                        break
                subtree_remote = proj_dict.get("subtree_remote") if proj_dict else None
            except Exception:
                subtree_remote = None

            if subtree_remote:
                plain_tag = target.tag_format(new_version)
                log(f"Publishing subtree to {subtree_remote}...")
                try:
                    run("git", ["subtree", "split", f"--prefix={monorepo_project_path}", "-b", "_rlsbl-subtree-tmp"])
                    run("git", ["push", subtree_remote, f"_rlsbl-subtree-tmp:refs/tags/{plain_tag}"], env=push_env)
                    run("git", ["push", subtree_remote, "_rlsbl-subtree-tmp:refs/heads/main"], env=push_env)
                    log(f"Subtree published: {plain_tag} -> {subtree_remote}")
                except Exception as e:
                    print(f"Warning: subtree push failed: {e}", file=sys.stderr)
                finally:
                    try:
                        run("git", ["branch", "-D", "_rlsbl-subtree-tmp"])
                    except Exception:
                        pass

                # Create GitHub Release on the mirror repo (non-fatal)
                try:
                    run("gh", ["release", "create", plain_tag, "--repo", subtree_remote,
                         "--title", plain_tag, "--notes-file", notes_file])
                    log(f"Created mirror GitHub Release: {plain_tag} on {subtree_remote}")
                except Exception as e:
                    print(f"Warning: mirror GitHub Release failed: {e}", file=sys.stderr)
    finally:
        # Clean up temp files after both main and mirror releases
        for tmp in (notes_file, writing_file):
            if os.path.exists(tmp):
                os.unlink(tmp)

    # Publish step (no-op for npm/pypi/go targets)
    try:
        target.publish(primary_path, new_version)
    except Exception as e:
        print(f"Warning: target publish step failed: {e}", file=sys.stderr)

    # Multi-target: run build/publish for secondary targets resolved earlier
    if secondary_targets:
        from ..targets import TARGETS as ALL_TARGETS
        for sec_name in sorted(secondary_targets):
            sec_target = ALL_TARGETS.get(sec_name)
            if sec_target is None:
                continue
            sec_path = secondary_targets[sec_name]
            try:
                sec_target.build(sec_path, new_version)
            except Exception as e:
                print(f"Warning: {sec_name} target build failed: {e}", file=sys.stderr)
            try:
                sec_target.publish(sec_path, new_version)
            except Exception as e:
                print(f"Warning: {sec_name} target publish failed: {e}", file=sys.stderr)

    # Deploy phase (after publish, before post-release hook)
    deploy_targets, deploy_errors = read_deploy_config()
    if deploy_targets and not deploy_errors:
        current_branch = get_current_branch()
        for target_config in deploy_targets:
            print(f"\nDeploying to {target_config['name']}...")
            result = deploy_target(target_config, current_branch)
            if result.success:
                print(f"  Deploy to {result.target_name}: {result.message}")
            else:
                print(f"  Deploy to {result.target_name} FAILED: {result.message}", file=sys.stderr)
                if result.rolled_back:
                    print("  Rollback was executed.", file=sys.stderr)
                print(f"  Retry with: rlsbl deploy {result.target_name}", file=sys.stderr)
                break  # Stop at first failure
    elif deploy_errors:
        print("Warning: deploy config has errors, skipping deploy:", file=sys.stderr)
        for err in deploy_errors:
            print(f"  {err}", file=sys.stderr)
    # If no deploy targets configured, silently skip (most projects don't have deploy)

    # Ecosystem tagging: add GitHub topic after release is created
    if should_tag(flags):
        ensure_github_topic(quiet=quiet)

    # Run post-release hook if present (non-fatal: release is already complete)
    post_release_script = os.path.join(version_dir, ".rlsbl", "hooks", "post-release.sh")
    if os.path.exists(post_release_script):
        post_release_script = os.path.abspath(post_release_script)
        log("Running post-release hook...")
        hook_timeout = get_hook_timeout()
        try:
            env = os.environ.copy()
            env["RLSBL_VERSION"] = new_version
            subprocess.run(["bash", post_release_script], env=env, check=True, timeout=hook_timeout, cwd=abs_project_dir)
        except subprocess.CalledProcessError as e:
            print(f"Warning: post-release hook exited with code {e.returncode}.", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print(f"Warning: post-release hook timed out after {hook_timeout}s.", file=sys.stderr)

    # Hint: how to watch CI for this release (uses SHA captured before post-release hooks)
    log(f"Watch CI: rlsbl watch {pushed_sha}")

    log(f"\nRelease {new_version} complete!")
