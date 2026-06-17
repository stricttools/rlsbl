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
from ...errors import ConfigError, PostReleaseError
from ...git_util import validate_subtree_remote_ssh_host
from ...config import read_deploy_config, read_json_config, should_tag, update_last_build_release
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
    validate_release_targets, validate_ota_mode, validate_config_integrity,
    validate_pipeline_config, validate_gh_cli, validate_clean_tree,
    validate_branch_and_remote, resolve_monorepo_context,
    compute_release_version, validate_changelog_state,
    print_dry_run_summary,
)
from .hooks import _compute_content_hash, _get_pre_release_template_hashes, _is_hook_effectively_empty, run_release_hook
from .execute import (
    _bump_selfdoc_version,
    _rel_to_git_root,
    ReleaseAbortError,
    ReleaseState,
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


def run_cmd(release_config: "ReleaseConfig", flags: dict | None = None, *,
            ctx):
    """Release command handler.

    Accepts a ReleaseConfig instance (from the release file) and an optional
    flags dict.  Bumps version, commits, pushes, and creates a GitHub Release.

    ctx: ProjectContext carrying project_root, monorepo_root, and config.
    """
    try:
        _run_cmd_inner(release_config, flags, ctx=ctx)
    except PostReleaseError:
        sys.exit(1)
    except (ReleaseValidationError, HookError, ConfigError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _run_cmd_inner(release_config, flags, *, ctx):
    """Inner implementation of run_cmd. Raises exceptions instead of sys.exit."""
    if flags is None:
        flags = {}

    project_root = ctx.project_root
    monorepo_root = ctx.workspace_root
    config = ctx.config
    quiet = flags.get("quiet", False)

    def log(msg):
        if not quiet:
            print(msg)

    # --- Validate inputs and environment ---
    registry = validate_release_targets(release_config, project_root)
    validate_ota_mode(release_config, project_root, config)
    validate_config_integrity(config)

    # Load env file if configured
    env_file = config.get("env_file")
    if env_file:
        from ...config import load_env_file
        load_env_file(env_file)
        if "CF_ACCOUNT_ID" in os.environ and "CLOUDFLARE_ACCOUNT_ID" not in os.environ:
            os.environ["CLOUDFLARE_ACCOUNT_ID"] = os.environ["CF_ACCOUNT_ID"]

    validate_pipeline_config(config)
    validate_gh_cli()
    pre_existing_dirty = validate_clean_tree(flags)
    branch = validate_branch_and_remote(flags)

    # --- Resolve context ---
    monorepo_name, monorepo_project_path, is_library, is_dev_node = resolve_monorepo_context(
        monorepo_root, project_root, log,
    )

    project_dir = str(project_root)

    # Scaffold conflict guard
    _abort_on_scaffold_conflicts(project_dir)

    # Resolve target paths and compute version
    target = TARGETS[registry]
    target_paths = resolve_target_paths(project_dir)
    primary_path = target_paths.get(registry, project_dir)

    current_version, new_version, bump_type, tag = compute_release_version(
        target, primary_path, release_config.bump,
        monorepo_name, monorepo_project_path, log,
    )

    # --- Validate changelog ---
    # In monorepo mode, resolve the project dict for scoped coverage checks
    monorepo_project = None
    if monorepo_name:
        monorepo_project = resolve_project(monorepo_root, str(project_root))

    changes_dir = validate_changelog_state(
        project_dir, target, monorepo_name, monorepo_project_path,
        config, monorepo_project=monorepo_project,
    )

    # Validate blog body file if blog is enabled
    _blog_body_path, blog_warning = validate_blog_body(project_dir, release_config.blog)
    if blog_warning:
        log(blog_warning)

    # Compute changelog content in memory (deferred write after pre-release checks pass)
    changelog_content = generate_changelog(
        project_dir, write_to_disk=False, version_override=new_version,
        description=release_config.description, context=release_config.context,
    )
    log("Generated CHANGELOG.md from JSONL entries (in-memory preview)")

    if isinstance(changelog_content, str):
        changelog_entry = extract_changelog_entry_from_text(changelog_content, new_version)
    else:
        # Mocked in tests (returns MagicMock). Fall back to the on-disk file.
        changelog_path = os.path.join(project_dir, "CHANGELOG.md")
        if not os.path.exists(changelog_path):
            raise ReleaseValidationError("CHANGELOG.md not found after generation.")
        changelog_entry = extract_changelog_entry(changelog_path, new_version)

    # --- Run pre-release hooks and checks ---
    pre_hook_output = run("git", ["status", "--porcelain"])
    pre_hook_dirty = parse_porcelain_paths(pre_hook_output) if pre_hook_output else set()

    # Build hook environment
    hook_env = os.environ.copy()
    hook_env["RLSBL_VERSION"] = new_version
    hook_env["RLSBL_BUMP_TYPE"] = bump_type or ""
    hook_env["RLSBL_PREV_VERSION"] = current_version or ""
    hook_env["RLSBL_DESCRIPTION"] = release_config.description if release_config else ""
    hook_timeout = get_hook_timeout()

    pre_checks_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-checks.sh")
    if os.path.exists(pre_checks_script):
        log("Running pre-checks hook...")
        run_release_hook("pre-checks", pre_checks_script, project_dir, hook_env, hook_timeout)

    _run_strictcli_schema_dump(flags, log, project_dir=project_dir)
    _run_selfdoc_gen(flags, project_dir=project_dir)
    _run_selfdoc_check(flags, project_dir=project_dir)

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

    # Built-in tests and lint (skipped when pre-release hook is customized)
    pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")
    hook_is_customized = not _is_hook_effectively_empty(pre_release_script)

    if hook_is_customized:
        log("Skipping built-in tests (pre-release hook handles testing)")
    else:
        _run_builtin_tests(registry, flags, project_dir=project_dir, ctx=ctx)

    if hook_is_customized:
        log("Skipping built-in lint (pre-release hook handles linting)")
    else:
        _run_builtin_lint(flags, is_library=is_library, project_dir=project_dir)

    # Run pre-release hook
    pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")
    if os.path.exists(pre_release_script):
        log("Running pre-release hook...")
        run_release_hook("pre-release", pre_release_script, project_dir, hook_env, hook_timeout)

    # Snapshot dirty files after all hooks
    post_hook_output = run("git", ["status", "--porcelain"])
    post_hook_dirty = parse_porcelain_paths(post_hook_output) if post_hook_output else set()
    hook_generated = post_hook_dirty - pre_hook_dirty

    commit_msg = f"{monorepo_name}: release v{new_version}" if monorepo_name else tag

    # --- Dry run: print summary and return ---
    if flags.get("dry-run", False):
        print_dry_run_summary(
            log, registry, monorepo_name, monorepo_project_path,
            bump_type, current_version, new_version, tag,
            commit_msg, branch, target_paths, project_dir,
            changelog_entry, monorepo_root=monorepo_root,
        )
        return

    # --- Execute release ---
    secondary_targets = resolve_release_targets(registry, flags, project_dir=project_dir, config=ctx.config)

    lock_dir = ".rlsbl-monorepo" if monorepo_name else ".rlsbl"
    lock_root = monorepo_root if monorepo_name else project_root
    acquire_lock(lock_dir=lock_dir, project_root=lock_root)

    # Materialize CHANGELOG.md on disk now that all pre-release checks passed
    if changes_dir is not None:
        generate_changelog(
            project_dir, version_override=new_version,
            description=release_config.description, context=release_config.context,
        )

    try:
        _run_release_mutating(ReleaseState(
            registry=registry,
            target=TARGETS[registry],
            new_version=new_version,
            current_version=current_version,
            bump_type=bump_type,
            tag=tag,
            branch=branch,
            primary_path=primary_path,
            target_paths=target_paths,
            lock_dir=lock_dir,
            monorepo_name=monorepo_name,
            monorepo_project_path=monorepo_project_path,
            changelog_entry=changelog_entry,
            commit_msg=commit_msg,
            description=release_config.description,
            context=release_config.context,
            pre_existing_dirty=pre_existing_dirty,
            hook_generated=hook_generated,
            secondary_targets=secondary_targets,
            flags=flags,
            quiet=quiet,
            log=log,
            ctx=ctx,
        ))
    except ReleaseAbortError:
        sys.exit(1)
    except (KeyboardInterrupt, SystemExit):
        raise
    finally:
        release_lock()

    # Track build releases for Flutter OTA validation
    flutter_targets = [t for t in release_config.include if t.startswith("flutter-")]
    if flutter_targets:
        mode = release_config.targets.get(flutter_targets[0], {}).get("mode")
        if mode == "build":
            update_last_build_release(project_dir, new_version)


