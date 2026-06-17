"""Release command: bumps version, validates changelog, runs hooks, regenerates selfdoc, syncs lockfiles, tags, pushes, and creates a GitHub Release."""

import json
import os
import shutil
import subprocess
import sys
import time

from ...changelog import (
    changes_dir_exists,
    finalize_version,
    generate_changelog,
    generate_version_file,
    get_changes_dir,
    validate_unreleased,
)
from ...changelog.generate import _read_release_metadata
from ...errors import ConfigError
from ...git_util import validate_subtree_remote_ssh_host
from ...config import read_deploy_config, read_json_config, should_tag
from ...pipelines import load_pipelines
from ...deploy import deploy_target
from ...lock import acquire_lock, release_lock
from ...targets import TARGETS, detect_targets, _parse_target_entry
from ...tagging import ensure_github_topic, ensure_npm_keyword, ensure_pypi_keyword
from ...strictcli_detect import detect_strictcli
from ...workspace import load_workspace, resolve_project
from ...testing import run_project_tests
from ...utils import (
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
    remote_branch_exists,
    require_tool,
    run,
)
from .rollback import _cleanup_release_artifacts
from .publish import _run_selfdoc_post_generate, _print_stale_dep_advisory, upload_release_assets
from .validate import (
    parse_porcelain_paths, _run_builtin_tests, _run_builtin_lint,
    _run_selfdoc_gen, _run_selfdoc_check, _abort_on_scaffold_conflicts,
    _run_strictcli_schema_dump, validate_blog_body,
    ReleaseValidationError, HookError, _SCHEMA_DUMP_TIMEOUT,
)
from .hooks import _compute_content_hash, _get_pre_release_template_hashes, _is_hook_effectively_empty
from .execute import (
    _bump_selfdoc_version,
    _rel_to_git_root,
    ReleaseAbortError,
    resolve_target_paths,
    resolve_release_targets,
    _refresh_selfdoc_hashes,
    _sync_lockfiles,
    archive_blog_body,
    _run_release_mutating,
    _LOCKFILE_SPECS,
    _LOCKFILE_SYNC_TIMEOUT,
)

VALID_BUMP_TYPES = ("patch", "minor", "major")


