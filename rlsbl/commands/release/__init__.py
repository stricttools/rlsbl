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
from ...changelog.generate import _read_release_metadata, _read_release_metadata_full
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
    extract_github_repo_from_remote,
    get_current_branch,
    get_check_timeout,
    get_hook_timeout,
    get_push_timeout,
    has_staged_or_modified,
    is_clean_tree,
    push_if_needed,
    remote_branch_exists,
    require_tool,
    run,
    run_gh,
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
    _format_releasable_tag, _releasable_tag_glob,
)
from .hooks import (
    _compute_content_hash, _get_pre_release_template_hashes,
    _is_hook_effectively_empty, is_hook_customized, run_release_hook,
    normalize_hook_entry, run_config_hooks, _get_config_hooks,
    get_releasable_hook_path, get_package_hook_path,
    build_hook_env, run_releasable_hooks,
    run_releasable_tests, run_releasable_lint,
    is_releasable_hook_customized,
    warn_if_hook_needs_migration,
)
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

from ...release_file import VALID_BUMP_TYPES


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
    # Target validation is deferred until after releasable context is resolved
    # so that member_dirs can be passed for releasable target union.
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

    # In batch mode the batch orchestrator already validated gh CLI,
    # clean tree, and branch/remote upfront -- skip redundant checks.
    if flags.get("batch-mode", False):
        pre_existing_dirty = set()
        branch = get_current_branch()
    else:
        validate_gh_cli()
        pre_existing_dirty = validate_clean_tree(flags)
        branch = validate_branch_and_remote(flags)

    # --- Resolve context ---
    monorepo_name, monorepo_project_path, is_library, is_non_releasable, releasable_name = resolve_monorepo_context(
        monorepo_root, project_root, log,
    )

    # In explicit mode, resolve the full releasable object and member info
    releasable_tag_fmt = None
    member_package_paths = None
    member_projs = None
    if releasable_name and monorepo_root:
        from ...workspace import load_releasables, load_workspace, members_of
        ws_projects = load_workspace(monorepo_root)
        releasables = load_releasables(monorepo_root, ws_projects)
        releasable_obj = next((r for r in releasables if r.name == releasable_name), None)
        if releasable_obj:
            releasable_tag_fmt = releasable_obj.tag_format
            member_projs = members_of(releasable_name, ws_projects)
            member_package_paths = [p["path"] for p in member_projs]
            log(f"Releasable: {releasable_name} ({len(member_package_paths)} member(s))")

    # Validate release targets (deferred to here so releasable context is available)
    if member_package_paths is not None and monorepo_root:
        from ...workspace import get_releasable_dir
        _member_abs_dirs = [
            os.path.join(str(monorepo_root), p) for p in member_package_paths
        ]
        _rel_cfg_dir = get_releasable_dir(str(monorepo_root), releasable_name)
        registry = validate_release_targets(
            release_config, project_root,
            member_dirs=_member_abs_dirs,
            releasable_config_dir=_rel_cfg_dir,
        )
    else:
        registry = validate_release_targets(release_config, project_root)

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
        workspace_root=monorepo_root if releasable_name else None,
        releasable_name=releasable_name,
        releasable_tag_fmt=releasable_tag_fmt,
    )

    # --- Validate changelog ---
    # In monorepo mode, resolve the project dict for scoped coverage checks.
    # For releasable mode, pass the full member project list so
    # _filter_commits_for_scope uses filter_commits_for_releasable.
    monorepo_project = None
    if releasable_name and member_projs:
        monorepo_project = member_projs
    elif monorepo_name and not releasable_name:
        monorepo_project = resolve_project(monorepo_root, str(project_root))

    changes_dir = validate_changelog_state(
        project_dir, target, monorepo_name, monorepo_project_path,
        config, monorepo_project=monorepo_project,
        releasable_name=releasable_name,
        releasable_tag_fmt=releasable_tag_fmt,
        workspace_root=monorepo_root,
        bump_type=bump_type,
    )

    # Validate blog body file if blog is enabled
    _blog_body_path, blog_warning = validate_blog_body(project_dir, release_config.blog)
    if blog_warning:
        log(blog_warning)

    # Compute changelog content in memory (deferred write after pre-release checks pass)
    # In explicit releasable mode, changes_dir points to the releasable-level
    # directory, not the per-project default.
    changelog_gen_kwargs = {}
    if releasable_name and changes_dir:
        changelog_gen_kwargs["changes_dir_override"] = changes_dir
    changelog_content = generate_changelog(
        project_dir, write_to_disk=False, version_override=new_version,
        description=release_config.description, context=release_config.context,
        bump_type=bump_type,
        **changelog_gen_kwargs,
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
    hook_env = build_hook_env(
        os.environ.copy(),
        new_version,
        bump_type=bump_type or "",
        prev_version=current_version or "",
        description=release_config.description if release_config else "",
    )
    hook_timeout = get_hook_timeout()

    # In explicit releasable mode with members, use the multi-level hook system:
    #   1. Releasable pre-checks
    #   2. Per-package pre-checks (alphabetical)
    #   3. Built-in tests (each member) / lint (library members)
    #   4. Per-package pre-release (alphabetical)
    #   5. Releasable pre-release
    #
    # In implicit mode or standalone, use the single-level hook system.
    _use_releasable_hooks = releasable_name and monorepo_root and member_package_paths

    if _use_releasable_hooks:
        # Build (name, dir) tuples for member packages
        _member_tuples = []
        from ...workspace import load_workspace, members_of, get_releasable_dir
        _ws_projects = load_workspace(str(monorepo_root))
        _member_projs = members_of(releasable_name, _ws_projects)
        for mp in _member_projs:
            mp_name = mp.name if hasattr(mp, 'name') else mp["name"]
            mp_path = mp.path if hasattr(mp, 'path') else mp["path"]
            mp_dir = os.path.join(str(monorepo_root), mp_path)
            _member_tuples.append((mp_name, mp_dir))

        # Load releasable-level config and per-package configs for hook dispatch
        _rel_cfg_dir = get_releasable_dir(str(monorepo_root), releasable_name)
        _releasable_config = read_json_config(os.path.join(_rel_cfg_dir, "config.json"))
        _package_configs = {}
        for _mp_name, _mp_dir in _member_tuples:
            _pkg_cfg = read_json_config(os.path.join(_mp_dir, ".rlsbl", "config.json"))
            if _pkg_cfg:
                _package_configs[_mp_name] = _pkg_cfg

        # 1+2. Pre-checks: releasable first, then per-package
        run_releasable_hooks(
            "pre-checks", monorepo_root, releasable_name,
            _member_tuples, hook_env, hook_timeout, log,
            project_dir=project_dir,
            releasable_config=_releasable_config,
            package_configs=_package_configs,
        )
    else:
        pre_checks_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-checks.sh")
        log("Running pre-checks hook...")
        run_release_hook("pre-checks", pre_checks_script, project_dir, hook_env, hook_timeout, config=config)

    _run_strictcli_schema_dump(flags, log, project_dir=project_dir)

    # Snapshot dirty files before selfdoc runs so we can isolate files that
    # selfdoc generates (excluding anything dirtied by pre-checks hooks or
    # strictcli schema dump).  Only needed in non-dry-run mode.
    if not flags.get("dry-run", False):
        _pre_selfdoc_output = run("git", ["status", "--porcelain"])
        _pre_selfdoc_dirty = parse_porcelain_paths(_pre_selfdoc_output) if _pre_selfdoc_output else set()

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

    # Commit selfdoc-generated files immediately so the tree is clean if later
    # steps fail.  Compare dirty snapshot against _pre_selfdoc_dirty to isolate
    # files produced by selfdoc gen/check/post_generate only.
    if not flags.get("dry-run", False):
        _post_selfdoc_output = run("git", ["status", "--porcelain"])
        _post_selfdoc_dirty = parse_porcelain_paths(_post_selfdoc_output) if _post_selfdoc_output else set()
        _selfdoc_generated = _post_selfdoc_dirty - _pre_selfdoc_dirty
        if _selfdoc_generated:
            commit_files(
                "selfdoc: regenerate",
                sorted(_selfdoc_generated),
                autogenerated=True,
            )
            log("Committed selfdoc-generated files")

    if _use_releasable_hooks:
        # Check if releasable-level pre-release hook is customized
        hook_is_customized = is_releasable_hook_customized(str(monorepo_root), releasable_name, config=_releasable_config)

        # 3. Built-in tests and lint (skipped when releasable pre-release hook is customized)
        if hook_is_customized:
            log("Skipping built-in tests (releasable pre-release hook handles testing)")
        else:
            run_releasable_tests(_member_tuples, flags, ctx=ctx, log=log, releasable_config_dir=_rel_cfg_dir)

        if hook_is_customized:
            log("Skipping built-in lint (releasable pre-release hook handles linting)")
        else:
            run_releasable_lint(_member_tuples, flags, ws_projects=_ws_projects, log=log, check_timeout=get_check_timeout(config))

        # 4+5. Pre-release: per-package first, then releasable
        run_releasable_hooks(
            "pre-release", monorepo_root, releasable_name,
            _member_tuples, hook_env, hook_timeout, log,
            project_dir=project_dir,
            releasable_config=_releasable_config,
            package_configs=_package_configs,
        )
    else:
        # Built-in tests and lint (skipped when pre-release hook is customized)
        pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")

        # Backward compatibility bridge: warn if customized script exists
        # without config-driven hook entries
        warn_if_hook_needs_migration(config, pre_release_script)

        hook_is_customized = is_hook_customized(config, pre_release_script)

        if hook_is_customized:
            log("Skipping built-in tests (pre-release hook handles testing)")
        else:
            _run_builtin_tests(registry, flags, project_dir=project_dir, ctx=ctx)

        if hook_is_customized:
            log("Skipping built-in lint (pre-release hook handles linting)")
        else:
            _run_builtin_lint(flags, is_library=is_library, project_dir=project_dir, check_timeout=get_check_timeout(config))

        # Run pre-release hook
        pre_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "pre-release.sh")
        log("Running pre-release hook...")
        run_release_hook("pre-release", pre_release_script, project_dir, hook_env, hook_timeout, config=config)

    # Snapshot dirty files after all hooks
    post_hook_output = run("git", ["status", "--porcelain"])
    post_hook_dirty = parse_porcelain_paths(post_hook_output) if post_hook_output else set()
    hook_generated = post_hook_dirty - pre_hook_dirty

    # In explicit mode, commit message uses releasable name;
    # in implicit monorepo mode, uses the package name; standalone uses the tag.
    if releasable_name:
        commit_msg = f"{releasable_name}: release v{new_version}"
    elif monorepo_name:
        commit_msg = f"{monorepo_name}: release v{new_version}"
    else:
        commit_msg = tag

    # --- Dry run: print summary and return ---
    if flags.get("dry-run", False):
        print_dry_run_summary(
            log, registry, monorepo_name, monorepo_project_path,
            bump_type, current_version, new_version, tag,
            commit_msg, branch, target_paths, project_dir,
            changelog_entry, monorepo_root=monorepo_root,
            member_package_paths=member_package_paths,
        )
        return

    # --- Execute release ---
    secondary_targets = resolve_release_targets(registry, flags, project_dir=project_dir, config=ctx.config)

    lock_dir = ".rlsbl-monorepo" if monorepo_name else ".rlsbl"
    lock_root = monorepo_root if monorepo_name else project_root
    skip_lock = flags.get("skip-lock", False)
    if not skip_lock:
        acquire_lock(lock_dir=lock_dir, project_root=lock_root)

    # Materialize CHANGELOG.md on disk now that all pre-release checks passed
    if changes_dir is not None:
        generate_changelog(
            project_dir, version_override=new_version,
            description=release_config.description, context=release_config.context,
            bump_type=bump_type,
            **changelog_gen_kwargs,
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
            changes_dir=changes_dir,
            monorepo_name=monorepo_name,
            monorepo_project_path=monorepo_project_path,
            releasable_name=releasable_name,
            member_package_paths=member_package_paths,
            releasable_tag_format=releasable_tag_fmt,
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
        if not skip_lock:
            release_lock()

    # Track build releases for Flutter OTA validation
    flutter_targets = [t for t in release_config.include if t.startswith("flutter-")]
    if flutter_targets:
        mode = release_config.targets.get(flutter_targets[0], {}).get("mode")
        if mode == "build":
            update_last_build_release(project_dir, new_version)


