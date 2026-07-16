"""Release execution: version bump, commit, tag, push, GitHub Release creation, JSONL changelog finalization, and post-release hook invocation."""

import dataclasses
import json
import os
import sys
import time

from .release_state import (
    get_state_path,
    get_missing_steps,
    get_failed_steps,
    save_release_state,
    load_release_state,
    save_step,
    save_step_failure,
    clear_release_state,
)


class ReleaseAbortError(Exception):
    """Raised when the release must abort (e.g., unexpected dirty files)."""


class RollbackClobberError(Exception):
    """Raised when rollback would destroy foreign commits or dirty files.

    This prevents ``git reset --hard`` from silently discarding work
    created by concurrent sessions sharing the same worktree.
    """


def _track_release_commit(state_path, sha=None, cwd=None):
    """Record a release commit SHA in the state file.

    Called immediately after each ``commit_files()`` /
    ``commit_files_if_changed()`` invocation so the rollback guard can
    distinguish release-owned commits from foreign ones.

    Best-effort: failures are silently ignored. When tracking fails
    (e.g., in test environments without a real git repo), the rollback
    guard treats all commits as foreign and refuses rollback -- the
    safe default.

    If ``sha`` is not provided, reads HEAD via subprocess directly
    (bypasses the mock-patched ``run`` function used by the release
    flow, avoiding mock side-effect exhaustion in tests).
    """
    try:
        if sha is None:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
                cwd=cwd,
            )
            sha = result.stdout.strip()
        else:
            sha = sha.strip()
        state = load_release_state(state_path)
        if state is None:
            state = {}
        commits = state.setdefault("release_commits", [])
        if sha not in commits:
            commits.append(sha)
        save_release_state(state_path, state)
    except Exception:
        pass  # Best-effort: never mask the original release error


def _guard_rollback(pre_release_sha, state_path, cwd=None):
    """Refuse rollback if foreign commits exist between pre_release_sha and HEAD.

    Compares commits between ``pre_release_sha`` and HEAD against the
    ``release_commits`` list persisted in the state file.  Any commit
    not in ``release_commits`` is a foreign commit (from a concurrent
    session).

    Dirty files (uncommitted modifications) are NOT checked because the
    release flow itself writes version-bump and changelog files before
    committing them -- those dirty files are the expected rollback
    target, not concurrent work.  Untracked files survive
    ``git reset --hard`` anyway.

    Uses ``subprocess`` directly (not the mock-patched ``run`` function)
    to avoid consuming mock side-effect entries in tests.

    Raises :class:`RollbackClobberError` with details and manual
    recovery instructions when rollback is unsafe.
    """
    import subprocess

    state = load_release_state(state_path)
    release_commits = set((state or {}).get("release_commits", []))

    # Find all commits between pre_release_sha and HEAD
    try:
        result = subprocess.run(
            ["git", "rev-list", f"{pre_release_sha.strip()}..HEAD"],
            capture_output=True, text=True, check=True,
            cwd=cwd,
        )
        rev_list_output = result.stdout.strip()
    except Exception:
        # If rev-list fails (e.g. pre_release_sha is invalid), allow
        # the rollback -- the guard is best-effort, and blocking here
        # would leave the release in a worse state.
        return

    if rev_list_output:
        all_commits = [c.strip() for c in rev_list_output.splitlines() if c.strip()]
    else:
        all_commits = []

    foreign_commits = [c for c in all_commits if c not in release_commits]

    if not foreign_commits:
        return  # Safe to roll back

    parts = [
        "Rollback aborted: git reset --hard would destroy work from "
        "concurrent sessions.",
        f"\nForeign commits (not created by this release):",
    ]
    for fc in foreign_commits:
        parts.append(f"  {fc}")
    parts.append(
        "\nManual recovery:"
        f"\n  1. Inspect the commits above"
        f"\n  2. If safe, run: git reset --hard {pre_release_sha.strip()[:10]}"
        f"\n  3. Otherwise, cherry-pick or stash foreign work first"
    )
    raise RollbackClobberError("\n".join(parts))


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


def _rel_to_git_root(path, git_root):
    """Normalize path; make relative to git root if absolute."""
    n = os.path.normpath(path)
    if os.path.isabs(n):
        return os.path.relpath(n, git_root)
    return n


def resolve_target_paths(project_dir=".", releasable_config_dir=None):
    """Build a dict mapping target names to their resolved paths.

    Resolution goes through :func:`rlsbl.member_context.resolve_member_context`,
    which reads the merged config "targets" (supporting both plain strings and
    dicts with "name"/"path", with releasable-level inheritance when
    ``releasable_config_dir`` is given) and falls back to auto-detection.

    Returns dict[str, str] mapping target name -> resolved directory path.
    """
    from ...member_context import resolve_member_context

    member = resolve_member_context(
        project_dir, releasable_config_dir=releasable_config_dir,
    )
    return member.target_paths


def resolve_release_targets(primary, flags, project_dir=".", *, config,
                            releasable_config_dir=None):
    """Compute the effective set of secondary targets for this release.

    Reads the baseline from config "release_targets" list.
    If absent, falls back to auto-detect (all targets that detect("."),
    with releasable-level inheritance when ``releasable_config_dir`` is
    given). Entries can be plain strings or dicts with "name" and
    optional "path".

    The primary target is always excluded from the secondary set
    (it's handled separately by the main release flow).

    Returns a dict mapping target name -> resolved directory path.
    """
    from . import TARGETS as ALL_TARGETS, _parse_target_entry, ConfigError

    configured = config.get("release_targets")

    # Build baseline: dict of name -> path
    if configured is not None:
        baseline = {}
        for entry in configured:
            try:
                te = _parse_target_entry(entry, project_dir)
            except (ConfigError, TypeError):
                # Unparseable entry -- skip
                continue
            if te.name in ALL_TARGETS:
                baseline[te.name] = te.path
    else:
        # Auto-detect: use detect_targets which handles config and fallback
        baseline = resolve_target_paths(
            project_dir, releasable_config_dir=releasable_config_dir,
        )

    # Never include the primary target in the secondary set
    baseline.pop(primary, None)

    return baseline


