"""Release command: bumps version, validates changelog, runs hooks, regenerates selfdoc, syncs lockfiles, tags, pushes, and creates a GitHub Release."""

import json
import os
import shutil
import subprocess
import sys
import time

from ...changelog import (
    changes_dir_exists,
    finalize_changeset_version,
    finalize_version,
    generate_changelog,
    generate_version_file,
    get_changes_dir,
    read_coverage_unit,
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
    get_hook_timeout,
    get_push_timeout,
    has_staged_or_modified,
    is_clean_tree,
    push_if_needed,
    remote_branch_exists,
    require_tool,
    run,
    run_gh,
    tag_exists_locally,
    tag_exists_on_remote,
)
from .rollback import _cleanup_release_artifacts
from .publish import _run_selfdoc_post_generate, _print_stale_dep_advisory, upload_release_assets, _upload_assets_for_config
from .validate import (
    parse_porcelain_paths,
    _run_selfdoc_gen, _run_selfdoc_check, _abort_on_scaffold_conflicts,
    _abort_on_cross_repo_sources, _abort_on_version_skew,
    _run_strictcli_schema_dump, validate_blog_body,
    ReleaseValidationError, HookError, _SCHEMA_DUMP_TIMEOUT,
    validate_release_targets, validate_ota_mode, validate_config_integrity,
    validate_pipeline_config, validate_gh_cli, validate_gh_push_access,
    validate_clean_tree,
    validate_branch_and_remote, BranchValidation, resolve_monorepo_context,
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
    _sync_lockfiles,
    archive_blog_body,
    _run_release_mutating,
    _LOCKFILE_SPECS,
    _LOCKFILE_SYNC_TIMEOUT,
)

from ...release_file import VALID_BUMP_TYPES
from .release_state import (
    FATAL_STEPS,
    RELEASE_STEPS,
    clear_release_state,
    get_failed_steps,
    get_missing_steps,
    get_state_path,
    is_state_complete,
    load_release_state,
    resolve_releasable_dir,
)


