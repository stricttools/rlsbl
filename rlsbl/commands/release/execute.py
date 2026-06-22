"""Release execution: version bump, commit, tag, push, GitHub Release, and post-release steps."""

import dataclasses
import json
import os
import sys
import time


class ReleaseAbortError(Exception):
    """Raised when the release must abort (e.g., unexpected dirty files)."""


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


def resolve_target_paths(project_dir="."):
    """Build a dict mapping target names to their resolved paths.

    Uses detect_targets() which reads .rlsbl/config.json "targets" (supporting
    both plain strings and dicts with "name"/"path") and falls back to
    auto-detection.

    Returns dict[str, str] mapping target name -> resolved directory path.
    """
    from . import detect_targets

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
        baseline = resolve_target_paths(project_dir)

    # Never include the primary target in the secondary set
    baseline.pop(primary, None)

    return baseline


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
    from . import require_tool, subprocess

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


# Lockfile specs: (lockfile, tool_name, sync_cmd, guard_file)
# guard_file: if set, the spec only applies when this file exists in the same directory.
# This distinguishes e.g. go.sum (per-module) from go.work.sum (workspace root only).
_LOCKFILE_SPECS = [
    ("uv.lock", "uv", ["uv", "lock"], None),
    ("package-lock.json", "npm", ["npm", "install", "--package-lock-only"], None),
    ("go.sum", "go", ["go", "mod", "tidy"], None),
    ("go.work.sum", "go", ["go", "work", "sync"], "go.work"),
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
                except Exception as e:
                    from ...utils import warn_exception
                    warn_exception("git check-ignore failed for lockfile", e)
                if norm_path not in files_to_commit:
                    files_to_commit.append(norm_path)
                    log(f"Lockfile updated: {lockfile}")


def archive_blog_body(project_dir, version):
    """Archive unreleased.md to v{version}.md during release finalization.

    Returns the archived path if the file existed, None otherwise.
    """
    releases_dir = os.path.join(project_dir, ".rlsbl", "releases")
    blog_body_src = os.path.join(releases_dir, "unreleased.md")
    blog_body_dst = os.path.join(releases_dir, f"v{version}.md")
    if os.path.exists(blog_body_src):
        os.rename(blog_body_src, blog_body_dst)
        os.chmod(blog_body_dst, 0o444)
        return blog_body_dst
    return None


def _sync_member_package_versions(
    member_package_paths, monorepo_root, new_version,
    files_to_commit, git_root, log, ctx,
    exclude_path=None,
    releasable_config_dir=None,
):
    """Sync version to published member packages in explicit releasable mode.

    For each member package that has publishing pipelines (non-private,
    has a detected target), writes the version to the manifest.
    Private-only packages are left untouched.

    Uses ``read_project_config`` with releasable inheritance so that
    releasable-level ``private: false`` is respected by member packages
    that don't set ``private`` themselves.

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
    from . import TARGETS, detect_targets
    from ...config import read_project_config

    for pkg_path in member_package_paths:
        if exclude_path and pkg_path == exclude_path:
            continue

        abs_pkg = os.path.join(str(monorepo_root), pkg_path)
        if not os.path.isdir(abs_pkg):
            continue

        # Load config through the inheritance-aware path
        try:
            pkg_config = read_project_config(abs_pkg, releasable_config_dir=releasable_config_dir)
        except Exception:
            continue

        # Skip private packages (default True when unset)
        if pkg_config.get("private", True):
            continue

        # Detect targets and write version
        entries = detect_targets(abs_pkg)
        if not entries:
            continue

        for entry in entries:
            tgt = TARGETS.get(entry.name)
            if not tgt:
                continue
            try:
                modified = tgt.write_version(entry.path, new_version, ctx=ctx)
                for rel in modified:
                    fpath = _rel_to_git_root(os.path.join(entry.path, rel), git_root)
                    if fpath not in files_to_commit:
                        files_to_commit.append(fpath)
                if modified:
                    log(f"Synced version to member {pkg_path}: {', '.join(modified)}")
            except Exception as e:
                log(f"Warning: failed to sync version to {pkg_path}/{entry.name}: {e}")


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
        push_if_needed,
        commit_files,
        commit_files_if_changed,
        has_staged_or_modified,
        get_push_timeout,
        get_hook_timeout,
        get_current_branch,
        should_tag,
        subprocess,
        TARGETS,
        load_pipelines,
        load_workspace,
        read_deploy_config,
        deploy_target,
        ensure_github_topic,
        ensure_npm_keyword,
        ensure_pypi_keyword,
        changes_dir_exists,
        finalize_version,
        generate_version_file,
        get_changes_dir,
        validate_subtree_remote_ssh_host,
        _cleanup_release_artifacts,
        upload_release_assets,
        _print_stale_dep_advisory,
        parse_porcelain_paths,
        ReleaseValidationError,
        HookError,
        _read_release_metadata,
    )

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
            raise KeyboardInterrupt
        if answer != "y":
            print("Aborted.")
            raise SystemExit(0)

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
            # In explicit releasable mode, write the releasable version file first
            if releasable_name and monorepo_root:
                from ...workspace import write_releasable_version, get_releasable_version_path
                write_releasable_version(str(monorepo_root), releasable_name, new_version)
                ver_path = get_releasable_version_path(str(monorepo_root), releasable_name)
                ver_rel = _rel_to_git_root(ver_path, _git_root)
                files_to_commit.append(ver_rel)
                log(f"Updated releasable version: {releasable_name} -> {new_version}")

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
                from ...workspace import get_releasable_dir
                _rel_cfg_dir = get_releasable_dir(str(monorepo_root), releasable_name)
                _sync_member_package_versions(
                    member_package_paths, monorepo_root, new_version,
                    files_to_commit, _git_root, log, ctx,
                    # Skip the current project's path -- already handled above
                    exclude_path=monorepo_project_path,
                    releasable_config_dir=_rel_cfg_dir,
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

        # Ecosystem tagging: add keyword to manifests if enabled
        if should_tag(flags, ctx.config):
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

        # Update .rlsbl/version marker so it's included in the release commit
        rlsbl_version_marker = vpath(os.path.join(".rlsbl", "version"))
        if os.path.exists(os.path.dirname(rlsbl_version_marker)):
            try:
                from ... import __version__ as rlsbl_ver
                with open(rlsbl_version_marker, "w") as f:
                    f.write(rlsbl_ver + "\n")
                if rlsbl_version_marker not in files_to_commit:
                    files_to_commit.append(rlsbl_version_marker)
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("writing .rlsbl/version marker failed", e)

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
        #
        # In explicit releasable mode, the changes dir lives at the releasable
        # level, and the tag glob uses the releasable's tag format. The
        # resolved changes_dir is passed via state.changes_dir.
        changes_dir = state.changes_dir
        if changes_dir is None and changes_dir_exists(project_dir):
            changes_dir = get_changes_dir(project_dir)
        if changes_dir and os.path.isdir(changes_dir):
            if releasable_name and releasable_tag_format_str:
                from .validate import _releasable_tag_glob
                tag_glob = _releasable_tag_glob(releasable_tag_format_str, releasable_name)
            elif monorepo_name:
                tag_glob = target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path)
            else:
                tag_glob = None
            finalize_version(changes_dir, new_version, tag_glob=tag_glob)
            # Pass release metadata so the new version's .md matches what a
            # future backfill from the archived v{version}.toml would produce
            # (the archived toml is stripped on read, so strip here too).
            generate_version_file(
                changes_dir, new_version,
                description=(description or "").strip(),
                context=(context or "").strip(),
            )
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
            log(f"Committed finalized changelog files")
        else:
            log("No .rlsbl/changes/ directory; skipping changelog finalization")

        # Clean stale batch_limits exclusions that referenced unreleased.jsonl
        from ...config import clean_stale_exclusions
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
                log(f"Cleaned {removed} stale batch exclusion(s) from config.json")

        # Finalize release file: rename unreleased.toml to vX.Y.Z.toml
        # Only if the release file exists (backward compat with legacy path)
        from ...release_file import get_release_file_path
        release_file_path = get_release_file_path(project_dir)
        if os.path.exists(release_file_path):
            releases_dir = os.path.dirname(release_file_path)
            versioned_release = os.path.join(releases_dir, f"v{new_version}.toml")
            os.rename(release_file_path, versioned_release)
            os.chmod(versioned_release, 0o444)
            # Create a fresh empty unreleased.toml
            with open(release_file_path, "w", encoding="utf-8") as f:
                pass  # empty file
            # Archive blog body file if it exists (unreleased.md -> v{version}.md)
            blog_body_dst = archive_blog_body(project_dir, new_version)
            release_finalize_files = [
                _rel_to_git_root(versioned_release, _git_root),
                _rel_to_git_root(release_file_path, _git_root),
            ]
            if blog_body_dst:
                release_finalize_files.append(_rel_to_git_root(blog_body_dst, _git_root))
            commit_files(f"chore: finalize release file for {new_version}", release_finalize_files, cwd=_git_root)
            log(f"Finalized release file for {new_version}")

            # Now that v{version}.toml is archived, regenerate the per-version
            # .md so its content is derived from _read_release_metadata() rather
            # than the direct params passed earlier. This keeps the .md
            # consistent with what future generate_changelog() calls produce.
            changes_dir_regen = state.changes_dir or (get_changes_dir(project_dir) if changes_dir_exists(project_dir) else None)
            if changes_dir_regen and os.path.isdir(changes_dir_regen):
                ver_desc, ver_ctx = _read_release_metadata(project_dir, new_version)
                generate_version_file(
                    changes_dir_regen, new_version,
                    description=ver_desc, context=ver_ctx,
                )
                md_regen_path = os.path.join(changes_dir_regen, f"{new_version}.md")
                md_regen_rel = _rel_to_git_root(md_regen_path, _git_root)
                if has_staged_or_modified([md_regen_rel], cwd=_git_root):
                    commit_files(
                        f"chore: regenerate {new_version}.md from archived release metadata",
                        [md_regen_rel],
                        cwd=_git_root,
                    )

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
        _cleanup_release_artifacts(project_dir, new_version)
        print(str(e), file=sys.stderr)
        print(
            f"Local state has been rolled back to {pre_release_sha[:10]}.",
            file=sys.stderr,
        )
        raise
    except Exception:
        # Roll back local mutations: delete tag (may not exist yet) and
        # reset commits so the working tree looks like it did before the
        # release attempt.
        try:
            run("git", ["tag", "-d", tag])
        except Exception:
            pass
        run("git", ["reset", "--hard", pre_release_sha])
        _cleanup_release_artifacts(project_dir, new_version)
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
    release_created = True
    try:
        with open(writing_file, "w", encoding="utf-8") as f:
            f.write(changelog_entry or "")
        os.rename(writing_file, notes_file)
        # Retry gh release create with race-condition detection.
        # GitHub API can return an error even when the release was actually created,
        # so after each failure we check whether the release exists before retrying.
        gh_release_succeeded = False
        for attempt in range(2):
            try:
                run("gh", ["release", "create", tag, "--title", tag, "--notes-file", notes_file])
                gh_release_succeeded = True
                log(f"Created GitHub Release: {tag}")
                break
            except Exception:
                # Check if the release was created despite the error (race condition)
                try:
                    run("gh", ["release", "view", tag])
                    gh_release_succeeded = True
                    log(f"GitHub Release created (confirmed via view): {tag}")
                    break
                except Exception:
                    pass  # Release truly doesn't exist; retry or fail

        if not gh_release_succeeded:
            release_created = False
            notes_path = f".rlsbl/changes/{new_version}.md"
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

    # Upload release assets for pipelines with assets/custom_assets config
    if release_created:
        try:
            upload_release_assets(tag, new_version, log, flags, ctx=ctx)
        except (ReleaseValidationError, HookError) as e:
            from ...errors import PostReleaseError
            raise PostReleaseError(str(e)) from e

    # Load pipelines for the publish step (validation already ran in run_cmd)
    release_pipelines = load_pipelines(ctx.config)

    # Publish step: skip for private repos (they don't publish to registries)
    is_private = ctx.config["private"]
    if not is_private:
        # Pipeline dispatch: run publish for each pipeline (runs once per release, not per-target)
        for pl_name, pl in release_pipelines.items():
            try:
                pl.publish(primary_path, new_version, ctx=ctx)
            except Exception as e:
                print(f"Warning: pipeline '{pl_name}' publish failed: {e}", file=sys.stderr)

        # Multi-target: run build for secondary targets (build stays on targets, not pipelines)
        if secondary_targets:
            from ...targets import TARGETS as ALL_TARGETS
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
    _use_releasable_hooks = releasable_name and monorepo_root and member_package_paths
    hook_timeout = get_hook_timeout()

    if _use_releasable_hooks:
        # Multi-level post-release: releasable first, then per-package
        from .hooks import build_hook_env, run_releasable_hooks
        from ...workspace import members_of

        _ws_projects = load_workspace(str(monorepo_root))
        _member_projs = members_of(releasable_name, _ws_projects)
        _member_tuples = []
        for mp in _member_projs:
            mp_name = mp.name if hasattr(mp, "name") else mp["name"]
            mp_path = mp.path if hasattr(mp, "path") else mp["path"]
            mp_dir = os.path.join(str(monorepo_root), mp_path)
            _member_tuples.append((mp_name, mp_dir))

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
            )
        except Exception as e:
            # Post-release hooks are non-fatal
            print(f"Warning: post-release hook failed: {e}", file=sys.stderr)
    else:
        post_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "post-release.sh")
        if os.path.exists(post_release_script):
            post_release_script = os.path.abspath(post_release_script)
            log("Running post-release hook...")
            try:
                env = os.environ.copy()
                env["RLSBL_VERSION"] = new_version
                env["RLSBL_BUMP_TYPE"] = bump_type or ""
                env["RLSBL_PREV_VERSION"] = current_version or ""
                env["RLSBL_DESCRIPTION"] = description or ""
                subprocess.run(["bash", post_release_script], env=env, check=True, timeout=hook_timeout, cwd=project_dir)
            except subprocess.CalledProcessError as e:
                print(f"Warning: post-release hook exited with code {e.returncode}.", file=sys.stderr)
            except subprocess.TimeoutExpired:
                print(f"Warning: post-release hook timed out after {hook_timeout}s.", file=sys.stderr)

    # Auto-regenerate monorepo snapshot after release (non-fatal)
    if monorepo_name:
        try:
            from ...snapshot import generate_snapshot, write_snapshot
            from ...workspace_graph import WorkspaceGraph

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
    # In batch-mode, the batch orchestrator handles watch after all packages
    # are released, so skip both the watch call and the hint here.
    # Dry-run returns earlier (no push happens), but guard defensively.
    if not flags.get("dry-run", False) and not flags.get("batch-mode", False):
        if flags.get("watch"):
            log(f"Watching CI for {pushed_sha}...")
            from ..watch import run_cmd as watch_run_cmd
            watch_run_cmd(None, [pushed_sha], {})
        else:
            log(f"Watch CI: rlsbl watch {pushed_sha}")

    log(f"\nRelease {new_version} complete!")

    if not release_created:
        from ...errors import PostReleaseError
        raise PostReleaseError(f"GitHub Release creation failed for {tag}")