def _update_last_build_release(project_dir, version):
    """Store last_build_release version in .rlsbl/config.json for OTA validation."""
    config_path = os.path.join(project_dir, ".rlsbl", "config.json")
    try:
        config = read_json_config(config_path)
    except Exception as e:
        raise RuntimeError(
            f"{config_path} is corrupted or unreadable — fix it before releasing: {e}"
        ) from e
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
    from ...release_file import ReleaseConfig

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
            from ...targets.native_changes import detect_native_changes
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
                    for nf in native_files:
                        print(f"  {nf}", file=sys.stderr)
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
        from ...config import load_env_file
        load_env_file(env_file)
        if "CF_ACCOUNT_ID" in os.environ and "CLOUDFLARE_ACCOUNT_ID" not in os.environ:
            os.environ["CLOUDFLARE_ACCOUNT_ID"] = os.environ["CF_ACCOUNT_ID"]

    # Validate pipeline config and env vars BEFORE any mutating operations.
    # These checks used to run after commit/tag/push/GitHub Release, which
    # meant a failed check would leave a half-published release.
    if config.get("publish") is not None:
        print(
            "Error: the 'publish' key in .rlsbl/config.json is no longer recognized. "
            "Use 'pipelines' instead.",
            file=sys.stderr,
        )
        sys.exit(1)
    if config.get("pipelines") is None:
        print(
            "Error: no 'pipelines' key in .rlsbl/config.json. "
            "Add a pipelines section or run 'rlsbl scaffold'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load pipelines and validate env vars for local pipelines
    _early_pipelines = load_pipelines(config)
    missing_vars = []
    for pl_name, pl in _early_pipelines.items():
        if pl.local:
            for var in pl.required_env_vars():
                if var not in os.environ:
                    missing_vars.append(f"  pipeline '{pl_name}' requires {var}")
    if missing_vars:
        print("Error: missing environment variables for local pipelines:", file=sys.stderr)
        for line in missing_vars:
            print(line, file=sys.stderr)
        sys.exit(1)

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
        if not remote_branch_exists(branch):
            print(
                f"Remote branch origin/{branch} does not exist yet. Skipping remote-ahead check.",
                file=sys.stderr,
            )
        else:
            try:
                behind_count = int(run("git", ["rev-list", "--count", f"HEAD..origin/{branch}"]))
            except Exception as e:
                print(f"Error: could not check if local branch is behind origin: {e}", file=sys.stderr)
                print("Cannot verify remote-ahead status. Aborting for safety.", file=sys.stderr)
                sys.exit(1)
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
        if is_dev_node:
            print(
                "Error: dev_node projects cannot be released. Dev nodes are "
                "infrastructure projects that do not produce releases. Remove "
                "dev_node = true from workspace.toml if this project should be "
                "releasable.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Project directory: ctx.project_root is already resolved to the sub-project
    # in monorepo mode (via _require_sub_project_root).
    project_dir = str(project_root)

    # Scaffold conflict guard: abort before any mutation if scaffold-managed
    # files still contain unresolved merge conflict markers.
    try:
        _abort_on_scaffold_conflicts(project_dir)
    except (ReleaseValidationError, HookError):
        sys.exit(1)

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

    # Validate blog body file if blog is enabled
    try:
        _blog_body_path, blog_warning = validate_blog_body(project_dir, release_config.blog)
    except (ReleaseValidationError, HookError):
        sys.exit(1)
    if blog_warning:
        log(blog_warning)

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
            env["RLSBL_BUMP_TYPE"] = bump_type or ""
            env["RLSBL_PREV_VERSION"] = current_version or ""
            env["RLSBL_DESCRIPTION"] = release_config.description if release_config else ""
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
    try:
        _run_selfdoc_gen(flags, project_dir=project_dir)
    except (ReleaseValidationError, HookError):
        sys.exit(1)

    # Built-in selfdoc check (before tests so doc issues surface early)
    try:
        _run_selfdoc_check(flags, project_dir=project_dir)
    except (ReleaseValidationError, HookError):
        sys.exit(1)

    # Generate blog post via selfdoc if blog is enabled
    try:
        _run_selfdoc_post_generate(
            flags,
            project_dir=project_dir,
            release_config=release_config,
            new_version=new_version,
            current_version=current_version,
            bump_type=bump_type,
            changelog_entry=changelog_entry,
            tag=tag,
        )
    except (ReleaseValidationError, HookError):
        sys.exit(1)

    # Check if the pre-release hook has been customized. When it has,
    # skip built-in tests and lint -- the hook is expected to handle them.
    pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")
    hook_is_customized = not _is_hook_effectively_empty(pre_release_script)

    # Built-in test runner (skipped when pre-release hook is customized)
    try:
        if hook_is_customized:
            log("Skipping built-in tests (pre-release hook handles testing)")
        else:
            _run_builtin_tests(registry, flags, project_dir=project_dir, ctx=ctx)

        # Built-in lint runner (skipped when pre-release hook is customized)
        if hook_is_customized:
            log("Skipping built-in lint (pre-release hook handles linting)")
        else:
            _run_builtin_lint(flags, is_library=is_library, project_dir=project_dir)
    except (ReleaseValidationError, HookError):
        sys.exit(1)

    # Run pre-release hook if present
    pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")
    if os.path.exists(pre_release_script):
        pre_release_script = os.path.abspath(pre_release_script)
        log("Running pre-release hook...")
        hook_timeout = get_hook_timeout()
        try:
            env = os.environ.copy()
            env["RLSBL_VERSION"] = new_version
            env["RLSBL_BUMP_TYPE"] = bump_type or ""
            env["RLSBL_PREV_VERSION"] = current_version or ""
            env["RLSBL_DESCRIPTION"] = release_config.description if release_config else ""
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
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("could not load workspace for subtree info", e)
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
            description=release_config.description,
            context=release_config.context,
            ctx=ctx,
        )
    except ReleaseAbortError:
        # Unexpected dirty files triggered abort; rollback already happened
        # inside _run_release_mutating.
        sys.exit(1)
    except (KeyboardInterrupt, SystemExit):
        raise
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


