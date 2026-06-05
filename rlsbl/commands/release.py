"""Release command: bumps version, validates changelog, runs hooks, regenerates selfdoc, syncs lockfiles, tags, pushes, and creates a GitHub Release."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from ..changelog import (
    changes_dir_exists,
    finalize_version,
    generate_changelog,
    generate_version_file,
    get_changes_dir,
    validate_unreleased,
)
from ..config import read_deploy_config, read_json_config, should_tag
from ..pipelines import load_pipelines
from ..deploy import deploy_target
from ..lock import acquire_lock, release_lock
from ..targets import TARGETS, detect_targets, _parse_target_entry
from ..tagging import ensure_github_topic, ensure_npm_keyword, ensure_pypi_keyword
from ..strictcli_detect import detect_strictcli
from ..workspace import load_workspace, resolve_project
from ..utils import (
    bump_version,
    check_gh_auth,
    check_gh_installed,
    commit_files,
    commit_files_if_changed,
    extract_changelog_entry,
    extract_changelog_entry_from_text,
    get_current_branch,
    get_hook_timeout,
    get_push_timeout,
    has_staged_or_modified,
    is_clean_tree,
    push_if_needed,
    require_tool,
    run,
)

VALID_BUMP_TYPES = ("patch", "minor", "major")


def _bump_selfdoc_version(project_dir, new_version):
    """Bump version in selfdoc.json if it exists. Returns list of modified file paths."""
    import tempfile

    config_path = os.path.join(project_dir, "selfdoc.json")
    if not os.path.exists(config_path):
        return []

    with open(config_path, "r", encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    data["version"] = new_version
    versions = data.get("versions")
    if versions and isinstance(versions, list):
        versions[-1]["version"] = new_version

    # Detect indent from existing file
    indent = 2
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = len(line) - len(stripped)
            break

    new_content = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=project_dir, prefix=".selfdoc.json.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, config_path)
    except BaseException:
        os.unlink(tmp_path)
        raise
    return ["selfdoc.json"]


def _compute_content_hash(content):
    """SHA-256 of content with trailing whitespace stripped."""
    return hashlib.sha256(content.rstrip().encode("utf-8")).hexdigest()


# Lazily computed on first access via _get_pre_release_template_hashes().
_PRE_RELEASE_TEMPLATE_HASHES = None


def _get_pre_release_template_hashes():
    """Return a frozenset of content hashes for known scaffold template versions of pre-release.sh.

    Currently there is only one version (the template has never changed),
    but using a set follows the same pattern as hook_hashes.py, making it
    easy to add historical versions later.
    """
    global _PRE_RELEASE_TEMPLATE_HASHES
    if _PRE_RELEASE_TEMPLATE_HASHES is not None:
        return _PRE_RELEASE_TEMPLATE_HASHES

    template_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "templates", "shared", "hooks", "pre-release.sh.tpl",
    )
    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    # V1: original scaffold template (before the comment was updated to
    # describe the override behavior).
    _V1 = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# Project-specific pre-release checks.\n"
        "# Built-in checks (tests, lint) run automatically before this hook.\n"
        "# Add custom validation here, e.g.:\n"
        "#   - Check for uncommitted documentation\n"
        "#   - Verify external service connectivity\n"
        "#   - Run integration tests not covered by the test suite\n"
    )

    _PRE_RELEASE_TEMPLATE_HASHES = frozenset({
        _compute_content_hash(template_content),
        _compute_content_hash(_V1),
    })
    return _PRE_RELEASE_TEMPLATE_HASHES


def _is_hook_effectively_empty(hook_path):
    """Check if a pre-release hook file is effectively empty (matches scaffold template).

    Returns True (hook is boilerplate / not customized) when:
    - The hook file does not exist
    - The hook file's content hash matches a known scaffold template version

    Returns False (hook has been customized) when:
    - The hook exists and its content does not match any known template version
    """
    if not os.path.exists(hook_path):
        return True

    with open(hook_path, "r", encoding="utf-8") as f:
        hook_content = f.read()

    hook_hash = _compute_content_hash(hook_content)
    return hook_hash in _get_pre_release_template_hashes()


def _rel_to_git_root(path, git_root):
    """Normalize path; make relative to git root if absolute."""
    n = os.path.normpath(path)
    if os.path.isabs(n):
        return os.path.relpath(n, git_root)
    return n


class ReleaseAbortError(Exception):
    """Raised when the release must abort (e.g., unexpected dirty files)."""


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


def resolve_target_paths(project_dir="."):
    """Build a dict mapping target names to their resolved paths.

    Uses detect_targets() which reads .rlsbl/config.json "targets" (supporting
    both plain strings and dicts with "name"/"path") and falls back to
    auto-detection.

    Returns dict[str, str] mapping target name -> resolved directory path.
    """
    entries = detect_targets(project_dir)
    return {e.name: e.path for e in entries}


def resolve_release_targets(primary, flags, project_dir=".", *, config):
    """Compute the effective set of secondary targets for this release.

    Reads the baseline from config "release_targets" list.
    If absent, falls back to auto-detect (all targets that detect(".")).
    Entries can be plain strings or dicts with "name" and optional "path".

    The primary target is always excluded from the secondary set
    (it's handled separately by the main release flow).

    Returns a dict mapping target name -> resolved directory path.
    """
    from ..targets import TARGETS as ALL_TARGETS

    configured = config.get("release_targets")

    # Build baseline: dict of name -> path
    if configured is not None:
        baseline = {}
        for entry in configured:
            try:
                te = _parse_target_entry(entry, project_dir)
            except (ValueError, TypeError):
                # Unparseable entry -- skip
                continue
            if te.name in ALL_TARGETS:
                baseline[te.name] = te.path
    else:
        # Auto-detect: use detect_targets which handles config and fallback
        baseline = resolve_target_paths(project_dir)

    # Never include the primary target in the secondary set
    baseline.pop(primary, None)

    return baseline


def _run_builtin_tests(registry, flags, *, project_dir=None, ctx):
    """Run built-in tests for the detected project type.

    Detects the project type from registry and runs the appropriate test command.
    When project_dir is set (monorepo mode), subprocess calls use it as cwd and
    filesystem checks are resolved relative to it.
    Returns True if tests pass, calls sys.exit(1) on failure.
    """
    if flags.get("dry-run"):
        return True

    print("Running tests...")

    if registry == "pypi":
        config = ctx.config
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
    if flags.get("dry-run"):
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


def _run_selfdoc_gen(flags, project_dir=None):
    """Run selfdoc gen if selfdoc.json exists in the project directory.

    Regenerates documentation pages from source before the selfdoc check step,
    ensuring the check validates fresh content rather than stale pages.
    """
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
        subprocess.run(["selfdoc", "gen", "--no-commit"], cwd=project_dir, check=True)
    except subprocess.CalledProcessError as e:
        print(
            f"Error: selfdoc gen failed (exit code {e.returncode}).",
            file=sys.stderr,
        )
        sys.exit(1)
    return True


def _run_selfdoc_check(flags, project_dir=None):
    """Run selfdoc check if selfdoc.json exists in the project directory.

    Checks documentation consistency before releasing. Non-fatal if selfdoc
    is not installed; fatal if it is installed and the check fails.
    When project_dir is set (monorepo mode), checks are resolved relative to it.
    """
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
        subprocess.run(["selfdoc", "check"], cwd=project_dir, check=True)
    except subprocess.CalledProcessError as e:
        print(
            f"Error: selfdoc check failed (exit code {e.returncode}).",
            file=sys.stderr,
        )
        sys.exit(1)
    return True


def _refresh_selfdoc_hashes(files_to_commit, log, project_dir="."):
    """Re-run selfdoc check after version bump to refresh content hashes.

    The early selfdoc check (before tests) validates documentation correctness,
    but its hashes are based on pre-bump file contents. After the version bump
    modifies pyproject.toml, selfdoc.json, etc., the hashes in
    .selfdoc/hashes/hashes.json become stale. This function re-runs selfdoc
    check to recalculate hashes based on bumped versions, then adds the hash
    file to files_to_commit if it changed.

    Non-fatal: errors are warned about but do not abort the release.
    """
    check_dir = project_dir
    selfdoc_config = os.path.join(check_dir, "selfdoc.json")
    if not os.path.exists(selfdoc_config):
        return

    hashes_file = os.path.join(check_dir, ".selfdoc", "hashes", "hashes.json")
    if not os.path.exists(hashes_file):
        return

    if not require_tool("selfdoc", fatal=False):
        return

    log("Refreshing selfdoc hashes after version bump...")
    try:
        subprocess.run(["selfdoc", "check"], cwd=project_dir, capture_output=True)
    except Exception as e:
        log(f"Warning: selfdoc hash refresh failed: {e}")
        return

    # Check if hashes.json is now dirty
    try:
        norm_hashes = os.path.normpath(hashes_file)
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", norm_hashes],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            if norm_hashes not in files_to_commit:
                files_to_commit.append(norm_hashes)
                log("Selfdoc hashes updated after version bump")
    except Exception as e:
        log(f"Warning: could not check selfdoc hash status: {e}")


_SCHEMA_DUMP_TIMEOUT = 30


def _run_strictcli_schema_dump(flags, log, project_dir="."):
    """Run --dump-schema for strictcli projects to regenerate .strictcli/schema.json.

    Detects strictcli usage via pyproject.toml, runs the entry point with
    --dump-schema, and logs the result. The generated file is picked up by
    the hook-generated file mechanism (pre/post hook dirty snapshots).

    Non-fatal: a failing dump command prints a warning but does not abort.
    """
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
        subprocess.run(
            ["uv", "run", entry_point, "--dump-schema"],
            cwd=project_dir,
            timeout=_SCHEMA_DUMP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Warning: strictcli schema dump timed out after {_SCHEMA_DUMP_TIMEOUT}s.",
            file=sys.stderr,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Warning: strictcli schema dump failed: {e}", file=sys.stderr)


# Lockfile -> (tool name, sync command args)
_LOCKFILE_SPECS = [
    ("uv.lock", "uv", ["uv", "lock"]),
    ("package-lock.json", "npm", ["npm", "install", "--package-lock-only"]),
    ("go.sum", "go", ["go", "mod", "tidy"]),
]

_LOCKFILE_SYNC_TIMEOUT = 30


def _sync_lockfiles(target_paths, files_to_commit, log):
    """Re-sync lockfiles after version bumps so they stay consistent.

    For each known lockfile found in a target directory, runs the
    corresponding sync command. If the lockfile is modified, its path
    is appended to files_to_commit so it is included in the release
    commit and not flagged by the unexpected-files guard.

    Missing tools and sync failures are warnings, not errors.
    """
    for _target_name, t_path in target_paths.items():
        for lockfile, tool_name, sync_cmd in _LOCKFILE_SPECS:
            lockfile_path = os.path.join(t_path, lockfile)
            if not os.path.exists(lockfile_path):
                continue

            if shutil.which(tool_name) is None:
                log(f"Warning: {tool_name} not found on PATH, skipping {lockfile} sync")
                continue

            # Record mtime before sync
            try:
                mtime_before = os.stat(lockfile_path).st_mtime_ns
            except OSError:
                mtime_before = None

            try:
                subprocess.run(
                    sync_cmd,
                    cwd=t_path,
                    timeout=_LOCKFILE_SYNC_TIMEOUT,
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                log(f"Warning: {lockfile} sync failed: {e}")
                continue

            # Check if lockfile was modified
            try:
                mtime_after = os.stat(lockfile_path).st_mtime_ns
            except OSError:
                continue

            if mtime_before != mtime_after:
                norm_path = os.path.normpath(lockfile_path)
                # Skip gitignored lockfiles -- git add would fail
                try:
                    result = subprocess.run(
                        ["git", "check-ignore", "-q", norm_path],
                        cwd=t_path,
                        capture_output=True,
                    )
                    if result.returncode == 0:
                        # exit 0 means the file IS ignored
                        log(f"Lockfile updated but gitignored, skipping: {lockfile}")
                        continue
                except Exception:
                    pass  # check-ignore failure -- proceed cautiously
                if norm_path not in files_to_commit:
                    files_to_commit.append(norm_path)
                    log(f"Lockfile updated: {lockfile}")


def _update_last_build_release(project_dir, version):
    """Store last_build_release version in .rlsbl/config.json for OTA validation."""
    config_path = os.path.join(project_dir, ".rlsbl", "config.json")
    try:
        config = read_json_config(config_path)
    except Exception:
        config = {}
    config["last_build_release"] = version
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, config_path)


def run_cmd(release_config: "ReleaseConfig", flags: dict | None = None, *,
            ctx):
    """Release command handler.

    Accepts a ReleaseConfig instance (from the release file) and an optional
    flags dict.  Bumps version, commits, pushes, and creates a GitHub Release.

    ctx: ProjectContext carrying project_root, monorepo_root, and config.
    """
    from ..release_file import ReleaseConfig

    if flags is None:
        flags = {}

    project_root = ctx.project_root
    monorepo_root = ctx.workspace_root

    if not release_config.include:
        print("Error: release file has an empty include list. Add at least one target.", file=sys.stderr)
        sys.exit(1)
    registry = release_config.include[0]

    # Validate that all included/excluded targets are known
    for t_name in release_config.include + release_config.exclude:
        if t_name not in TARGETS:
            print(
                f"Error: unknown target '{t_name}' in release file. "
                f"Valid: {', '.join(TARGETS.keys())}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Validate exhaustiveness: include + exclude must cover all detected targets
    detected = {entry.name for entry in detect_targets(str(project_root))}
    declared = set(release_config.include) | set(release_config.exclude)
    missing = detected - declared
    extra = declared - detected
    if missing:
        print(
            f"Error: detected targets not in release file: {', '.join(sorted(missing))}. "
            "Add them to include or exclude.",
            file=sys.stderr,
        )
        sys.exit(1)
    if extra:
        print(
            f"Warning: release file lists targets not detected in project: {', '.join(sorted(extra))}",
            file=sys.stderr,
        )

    # OTA validation: check for native changes when Flutter targets use mode="ota"
    flutter_targets = [
        t for t in release_config.include if t.startswith("flutter-")
    ]
    if flutter_targets:
        flutter_cfg = release_config.targets
        ota_targets = [
            t for t in flutter_targets
            if flutter_cfg.get(t, {}).get("mode") == "ota"
        ]
        if ota_targets:
            from ..targets.native_changes import detect_native_changes
            # Find last build release tag for native change detection
            _ota_root = str(project_root)
            last_build = ctx.config.get("last_build_release")
            if last_build:
                since_ref = f"v{last_build}"
                native_files = detect_native_changes(_ota_root, since_ref)
                if native_files:
                    print(
                        "Error: OTA release requested but native files changed "
                        f"since last build release ({last_build}):",
                        file=sys.stderr,
                    )
                    for nf in native_files[:10]:
                        print(f"  {nf}", file=sys.stderr)
                    if len(native_files) > 10:
                        print(
                            f"  ... and {len(native_files) - 10} more",
                            file=sys.stderr,
                        )
                    print(
                        "Use mode = \"build\" for a full release instead.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

    bump_arg = release_config.bump
    args = [bump_arg]

    quiet = flags.get("quiet", False)

    def log(msg):
        if not quiet:
            print(msg)

    # Load env file if configured
    config = ctx.config

    # Require explicit "private" key in config
    if "private" not in config:
        print(
            'Error: "private" key missing from .rlsbl/config.json.',
            file=sys.stderr,
        )
        print(
            'Set "private": true for private repos or "private": false for public repos.',
            file=sys.stderr,
        )
        print(
            'Quick fix: rlsbl scaffold',
            file=sys.stderr,
        )
        sys.exit(1)

    # Private repos must not have any pipeline with local = true
    if config["private"]:
        pipelines_cfg = config.get("pipelines", {})
        if isinstance(pipelines_cfg, dict):
            for pipeline_name, pipeline_cfg in pipelines_cfg.items():
                if isinstance(pipeline_cfg, dict) and pipeline_cfg.get("local"):
                    print(
                        "Error: private repo cannot publish to public registries.",
                        file=sys.stderr,
                    )
                    print(
                        f'Remove pipelines.{pipeline_name}.local or set "private": false.',
                        file=sys.stderr,
                    )
                    sys.exit(1)

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
    monorepo_name = None
    monorepo_project_path = None
    is_library = False
    is_dev_node = False

    if monorepo_root:
        project = resolve_project(monorepo_root, str(project_root))
        if project is None:
            print("Error: current directory is inside a monorepo but not inside any project.", file=sys.stderr)
            print("Run 'rlsbl monorepo status' to see registered projects.", file=sys.stderr)
            sys.exit(1)
        monorepo_name = project["name"]
        monorepo_project_path = project["path"]
        is_library = bool(project.get("library"))
        is_dev_node = bool(project.get("dev_node"))
        log(f"Monorepo project: {monorepo_name} ({monorepo_project_path})")

    # Project directory: ctx.project_root is already resolved to the sub-project
    # in monorepo mode (via _require_sub_project_root).
    project_dir = str(project_root)

    # Get target instance for tag_format/build/publish
    target = TARGETS[registry]

    # Resolve per-target paths from config (supports subdirectory targets)
    target_paths = resolve_target_paths(project_dir)

    # Primary target's path: from config if available, else project_dir
    primary_path = target_paths.get(registry, project_dir)

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

    # Dev node projects don't participate in the changelog system
    if monorepo_name and is_dev_node:
        log("Dev node project: skipping changelog infrastructure")
        # Enforce mandatory description for dev_node releases
        if not release_config.description:
            print(
                "Error: dev node releases require a description in unreleased.toml.",
                file=sys.stderr,
            )
            sys.exit(1)
        changes_dir = None
        changelog_content = None
        # Build GitHub Release body from description + context
        body_parts = [release_config.description]
        if release_config.context:
            body_parts.append("")
            body_parts.append(
                "<details>\n<summary>Context</summary>\n\n"
                f"{release_config.context}\n\n</details>"
            )
        changelog_entry = "\n".join(body_parts)
    else:
        if not changes_dir_exists(project_dir):
            print(
                "Error: JSONL changelog not set up. Run 'rlsbl scaffold' to create .rlsbl/changes/",
                file=sys.stderr,
            )
            sys.exit(1)

        changes_dir = get_changes_dir(project_dir)
        tag_glob = target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path) if monorepo_name else None
        # In monorepo mode, pass the project dict so coverage/range checks
        # only consider commits touching this package's files.
        monorepo_project = project if monorepo_name else None
        validation = validate_unreleased(changes_dir, tag_glob=tag_glob, project=monorepo_project, config=config)
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
        changelog_content = generate_changelog(
            project_dir, write_to_disk=False, version_override=new_version,
            description=release_config.description, context=release_config.context,
        )
        log("Generated CHANGELOG.md from JSONL entries (in-memory preview)")

        if isinstance(changelog_content, str):
            changelog_entry = extract_changelog_entry_from_text(changelog_content, new_version)
        else:
            # Mocked in tests (returns MagicMock). Fall back to the on-disk file,
            # which test fixtures pre-populate with a known entry.
            changelog_path = os.path.join(project_dir, "CHANGELOG.md")
            if not os.path.exists(changelog_path):
                print(
                    "Error: CHANGELOG.md not found after generation.",
                    file=sys.stderr,
                )
                sys.exit(1)
            changelog_entry = extract_changelog_entry(changelog_path, new_version)

    # Snapshot dirty files BEFORE any hooks run, so we can detect which files
    # hooks create or modify (the diff between pre-hook and post-hook snapshots).
    pre_hook_output = run("git", ["status", "--porcelain"])
    pre_hook_dirty = parse_porcelain_paths(pre_hook_output) if pre_hook_output else set()

    # Run pre-checks hook if present
    pre_checks_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-checks.sh")
    if os.path.exists(pre_checks_script):
        pre_checks_script = os.path.abspath(pre_checks_script)
        log("Running pre-checks hook...")
        hook_timeout = get_hook_timeout()
        try:
            env = os.environ.copy()
            env["RLSBL_VERSION"] = new_version
            subprocess.run(["bash", pre_checks_script], env=env, check=True, timeout=hook_timeout, cwd=project_dir)
        except subprocess.CalledProcessError as e:
            print(f"Error: pre-checks hook exited with code {e.returncode}.", file=sys.stderr)
            sys.exit(1)
        except subprocess.TimeoutExpired:
            print(f"Error: pre-checks hook timed out after {hook_timeout}s.", file=sys.stderr)
            sys.exit(1)

    # Dump strictcli schema if the project uses strictcli
    _run_strictcli_schema_dump(flags, log, project_dir=project_dir)

    # Regenerate selfdoc pages so the subsequent check validates fresh content
    _run_selfdoc_gen(flags, project_dir=project_dir)

    # Built-in selfdoc check (before tests so doc issues surface early)
    _run_selfdoc_check(flags, project_dir=project_dir)

    # Check if the pre-release hook has been customized. When it has,
    # skip built-in tests and lint -- the hook is expected to handle them.
    pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")
    hook_is_customized = not _is_hook_effectively_empty(pre_release_script)

    # Built-in test runner (skipped when pre-release hook is customized)
    if hook_is_customized:
        log("Skipping built-in tests (pre-release hook handles testing)")
    else:
        _run_builtin_tests(registry, flags, project_dir=project_dir, ctx=ctx)

    # Built-in lint runner (skipped when pre-release hook is customized)
    if hook_is_customized:
        log("Skipping built-in lint (pre-release hook handles linting)")
    else:
        _run_builtin_lint(flags, is_library=is_library, project_dir=project_dir)

    # Run pre-release hook if present
    pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")
    if os.path.exists(pre_release_script):
        pre_release_script = os.path.abspath(pre_release_script)
        log("Running pre-release hook...")
        hook_timeout = get_hook_timeout()
        try:
            env = os.environ.copy()
            env["RLSBL_VERSION"] = new_version
            subprocess.run(["bash", pre_release_script], env=env, check=True, timeout=hook_timeout, cwd=project_dir)
        except subprocess.CalledProcessError as e:
            print(f"Error: pre-release hook exited with code {e.returncode}.", file=sys.stderr)
            sys.exit(1)
        except subprocess.TimeoutExpired:
            print(f"Error: pre-release hook timed out after {hook_timeout}s.", file=sys.stderr)
            sys.exit(1)

    # Snapshot dirty files AFTER all hooks ran. Files in the diff are
    # hook-generated and must be included in the release commit.
    post_hook_output = run("git", ["status", "--porcelain"])
    post_hook_dirty = parse_porcelain_paths(post_hook_output) if post_hook_output else set()
    hook_generated = post_hook_dirty - pre_hook_dirty

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
                other_file = other_reg.version_file(t_path)
                if other_file:
                    rel = os.path.relpath(os.path.join(t_path, other_file), project_dir)
                    other_files.append(os.path.normpath(rel))
        if other_files:
            log(f"Sync to:   {', '.join(other_files)}")
        # Show subtree publishing info in dry-run
        if monorepo_name:
            try:
                projects = load_workspace(monorepo_root)
                proj_dict = next((p for p in projects if p["name"] == monorepo_name), None)
                subtree_remote = proj_dict.get("subtree_remote") if proj_dict else None
            except Exception:
                subtree_remote = None
            if subtree_remote:
                plain_tag = target.tag_format(new_version)
                log(f"Subtree:   {subtree_remote} (tag: {plain_tag})")
        log(f"Changelog:\n{changelog_entry or '(none)'}")
        log("--- No changes made ---")
        return

    # Resolve which secondary targets participate in this release
    secondary_targets = resolve_release_targets(registry, flags, project_dir=project_dir, config=ctx.config)

    # Acquire advisory lock to prevent concurrent rlsbl operations.
    # In monorepo mode the lock goes in .rlsbl-monorepo/ (the workspace
    # config dir) instead of .rlsbl/ to avoid creating a spurious directory.
    lock_dir = ".rlsbl-monorepo" if monorepo_name else ".rlsbl"
    lock_root = monorepo_root if monorepo_name else project_root
    acquire_lock(lock_dir=lock_dir, project_root=lock_root)

    # Pre-release checks all passed; now safe to materialize CHANGELOG.md and
    # per-version .md files on disk. The version-bump commit below includes
    # CHANGELOG.md, so it must exist before commit_files() runs.
    # Pass version_override so the section heading is "## X.Y.Z" from the
    # start; finalize_version() below renames unreleased.jsonl in place, and
    # no further changelog regeneration is needed.
    if changes_dir is not None:
        generate_changelog(
            project_dir, version_override=new_version,
            description=release_config.description, context=release_config.context,
        )

    try:
        _run_release_mutating(
            registry, reg, flags, quiet, log, new_version, current_version,
            bump_type, tag, branch, changelog_entry, target,
            secondary_targets=secondary_targets,
            monorepo_name=monorepo_name,
            monorepo_project_path=monorepo_project_path,
            commit_msg=commit_msg,
            primary_path=primary_path,
            target_paths=target_paths,
            lock_dir=lock_dir,
            pre_existing_dirty=pre_existing_dirty,
            hook_generated=hook_generated,
            ctx=ctx,
        )
    finally:
        release_lock()

    # Track build releases for Flutter OTA validation.
    # When a Flutter target completes a "build" release, store the version
    # in .rlsbl/config.json so future OTA releases can detect native changes.
    flutter_targets = [t for t in release_config.include if t.startswith("flutter-")]
    if flutter_targets:
        mode = release_config.targets.get(flutter_targets[0], {}).get("mode")
        if mode == "build":
            _update_last_build_release(project_dir, new_version)


def _print_stale_dep_advisory(monorepo_name, new_version, monorepo_root=None):
    """Print advisory about downstream packages with stale constraints.

    After releasing a package, checks if any workspace package that depends
    on the just-released package has a constraint that no longer satisfies
    the new version. Prints to stderr as a non-blocking advisory.
    """
    try:
        from .monorepo import _evaluate_constraint
        from ..workspace_graph import WorkspaceGraph

        ws_root = monorepo_root or "."
        projects = load_workspace(ws_root)
        graph = WorkspaceGraph(ws_root, projects)

        # Find direct dependents of the released package
        dependents = graph.dependents(monorepo_name)
        if not dependents:
            return

        stale_lines = []
        for dep_name in dependents:
            deps = graph.dependencies(dep_name)
            for dep in deps:
                if dep.name != monorepo_name:
                    continue
                if dep.dep_type != "versioned":
                    continue
                status = _evaluate_constraint(dep.constraint, new_version)
                if status == "outdated":
                    stale_lines.append(
                        f"  {dep_name} depends on {monorepo_name} "
                        f"{dep.constraint} but {monorepo_name} is now {new_version}\n"
                        f"    Suggested: update to >={new_version}"
                    )

        if stale_lines:
            print("! Stale dependency constraints:", file=sys.stderr)
            for line in stale_lines:
                print(line, file=sys.stderr)
    except Exception:
        # Advisory is non-blocking; swallow errors silently
        pass


def upload_release_assets(tag, new_version, log, flags, *, ctx):
    """Build and upload release assets for pipelines with ``assets: true`` or ``custom_assets``.

    For each pipeline that has assets enabled:
    1. Create a dist directory under ``.rlsbl/dist/<pipeline_name>/``
    2. Call ``pipeline.build_assets()`` and/or ``pipeline.build_custom_assets()``
    3. Check each artifact against ``max_asset_size_mb``
    4. Upload via ``gh release upload``
    5. Clean up the dist directory

    Skips silently if no pipelines have assets enabled.

    ctx: ProjectContext carrying project_root, monorepo_root, and config.
    """
    project_dir = str(ctx.project_root)
    config = ctx.config

    pipelines_cfg = config.get("pipelines", {})
    if not isinstance(pipelines_cfg, dict):
        return

    # Find pipelines with assets or custom_assets
    pipelines_with_assets = {}
    for name, entry in pipelines_cfg.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("assets") or entry.get("custom_assets"):
            pipelines_with_assets[name] = entry

    if not pipelines_with_assets:
        return

    dry_run = flags.get("dry-run", False)

    # Load pipeline instances for asset building
    all_pipelines = load_pipelines(config)

    for name, entry in pipelines_with_assets.items():
        pipeline = all_pipelines.get(name)
        if pipeline is None:
            continue

        max_size_mb = entry.get("max_asset_size_mb")
        dist_dir = os.path.join(project_dir, ".rlsbl", "dist", name)

        if dry_run:
            log(f"Would build and upload assets for pipeline '{name}' to release {tag}")
            continue

        # Build standard assets
        artifacts = []
        if entry.get("assets"):
            artifacts.extend(pipeline.build_assets(project_dir, new_version, dist_dir, ctx))

        # Build custom assets
        if entry.get("custom_assets"):
            artifacts.extend(pipeline.build_custom_assets(dist_dir))

        if not artifacts:
            log(f"No artifacts produced for pipeline '{name}', skipping upload.")
            continue

        # Size check
        if max_size_mb is not None:
            max_size_bytes = max_size_mb * 1024 * 1024
            for artifact_path in artifacts:
                try:
                    file_size = os.path.getsize(artifact_path)
                except OSError:
                    continue
                if file_size > max_size_bytes:
                    file_name = os.path.basename(artifact_path)
                    actual_mb = file_size / (1024 * 1024)
                    print(
                        f"Error: artifact '{file_name}' is {actual_mb:.1f}MB, "
                        f"exceeds max_asset_size_mb ({max_size_mb}MB) for pipeline '{name}'.",
                        file=sys.stderr,
                    )
                    # Clean up dist before aborting
                    if os.path.isdir(dist_dir):
                        shutil.rmtree(dist_dir)
                    sys.exit(1)

        # Upload
        try:
            run("gh", ["release", "upload", tag] + artifacts + ["--clobber"])
            log(f"Uploaded {len(artifacts)} asset(s) for pipeline '{name}'")
        except Exception as e:
            print(f"Warning: asset upload failed for pipeline '{name}': {e}", file=sys.stderr)

        # Clean up dist directory
        if os.path.isdir(dist_dir):
            shutil.rmtree(dist_dir)


def _run_release_mutating(registry, reg, flags, quiet, log, new_version, current_version,
                          bump_type, tag, branch, changelog_entry, target, *,
                          secondary_targets=None, monorepo_name=None,
                          monorepo_project_path=None,
                          commit_msg=None,
                          primary_path=None, target_paths=None,
                          lock_dir=".rlsbl",
                          pre_existing_dirty=None,
                          hook_generated=None,
                          ctx):
    """Inner release logic that runs under the advisory lock (mutating phase).

    ctx: ProjectContext carrying project_root, monorepo_root, and config.
    """
    project_root = ctx.project_root
    monorepo_root = ctx.workspace_root
    project_dir = str(project_root)
    # Snapshot dirty files BEFORE any version-bump writes. This captures
    # everything dirtied by prior stages (generate_changelog, hooks, lint,
    # --allow-dirty pre-existing files, etc.). Only files that become dirty
    # AFTER this point — i.e. during the version bump — are candidates for
    # the "unexpected modified files" abort.
    baseline_output = run("git", ["status", "--porcelain"])
    baseline_dirty = parse_porcelain_paths(baseline_output) if baseline_output else set()

    if commit_msg is None:
        commit_msg = tag
    if primary_path is None:
        primary_path = project_dir
    if target_paths is None:
        target_paths = resolve_target_paths(project_dir)

    # git status --porcelain outputs paths relative to the repo root.
    # Compute the repo root so vpath can produce matching relative paths.
    _git_root = run("git", ["rev-parse", "--show-toplevel"]).strip()

    def vpath(filename):
        """Join filename with project_dir, return relative to git root."""
        return _rel_to_git_root(os.path.join(project_dir, filename), _git_root)

    def target_vpath(t_path, filename):
        """Join filename with a target's resolved path, return relative to git root."""
        return _rel_to_git_root(os.path.join(t_path, filename), _git_root)

    # Pre-compute expected version files for the confirmation prompt display.
    # The actual files_to_commit list is built from write_version() return
    # values below, which may include additional files (e.g. __init__.py).
    version_file = reg.version_file(primary_path)
    preview_files = []
    if version_file:
        preview_files.append(target_vpath(primary_path, version_file))
    for t_name, t_path in target_paths.items():
        if t_name == registry:
            continue
        other_reg = TARGETS.get(t_name)
        if other_reg and other_reg.check_project_exists(t_path):
            other_file = other_reg.version_file(t_path)
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
        if should_tag(flags, ctx.config):
            print("  Will add 'rlsbl' keyword to project manifests")
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # Capture HEAD before any version-bump writes so we can roll back on failure.
    # This must happen before write_version() so that git reset --hard reverts
    # the uncommitted version-bumped files if the release aborts.
    pre_release_sha = run("git", ["rev-parse", "HEAD"])

    # Everything from version-bump writes through commit/tag/push is wrapped
    # in a single try block so that any failure (including ReleaseAbortError
    # from the unexpected-files check) triggers rollback of version-bumped
    # files via git reset --hard.
    try:
        # Write new version to version files (skip if version didn't change, e.g. first release)
        # Build files_to_commit from the paths actually modified by write_version().
        files_to_commit = []
        if new_version != current_version:
            modified = reg.write_version(primary_path, new_version, ctx=ctx)
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
                    other_modified = other_reg.write_version(t_path, new_version, ctx=ctx)
                    for rel in other_modified:
                        files_to_commit.append(target_vpath(t_path, rel))
                    if other_modified:
                        log(f"Synced version to {', '.join(target_vpath(t_path, r) for r in other_modified)}")

            # Bump selfdoc.json version inline (no DocsTarget dependency).
            bumped_files = set(files_to_commit)
            selfdoc_modified = _bump_selfdoc_version(project_dir, new_version)
            for rel in selfdoc_modified:
                fpath = vpath(rel)
                if fpath not in bumped_files:
                    files_to_commit.append(fpath)
            if selfdoc_modified:
                log(f"Synced version to {', '.join(vpath(r) for r in selfdoc_modified)}")

        # Ecosystem tagging: add keyword to manifests if enabled
        if should_tag(flags, ctx.config):
            npm_path = target_paths.get("npm", project_dir)
            try:
                if TARGETS["npm"].check_project_exists(npm_path):
                    if ensure_npm_keyword(npm_path, quiet=quiet, project_root=project_root):
                        pkg_path = target_vpath(npm_path, "package.json")
                        if pkg_path not in files_to_commit:
                            files_to_commit.append(pkg_path)
            except Exception:
                pass
            pypi_path = target_paths.get("pypi", project_dir)
            try:
                if TARGETS["pypi"].check_project_exists(pypi_path):
                    if ensure_pypi_keyword(pypi_path, quiet=quiet, project_root=project_root):
                        pyproject_path = target_vpath(pypi_path, "pyproject.toml")
                        if pyproject_path not in files_to_commit:
                            files_to_commit.append(pyproject_path)
            except Exception:
                pass

        # Sync lockfiles after version bumps so they reflect the new version
        _sync_lockfiles(target_paths, files_to_commit, log)

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

        # Re-run selfdoc check to refresh hashes after version bump
        _refresh_selfdoc_hashes(files_to_commit, log, project_dir=project_dir)

        # Include the generated CHANGELOG.md in the commit (dev nodes have no CHANGELOG.md)
        changelog_path = os.path.join(project_dir, "CHANGELOG.md")
        if os.path.exists(changelog_path):
            changelog_file = vpath("CHANGELOG.md")
            if changelog_file not in files_to_commit:
                files_to_commit.append(changelog_file)

        # Include hook-generated files (created or modified by pre-checks/pre-release hooks)
        if hook_generated:
            for hf in sorted(hook_generated):
                if hf not in files_to_commit:
                    files_to_commit.append(hf)
                    log(f"Including hook-generated file: {hf}")

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
            # Normalize all files_to_commit to git-relative paths.
            # Some callers (e.g. _sync_lockfiles, _refresh_selfdoc_hashes)
            # add absolute paths via os.path.normpath(); git status
            # --porcelain outputs repo-relative paths, so we must match.
            expected_files = {
                os.path.relpath(os.path.abspath(f), _git_root) if os.path.isabs(f) else f
                for f in files_to_commit
            }
            expected_files.add(vpath(os.path.join(lock_dir, "lock")))
            # The .validated cache is written by changelog validation earlier in the
            # release flow.  It may be tracked (dirty) or gitignored (invisible to
            # git status).  Either way it is not a concurrent-change signal.
            validated_raw = os.path.normpath(
                os.path.join(get_changes_dir(project_dir), ".validated")
            )
            validated_file = os.path.relpath(validated_raw, _git_root) if os.path.isabs(validated_raw) else validated_raw
            expected_files.add(validated_file)
            # When --allow-dirty was used, files that were already dirty before the
            # release started are not "unexpected" -- only genuinely new modifications
            # (from e.g. concurrent processes) should trigger the abort.
            if pre_existing_dirty:
                expected_files |= pre_existing_dirty
            # Subtract the baseline snapshot taken at the start of the mutating
            # phase.  This covers files written by intermediate stages that ran
            # BEFORE version-bump writes (generate_changelog, hooks, lint) which
            # are not in files_to_commit or pre_existing_dirty.
            unexpected = dirty_files - expected_files - baseline_dirty
            if unexpected:
                unexpected_list = ", ".join(sorted(unexpected))
                raise ReleaseAbortError(
                    f"Unexpected modified files detected (possible concurrent change): {unexpected_list}. Aborting release."
                )

        # Commit if any of the files we track actually have changes.
        # Don't use is_clean_tree() as a proxy — the advisory lock file (.rlsbl/lock)
        # makes the tree appear dirty even when no release-relevant files changed.

        needs_commit = new_version != current_version or has_staged_or_modified(files_to_commit, cwd=_git_root)
        if files_to_commit and needs_commit:
            commit_files(commit_msg, files_to_commit, cwd=_git_root)
            log(f"Committed: {commit_msg}")
        elif not needs_commit:
            log("No changes to commit")

        # Finalize JSONL changelog: rename unreleased.jsonl to x.y.z.jsonl.
        # CHANGELOG.md already has the correct "## X.Y.Z" heading because the
        # earlier generate_changelog() call (above acquire_lock) was passed
        # version_override=new_version, so no regeneration is needed here.
        if changes_dir_exists(project_dir):
            changes_dir = get_changes_dir(project_dir)
            tag_glob = target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path) if monorepo_name else None
            finalize_version(changes_dir, new_version, tag_glob=tag_glob)
            generate_version_file(changes_dir, new_version)
            log(f"Finalized JSONL changelog for {new_version}")
            # Commit the finalized JSONL file and the new empty unreleased.jsonl
            jsonl_finalized = _rel_to_git_root(os.path.join(changes_dir, f"{new_version}.jsonl"), _git_root)
            jsonl_unreleased = _rel_to_git_root(os.path.join(changes_dir, "unreleased.jsonl"), _git_root)
            # Also commit the generated per-version .md file if it exists
            jsonl_md = _rel_to_git_root(os.path.join(changes_dir, f"{new_version}.md"), _git_root)
            changelog_file = vpath("CHANGELOG.md")
            finalize_files = [jsonl_finalized, jsonl_unreleased, changelog_file]
            if os.path.exists(jsonl_md):
                finalize_files.append(jsonl_md)
            commit_files(f"chore: finalize changelog for {new_version}", finalize_files, cwd=_git_root)
            log(f"Committed finalized changelog files")
        else:
            log("No .rlsbl/changes/ directory; skipping changelog finalization")

        # Finalize release file: rename unreleased.toml to vX.Y.Z.toml
        # Only if the release file exists (backward compat with legacy path)
        from ..release_file import get_release_file_path
        release_file_path = get_release_file_path(project_dir)
        if os.path.exists(release_file_path):
            releases_dir = os.path.dirname(release_file_path)
            versioned_release = os.path.join(releases_dir, f"v{new_version}.toml")
            os.rename(release_file_path, versioned_release)
            os.chmod(versioned_release, 0o444)
            # Create a fresh empty unreleased.toml
            with open(release_file_path, "w", encoding="utf-8") as f:
                pass  # empty file
            release_finalize_files = [
                _rel_to_git_root(versioned_release, _git_root),
                _rel_to_git_root(release_file_path, _git_root),
            ]
            commit_files(f"chore: finalize release file for {new_version}", release_finalize_files, cwd=_git_root)
            log(f"Finalized release file for {new_version}")

        # Create local git tag
        run("git", ["tag", tag])
        log(f"Tagged: {tag}")

        # Push commits and tag
        push_timeout = get_push_timeout(ctx.config)
        if push_timeout != 120:
            log(f"Push timeout: {push_timeout}s (from RLSBL_PUSH_TIMEOUT)")
        # Mark pushes as release-authorized so the pre-push hook skips its
        # "manual push" warning. The hook still runs JSONL coverage checks.
        push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
        push_if_needed(branch, env=push_env, config=ctx.config)
        run("git", ["push", "origin", tag], timeout=push_timeout, env=push_env)
        log(f"Pushed to origin/{branch}")
    except ReleaseAbortError as e:
        # Release was explicitly aborted (e.g., unexpected dirty files).
        # Roll back version-bumped files so the working tree is clean.
        run("git", ["reset", "--hard", pre_release_sha])
        print(str(e), file=sys.stderr)
        print(
            f"Local state has been rolled back to {pre_release_sha[:10]}.",
            file=sys.stderr,
        )
        sys.exit(1)
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
            f.write(changelog_entry or "")
        os.rename(writing_file, notes_file)
        run("gh", ["release", "create", tag, "--title", tag, "--notes-file", notes_file])
        log(f"Created GitHub Release: {tag}")

        # Subtree publishing for monorepo projects with subtree_remote configured
        if monorepo_name and monorepo_project_path:
            try:
                projects = load_workspace(monorepo_root)
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

    # Upload release assets for pipelines with assets/custom_assets config
    upload_release_assets(tag, new_version, log, flags, ctx=ctx)

    # Three-state pipeline config cascade
    release_config = ctx.config
    if release_config.get("publish") is not None:
        print(
            "Error: the 'publish' key in .rlsbl/config.json is no longer recognized. "
            "Use 'pipelines' instead.",
            file=sys.stderr,
        )
        sys.exit(1)
    if release_config.get("pipelines") is None:
        print(
            "Error: no 'pipelines' key in .rlsbl/config.json. "
            "Add a pipelines section or run 'rlsbl scaffold'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load pipelines and validate env vars for local pipelines
    release_pipelines = load_pipelines(release_config)
    missing_vars = []
    for pl_name, pl in release_pipelines.items():
        if pl.local:
            for var in pl.required_env_vars():
                if var not in os.environ:
                    missing_vars.append(f"  pipeline '{pl_name}' requires {var}")
    if missing_vars:
        print("Error: missing environment variables for local pipelines:", file=sys.stderr)
        for line in missing_vars:
            print(line, file=sys.stderr)
        sys.exit(1)

    # Publish step: skip for private repos (they don't publish to registries)
    is_private = ctx.config.get("private", False)
    if not is_private:
        # Pipeline dispatch: run publish for each pipeline (runs once per release, not per-target)
        for pl_name, pl in release_pipelines.items():
            try:
                pl.publish(primary_path, new_version, ctx=ctx)
            except Exception as e:
                print(f"Warning: pipeline '{pl_name}' publish failed: {e}", file=sys.stderr)

        # Multi-target: run build for secondary targets (build stays on targets, not pipelines)
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

    # Deploy phase (after publish, before post-release hook)
    deploy_targets, deploy_errors = read_deploy_config(ctx.config)
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
    if should_tag(flags, ctx.config):
        ensure_github_topic(quiet=quiet)

    # Run post-release hook if present (non-fatal: release is already complete)
    post_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "post-release.sh")
    if os.path.exists(post_release_script):
        post_release_script = os.path.abspath(post_release_script)
        log("Running post-release hook...")
        hook_timeout = get_hook_timeout()
        try:
            env = os.environ.copy()
            env["RLSBL_VERSION"] = new_version
            subprocess.run(["bash", post_release_script], env=env, check=True, timeout=hook_timeout, cwd=project_dir)
        except subprocess.CalledProcessError as e:
            print(f"Warning: post-release hook exited with code {e.returncode}.", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print(f"Warning: post-release hook timed out after {hook_timeout}s.", file=sys.stderr)

    # Auto-regenerate monorepo snapshot after release (non-fatal)
    if monorepo_name:
        try:
            from ..snapshot import generate_snapshot, write_snapshot
            from ..workspace_graph import WorkspaceGraph

            projects = load_workspace(monorepo_root)
            graph = WorkspaceGraph(monorepo_root, projects)
            snapshot = generate_snapshot(monorepo_root, projects, graph)
            rel_path = write_snapshot(monorepo_root, snapshot)
            commit_files_if_changed("snapshot", [rel_path], skip_message="Snapshot unchanged.", autogenerated=True, cwd=monorepo_root)
            log(f"Regenerated monorepo snapshot: {rel_path}")
        except Exception as e:
            print(f"Warning: snapshot regeneration failed: {e}", file=sys.stderr)

    # Advisory: constraint propagation
    if monorepo_name:
        _print_stale_dep_advisory(monorepo_name, new_version, monorepo_root=monorepo_root)

    # Watch CI or print hint (uses SHA captured before post-release hooks).
    # Dry-run returns earlier (no push happens), but guard defensively.
    if not flags.get("dry-run", False):
        if flags.get("watch"):
            log(f"Watching CI for {pushed_sha}...")
            from .watch import run_cmd as watch_run_cmd
            watch_run_cmd(None, [pushed_sha], {})
        else:
            log(f"Watch CI: rlsbl watch {pushed_sha}")

    log(f"\nRelease {new_version} complete!")