# Lockfile specs: (lockfile, tool_name, sync_cmd, guard_file)
# guard_file: if set, the spec only applies when this file exists in the same directory.
# This distinguishes e.g. go.sum (per-module) from go.work.sum (workspace root only).
_LOCKFILE_SPECS = [
    ("uv.lock", "uv", ["uv", "lock"], None),
    ("package-lock.json", "npm", ["npm", "install", "--package-lock-only"], None),
    ("go.sum", "go", ["go", "mod", "tidy"], None),
    ("go.work.sum", "go", ["go", "work", "sync"], "go.work"),
    ("gradle.lockfile", "gradle", ["./gradlew", "dependencies", "--write-locks"], None),
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
    import shutil

    from . import subprocess

    for _target_name, t_path in target_paths.items():
        for lockfile, tool_name, sync_cmd, guard_file in _LOCKFILE_SPECS:
            if guard_file and not os.path.exists(os.path.join(t_path, guard_file)):
                continue
            lockfile_path = os.path.join(t_path, lockfile)
            if not os.path.exists(lockfile_path):
                continue

            # For commands using a project-local wrapper (e.g. ./gradlew),
            # check if the wrapper exists in the target directory instead of
            # looking for the tool on PATH.
            if sync_cmd[0].startswith("./"):
                wrapper_path = os.path.join(t_path, sync_cmd[0][2:])
                if not os.path.exists(wrapper_path):
                    log(f"Warning: {sync_cmd[0]} not found in {t_path}, skipping {lockfile} sync")
                    continue
            elif shutil.which(tool_name) is None:
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
                except Exception as e:
                    from ...utils import warn_exception
                    warn_exception("git check-ignore failed for lockfile", e)
                if norm_path not in files_to_commit:
                    files_to_commit.append(norm_path)
                    log(f"Lockfile updated: {lockfile}")


def archive_blog_body(releases_dir, version):
    """Archive unreleased.md to v{version}.md during release finalization.

    ``releases_dir`` is the resolved releases directory (member
    ``.rlsbl/releases/``, or the releasable's ``releases/`` dir in
    explicit releasable mode).

    Returns the archived path if the file existed, None otherwise.
    """
    blog_body_src = os.path.join(releases_dir, "unreleased.md")
    blog_body_dst = os.path.join(releases_dir, f"v{version}.md")
    if os.path.exists(blog_body_src):
        os.rename(blog_body_src, blog_body_dst)
        os.chmod(blog_body_dst, 0o444)
        return blog_body_dst
    return None


def collect_companion_tags(member_package_paths, workspace_root, version,
                           primary_tag, releasable_config_dir=None):
    """Collect companion tags from all publishing member packages.

    Iterates member packages in an explicit releasable, detects their
    targets, and collects companion tags (e.g. Go module proxy tags).

    Guards:
    - Only meaningful in explicit releasable mode (caller checks).
    - Skips companion creation if the primary tag already contains a
      ``/v`` pattern (Go-compatible), to avoid duplicate tags.
    - Skips publish-suppressed packages (same logic as _sync_member_package_versions,
      including releasable-level config inheritance).

    Args:
        member_package_paths: workspace-relative paths for member packages.
        workspace_root: absolute path to the monorepo root.
        version: version string being released.
        primary_tag: the primary release tag string.
        releasable_config_dir: optional path to the releasable's state
            directory for config inheritance.

    Returns:
        List of companion tag strings (deduplicated, excluding the primary tag).
    """
    from . import TARGETS
    from ...member_context import resolve_member_context

    # If the primary tag is already Go-compatible (contains /v), skip
    # companion creation to avoid duplicates.
    if "/v" in primary_tag:
        return []

    seen = set()
    result = []
    for pkg_path in member_package_paths:
        abs_pkg = os.path.join(str(workspace_root), pkg_path)
        if not os.path.isdir(abs_pkg):
            continue

        # A broken member config is a hard error, mirroring
        # _sync_member_package_versions: version sync and companion-tag
        # collection must agree on the member set, so this must never
        # silently skip a member the sync path would abort on. (The undo
        # flow wraps its collect_companion_tags call in its own try/except
        # and degrades gracefully there.)
        member = resolve_member_context(
            abs_pkg, releasable_config_dir=releasable_config_dir,
        )

        # Skip publish-suppressed packages (publish_mode == "none")
        if member.publish_mode == "none":
            continue

        entries = member.targets
        if not entries:
            continue

        for entry in entries:
            tgt = TARGETS.get(entry.name)
            if not tgt:
                continue
            for ctag in tgt.companion_tags(entry.name, version, path=pkg_path):
                if ctag not in seen and ctag != primary_tag:
                    seen.add(ctag)
                    result.append(ctag)

    return result


def _sync_member_package_versions(
    member_package_paths, monorepo_root, new_version,
    files_to_commit, git_root, log, ctx,
    exclude_path=None,
    releasable_config_dir=None,
):
    """Sync version to published member packages in explicit releasable mode.

    For each member package that publishes (publish_mode != "none", has a
    detected target), writes the version to the manifest. Publish-suppressed
    packages are left untouched.

    Uses ``read_project_config`` with releasable inheritance so that
    releasable-level ``publish_mode`` is respected by member packages
    that don't set it themselves.

    Args:
        member_package_paths: list of workspace-relative paths for member packages.
        monorepo_root: absolute path to the monorepo root.
        new_version: version string to write.
        files_to_commit: list to append modified file paths to.
        git_root: absolute path to the git root.
        log: logging callable.
        ctx: ProjectContext.
        exclude_path: optional workspace-relative path to skip (already handled).
        releasable_config_dir: optional path to the releasable's state directory
            for config inheritance.
    """
    from . import TARGETS
    from ...member_context import resolve_member_context

    for pkg_path in member_package_paths:
        if exclude_path and pkg_path == exclude_path:
            continue

        abs_pkg = os.path.join(str(monorepo_root), pkg_path)
        if not os.path.isdir(abs_pkg):
            continue

        # Resolve config through the inheritance-aware path
        member = resolve_member_context(
            abs_pkg, releasable_config_dir=releasable_config_dir,
        )

        # Skip publish-suppressed packages (publish_mode == "none")
        if member.publish_mode == "none":
            continue

        # Detect targets and write version
        entries = member.targets
        if not entries:
            continue

        for entry in entries:
            tgt = TARGETS.get(entry.name)
            if not tgt:
                continue
            if not tgt.check_project_exists(entry.path):
                from ...errors import ConfigError
                raise ConfigError(
                    f"member '{pkg_path}' declares target '{entry.name}' but "
                    f"its manifest does not exist at {entry.path}. "
                    f"Cannot sync version."
                )
            modified = tgt.write_version(entry.path, new_version, ctx=ctx)
            for rel in modified:
                fpath = _rel_to_git_root(os.path.join(entry.path, rel), git_root)
                if fpath not in files_to_commit:
                    files_to_commit.append(fpath)
            if modified:
                log(f"Synced version to member {pkg_path}: {', '.join(modified)}")


@dataclasses.dataclass
class ReleaseState:
    """All state needed by _run_release_mutating, grouped logically."""

    # Identity
    registry: str
    target: object  # TARGETS[registry] instance -- replaces both 'reg' and 'target' params
    new_version: str
    current_version: str
    bump_type: str | None
    tag: str
    branch: str

    # Paths
    primary_path: str | None = None
    target_paths: dict | None = None
    lock_dir: str = ".rlsbl"
    changes_dir: str | None = None  # resolved changes dir (releasable or per-project)

    # Monorepo
    monorepo_name: str | None = None
    monorepo_project_path: str | None = None

    # Releasable (explicit mode) -- None in implicit mode
    releasable_name: str | None = None
    member_package_paths: list[str] | None = None
    releasable_tag_format: str | None = None

    # Metadata
    changelog_entry: str | None = None
    commit_msg: str | None = None
    description: str = ""
    context: str = ""

    # State
    pre_existing_dirty: set | None = None
    hook_generated: set | None = None
    secondary_targets: dict | None = None
    companion_tags: list[str] = dataclasses.field(default_factory=list)
    completed_steps: list[str] = dataclasses.field(default_factory=list)

    # Release config fields (persisted in state file for resume)
    include: list[str] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)
    preid: str = ""
    blog: bool = False

    # Control
    flags: dict = dataclasses.field(default_factory=dict)
    quiet: bool = False
    log: object = None  # callable
    ctx: object = None  # ProjectContext