def run_cmd(release_config: "ReleaseConfig", flags: dict | None = None, *,
            ctx):
    """Release command handler.

    Accepts a ReleaseConfig instance (from the release file) and an optional
    flags dict.  Bumps version, commits, pushes, and creates a GitHub Release.

    ctx: ProjectContext carrying project_root, monorepo_root, and config.
    """
    try:
        _run_cmd_inner(release_config, flags, ctx=ctx)
    except PostReleaseError as e:
        if str(e):
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except (ReleaseValidationError, HookError, ConfigError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def resume_cmd(saved_state: dict, flags: dict | None = None, *, ctx):
    """Resume a previously failed release from the mutating phase.

    Reads the saved state dict (from in-progress.json), resolves just enough
    context to call _run_release_mutating directly, skipping all validation
    and pre-release hooks (which already ran successfully in the original run).
    """
    try:
        _resume_cmd_inner(saved_state, flags, ctx=ctx)
    except PostReleaseError as e:
        if str(e):
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ReleaseAbortError:
        sys.exit(1)
    except (ReleaseValidationError, HookError, ConfigError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _resume_cmd_inner(saved_state, flags, *, ctx):
    """Inner implementation of resume_cmd."""
    if flags is None:
        flags = {}

    project_root = ctx.project_root
    monorepo_root = ctx.workspace_root
    config = ctx.config
    quiet = flags.get("quiet", False)

    def log(msg):
        if not quiet:
            print(msg)

    project_dir = str(project_root)

    # Extract saved state fields
    new_version = saved_state["new_version"]
    tag = saved_state["tag"]
    branch = saved_state["branch"]
    bump_type = saved_state.get("bump_type")
    registry = saved_state["registry"]
    monorepo_name = saved_state.get("monorepo_name")
    releasable_name = saved_state.get("releasable_name")
    commit_msg = saved_state.get("commit_msg", tag)
    description = saved_state.get("description", "")
    context = saved_state.get("context", "")

    # Resolve target and paths (with releasable-level inheritance)
    _rel_cfg_dir = None
    if releasable_name and monorepo_root:
        from ...workspace import get_releasable_dir
        _rel_cfg_dir = get_releasable_dir(str(monorepo_root), releasable_name)
    target = TARGETS[registry]
    target_paths = resolve_target_paths(project_dir, releasable_config_dir=_rel_cfg_dir)
    primary_path = target_paths.get(registry, project_dir)

    # Read current version from disk (the version bump may already be done)
    try:
        current_version = target.read_version(primary_path)
    except Exception:
        current_version = new_version  # If we can't read, assume already bumped

    # Extract changelog entry from CHANGELOG.md (already generated in prior
    # run). Releasable mode: the canonical file lives in the releasable dir.
    from ...changelog.home import get_changelog_home
    changelog_path = get_changelog_home(project_dir, releasable_dir=_rel_cfg_dir)
    if os.path.exists(changelog_path):
        changelog_entry = extract_changelog_entry(changelog_path, new_version)
    else:
        changelog_entry = None

    # Resolve monorepo context for releasable mode
    monorepo_project_path = None
    member_package_paths = None
    releasable_tag_fmt = None
    if monorepo_name and monorepo_root:
        project = resolve_project(monorepo_root, str(project_root))
        if project:
            monorepo_project_path = project.get("path") if hasattr(project, "get") else getattr(project, "path", None)

    if releasable_name and monorepo_root:
        from ...workspace import load_releasables, members_of
        try:
            ws_projects = load_workspace(monorepo_root)
            releasables = load_releasables(monorepo_root, ws_projects)
            releasable_obj = next((r for r in releasables if r.name == releasable_name), None)
            if releasable_obj:
                releasable_tag_fmt = releasable_obj.tag_format
                member_projs = members_of(releasable_name, ws_projects)
                member_package_paths = [p["path"] for p in member_projs]
        except Exception:
            pass  # Best-effort for resume

    # Resolve changes_dir
    changes_dir = None
    if releasable_name and monorepo_root:
        from ...workspace import get_releasable_dir
        try:
            rel_dir = get_releasable_dir(str(monorepo_root), releasable_name)
            _rel_changes = os.path.join(rel_dir, "changes")
            if os.path.isdir(_rel_changes):
                changes_dir = _rel_changes
        except Exception:
            pass
    if changes_dir is None and changes_dir_exists(project_dir):
        changes_dir = get_changes_dir(project_dir)

    secondary_targets = resolve_release_targets(
        registry, flags, project_dir=project_dir, config=config,
        releasable_config_dir=_rel_cfg_dir,
    )

    lock_dir = ".rlsbl-monorepo" if monorepo_name else ".rlsbl"
    lock_root = monorepo_root if monorepo_name else project_root
    skip_lock = flags.get("skip-lock", False)
    if not skip_lock:
        acquire_lock(lock_dir=lock_dir, project_root=lock_root)

    try:
        _run_release_mutating(ReleaseState(
            registry=registry,
            target=target,
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
            description=description,
            context=context,
            pre_existing_dirty=set(),
            hook_generated=set(),
            secondary_targets=secondary_targets,
            include=saved_state.get("include", []),
            exclude=saved_state.get("exclude", []),
            preid=saved_state.get("preid", ""),
            blog=saved_state.get("blog", False),
            flags=flags,
            quiet=quiet,
            log=log,
            ctx=ctx,
        ))
    except (KeyboardInterrupt, SystemExit):
        raise
    finally:
        if not skip_lock:
            release_lock()


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

    # Check for in-progress release -- must use resume instead.
    # The state path is releasable-aware: releasable members keep their
    # state under .rlsbl-monorepo/releasables/<name>/releases/.
    _ip_releasable_dir = None
    if monorepo_root:
        _ip_releasable_dir = resolve_releasable_dir(
            str(project_root), str(monorepo_root),
        )
    _ip_state_path = get_state_path(
        str(project_root), releasable_dir=_ip_releasable_dir,
    )
    # Legacy release-FILE check: releasable release files (unreleased.toml)
    # used to live under the representative member's .rlsbl/releases/. A
    # file there must never be silently ignored (the relocated read path
    # would skip it and the archive step would leave it as residue).
    if _ip_releasable_dir is not None:
        from ...release_file import check_legacy_release_file
        from ...errors import ReleaseFileError
        try:
            check_legacy_release_file(str(project_root), _ip_releasable_dir)
        except ReleaseFileError as e:
            raise ReleaseValidationError(str(e))

    _ip_state = load_release_state(_ip_state_path)
    if _ip_state is None and _ip_releasable_dir is not None:
        # Legacy location check: older rlsbl versions wrote releasable
        # release state under the representative member's .rlsbl/releases/.
        # Never silently ignore pre-existing in-flight state.
        _ip_legacy_path = get_state_path(str(project_root))
        if load_release_state(_ip_legacy_path) is not None:
            raise ReleaseValidationError(
                f"found in-progress release state at the legacy location "
                f"{_ip_legacy_path}. Releasable release state now lives at "
                f"{_ip_state_path}. Move the file to the new location and "
                f"run `rlsbl release resume` to continue, or run "
                f"`rlsbl release undo` to roll back."
            )
    if _ip_state is not None:
        _ip_version = _ip_state.get("new_version", "unknown")
        if is_state_complete(_ip_state):
            # Provably complete (all steps marked, no fatal failure): the
            # previous run finished but crashed before clearing its state
            # (or a legacy complete file was left behind). Auto-clear.
            log(
                f"Found completed release state for v{_ip_version} "
                f"(all steps marked, no fatal failures); clearing it."
            )
            _ip_failed = get_failed_steps(_ip_state)
            if _ip_failed:
                print(
                    f"The previous release (v{_ip_version}) completed with "
                    f"non-fatal step failures:",
                    file=sys.stderr,
                )
                for _step, _msg in _ip_failed.items():
                    print(f"  {_step}: {_msg}", file=sys.stderr)
            clear_release_state(_ip_state_path)
        else:
            _ip_completed = set(_ip_state.get("completed_steps", []))
            _ip_done = len([s for s in RELEASE_STEPS if s in _ip_completed])
            _ip_missing = get_missing_steps(_ip_state)
            _ip_fatal = [
                s for s in get_failed_steps(_ip_state) if s in FATAL_STEPS
            ]
            _parts = [
                f"a previous release is in progress "
                f"(v{_ip_version}, {_ip_done}/{len(RELEASE_STEPS)} steps completed"
            ]
            if _ip_missing:
                _parts.append(f"; missing: {', '.join(_ip_missing)}")
            if _ip_fatal:
                _parts.append(f"; fatal step failure(s): {', '.join(_ip_fatal)}")
            _parts.append(
                "). Run `rlsbl release resume` to continue or "
                "`rlsbl release undo` to roll back."
            )
            raise ReleaseValidationError("".join(_parts))

    # --- Validate inputs and environment ---
    # Consolidated config schema validation (banned keys, structural invariants).
    from ...config import validate_config_schema
    validate_config_schema(config, project_dir=str(project_root))

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

    # Pipeline config validation is deferred until after releasable context
    # is resolved, so per-member pipeline validation can run in releasable
    # mode. Standalone/implicit mode validates the single representative.
    # (Moved below, after member_package_paths is known.)

    # In batch mode the batch orchestrator already validated gh CLI,
    # clean tree, and branch/remote upfront -- skip redundant checks.
    # Batch mode does not support release-from-dev (the orchestrator
    # always runs from the release branch).
    if flags.get("batch-mode", False):
        pre_existing_dirty = set()
        branch = get_current_branch()
        dev_branch = None
        needs_ff_merge = False
    else:
        validate_gh_cli()
        validate_gh_push_access(config)
        pre_existing_dirty = validate_clean_tree(flags)
        branch_info = validate_branch_and_remote(flags, config=config)
        # Extract branch-role information for the release-from-dev flow.
        # BranchValidation.branch is the release branch (e.g. "main");
        # dev_branch is the branch we started from (e.g. "dev"), or None.
        # Handle both BranchValidation and plain string (from test mocks).
        if isinstance(branch_info, BranchValidation):
            branch = branch_info.branch
            dev_branch = branch_info.dev_branch
            needs_ff_merge = branch_info.needs_ff_merge
        else:
            branch = str(branch_info)
            dev_branch = None
            needs_ff_merge = False

    # --- Resolve context ---
    monorepo_name, monorepo_project_path, is_library, is_non_releasable, releasable_name = resolve_monorepo_context(
        monorepo_root, project_root, log,
    )

    # In explicit mode, resolve the full releasable object and member info
    releasable_tag_fmt = None
    member_package_paths = None
    member_projs = None
    if releasable_name and monorepo_root:
        from ...workspace import load_releasables, members_of
        ws_projects = load_workspace(monorepo_root)
        releasables = load_releasables(monorepo_root, ws_projects)
        releasable_obj = next((r for r in releasables if r.name == releasable_name), None)
        if releasable_obj:
            releasable_tag_fmt = releasable_obj.tag_format
            member_projs = members_of(releasable_name, ws_projects)
            member_package_paths = [p["path"] for p in member_projs]
            log(f"Releasable: {releasable_name} ({len(member_package_paths)} member(s))")

    # Validate release targets (deferred to here so releasable context is available)
    _rel_cfg_dir = None
    _member_abs_dirs = None
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

    # Pipeline config validation (deferred from above so releasable
    # context is available). In releasable mode, validates each non-private
    # publishing member's pipeline config; in standalone/implicit mode,
    # validates the representative's config.
    if member_package_paths is not None and monorepo_root and _rel_cfg_dir:
        from ...member_context import resolve_member_context as _rmc
        for _mp in member_package_paths:
            _mp_abs = os.path.join(str(monorepo_root), _mp)
            if not os.path.isdir(_mp_abs):
                continue
            _m_ctx = _rmc(_mp_abs, releasable_config_dir=_rel_cfg_dir)
            if _m_ctx.is_private:
                continue
            _m_pipelines = _m_ctx.config.get("pipelines")
            if _m_pipelines is not None:
                validate_pipeline_config(_m_ctx.config)
    else:
        validate_pipeline_config(config)

    project_dir = str(project_root)

    # Scaffold conflict guard
    _abort_on_scaffold_conflicts(project_dir)

    # Cross-repo path source guard (pre-mutation, unconditional)
    _abort_on_cross_repo_sources(
        project_dir,
        boundary_root=str(monorepo_root) if monorepo_root else project_dir,
        member_dirs=_member_abs_dirs,
    )

    # Version-skew guard: local dev-sources overlay checkouts must not be
    # ahead of the registry (pre-mutation, unconditional)
    _abort_on_version_skew(
        project_dir,
        workspace_root=str(monorepo_root) if monorepo_root else None,
    )

    # Resolve target paths (with releasable-level inheritance in explicit
    # mode, so releasable config "targets" drives primary path resolution)
    target = TARGETS[registry]
    target_paths = resolve_target_paths(project_dir, releasable_config_dir=_rel_cfg_dir)
    primary_path = target_paths.get(registry, project_dir)

    current_version, new_version, bump_type, tag = compute_release_version(
        target, primary_path, release_config.bump,
        monorepo_name, monorepo_project_path, log,
        workspace_root=monorepo_root if releasable_name else None,
        releasable_name=releasable_name,
        releasable_tag_fmt=releasable_tag_fmt,
        preid=release_config.preid,
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

    # Changelog preflight: run preflight-changelog checks via the check system
    if not flags.get("dry-run", False):
        from rlsbl import app as _rlsbl_app, _register_external_checks_from_config
        from pathlib import Path as _Path

        _register_external_checks_from_config(config)

        if releasable_name and monorepo_root:
            from ...check_context import WorkspaceCheckContext as _WsCtx
            _changelog_ctx = _WsCtx(
                project_root=_Path(project_dir),
                workspace_root=_Path(str(monorepo_root)),
                config=config,
                projects=list(ws_projects) if ws_projects else [],
                graph=None,
                releasables=[releasable_obj] if releasable_obj else [],
            )
        elif monorepo_root:
            from ...check_context import WorkspaceCheckContext as _WsCtx
            _mono_projects = load_workspace(monorepo_root) if monorepo_root else []
            _changelog_ctx = _WsCtx(
                project_root=_Path(project_dir),
                workspace_root=_Path(str(monorepo_root)),
                config=config,
                projects=list(_mono_projects),
                graph=None,
                releasables=[],
            )
        else:
            from ...context import ProjectContext as _ProjCtx
            _changelog_ctx = _ProjCtx(
                project_root=_Path(project_dir),
                workspace_root=None,
                config=config,
            )

        # For hotfix releases, ignore warnings (the user-facing check
        # produces a warning when no user-facing entries exist, which
        # is expected for hotfix).
        _cl_ignore_warn = (bump_type == "hotfix")
        _cl_results, _cl_exit = _rlsbl_app.run_checks(
            _changelog_ctx, tag_expr="preflight-changelog",
            ignore_warnings=_cl_ignore_warn,
        )
        if _cl_exit != 0:
            # When warnings are treated as errors, include them in the report
            _cl_error_statuses = {"fail"} if _cl_ignore_warn else {"fail", "warn"}
            _cl_failed = [
                f"{r.name}: {r.result.message}"
                for r in _cl_results
                if r.result.status in _cl_error_statuses
            ]
            for msg in _cl_failed:
                print(f"  FAIL  {msg}", file=sys.stderr)
            raise HookError(
                f"Changelog preflight checks failed ({len(_cl_failed)} failure(s))"
            )

        # Hotfix releases must not have user-facing changelog entries.
        # The preflight-changelog checks above validate structural integrity;
        # this is a semantic constraint specific to the hotfix bump type.
        if bump_type == "hotfix":
            from ...changelog.files import read_unreleased as _read_unreleased
            _hotfix_entries = _read_unreleased(changes_dir)
            if any(e.user_facing for e in _hotfix_entries):
                raise ReleaseValidationError(
                    "hotfix releases must not have user-facing changelog entries "
                    "— use patch, minor, or major instead"
                )

    # Validate blog body file if blog is enabled (releasable-aware: the
    # body lives alongside unreleased.toml in the releasable releases dir)
    from ...release_file import get_releases_dir as _get_releases_dir
    _blog_body_path, blog_warning = validate_blog_body(
        project_dir, release_config.blog,
        releases_dir=_get_releases_dir(project_dir, releasable_dir=_rel_cfg_dir),
    )
    if blog_warning:
        log(blog_warning)

    # Compute changelog content in memory (deferred write after pre-release checks pass)
    # In explicit releasable mode, changes_dir points to the releasable-level
    # directory, not the per-project default, and the canonical CHANGELOG.md
    # lives in the releasable's directory (single source of truth:
    # rlsbl.changelog.home).
    from ...changelog.home import get_changelog_home, generate_workspace_changelog
    changelog_gen_kwargs = {}
    if releasable_name and changes_dir:
        from ...release_file import get_releases_dir
        changelog_gen_kwargs["changes_dir_override"] = changes_dir
        changelog_gen_kwargs["changelog_output_path"] = get_changelog_home(
            project_dir, releasable_dir=_rel_cfg_dir,
        )
        # Archived release files (v{x}.toml) live at the releasable level too.
        changelog_gen_kwargs["releases_dir_override"] = get_releases_dir(
            project_dir, releasable_dir=_rel_cfg_dir,
        )
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
        from ...workspace import members_of, get_releasable_dir
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
        releases_dir=_get_releases_dir(project_dir, releasable_dir=_rel_cfg_dir),
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

        # 3. Built-in tests and lint via check system (skipped when releasable pre-release hook is customized)
        if hook_is_customized:
            log("Skipping built-in checks (releasable pre-release hook handles testing/linting)")
        elif flags.get("dry-run", False):
            log("Skipping preflight checks (dry-run)")
        else:
            from rlsbl import app as _rlsbl_app, _register_external_checks_from_config
            from ...check_context import WorkspaceCheckContext
            from pathlib import Path as _Path

            _register_external_checks_from_config(ctx.config)

            all_failed = []
            for pkg_name, pkg_dir in sorted(_member_tuples):
                member_proj = next(
                    (p for p in _ws_projects if p.name == pkg_name),
                    None,
                )
                if member_proj is None:
                    continue
                member_ctx = WorkspaceCheckContext(
                    project_root=_Path(str(pkg_dir)),
                    workspace_root=_Path(str(monorepo_root)),
                    config=ctx.config,
                    projects=[member_proj],
                    graph=None,
                    releasables=[],
                )
                results, exit_code = _rlsbl_app.run_checks(
                    member_ctx, tag_expr="preflight",
                )
                if exit_code != 0:
                    for r in results:
                        if r.result.status == "fail":
                            all_failed.append(
                                f"{pkg_name}: {r.name}: {r.result.message}"
                            )
            if all_failed:
                for msg in all_failed:
                    print(f"  FAIL  {msg}", file=sys.stderr)
                raise HookError(
                    f"Preflight checks failed ({len(all_failed)} failure(s))"
                )

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
            log("Skipping built-in checks (pre-release hook handles testing/linting)")
        elif flags.get("dry-run", False):
            log("Skipping preflight checks (dry-run)")
        else:
            from rlsbl import app as _rlsbl_app, _register_external_checks_from_config
            from ...context import ProjectContext as _ProjectContext
            from pathlib import Path as _Path

            _register_external_checks_from_config(config)

            standalone_ctx = _ProjectContext(
                project_root=_Path(project_dir),
                workspace_root=_Path(str(monorepo_root)) if monorepo_root else None,
                config=config,
            )
            results, exit_code = _rlsbl_app.run_checks(
                standalone_ctx, tag_expr="preflight",
            )
            if exit_code != 0:
                failed = [
                    f"{r.name}: {r.result.message}"
                    for r in results
                    if r.result.status == "fail"
                ]
                for msg in failed:
                    print(f"  FAIL  {msg}", file=sys.stderr)
                raise HookError(
                    f"Preflight checks failed ({len(failed)} failure(s))"
                )

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
            releasable_config_dir=_rel_cfg_dir,
        )
        return

    # --- Execute release ---
    secondary_targets = resolve_release_targets(
        registry, flags, project_dir=project_dir, config=ctx.config,
        releasable_config_dir=_rel_cfg_dir,
    )

    lock_dir = ".rlsbl-monorepo" if monorepo_name else ".rlsbl"
    lock_root = monorepo_root if monorepo_name else project_root
    skip_lock = flags.get("skip-lock", False)
    if not skip_lock:
        acquire_lock(lock_dir=lock_dir, project_root=lock_root)

    # --- Release-from-dev: fast-forward merge ---
    # When releasing from a dev branch, checkout the release branch and
    # fast-forward merge it to the dev branch HEAD. This must happen
    # AFTER all validation and hooks pass (so a failed hook doesn't leave
    # us on the wrong branch) but BEFORE any mutating writes (version
    # bump, changelog materialization).
    #
    # Re-verify ff-ability at merge time: another session may have pushed
    # to the release branch between our initial fetch and now.
    if needs_ff_merge:
        from ...utils import run as _run_util
        log(f'Fast-forward merging "{branch}" to {dev_branch} HEAD...')
        try:
            _run_util("git", ["checkout", branch])
        except Exception as e:
            raise ReleaseValidationError(
                f'failed to checkout release branch "{branch}": {e}'
            )
        try:
            _run_util("git", ["merge", "--ff-only", dev_branch])
        except Exception as e:
            # ff-merge failed -- switch back to dev before raising
            try:
                _run_util("git", ["checkout", dev_branch])
            except Exception:
                pass
            raise ReleaseValidationError(
                f'fast-forward merge of "{dev_branch}" into "{branch}" '
                f'failed: {e}. This usually means "{branch}" has diverged '
                f'since validation. Rebase or merge and try again.'
            )
        log(f'Fast-forward merge complete: {branch} is now at {dev_branch} HEAD')

    # Materialize CHANGELOG.md on disk now that all pre-release checks passed
    if changes_dir is not None:
        generate_changelog(
            project_dir, version_override=new_version,
            description=release_config.description, context=release_config.context,
            bump_type=bump_type,
            **changelog_gen_kwargs,
        )
        # Releasable mode: also regenerate the combined root CHANGELOG.md
        # covering all releasables of the workspace.
        if releasable_name and monorepo_root:
            generate_workspace_changelog(str(monorepo_root))

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
            include=list(release_config.include),
            exclude=list(release_config.exclude),
            preid=release_config.preid,
            blog=release_config.blog,
            flags=flags,
            quiet=quiet,
            log=log,
            ctx=ctx,
        ))
    except ReleaseAbortError:
        # On failure, switch back to dev branch if we came from one
        if dev_branch:
            try:
                from ...utils import run as _run_util
                _run_util("git", ["checkout", dev_branch])
            except Exception:
                print(
                    f'Warning: could not switch back to dev branch "{dev_branch}". '
                    f'You are on "{branch}".',
                    file=sys.stderr,
                )
        sys.exit(1)
    except (KeyboardInterrupt, SystemExit):
        # On interrupt, switch back to dev branch
        if dev_branch:
            try:
                from ...utils import run as _run_util
                _run_util("git", ["checkout", dev_branch])
            except Exception:
                pass
        raise
    except Exception:
        # On any other failure, switch back to dev branch
        if dev_branch:
            try:
                from ...utils import run as _run_util
                _run_util("git", ["checkout", dev_branch])
            except Exception:
                print(
                    f'Warning: could not switch back to dev branch "{dev_branch}". '
                    f'You are on "{branch}".',
                    file=sys.stderr,
                )
        raise
    finally:
        if not skip_lock:
            release_lock()

    # --- Release-from-dev: return to dev branch ---
    # After a successful release, switch back to the dev branch and
    # merge the release branch (which now has the version bump commit)
    # back to dev so dev stays up to date.
    if dev_branch:
        from ...utils import run as _run_util
        try:
            _run_util("git", ["checkout", dev_branch])
            # Merge release branch to dev -- should be ff-only since
            # the release added commits on top of the ff-merged HEAD.
            _run_util("git", ["merge", "--ff-only", branch])
            log(f'Switched back to "{dev_branch}" (merged release commits from "{branch}")')
        except Exception as e:
            print(
                f'Warning: could not merge "{branch}" back to "{dev_branch}": {e}. '
                f'You may need to manually merge or rebase.',
                file=sys.stderr,
            )

    # Track build releases for Flutter OTA validation
    flutter_targets = [t for t in release_config.include if t.startswith("flutter-")]
    if flutter_targets:
        mode = release_config.targets.get(flutter_targets[0], {}).get("mode")
        if mode == "build":
            update_last_build_release(project_dir, new_version)