def _run_release_mutating(state: ReleaseState):
    """Inner release logic that runs under the advisory lock (mutating phase)."""
    # Unpack frequently-used state into locals for readability and to preserve
    # the existing closure/reference patterns (commit_msg, primary_path, and
    # target_paths are conditionally reassigned below).
    registry = state.registry
    target = state.target
    reg = state.target  # 'reg' and 'target' were always the same object
    flags = state.flags
    quiet = state.quiet
    log = state.log
    new_version = state.new_version
    current_version = state.current_version
    bump_type = state.bump_type
    tag = state.tag
    branch = state.branch
    changelog_entry = state.changelog_entry
    secondary_targets = state.secondary_targets
    monorepo_name = state.monorepo_name
    monorepo_project_path = state.monorepo_project_path
    releasable_name = state.releasable_name
    member_package_paths = state.member_package_paths
    releasable_tag_format_str = state.releasable_tag_format
    commit_msg = state.commit_msg
    primary_path = state.primary_path
    target_paths = state.target_paths
    lock_dir = state.lock_dir
    pre_existing_dirty = state.pre_existing_dirty
    hook_generated = state.hook_generated
    description = state.description
    context = state.context
    ctx = state.ctx

    # Late-bound imports from the package namespace for mock.patch compatibility.
    # All rlsbl-internal names are resolved through __init__.py so that
    # mock.patch("rlsbl.commands.release.X") is picked up at call time.
    from . import (
        run,
        run_gh,
        push_if_needed,
        commit_files,
        commit_files_if_changed,
        has_staged_or_modified,
        get_push_timeout,
        get_hook_timeout,
        get_current_branch,
        should_tag,
        tag_exists_locally,
        tag_exists_on_remote,
        TARGETS,
        load_pipelines,
        load_workspace,
        read_deploy_config,
        deploy_target,
        ensure_github_topic,
        ensure_npm_keyword,
        ensure_pypi_keyword,
        changes_dir_exists,
        finalize_changeset_version,
        finalize_version,
        generate_version_file,
        get_changes_dir,
        read_coverage_unit,
        validate_subtree_remote_ssh_host,
        _cleanup_release_artifacts,
        upload_release_assets,
        _print_stale_dep_advisory,
        parse_porcelain_paths,
        ReleaseValidationError,
        HookError,
        _read_release_metadata_full,
    )

    project_root = ctx.project_root
    monorepo_root = ctx.workspace_root
    project_dir = str(project_root)

    # Releasable state dir for member config/target inheritance (explicit mode)
    _releasable_cfg_dir = None
    if releasable_name and monorepo_root:
        from ...workspace import get_releasable_dir
        _releasable_cfg_dir = get_releasable_dir(str(monorepo_root), releasable_name)

    # In releasable mode, the representative member is subject to the same
    # publish_mode-awareness as every other member (resolve_member_context with
    # releasable-level inheritance): a publish-suppressed representative's manifests
    # are never version-bumped or keyword-tagged. The releasable version
    # file remains the source of truth and is always updated.
    _rep_is_private = False
    if _releasable_cfg_dir is not None:
        from ...member_context import resolve_member_context
        _rep_is_private = resolve_member_context(
            project_dir, releasable_config_dir=_releasable_cfg_dir,
        ).publish_mode == "none"

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
        target_paths = resolve_target_paths(
            project_dir, releasable_config_dir=_releasable_cfg_dir,
        )

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
            raise KeyboardInterrupt
        if answer != "y":
            print("Aborted.")
            raise SystemExit(0)

    # Capture HEAD before any version-bump writes so we can roll back on failure.
    # This must happen before write_version() so that git reset --hard reverts
    # the uncommitted version-bumped files if the release aborts.
    pre_release_sha = run("git", ["rev-parse", "HEAD"])

    # Write release state file at the start of the mutating phase.
    # This persists if the release fails mid-way, enabling future resume.
    # On a resume, an existing state file may already contain completed_steps
    # from a prior run. Preserve those so per-step guards can skip them.
    # Releasable releases keep their state under the releasable's own dir
    # (.rlsbl-monorepo/releasables/<name>/releases/), never under the
    # representative member's .rlsbl/.
    _state_path = get_state_path(project_dir, releasable_dir=_releasable_cfg_dir)
    _existing_state = load_release_state(_state_path)
    _prior_completed = (
        _existing_state.get("completed_steps", [])
        if _existing_state is not None
        else list(state.completed_steps)
    )
    _prior_failed = (
        _existing_state.get("failed_steps", {})
        if _existing_state is not None
        else {}
    )
    _state_dict = {
        "new_version": new_version,
        "tag": tag,
        "branch": branch,
        "pre_release_sha": pre_release_sha,
        "bump_type": bump_type,
        "registry": registry,
        "completed_steps": list(_prior_completed),
        "failed_steps": dict(_prior_failed),
        "companion_tags": [],
        "monorepo_name": monorepo_name,
        "releasable_name": releasable_name,
        "commit_msg": commit_msg,
        "description": description,
        "context": context,
        "include": list(state.include),
        "exclude": list(state.exclude),
        "preid": state.preid,
        "blog": state.blog,
    }
    save_release_state(_state_path, _state_dict)
    # Load completed_steps to check which steps are already done (empty on
    # fresh start; populated when resuming from a prior failed attempt).
    _completed = set(_state_dict.get("completed_steps", []))

    # Track whether the branch push succeeded. Once commits are on the
    # remote, a local `git reset --hard` would create divergent state.
    # Set to True after push_if_needed() returns successfully.
    branch_pushed = False

    def _is_push_timeout(_exc):
        """True when a push failure was a timeout.

        Both the branch push (via ``push_if_needed``) and the tag push
        surface timeouts as :class:`GitError` whose message contains
        "timed out"; a raw :class:`subprocess.TimeoutExpired` counts too
        (belt-and-braces in case conversion is bypassed).
        """
        import subprocess as _sp
        from ...errors import GitError
        if isinstance(_exc, _sp.TimeoutExpired):
            return True
        return isinstance(_exc, GitError) and "timed out" in str(_exc).lower()

    def _handle_resumable_push_failure(_exc):
        """Classify a post-TAGGED push failure as RESUMABLE (no rollback).

        Once the release is TAGGED and its changelog / release-file
        artifacts are finalized on disk, a push failure is the canonical
        resumable state: the tag exists, the finalized files are committed,
        and ``rlsbl release resume`` re-attempts the push with idempotent
        guards. We therefore skip the entire rollback family — no clobber
        guard, no ``git reset --hard``, no tag deletion, no artifact
        cleanup, no state clearing — record a failed PUSHED marker, and
        print the resume command. When the branch is already on the remote,
        a best-effort tag-push retry is attempted first (transient stalls
        often clear on retry); a recovered tag push marks PUSHED complete so
        resume picks up at GITHUB_RELEASE.
        """
        _timed_out = _is_push_timeout(_exc)
        _retry_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
        _retry_timeout = get_push_timeout(ctx.config)
        if branch_pushed:
            # Branch commits are already on the remote; only the tag push
            # is outstanding. Retry it a couple of times before giving up.
            for _attempt in range(2):
                time.sleep(1)
                try:
                    run(
                        "git",
                        ["push", "origin", tag] + list(state.companion_tags),
                        timeout=_retry_timeout, env=_retry_env,
                    )
                    log(f"Tag push succeeded on retry {_attempt + 1}")
                    save_step(_state_path, "PUSHED")
                    _completed.add("PUSHED")
                    break
                except Exception:
                    pass
        if "PUSHED" not in _completed:
            # Record the failed PUSHED step (fatal + resumable). This does
            # NOT gate resume-skip; resume re-attempts PUSHED via its own
            # idempotent branch/tag guards.
            save_step_failure(
                _state_path, "PUSHED", str(_exc) or _exc.__class__.__name__,
            )
        if hasattr(_exc, "stderr") and _exc.stderr:
            print(f"Command error: {_exc.stderr.strip()}", file=sys.stderr)
        print(
            f"Error: push failed after the release was tagged ({tag}). "
            f"Local state is intact and fully resumable — nothing was rolled "
            f"back; the tag and finalized changelog files are preserved.",
            file=sys.stderr,
        )
        if _timed_out:
            print(
                f"The push timed out (limit: {_retry_timeout}s). Raise the "
                f"timeout and resume:\n"
                f"  RLSBL_PUSH_TIMEOUT=300 rlsbl release resume",
                file=sys.stderr,
            )
        else:
            print(
                "Fix the issue and resume:\n  rlsbl release resume",
                file=sys.stderr,
            )

    def _warn_rollback_residuals():
        """Postcondition check: warn if the working tree is not clean after a
        pre-TAGGED rollback.

        A correct rollback (``git reset --hard`` + orphan-artifact cleanup)
        must leave the working tree byte-identical to the pre-release HEAD.
        If ``git status --porcelain`` still reports changes, something was not
        fully reverted (e.g. a residual generated file). This is a warning,
        not a fatal error -- the original release failure is the primary
        signal -- but it names every leftover path so manual cleanup is
        possible before retrying.

        Transient release-machinery files that are not rollback residuals are
        excluded: the advisory lock file (``.rlsbl/lock``, released by the
        caller's ``finally`` after this handler) and the in-progress state
        file (already removed by ``clear_release_state`` above, but excluded
        defensively).
        """
        try:
            residual = run("git", ["status", "--porcelain"]).strip()
        except Exception:
            return
        if not residual:
            return
        # Transient paths that are not rollback residuals, as absolute paths.
        _transient_abs = {
            os.path.abspath(os.path.join(project_dir, lock_dir, "lock")),
            os.path.abspath(_state_path),
        }
        leftover_paths = []
        for _p in parse_porcelain_paths(residual):
            _abs = os.path.abspath(os.path.join(_git_root, _p.rstrip("/")))
            if _abs in _transient_abs:
                continue
            leftover_paths.append(_p)
        if not leftover_paths:
            return
        print(
            "Warning: rollback left residual working-tree changes; "
            "may need manual cleanup before retrying:",
            file=sys.stderr,
        )
        for _p in sorted(leftover_paths):
            print(f"  {_p}", file=sys.stderr)

    # Everything from version-bump writes through commit/tag/push is wrapped
    # in a single try block so that any failure (including ReleaseAbortError
    # from the unexpected-files check) triggers rollback of version-bumped
    # files via git reset --hard.
    try:
        # Write new version to version files (skip if version didn't change, e.g. first release)
        # Build files_to_commit from the paths actually modified by write_version().
        files_to_commit = []

        # VERSION_BUMPED guard: skip if the primary target already has the new version
        _version_already_bumped = False
        if "VERSION_BUMPED" in _completed:
            _version_already_bumped = True
            log("Skipping version bump (already done)")
        elif new_version != current_version:
            try:
                _on_disk_version = reg.read_version(primary_path)
                if _on_disk_version == new_version:
                    _version_already_bumped = True
                    save_step(_state_path, "VERSION_BUMPED")
                    _completed.add("VERSION_BUMPED")
                    log("Skipping version bump (version already matches)")
            except Exception:
                pass  # read_version may fail if target has no manifest yet

        if not _version_already_bumped and new_version != current_version:
            # In explicit releasable mode, write the releasable version file first
            if releasable_name and monorepo_root:
                from ...workspace import write_releasable_version, get_releasable_version_path
                write_releasable_version(str(monorepo_root), releasable_name, new_version)
                ver_path = get_releasable_version_path(str(monorepo_root), releasable_name)
                ver_rel = _rel_to_git_root(ver_path, _git_root)
                files_to_commit.append(ver_rel)
                log(f"Updated releasable version: {releasable_name} -> {new_version}")

            if _rep_is_private:
                log("Skipping representative manifest bump (publish-suppressed member)")
            else:
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

            # In explicit mode, sync version to published member packages
            if releasable_name and member_package_paths and monorepo_root:
                _sync_member_package_versions(
                    member_package_paths, monorepo_root, new_version,
                    files_to_commit, _git_root, log, ctx,
                    # Skip the current project's path -- already handled above
                    exclude_path=monorepo_project_path,
                    releasable_config_dir=_releasable_cfg_dir,
                )

            # Bump selfdoc.json version inline (no DocsTarget dependency).
            bumped_files = set(files_to_commit)
            selfdoc_modified = _bump_selfdoc_version(project_dir, new_version)
            for rel in selfdoc_modified:
                fpath = vpath(rel)
                if fpath not in bumped_files:
                    files_to_commit.append(fpath)
            if selfdoc_modified:
                log(f"Synced version to {', '.join(vpath(r) for r in selfdoc_modified)}")

            save_step(_state_path, "VERSION_BUMPED")
            _completed.add("VERSION_BUMPED")

        if "VERSION_BUMPED" not in _completed:
            # Version unchanged (first release): nothing to bump. Mark the
            # step so completeness is provable at the epilogue.
            save_step(_state_path, "VERSION_BUMPED")
            _completed.add("VERSION_BUMPED")

        # Ecosystem tagging: add keyword to manifests if enabled. A private
        # representative's manifests are never touched (same private-awareness
        # as the version bump above).
        if should_tag(flags, ctx.config) and not _rep_is_private:
            npm_path = target_paths.get("npm", project_dir)
            try:
                if TARGETS["npm"].check_project_exists(npm_path):
                    if ensure_npm_keyword(npm_path, quiet=quiet, project_root=project_root):
                        pkg_path = target_vpath(npm_path, "package.json")
                        if pkg_path not in files_to_commit:
                            files_to_commit.append(pkg_path)
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("npm ecosystem tagging failed", e)
            pypi_path = target_paths.get("pypi", project_dir)
            try:
                if TARGETS["pypi"].check_project_exists(pypi_path):
                    if ensure_pypi_keyword(pypi_path, quiet=quiet, project_root=project_root):
                        pyproject_path = target_vpath(pypi_path, "pyproject.toml")
                        if pyproject_path not in files_to_commit:
                            files_to_commit.append(pyproject_path)
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("pypi ecosystem tagging failed", e)

        # Sync lockfiles after version bumps so they reflect the new version
        _sync_lockfiles(target_paths, files_to_commit, log)
        # In releasable mode, also sync lockfiles for publishing members
        # whose manifests were version-bumped by _sync_member_package_versions.
        if releasable_name and member_package_paths and monorepo_root:
            from ...member_context import resolve_member_context as _rmc_lock
            for _mp_path in member_package_paths:
                _mp_abs = os.path.join(str(monorepo_root), _mp_path)
                if not os.path.isdir(_mp_abs):
                    continue
                _mp_member = _rmc_lock(
                    _mp_abs, releasable_config_dir=_releasable_cfg_dir,
                )
                if _mp_member.publish_mode == "none":
                    continue
                _mp_tpaths = _mp_member.target_paths
                if _mp_tpaths:
                    _sync_lockfiles(_mp_tpaths, files_to_commit, log)
        if monorepo_root:
            _sync_lockfiles({"workspace_root": str(monorepo_root)}, files_to_commit, log)
            # Unconditionally include workspace-root lockfiles that exist.
            # _sync_lockfiles only adds a lockfile when its mtime changes, but the
            # lockfile may already be stale from the version bump (especially npm,
            # see npm bug #5967). Including unconditionally ensures the release
            # commit captures any pre-existing staleness.
            for ws_lockfile_name, _, _, ws_guard in _LOCKFILE_SPECS:
                if ws_guard and not os.path.exists(os.path.join(str(monorepo_root), ws_guard)):
                    continue
                ws_lockfile = os.path.join(str(monorepo_root), ws_lockfile_name)
                if os.path.exists(ws_lockfile):
                    norm = os.path.normpath(ws_lockfile)
                    if norm not in files_to_commit:
                        files_to_commit.append(norm)
                        log(f"Workspace lockfile included: {ws_lockfile_name}")

        # Update .rlsbl/version marker (the rlsbl TOOL version that generated
        # the scaffolding) so it's included in the release commit. Only
        # refresh it when the project was actually scaffolded (scaffold
        # metadata present); never in releasable mode -- releasable member
        # dirs are not scaffolded and nothing reads a member-level marker
        # (the pre-push freshness check reads the repo root only).
        rlsbl_version_marker = vpath(os.path.join(".rlsbl", "version"))
        _scaffold_meta_present = any(
            os.path.exists(os.path.join(project_dir, ".rlsbl", meta))
            for meta in ("managed-files.json",)
        )
        if _releasable_cfg_dir is None and _scaffold_meta_present:
            try:
                from ... import __version__ as rlsbl_ver
                with open(rlsbl_version_marker, "w") as f:
                    f.write(rlsbl_ver + "\n")
                if rlsbl_version_marker not in files_to_commit:
                    files_to_commit.append(rlsbl_version_marker)
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("writing .rlsbl/version marker failed", e)

        # Include the generated CHANGELOG.md files in the commit (dev nodes
        # have no CHANGELOG.md). The canonical location is resolved via
        # rlsbl.changelog.home: releasable dir in releasable mode (plus the
        # combined root CHANGELOG.md), project root otherwise.
        from ...changelog.home import get_changelog_home, get_workspace_changelog_path
        _changelog_commit_files = []
        _canonical_changelog = get_changelog_home(
            project_dir, releasable_dir=_releasable_cfg_dir,
        )
        if os.path.exists(_canonical_changelog):
            _changelog_commit_files.append(
                _rel_to_git_root(_canonical_changelog, _git_root)
            )
        if _releasable_cfg_dir and monorepo_root:
            _root_changelog = get_workspace_changelog_path(str(monorepo_root))
            if os.path.exists(_root_changelog):
                _changelog_commit_files.append(
                    _rel_to_git_root(_root_changelog, _git_root)
                )
        for _cl_file in _changelog_commit_files:
            if _cl_file not in files_to_commit:
                files_to_commit.append(_cl_file)

        # Include hook-generated files (created or modified by pre-checks/pre-release hooks)
        if hook_generated:
            for hf in sorted(hook_generated):
                if hf not in files_to_commit:
                    files_to_commit.append(hf)
                    log(f"Including hook-generated file: {hf}")

        # Clear stale artifacts from dist/ BEFORE building so the secret scan
        # below is scoped to exactly this release's output. Build tools append
        # to dist/ without pruning older versions' artifacts; scanning those
        # stale files could surface old secrets or slow the release.
        from ...secret_scan import clean_stale_artifacts
        clean_stale_artifacts(project_dir, log=log)

        # Build step: primary target (e.g. pypi builds a wheel, maven runs
        # gradle/mvn, pgdesign validates the schema).  Failures propagate to
        # the outer rollback handler.
        _build_config = ctx.config if ctx else None
        target.build(primary_path, new_version, config=_build_config)

        # Build step: secondary targets (multi-target projects).
        if secondary_targets:
            from ...targets import TARGETS as ALL_TARGETS
            for sec_name in sorted(secondary_targets):
                sec_target = ALL_TARGETS.get(sec_name)
                if sec_target is None:
                    continue
                sec_path = secondary_targets[sec_name]
                sec_target.build(sec_path, new_version, config=_build_config)

        # Secret scan gate: scan built artifacts for leaked secrets before
        # any push or publish. This is a hard, non-bypassable gate.
        from ...secret_scan import scan_artifacts_for_secrets, SecretScanError
        try:
            scan_artifacts_for_secrets(project_dir, log=log)
        except SecretScanError as e:
            raise ReleaseAbortError(str(e))

        # Re-check working tree: abort if files outside our expected set were modified
        # (guards against concurrent processes dirtying the tree after our initial check)
        dirty_output = run("git", ["status", "--porcelain"])
        if dirty_output:
            dirty_files = parse_porcelain_paths(dirty_output)
            # Normalize all files_to_commit to git-relative paths.
            # Some callers (e.g. _sync_lockfiles)
            # add absolute paths via os.path.normpath(); git status
            # --porcelain outputs repo-relative paths, so we must match.
            expected_files = {
                os.path.relpath(os.path.abspath(f), _git_root) if os.path.isabs(f) else f
                for f in files_to_commit
            }
            expected_files.add(vpath(os.path.join(lock_dir, "lock")))
            # The release state file (in-progress.json) is written by this
            # function and should not trigger the unexpected-files guard.
            # Also add the parent directory with trailing slash since git
            # status --porcelain may show newly-created directories as e.g.
            # "?? .rlsbl/releases/" instead of listing individual files.
            _state_abs = os.path.abspath(_state_path)
            _state_rel = os.path.relpath(_state_abs, _git_root)
            expected_files.add(_state_rel)
            _state_dir_rel = os.path.relpath(os.path.dirname(_state_abs), _git_root)
            expected_files.add(_state_dir_rel + "/")
            # The .validated cache is written by changelog validation earlier in the
            # release flow.  It may be tracked (dirty) or gitignored (invisible to
            # git status).  Either way it is not a concurrent-change signal.
            # Use the resolved changes_dir (covers both releasable and per-project).
            _validated_changes_dir = state.changes_dir or get_changes_dir(project_dir)
            validated_raw = os.path.normpath(
                os.path.join(_validated_changes_dir, ".validated")
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

        # COMMITTED guard: skip if HEAD commit message already matches
        _commit_already_done = False
        if "COMMITTED" in _completed:
            _commit_already_done = True
            log("Skipping commit (already done)")
        else:
            _head_msg = run("git", ["log", "-1", "--format=%s"]).strip()
            if _head_msg == commit_msg:
                _commit_already_done = True
                save_step(_state_path, "COMMITTED")
                _completed.add("COMMITTED")
                log("Skipping commit (HEAD already matches)")

        if not _commit_already_done:
            # Commit if any of the files we track actually have changes.
            # Don't use is_clean_tree() as a proxy — the advisory lock file (.rlsbl/lock)
            # makes the tree appear dirty even when no release-relevant files changed.

            needs_commit = new_version != current_version or has_staged_or_modified(files_to_commit, cwd=_git_root)
            if files_to_commit and needs_commit:
                commit_files(commit_msg, files_to_commit, cwd=_git_root)
                _track_release_commit(_state_path)
                log(f"Committed: {commit_msg}")
            elif not needs_commit:
                log("No changes to commit")
            save_step(_state_path, "COMMITTED")
            _completed.add("COMMITTED")

        # Finalize JSONL changelog: rename unreleased.jsonl to x.y.z.jsonl.
        # CHANGELOG.md already has the correct "## X.Y.Z" heading because the
        # earlier generate_changelog() call (above acquire_lock) was passed
        # version_override=new_version, so no regeneration is needed here.
        #
        # In explicit releasable mode, the changes dir lives at the releasable
        # level, and the tag glob uses the releasable's tag format. The
        # resolved changes_dir is passed via state.changes_dir.
        changes_dir = state.changes_dir
        if changes_dir is None and changes_dir_exists(project_dir):
            changes_dir = get_changes_dir(project_dir)

        # CHANGELOG_FINALIZED guard: skip if {version}.jsonl already exists
        # and unreleased.jsonl is empty (indicating finalization already ran).
        _changelog_already_finalized = False
        if "CHANGELOG_FINALIZED" in _completed:
            _changelog_already_finalized = True
            log("Skipping changelog finalization (already done)")
        elif changes_dir and os.path.isdir(changes_dir):
            _versioned_jsonl = os.path.join(changes_dir, f"{new_version}.jsonl")
            _unreleased_jsonl = os.path.join(changes_dir, "unreleased.jsonl")
            if os.path.exists(_versioned_jsonl):
                _unreleased_empty = (
                    not os.path.exists(_unreleased_jsonl)
                    or os.path.getsize(_unreleased_jsonl) == 0
                )
                if _unreleased_empty:
                    _changelog_already_finalized = True
                    save_step(_state_path, "CHANGELOG_FINALIZED")
                    _completed.add("CHANGELOG_FINALIZED")
                    log("Skipping changelog finalization (version JSONL already exists)")

        if not _changelog_already_finalized and changes_dir and os.path.isdir(changes_dir):
            if releasable_name and releasable_tag_format_str:
                from .validate import _releasable_tag_glob
                tag_glob = _releasable_tag_glob(releasable_tag_format_str, releasable_name)
            elif monorepo_name:
                tag_glob = target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path)
            else:
                tag_glob = None

            # Determine coverage mode for finalization
            _coverage_unit = read_coverage_unit(ctx.config)
            if _coverage_unit == "changeset-file":
                finalize_changeset_version(changes_dir, new_version)
            else:
                finalize_version(changes_dir, new_version, tag_glob=tag_glob)
            # Pass release metadata so the new version's .md matches what a
            # future backfill from the archived v{version}.toml would produce
            # (the archived toml is stripped on read, so strip here too).
            generate_version_file(
                changes_dir, new_version,
                description=(description or "").strip(),
                context=(context or "").strip(),
                bump_type=bump_type,
            )
            log(f"Finalized JSONL changelog for {new_version}")
            # Commit the finalized JSONL file and the new empty unreleased.jsonl
            jsonl_finalized = _rel_to_git_root(os.path.join(changes_dir, f"{new_version}.jsonl"), _git_root)
            finalize_files = [jsonl_finalized, *_changelog_commit_files]
            if _coverage_unit != "changeset-file":
                jsonl_unreleased = _rel_to_git_root(os.path.join(changes_dir, "unreleased.jsonl"), _git_root)
                finalize_files.append(jsonl_unreleased)
            # Also commit the generated per-version .md file if it exists
            jsonl_md = _rel_to_git_root(os.path.join(changes_dir, f"{new_version}.md"), _git_root)
            if os.path.exists(jsonl_md):
                finalize_files.append(jsonl_md)
            # generate_changelog() (run before the mutating phase) backfills
            # description/context from archived release files into OLDER
            # per-version .md files. Include any it actually modified so the
            # release leaves a clean working tree. Only files git reports as
            # changed are added (passing unchanged files to safegit may error).
            md_status = run("git", ["status", "--porcelain", "--", changes_dir])
            if md_status:
                for md_path in sorted(parse_porcelain_paths(md_status)):
                    if md_path.endswith(".md") and md_path not in finalize_files:
                        finalize_files.append(md_path)
            commit_files(f"chore: finalize changelog for {new_version}", finalize_files, cwd=_git_root)
            _track_release_commit(_state_path)
            log(f"Committed finalized changelog files")
            save_step(_state_path, "CHANGELOG_FINALIZED")
            _completed.add("CHANGELOG_FINALIZED")
        elif not changes_dir or not os.path.isdir(changes_dir or ""):
            log("No .rlsbl/changes/ directory; skipping changelog finalization")
            # Not applicable: mark so completeness is provable at the epilogue.
            save_step(_state_path, "CHANGELOG_FINALIZED")
            _completed.add("CHANGELOG_FINALIZED")

        # Clean stale batch_limits exclusions that referenced unreleased.jsonl.
        # In releasable mode, `changelog add --allow-batch` writes exclusions
        # to the RELEASABLE-level config.json, so clean that one.
        from ...config import clean_stale_exclusions
        if _releasable_cfg_dir is not None:
            config_path = os.path.join(_releasable_cfg_dir, "config.json")
        else:
            config_path = os.path.join(project_dir, ".rlsbl", "config.json")
        if os.path.exists(config_path):
            removed = clean_stale_exclusions(config_path)
            if removed:
                config_rel = _rel_to_git_root(config_path, _git_root)
                commit_files(
                    f"chore: clean {removed} stale batch exclusion(s) from config.json",
                    [config_rel],
                    cwd=_git_root,
                )
                _track_release_commit(_state_path)
                log(f"Cleaned {removed} stale batch exclusion(s) from config.json")

        # Finalize release file: rename unreleased.toml to vX.Y.Z.toml
        # RELEASE_FILE_FINALIZED guard: skip if vX.Y.Z.toml exists and
        # unreleased.toml doesn't (indicating finalization already ran).
        # Releasable releases keep the release file (and its archive) under
        # the releasable's own releases dir, never the member's .rlsbl/.
        from ...release_file import get_release_file_path
        release_file_path = get_release_file_path(
            project_dir, releasable_dir=_releasable_cfg_dir,
        )
        _release_file_already_finalized = False
        if "RELEASE_FILE_FINALIZED" in _completed:
            _release_file_already_finalized = True
            log("Skipping release file finalization (already done)")
        else:
            releases_dir_rf = os.path.dirname(release_file_path)
            versioned_release_check = os.path.join(releases_dir_rf, f"v{new_version}.toml")
            if os.path.exists(versioned_release_check) and not os.path.exists(release_file_path):
                _release_file_already_finalized = True
                save_step(_state_path, "RELEASE_FILE_FINALIZED")
                _completed.add("RELEASE_FILE_FINALIZED")
                log("Skipping release file finalization (already archived)")

        if not _release_file_already_finalized and os.path.exists(release_file_path):
            releases_dir = os.path.dirname(release_file_path)
            versioned_release = os.path.join(releases_dir, f"v{new_version}.toml")
            os.rename(release_file_path, versioned_release)
            os.chmod(versioned_release, 0o444)
            # Archive blog body file if it exists (unreleased.md -> v{version}.md)
            blog_body_dst = archive_blog_body(releases_dir, new_version)
            release_finalize_files = [
                _rel_to_git_root(versioned_release, _git_root),
                _rel_to_git_root(release_file_path, _git_root),
            ]
            if blog_body_dst:
                release_finalize_files.append(_rel_to_git_root(blog_body_dst, _git_root))
            commit_files(f"chore: finalize release file for {new_version}", release_finalize_files, cwd=_git_root)
            _track_release_commit(_state_path)
            log(f"Finalized release file for {new_version}")

            # Now that v{version}.toml is archived, regenerate the per-version
            # .md so its content is derived from _read_release_metadata() rather
            # than the direct params passed earlier. This keeps the .md
            # consistent with what future generate_changelog() calls produce.
            changes_dir_regen = state.changes_dir or (get_changes_dir(project_dir) if changes_dir_exists(project_dir) else None)
            if changes_dir_regen and os.path.isdir(changes_dir_regen):
                ver_desc, ver_ctx, ver_bump = _read_release_metadata_full(
                    project_dir, new_version, releases_dir=releases_dir,
                )
                generate_version_file(
                    changes_dir_regen, new_version,
                    description=ver_desc, context=ver_ctx,
                    bump_type=ver_bump or None,
                )
                md_regen_path = os.path.join(changes_dir_regen, f"{new_version}.md")
                md_regen_rel = _rel_to_git_root(md_regen_path, _git_root)
                if has_staged_or_modified([md_regen_rel], cwd=_git_root):
                    commit_files(
                        f"chore: regenerate {new_version}.md from archived release metadata",
                        [md_regen_rel],
                        cwd=_git_root,
                    )
                    _track_release_commit(_state_path)
            save_step(_state_path, "RELEASE_FILE_FINALIZED")
            _completed.add("RELEASE_FILE_FINALIZED")

        if "RELEASE_FILE_FINALIZED" not in _completed:
            # No release file to archive (e.g. imperative invocation).
            # Mark so completeness is provable at the epilogue.
            save_step(_state_path, "RELEASE_FILE_FINALIZED")
            _completed.add("RELEASE_FILE_FINALIZED")

        # TAGGED guard: skip if the tag already exists and points to HEAD
        _tag_already_exists = False
        if "TAGGED" in _completed:
            _tag_already_exists = True
            log("Skipping tag creation (already done)")
        else:
            _existing_tag = tag_exists_locally(tag)
            if _existing_tag:
                # Tag exists -- verify it points to HEAD
                _tag_sha = run("git", ["rev-parse", f"refs/tags/{tag}^{{}}"]).strip()
                _head_sha = run("git", ["rev-parse", "HEAD"]).strip()
                if _tag_sha == _head_sha:
                    _tag_already_exists = True
                    save_step(_state_path, "TAGGED")
                    _completed.add("TAGGED")
                    log("Skipping tag creation (tag already exists at HEAD)")

        if not _tag_already_exists:
            # Create local git tag
            run("git", ["tag", tag])
            log(f"Tagged: {tag}")

            # Create companion tags (e.g. Go module proxy tags in releasable mode)
            if member_package_paths is not None:
                _companion_list = collect_companion_tags(
                    member_package_paths, monorepo_root, new_version, tag,
                    releasable_config_dir=_releasable_cfg_dir,
                )
                for ctag in _companion_list:
                    run("git", ["tag", ctag])
                    state.companion_tags.append(ctag)
                    log(f"Created Go companion tag: {ctag}")

            save_step(_state_path, "TAGGED")
            _completed.add("TAGGED")

        # PUSHED guard: skip branch push if remote matches local HEAD,
        # skip tag push if remote tag already exists.
        push_timeout = get_push_timeout(ctx.config)
        if push_timeout != 120:
            log(f"Push timeout: {push_timeout}s (from RLSBL_PUSH_TIMEOUT)")
        # Mark pushes as release-authorized so the pre-push hook skips its
        # "manual push" warning. The hook still runs JSONL coverage checks.
        push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}

        _push_already_done = False
        if "PUSHED" in _completed:
            _push_already_done = True
            branch_pushed = True
            log("Skipping push (already done)")
        else:
            # Check if branch push is needed
            _local_head = run("git", ["rev-parse", "HEAD"]).strip()
            _branch_needs_push = True
            try:
                _remote_head = run("git", ["rev-parse", f"origin/{branch}"]).strip()
                if _local_head == _remote_head:
                    _branch_needs_push = False
                    branch_pushed = True
                    log("Skipping branch push (remote already at local HEAD)")
            except Exception:
                pass  # Remote branch might not exist yet

            if _branch_needs_push:
                push_if_needed(branch, env=push_env, config=ctx.config)
                branch_pushed = True

            # Check if tag push is needed
            _tag_needs_push = True
            try:
                if tag_exists_on_remote(tag):
                    _tag_needs_push = False
                    log("Skipping tag push (remote tag already exists)")
            except Exception:
                pass

            if _tag_needs_push:
                import subprocess as _subprocess
                try:
                    run("git", ["push", "origin", tag] + state.companion_tags, timeout=push_timeout, env=push_env)
                except _subprocess.TimeoutExpired as _e:
                    from ...errors import GitError
                    raise GitError(
                        f"Tag push timed out after {push_timeout}s — remote "
                        f"state may be inconsistent. Check with: "
                        f"git ls-remote --tags origin {tag}"
                    ) from _e

            log(f"Pushed to origin/{branch}")
            save_step(_state_path, "PUSHED")
            _completed.add("PUSHED")
    except ReleaseAbortError as e:
        if "TAGGED" in _completed:
            # Post-TAGGED failure: canonical resumable state. Preserve
            # everything and record a failed PUSHED marker instead of
            # rolling back (which would destroy exactly what resume needs).
            _handle_resumable_push_failure(e)
            raise
        # Pre-TAGGED failure -- safe to roll back locally,
        # but only if no foreign commits or dirty files would be destroyed.
        _guard_rollback(pre_release_sha, _state_path)
        run("git", ["reset", "--hard", pre_release_sha])
        # State file is useless after local rollback -- clean it up.
        from ...release_file import get_releases_dir as _get_releases_dir
        _cleanup_release_artifacts(
            project_dir, new_version, changes_dir=state.changes_dir,
            releases_dir=_get_releases_dir(project_dir, releasable_dir=_releasable_cfg_dir),
        )
        clear_release_state(_state_path)
        print(str(e), file=sys.stderr)
        print(
            f"Local state has been rolled back to {pre_release_sha[:10]}.",
            file=sys.stderr,
        )
        _warn_rollback_residuals()
        raise
    except Exception as e:
        if "TAGGED" in _completed:
            # Post-TAGGED failure (push failed / timed out): canonical
            # resumable state. Preserve everything and record a failed
            # PUSHED marker instead of rolling back.
            _handle_resumable_push_failure(e)
            raise
        # Pre-TAGGED failure -- safe to roll back locally,
        # but only if no foreign commits or dirty files would be destroyed.
        _guard_rollback(pre_release_sha, _state_path)
        # Delete tag (may not exist yet) and reset commits so the working
        # tree looks like it did before the release attempt.
        try:
            run("git", ["tag", "-d", tag])
        except Exception:
            pass
        # Clean up companion tags (best-effort)
        for ctag in state.companion_tags:
            try:
                run("git", ["tag", "-d", ctag])
            except Exception:
                pass
        run("git", ["reset", "--hard", pre_release_sha])
        # State file is useless after local rollback -- clean it up.
        from ...release_file import get_releases_dir as _get_releases_dir
        _cleanup_release_artifacts(
            project_dir, new_version, changes_dir=state.changes_dir,
            releases_dir=_get_releases_dir(project_dir, releasable_dir=_releasable_cfg_dir),
        )
        clear_release_state(_state_path)
        if hasattr(e, 'stderr') and e.stderr:
            print(f"Command error: {e.stderr.strip()}", file=sys.stderr)
        print(
            f"Error: release failed. Local state has been rolled back to {pre_release_sha[:10]}.",
            file=sys.stderr,
        )
        print(
            "No push happened (the failure occurred before tagging), so nothing "
            "on the remote needs fixing. Address the error above and re-run:\n"
            "  rlsbl release run",
            file=sys.stderr,
        )
        _warn_rollback_residuals()
        raise

    # Capture the pushed commit SHA now, before any post-release hooks that
    # might create new commits and move HEAD past the release commit.
    pushed_sha = run("git", ["rev-parse", "HEAD"])

    # GITHUB_RELEASE guard: skip if the release already exists
    # Create GitHub Release using a temp notes file
    # Notes file cleanup is deferred until after subtree publishing (which reuses it)
    notes_file = f".rlsbl-notes-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    release_created = True
    _gh_release_already_exists = False
    if "GITHUB_RELEASE" in _completed:
        _gh_release_already_exists = True
        log("Skipping GitHub Release creation (already done)")
    else:
        try:
            run_gh(["release", "view", tag], config=ctx.config)
            _gh_release_already_exists = True
            save_step(_state_path, "GITHUB_RELEASE")
            _completed.add("GITHUB_RELEASE")
            log(f"Skipping GitHub Release creation (release {tag} already exists)")
        except Exception:
            pass  # Release doesn't exist yet -- proceed with creation

    try:
        if not _gh_release_already_exists:
            with open(writing_file, "w", encoding="utf-8") as f:
                f.write(changelog_entry or "")
            os.rename(writing_file, notes_file)
            # Retry gh release create with race-condition detection.
            # GitHub API can return an error even when the release was actually created,
            # so after each failure we check whether the release exists before retrying.
            gh_release_args = ["release", "create", tag, "--title", tag, "--notes-file", notes_file]
            # Mark pre-release versions as GitHub pre-releases
            if "-" in new_version:
                gh_release_args.append("--prerelease")
            gh_release_succeeded = False
            for attempt in range(2):
                try:
                    run_gh(gh_release_args, config=ctx.config)
                    gh_release_succeeded = True
                    log(f"Created GitHub Release: {tag}")
                    break
                except Exception as e:
                    if hasattr(e, 'stderr') and e.stderr:
                        print(f"Command error: {e.stderr.strip()}", file=sys.stderr)
                    # Check if the release was created despite the error (race condition)
                    try:
                        run_gh(["release", "view", tag], config=ctx.config)
                        gh_release_succeeded = True
                        log(f"GitHub Release created (confirmed via view): {tag}")
                        break
                    except Exception:
                        pass  # Release truly doesn't exist; retry or fail

            if gh_release_succeeded:
                save_step(_state_path, "GITHUB_RELEASE")
                _completed.add("GITHUB_RELEASE")
            else:
                release_created = False
                save_step_failure(
                    _state_path, "GITHUB_RELEASE",
                    f"GitHub Release creation failed for {tag}",
                )
                # Point at the resolved changes dir (releasable dir in
                # releasable mode, .rlsbl/changes/ otherwise), relative to
                # the CWD the release ran from so the hint is pasteable.
                _notes_base = changes_dir or os.path.join(project_dir, ".rlsbl", "changes")
                notes_path = os.path.relpath(os.path.join(_notes_base, f"{new_version}.md"))
                print(
                    f"Error: GitHub Release creation failed for {tag}. "
                    f"The tag and commit are on the remote.\n"
                    f"  To create the release: gh release create {tag} --title {tag} --notes-file {notes_path}\n"
                    f"  To roll back: rlsbl release undo",
                    file=sys.stderr,
                )

        # Subtree publishing for monorepo projects with subtree_remote configured
        if release_created and monorepo_name and monorepo_project_path:
            try:
                projects = load_workspace(monorepo_root)
                proj_dict = None
                for p in projects:
                    if p["name"] == monorepo_name:
                        proj_dict = p
                        break
                subtree_remote = proj_dict.get("subtree_remote") if proj_dict else None
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("could not load workspace for subtree publishing", e)
                subtree_remote = None

            if subtree_remote:
                validate_subtree_remote_ssh_host(subtree_remote, str(ctx.project_root))
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

    # ---- Post-release phase ----
    # Every step below is tracked in the state file. Success markers gate
    # resume-skip; failure markers feed the completion summary. Asset upload
    # and pipeline publish failures are FATAL (state preserved, resumable);
    # deploy / post-hooks / snapshot failures are non-fatal (recorded and
    # loudly reported, then the release completes and state is cleared).

    # Upload release assets for pipelines with assets/custom_assets config.
    # In releasable mode, iterate each publishing member (publish_mode != "none") and
    # upload assets from each member's directory with member-prefixed names.
    if release_created:
        if "ASSETS_UPLOADED" in _completed:
            log("Skipping asset upload (already done)")
        else:
            try:
                if releasable_name and member_package_paths and monorepo_root:
                    from ...member_context import resolve_member_context as _rmc_asset
                    from .publish import _upload_assets_for_config

                    for _a_pkg_path in member_package_paths:
                        _a_abs_pkg = os.path.join(str(monorepo_root), _a_pkg_path)
                        if not os.path.isdir(_a_abs_pkg):
                            continue
                        _a_member = _rmc_asset(
                            _a_abs_pkg, releasable_config_dir=_releasable_cfg_dir,
                        )
                        if _a_member.publish_mode == "none":
                            continue
                        # Use the member name (last path component) as prefix
                        _a_member_name = os.path.basename(_a_pkg_path.rstrip("/"))
                        _upload_assets_for_config(
                            tag, new_version, log, flags,
                            _a_member.config, _a_abs_pkg, ctx,
                            member_name=_a_member_name,
                        )
                else:
                    upload_release_assets(tag, new_version, log, flags, ctx=ctx)
            except (ReleaseValidationError, HookError) as e:
                from ...errors import PostReleaseError
                save_step_failure(_state_path, "ASSETS_UPLOADED", str(e))
                raise PostReleaseError(str(e)) from e
            save_step(_state_path, "ASSETS_UPLOADED")
            _completed.add("ASSETS_UPLOADED")

    # Publish step: skip when publish_mode is "none" (suppressed -- no
    # registry publishing). Publish failures are FATAL: for `local: true`
    # pipelines this IS the publish, so downgrading a failure to a warning
    # would silently ship a release that was never published.
    from ...config import suppresses_publish
    is_private = suppresses_publish(ctx.config)
    if "PIPELINES_PUBLISHED" in _completed:
        log("Skipping pipeline publish (already done)")
    else:
        if not is_private:
            if releasable_name and member_package_paths and monorepo_root:
                # Per-member publish: each publishing member with pipelines
                # publishes from its own directory at the shared version.
                # Resume support: state tracks published_members list so
                # completed members are skipped on retry.
                _existing_state_pub = load_release_state(_state_path) or {}
                _already_published = set(
                    _existing_state_pub.get("published_members", [])
                )
                _published_members = list(_already_published)

                from ...member_context import resolve_member_context as _resolve_mc

                for pkg_path in member_package_paths:
                    abs_pkg = os.path.join(str(monorepo_root), pkg_path)
                    if not os.path.isdir(abs_pkg):
                        continue

                    member = _resolve_mc(
                        abs_pkg, releasable_config_dir=_releasable_cfg_dir,
                    )

                    if member.publish_mode == "none":
                        log(f"  {pkg_path}: skipped (publish_mode none)")
                        continue

                    member_pipelines = load_pipelines(member.config)
                    if not member_pipelines:
                        log(f"  {pkg_path}: no pipelines, not published")
                        continue

                    if pkg_path in _already_published:
                        log(f"  {pkg_path}: skipped (already published)")
                        continue

                    for pl_name, pl in member_pipelines.items():
                        try:
                            pl.publish(abs_pkg, new_version, ctx=ctx)
                        except Exception as e:
                            from ...errors import PostReleaseError
                            # Save partial progress so resume can skip
                            # already-published members.
                            _pub_state = load_release_state(_state_path) or {}
                            _pub_state["published_members"] = _published_members
                            save_release_state(_state_path, _pub_state)
                            save_step_failure(
                                _state_path, "PIPELINES_PUBLISHED",
                                f"member '{pkg_path}' pipeline '{pl_name}': {e}",
                            )
                            raise PostReleaseError(
                                f"member '{pkg_path}' pipeline '{pl_name}' "
                                f"publish failed: {e}. "
                                f"Release state has been preserved; fix the "
                                f"issue and run `rlsbl release resume` to "
                                f"re-attempt the publish."
                            ) from e

                    _published_members.append(pkg_path)
                    log(f"  {pkg_path}: published ({', '.join(member_pipelines)})")

                # Persist final published_members list in state
                _pub_state_final = load_release_state(_state_path) or {}
                _pub_state_final["published_members"] = _published_members
                save_release_state(_state_path, _pub_state_final)
            else:
                # Standalone / implicit mode: single publish pass from
                # representative config.
                release_pipelines = load_pipelines(ctx.config)
                for pl_name, pl in release_pipelines.items():
                    try:
                        pl.publish(primary_path, new_version, ctx=ctx)
                    except Exception as e:
                        from ...errors import PostReleaseError
                        save_step_failure(
                            _state_path, "PIPELINES_PUBLISHED",
                            f"pipeline '{pl_name}': {e}",
                        )
                        raise PostReleaseError(
                            f"pipeline '{pl_name}' publish failed: {e}. "
                            f"Release state has been preserved; fix the issue and "
                            f"run `rlsbl release resume` to re-attempt the publish."
                        ) from e

        save_step(_state_path, "PIPELINES_PUBLISHED")
        _completed.add("PIPELINES_PUBLISHED")

    # Deploy phase (after publish, before post-release hook). Non-fatal:
    # a failure is recorded as a failure marker and named in the summary.
    if "DEPLOYED" in _completed:
        log("Skipping deploy (already done)")
    else:
        _deploy_failure = None
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
                    _deploy_failure = f"deploy to {result.target_name} failed: {result.message}"
                    break  # Stop at first failure
        elif deploy_errors:
            print("Warning: deploy config has errors, skipping deploy:", file=sys.stderr)
            for err in deploy_errors:
                print(f"  {err}", file=sys.stderr)
            _deploy_failure = "deploy config errors: " + "; ".join(deploy_errors)
        # If no deploy targets configured, the step is trivially done.
        if _deploy_failure is not None:
            save_step_failure(_state_path, "DEPLOYED", _deploy_failure)
        else:
            save_step(_state_path, "DEPLOYED")
            _completed.add("DEPLOYED")

    # Ecosystem tagging: add GitHub topic after release is created
    if should_tag(flags, ctx.config):
        ensure_github_topic(quiet=quiet)

    # Run post-release hook if present (non-fatal: release is already complete)
    _use_releasable_hooks = releasable_name and monorepo_root and member_package_paths
    hook_timeout = get_hook_timeout()
    _post_hook_error = None

    if "POST_HOOKS_RUN" in _completed:
        log("Skipping post-release hooks (already done)")
    elif _use_releasable_hooks:
        # Multi-level post-release: releasable first, then per-package
        from .hooks import build_hook_env, run_releasable_hooks
        from ...workspace import members_of, get_releasable_dir
        from . import read_json_config

        _ws_projects = load_workspace(str(monorepo_root))
        _member_projs = members_of(releasable_name, _ws_projects)
        _member_tuples = []
        for mp in _member_projs:
            mp_name = mp.name if hasattr(mp, "name") else mp["name"]
            mp_path = mp.path if hasattr(mp, "path") else mp["path"]
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

        hook_env = build_hook_env(
            os.environ.copy(),
            new_version,
            bump_type=bump_type or "",
            prev_version=current_version or "",
            description=description or "",
        )

        try:
            run_releasable_hooks(
                "post-release", monorepo_root, releasable_name,
                _member_tuples, hook_env, hook_timeout, log,
                project_dir=project_dir,
                releasable_config=_releasable_config,
                package_configs=_package_configs,
            )
        except Exception as e:
            # Post-release hooks are non-fatal
            print(f"Warning: post-release hook failed: {e}", file=sys.stderr)
            _post_hook_error = str(e)
    else:
        from .hooks import build_hook_env, run_release_hook

        post_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "post-release.sh")
        hook_env = build_hook_env(
            os.environ.copy(),
            new_version,
            bump_type=bump_type or "",
            prev_version=current_version or "",
            description=description or "",
        )
        log("Running post-release hook...")
        try:
            run_release_hook(
                "post-release", post_release_script, project_dir,
                hook_env, hook_timeout, config=ctx.config,
            )
        except Exception as e:
            # Post-release hooks are non-fatal
            print(f"Warning: post-release hook failed: {e}", file=sys.stderr)
            _post_hook_error = str(e)

    if "POST_HOOKS_RUN" not in _completed:
        if _post_hook_error is not None:
            save_step_failure(_state_path, "POST_HOOKS_RUN", _post_hook_error)
        else:
            save_step(_state_path, "POST_HOOKS_RUN")
            _completed.add("POST_HOOKS_RUN")

    # Auto-regenerate monorepo snapshot after release (non-fatal)
    if "SNAPSHOT_REGENERATED" in _completed:
        log("Skipping snapshot regeneration (already done)")
    elif monorepo_name:
        try:
            from ...snapshot import generate_snapshot, write_snapshot
            from ...workspace_graph import WorkspaceGraph

            projects = load_workspace(monorepo_root)
            graph = WorkspaceGraph(monorepo_root, projects)
            snapshot = generate_snapshot(monorepo_root, projects, graph)
            rel_path = write_snapshot(monorepo_root, snapshot)
            did_commit = commit_files_if_changed("snapshot", [rel_path], skip_message="Snapshot unchanged.", autogenerated=True, cwd=monorepo_root)
            if did_commit:
                _track_release_commit(_state_path)
            log(f"Regenerated monorepo snapshot: {rel_path}")
        except Exception as e:
            print(f"Warning: snapshot regeneration failed: {e}", file=sys.stderr)
            save_step_failure(_state_path, "SNAPSHOT_REGENERATED", str(e))
        else:
            save_step(_state_path, "SNAPSHOT_REGENERATED")
            _completed.add("SNAPSHOT_REGENERATED")
    else:
        # Not a monorepo: nothing to regenerate, the step is trivially done.
        save_step(_state_path, "SNAPSHOT_REGENERATED")
        _completed.add("SNAPSHOT_REGENERATED")

    # Advisory: constraint propagation
    if monorepo_name:
        _print_stale_dep_advisory(monorepo_name, new_version, monorepo_root=monorepo_root)

    # If GitHub Release creation failed, preserve the state file for resume
    # and raise PostReleaseError BEFORE clearing state.
    if not release_created:
        from ...errors import PostReleaseError
        raise PostReleaseError(f"GitHub Release creation failed for {tag}")

    # Completion summary: loudly name any non-fatal step failures.
    _final_state = load_release_state(_state_path) or {}
    _failed_final = get_failed_steps(_final_state)
    if _failed_final:
        print(
            f"\nRelease {new_version} completed with non-fatal step failures:",
            file=sys.stderr,
        )
        for _step, _msg in _failed_final.items():
            print(f"  {_step}: {_msg}", file=sys.stderr)

    # Provable completeness: every canonical step must carry a success or
    # failure marker before the state file is cleared. A missing marker
    # here is an internal bug (a step ran without recording itself).
    _missing_final = get_missing_steps(_final_state)
    if _missing_final:
        raise RuntimeError(
            "internal error: release reached the success epilogue with "
            f"unmarked steps: {', '.join(_missing_final)}"
        )

    # Success epilogue: clear state and announce BEFORE watch, because
    # watch_run_cmd() calls sys.exit() and would skip cleanup.
    clear_release_state(_state_path)

    log(f"\nRelease {new_version} complete!")

    # Watch CI or print hint (uses SHA captured before post-release hooks).
    # In batch-mode, the batch orchestrator handles watch after all packages
    # are released, so skip both the watch call and the hint here.
    # Dry-run returns earlier (no push happens), but guard defensively.
    if not flags.get("dry-run", False) and not flags.get("batch-mode", False):
        if flags.get("watch"):
            log(f"Watching CI for {pushed_sha}...")
            from ..watch import run_cmd as watch_run_cmd
            watch_run_cmd(None, [pushed_sha], {})
        elif flags.get("watch-async"):
            from ..watch import spawn_detached_watcher
            spawn_detached_watcher(pushed_sha)
        else:
            log(f"Watch CI: rlsbl watch {pushed_sha}")
