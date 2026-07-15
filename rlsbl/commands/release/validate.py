"""Validation helpers for release: test runner, lint, selfdoc gen/check, scaffold conflict detection, strictcli schema dump, and blog post body validation.

Also contains extracted validation steps from run_cmd: target validation, OTA mode,
config integrity, pipeline config, gh CLI, clean tree, branch/remote, monorepo context,
version/tag computation, and changelog state validation.
"""

import json
import os
import sys
import tempfile

from ...strictcli_detect import detect_strictcli
from ...utils import run_gh


class ReleaseValidationError(Exception):
    """Raised when a pre-release validation check fails."""
    pass


class HookError(Exception):
    """Raised when a built-in hook (tests, lint, selfdoc) fails."""
    pass


from ...release_file import VALID_BUMP_TYPES


def validate_release_targets(release_config, project_root, *,
                             member_dirs=None, releasable_config_dir=None):
    """Validate include/exclude targets in the release config.

    Checks:
    - include list is non-empty
    - all named targets are known
    - include + exclude exhaustively covers detected targets

    When ``member_dirs`` is provided (releasable mode), detected targets
    are the union of targets across all member directories instead of
    the single project root.  If ``releasable_config_dir`` is also set,
    its ``config.json`` ``targets`` key takes precedence (the releasable
    is the source of truth in explicit mode).

    Returns the primary registry name (first item in include).
    Raises ReleaseValidationError on failure.
    """
    from . import TARGETS, detect_targets
    from ...targets import read_releasable_targets

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

    if member_dirs is not None:
        # Releasable mode: union targets across all member directories.
        # Check releasable-level config first (authoritative in explicit mode).
        detected = set()
        if releasable_config_dir is not None:
            rel_config_path = os.path.join(str(releasable_config_dir), "config.json")
            rel_targets = read_releasable_targets(rel_config_path)
            if rel_targets is not None:
                detected = set(rel_targets)
        if not detected:
            for d in member_dirs:
                entries = detect_targets(str(d), releasable_config_dir=releasable_config_dir)
                for e in entries:
                    detected.add(e.name)
    else:
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


def validate_gh_push_access(config=None):
    """Validate that the authenticated gh user has push access to the repo.

    Uses the GitHub API to check push permissions. If push access is denied,
    raises ReleaseValidationError with a diagnostic message that includes the
    authenticated user, repo slug, and a suggestion to unset GH_TOKEN/GITHUB_TOKEN
    if either is set in the environment.

    Gracefully skips (warning only) on network/API errors.
    Silently returns if the repo cannot be determined.
    """
    from ...utils import get_github_repo, run_gh

    repo = get_github_repo(config)
    if repo is None:
        return

    try:
        result = run_gh(
            ["api", f"repos/{repo}", "--jq", ".permissions.push"],
            config=config,
        )
    except Exception:
        print(
            "Warning: could not check push access (GitHub API unreachable). "
            "Skipping push access check.",
            file=sys.stderr,
        )
        return

    if result.strip() == "true":
        return

    # Push access denied -- gather diagnostics
    try:
        user = run_gh(["api", "user", "--jq", ".login"], config=config)
    except Exception:
        user = "(unknown)"

    token_var = None
    if "GH_TOKEN" in os.environ:
        token_var = "GH_TOKEN"
    elif "GITHUB_TOKEN" in os.environ:
        token_var = "GITHUB_TOKEN"

    msg = f'authenticated user "{user.strip()}" does not have push access to {repo}.'
    if token_var:
        msg += (
            f"\nA {token_var} environment variable is set -- it may override "
            f"your gh CLI credentials.\n"
            f"Try: unset {token_var}"
        )
    raise ReleaseValidationError(msg)


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


class BranchValidation:
    """Result of :func:`validate_branch_and_remote`.

    Attributes:
        branch: the branch the release will happen on (always a release
            branch -- main/master or configured ``release_branches``).
        dev_branch: the dev branch we started from, or ``None`` when the
            release is already on a release branch.
        needs_ff_merge: ``True`` when the release was initiated from a dev
            branch and the release branch must be fast-forward merged to
            the dev branch HEAD before releasing.
    """

    __slots__ = ("branch", "dev_branch", "needs_ff_merge")

    def __init__(self, branch, *, dev_branch=None, needs_ff_merge=False):
        self.branch = branch
        self.dev_branch = dev_branch
        self.needs_ff_merge = needs_ff_merge

    # Make it behave like a string for backward compatibility with code
    # that treats the return value as a plain branch name.
    def __str__(self):
        return self.branch

    def __eq__(self, other):
        if isinstance(other, str):
            return self.branch == other
        if isinstance(other, BranchValidation):
            return (
                self.branch == other.branch
                and self.dev_branch == other.dev_branch
                and self.needs_ff_merge == other.needs_ff_merge
            )
        return NotImplemented

    def __hash__(self):
        return hash(self.branch)


def validate_branch_and_remote(flags, *, config=None):
    """Validate branch state and determine the release branch.

    When invoked from a **release branch** (listed in ``release_branches``
    config, default ``["main", "master"]``): validates local is not behind
    origin, returns a :class:`BranchValidation` with the branch name and
    ``needs_ff_merge=False``.

    When invoked from a **dev branch** (any branch NOT in the release
    branches list): fetches origin, verifies the first release branch is
    an ancestor of HEAD (i.e. the dev branch can be fast-forward merged
    to the release branch), and returns a :class:`BranchValidation` with
    ``branch`` set to the release branch, ``dev_branch`` set to the
    current branch, and ``needs_ff_merge=True``.  The actual ff-merge
    is deferred to the caller.

    Returns :class:`BranchValidation`.
    Raises :class:`ReleaseValidationError` on failure.
    """
    from . import run, get_current_branch, remote_branch_exists
    from ...prepush_utils import _get_release_branches

    branch = get_current_branch()

    # Determine which branches are release-only
    if config is not None:
        from ...context import ProjectContext
        # Build a minimal context to pass to _get_release_branches
        _ctx = ProjectContext(project_root=None, workspace_root=None, config=config)
        release_branches = _get_release_branches(_ctx)
    else:
        from ...prepush_utils import DEFAULT_RELEASE_BRANCHES
        release_branches = list(DEFAULT_RELEASE_BRANCHES)

    on_release_branch = branch in release_branches

    try:
        run("git", ["fetch", "origin", "--quiet"])
    except Exception:
        print("Warning: could not fetch from origin. Skipping remote-ahead check.", file=sys.stderr)
        if on_release_branch:
            return BranchValidation(branch)
        # Cannot verify ff-ability without fetching
        raise ReleaseValidationError(
            f"could not fetch from origin. Releasing from dev branch "
            f'"{branch}" requires fetching origin to verify '
            f"fast-forward ability."
        )

    if on_release_branch:
        # Normal release from a release branch
        if not remote_branch_exists(branch):
            print(
                f"Remote branch origin/{branch} does not exist yet. Skipping remote-ahead check.",
                file=sys.stderr,
            )
            return BranchValidation(branch)

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

        return BranchValidation(branch)

    # --- Release from dev branch ---
    # Target the first release branch (typically "main").
    target_branch = release_branches[0]

    # Verify the target release branch exists locally
    try:
        run("git", ["rev-parse", "--verify", f"refs/heads/{target_branch}"])
    except Exception:
        raise ReleaseValidationError(
            f'release branch "{target_branch}" does not exist locally. '
            f"Create it or configure release_branches in .rlsbl/config.json."
        )

    # Fetch and update the local tracking branch
    if remote_branch_exists(target_branch):
        # Update the local release branch to match origin (fast-forward only)
        try:
            run("git", ["fetch", "origin", f"{target_branch}:{target_branch}"])
        except Exception:
            # Non-fatal: the local branch might already be up to date,
            # or it might be checked out (can't update checked-out branch
            # with fetch). We'll verify ancestry below regardless.
            pass

    # Verify dev branch HEAD is a descendant of the release branch
    # (i.e. the release branch can be fast-forwarded to dev HEAD).
    import subprocess as _subprocess
    result = _subprocess.run(
        ["git", "merge-base", "--is-ancestor", target_branch, "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseValidationError(
            f'cannot fast-forward {target_branch} to {branch}: '
            f'{target_branch} has diverged. '
            f'Rebase {branch} onto {target_branch} first, or merge '
            f'{target_branch} into {branch}.'
        )

    print(
        f'Releasing from dev branch "{branch}": will fast-forward '
        f'"{target_branch}" to HEAD before releasing.',
        file=sys.stderr,
    )

    return BranchValidation(
        target_branch, dev_branch=branch, needs_ff_merge=True,
    )


def resolve_monorepo_context(monorepo_root, project_root, log):
    """Resolve monorepo project context if inside a monorepo.

    Returns (monorepo_name, monorepo_project_path, is_library, is_non_releasable, releasable_name).
    All values are None/False/None when not in a monorepo.
    ``releasable_name`` is a string when the project explicitly belongs to a
    named releasable (``releasable = "name"``), or None in implicit mode.
    Raises ReleaseValidationError if inside a monorepo but not a recognized project,
    or if the project is non-releasable.
    """
    from . import resolve_project
    from ...workspace import is_explicit_mode

    if not monorepo_root:
        return None, None, False, False, None

    project = resolve_project(monorepo_root, str(project_root))
    if project is None:
        raise ReleaseValidationError(
            "current directory is inside a monorepo but not inside any project.\n"
            "Run 'rlsbl monorepo status' to see registered projects."
        )
    monorepo_name = project["name"]
    monorepo_project_path = project["path"]
    is_library = bool(project.get("library"))
    is_non_releasable = not project.is_releasable
    log(f"Monorepo project: {monorepo_name} ({monorepo_project_path})")
    if is_non_releasable:
        raise ReleaseValidationError(
            "non-releasable projects cannot be released. Set "
            "releasable = \"<name>\" in workspace.toml if this project "
            "should be releasable, or remove dev_node / set "
            "releasable = false to confirm it is non-releasable."
        )

    # In explicit mode, the project's releasable field names the releasable
    # whose version file is the canonical version source.
    releasable_name = None
    if is_explicit_mode(str(monorepo_root)):
        rel_val = project.releasable
        if isinstance(rel_val, str):
            releasable_name = rel_val

    return monorepo_name, monorepo_project_path, is_library, is_non_releasable, releasable_name


def _format_releasable_tag(releasable_tag_format, releasable_name, version):
    """Format a tag using the releasable's tag_format template.

    Supports ``{name}`` and ``{version}`` placeholders.  E.g.::

        "{name}@v{version}" -> "www@v2.0.0"
        "v{version}"        -> "v2.0.0"
    """
    return releasable_tag_format.format(name=releasable_name, version=version)


def _releasable_tag_glob(releasable_tag_format, releasable_name):
    """Derive a glob pattern from a releasable's tag_format.

    Thin wrapper over the shared :func:`rlsbl.tag_glob.releasable_tag_glob`
    kept for the many existing callers in the release/status commands.
    """
    from ...tag_glob import releasable_tag_glob
    return releasable_tag_glob(releasable_tag_format, releasable_name)


def compute_release_version(target, primary_path, bump_arg, monorepo_name,
                            monorepo_project_path, log, *,
                            workspace_root=None, releasable_name=None,
                            releasable_tag_fmt=None, preid="",
                            project_dir=None):
    """Compute current and new version, bump type, and tag.

    In explicit releasable mode (when ``workspace_root`` and ``releasable_name``
    are both provided), the version is read from the releasable's version file
    at ``.rlsbl-monorepo/releasables/<name>/version`` instead of from the
    target's manifest file. This is the canonical version source for
    multi-package releasables.

    When ``releasable_tag_fmt`` is provided (explicit mode), tags are
    constructed from the releasable's tag format instead of the target's
    monorepo tag format.

    In implicit mode (the default, when either parameter is None), the version
    is read from the target's manifest as before.

    ``project_dir`` locates the project's ``.rlsbl/changes/`` directory for the
    destroyed-tag guard (see :func:`_abort_on_destroyed_tag`). When omitted it
    falls back to ``primary_path``, which coincides with the project root for
    standalone repos and implicit-mode monorepo projects.

    Returns (current_version, new_version, bump_type, tag).
    Raises ReleaseValidationError on invalid bump type or duplicate tag.
    """
    from . import bump_version, tag_exists_locally

    if workspace_root is not None and releasable_name is not None:
        from ...workspace import read_releasable_version
        current_version = read_releasable_version(str(workspace_root), releasable_name)
    else:
        current_version = target.read_version(primary_path)
    log(f"Current version: {current_version}")

    # Build tag using releasable tag format (explicit mode) or target format
    def _make_tag(version):
        if releasable_tag_fmt is not None and releasable_name is not None:
            return _format_releasable_tag(releasable_tag_fmt, releasable_name, version)
        elif monorepo_name:
            return target.monorepo_tag_format(
                monorepo_name, version, path=monorepo_project_path
            )
        else:
            return target.tag_format(version)

    current_tag = _make_tag(current_version)
    current_tag_exists = tag_exists_locally(current_tag)

    if not current_tag_exists:
        # A missing current tag normally means "first release". But a tag that
        # was destroyed (e.g. by an interrupted or undone release) is
        # indistinguishable from "never tagged" by this check alone. A
        # finalized changelog for this exact version proves it was released
        # before -- abort PRE-MUTATION rather than re-running the whole
        # pipeline only to crash at the finalize step.
        _guard_project_dir = project_dir if project_dir is not None else primary_path
        _abort_on_destroyed_tag(
            _guard_project_dir, current_version, current_tag,
            releasable_name=releasable_name, workspace_root=workspace_root,
        )
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
        new_version = bump_version(current_version, bump_type, preid=preid)
        tag = _make_tag(new_version)
        log(f"New version: {new_version} ({bump_type})")

    # Check tag doesn't already exist
    if tag_exists_locally(tag):
        raise ReleaseValidationError(f'tag "{tag}" already exists.')

    return current_version, new_version, bump_type, tag


def resolve_changes_dir(project_dir, releasable_name=None, workspace_root=None):
    """Resolve the JSONL changelog changes directory path.

    In explicit releasable mode (when both ``releasable_name`` and
    ``workspace_root`` are provided), returns the releasable-level changes
    directory.  Otherwise returns the per-project ``.rlsbl/changes/``
    directory.

    Returns the changes_dir path.
    Raises ReleaseValidationError if the directory does not exist.
    """
    from . import changes_dir_exists, get_changes_dir

    if releasable_name and workspace_root:
        from ...workspace import get_releasable_changes_dir
        changes_dir = get_releasable_changes_dir(str(workspace_root), releasable_name)
        if not os.path.isdir(changes_dir):
            raise ReleaseValidationError(
                f"JSONL changelog not set up for releasable '{releasable_name}'. "
                f"Expected: {changes_dir}"
            )
    else:
        if not changes_dir_exists(project_dir):
            raise ReleaseValidationError(
                "JSONL changelog not set up. Run 'rlsbl scaffold' to create .rlsbl/changes/"
            )
        changes_dir = get_changes_dir(project_dir)

    return changes_dir


def _abort_on_destroyed_tag(project_dir, current_version, tag, *,
                            releasable_name=None, workspace_root=None):
    """Abort a "first release" that is really a re-release of a destroyed tag.

    ``compute_release_version`` enters the first-release path whenever the
    current version's tag does not exist locally. But "never tagged", "tag
    existed and was later destroyed" (e.g. by an interrupted or undone
    release), and "tag exists under an old format after a tag_format change"
    are indistinguishable to that check. A finalized, immutable
    ``.rlsbl/changes/<version>.jsonl`` for the exact current version proves the
    version is NOT new: it was released once and its changelog was locked at
    release time -- only the tag went missing.

    Without this guard the release would run the entire pipeline (checks,
    tests, secret scan, version-bump commit) and only crash at the finalize
    step ("refusing to finalize changelog ... already exists"), triggering a
    rollback. This guard fires PRE-MUTATION -- nothing has been modified yet
    when it aborts, so no rollback is needed.

    The changes directory is resolved via :func:`resolve_changes_dir`, so
    releasable-mode monorepos resolve to the releasable's changes directory.
    A project with no changes directory at all cannot have a finalized version
    file, so the guard is a no-op there.
    """
    try:
        changes_dir = resolve_changes_dir(
            project_dir, releasable_name=releasable_name,
            workspace_root=workspace_root,
        )
    except ReleaseValidationError:
        # No changes directory -> changelogs were never set up here, so there
        # can be no finalized version file to contradict a first release.
        return

    finalized = os.path.join(changes_dir, f"{current_version}.jsonl")
    if not os.path.isfile(finalized):
        return

    raise ReleaseValidationError(
        f"version {current_version} appears to have been released before: its "
        f"finalized changelog {finalized} exists, but no tag \"{tag}\" is "
        f"present. This happens when the tag was deleted (e.g. by an "
        f"interrupted or undone release), OR when the tag format changed since "
        f"this version was released -- the version was tagged under an "
        f"old-format name, so the current-format tag \"{tag}\" does not exist. "
        f"Either way this looks like a first release when it is not.\n"
        f"Recover by either:\n"
        f"  (1) restore the tag \"{tag}\" pointing at the original release "
        f"commit (git tag {tag} <release-commit>), then re-run the release; or\n"
        f"  (2) move the version forward -- bump {current_version} to a new "
        f"version and release anew; or\n"
        f"  (3) if the tag format changed, check tag_format in workspace.toml / "
        f"the releasable config -- if {current_version} was released under a "
        f"different tag name, restore/create the current-format tag \"{tag}\" "
        f"pointing at that release commit."
    )


def validate_changelog_state(project_dir, target, monorepo_name,
                             monorepo_project_path, config, monorepo_project=None,
                             releasable_name=None, releasable_tag_fmt=None,
                             workspace_root=None, bump_type=None):
    """Resolve the JSONL changelog changes directory path.

    Thin wrapper around :func:`resolve_changes_dir` that preserves the
    existing call signature for backward compatibility.  Changelog
    validation is now handled by the ``preflight-changelog`` check tag
    in the release flow.

    Returns the changes_dir path.
    Raises ReleaseValidationError if the directory does not exist.
    """
    return resolve_changes_dir(
        project_dir, releasable_name=releasable_name,
        workspace_root=workspace_root,
    )


def print_dry_run_summary(log, registry, monorepo_name, monorepo_project_path,
                          bump_type, current_version, new_version, tag,
                          commit_msg, branch, target_paths, project_dir,
                          changelog_entry, monorepo_root=None,
                          member_package_paths=None,
                          releasable_config_dir=None):
    """Print dry-run summary and return (caller should exit after this)."""
    from . import TARGETS, load_workspace
    from .execute import collect_companion_tags

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
    # Show companion tags (e.g. Go module proxy tags)
    if member_package_paths is not None and monorepo_root:
        companion = collect_companion_tags(
            member_package_paths, monorepo_root, new_version, tag,
            releasable_config_dir=releasable_config_dir,
        )
        if companion:
            log(f"Companion tags: {', '.join(companion)}")
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
        print("Would run: selfdoc gen --no-auto-commit")
        return True

    if not require_tool("selfdoc", fatal=False):
        print(
            "Note: selfdoc.json found but selfdoc is not installed. Skipping docs generation."
        )
        return True

    print("Running selfdoc gen...")
    try:
        _subprocess.run(["selfdoc", "gen", "--no-auto-commit"], cwd=project_dir, check=True)
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
        _subprocess.run(["selfdoc", "check", "--no-auto-commit"], cwd=project_dir, check=True)
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


def _abort_on_cross_repo_sources(project_dir, *, boundary_root=None, member_dirs=None):
    """Abort the release if any committed pyproject.toml declares a
    [tool.uv.sources] path entry that resolves outside the repository.

    Cross-repo path sources make lockfiles and CI builds depend on sibling
    checkouts that only exist on the developer's machine. Local overrides
    belong in dev-sources.toml.local-only (gitignored), never in the
    committed pyproject.toml. Runs PRE-MUTATION: nothing has been modified
    yet when this aborts. Runs unconditionally (unlike the preflight tag,
    which is skipped when the pre-release hook is customized).

    ``member_dirs`` (releasable mode) adds member package directories to
    the scan; ``boundary_root`` is the repository/workspace root that
    in-repo paths must stay within.
    """
    from ...checks.project import find_cross_repo_path_sources

    dirs = [project_dir]
    if member_dirs:
        for d in member_dirs:
            if d not in dirs:
                dirs.append(d)

    offenders = []
    for d in dirs:
        for pkg, declared, resolved in find_cross_repo_path_sources(
            d, boundary_root=boundary_root or project_dir,
        ):
            rel = os.path.relpath(os.path.join(str(d), "pyproject.toml"), str(project_dir))
            offenders.append((rel, pkg, declared, resolved))

    if offenders:
        print(
            "Error: cross-repo path source(s) in [tool.uv.sources]:",
            file=sys.stderr,
        )
        for rel, pkg, declared, resolved in offenders:
            print(
                f'  {rel}: {pkg} = {{ path = "{declared}" }} resolves outside '
                f"the repository ({resolved})",
                file=sys.stderr,
            )
        print(
            "Remove the path source(s) -- depend on the registry release instead, "
            "and keep local checkout overrides in dev-sources.toml.local-only.",
            file=sys.stderr,
        )
        raise ReleaseValidationError("Cross-repo path sources in [tool.uv.sources]")


def _abort_on_version_skew(project_dir, *, workspace_root=None):
    """Abort the release when a dev-sources overlay checkout is ahead of
    the registry.

    Reads ``dev-sources.toml.local-only`` at the project root (falling back
    to the workspace root in a monorepo). For each declared overlay, the
    local checkout's ``[project].version`` is compared against the latest
    PyPI release: local ahead means the release was developed and tested
    against unreleased dependency code, so the dependency must be released
    first. Equal or behind passes. An unpublished dependency, a registry
    error, or an unreadable overlay file is a hard error -- never a silent
    skip. No overlays file means nothing is declared, so nothing to check.

    Runs PRE-MUTATION and unconditionally (like the other release guards,
    not the hook-skippable preflight tag).
    """
    from ..dev_sync import OVERRIDES_FILENAME, _load_overlays

    overlay_root = None
    if os.path.isfile(os.path.join(str(project_dir), OVERRIDES_FILENAME)):
        overlay_root = str(project_dir)
    elif workspace_root is not None and os.path.isfile(
        os.path.join(str(workspace_root), OVERRIDES_FILENAME)
    ):
        overlay_root = str(workspace_root)
    if overlay_root is None:
        return

    overlays = _load_overlays(overlay_root)
    if overlays is None:
        # _load_overlays already printed the specific error to stderr.
        raise ReleaseValidationError(
            f"invalid {OVERRIDES_FILENAME} at {overlay_root}"
        )

    from ...registry import query_pypi_version
    from ..monorepo.commands import _parse_version_tuple

    for overlay in overlays:
        pkg = overlay["package"]
        local_version = overlay["version"]
        if not local_version:
            raise ReleaseValidationError(
                f"version skew check: local checkout of '{pkg}' at "
                f"{overlay['path']} declares no [project].version"
            )

        result = query_pypi_version(pkg)
        status = result["status"]
        if status == "error":
            raise ReleaseValidationError(
                f"version skew check: could not query PyPI for '{pkg}': "
                f"{result.get('message', 'unknown error')}. The "
                f"{OVERRIDES_FILENAME} overlay declares a local checkout of "
                f"'{pkg}'; network access is required to verify it is not "
                "ahead of the registry."
            )
        if status == "not_found":
            raise ReleaseValidationError(
                f"release the dependency first: '{pkg}' local {local_version} "
                "is not published on PyPI"
            )

        registry_version = result["version"]
        local_tuple = _parse_version_tuple(local_version)
        registry_tuple = _parse_version_tuple(registry_version)
        if local_tuple is None or registry_tuple is None:
            raise ReleaseValidationError(
                f"version skew check: cannot compare versions for '{pkg}' "
                f"(local {local_version}, registry {registry_version})"
            )
        if local_tuple > registry_tuple:
            raise ReleaseValidationError(
                f"release the dependency first: {pkg} local {local_version} "
                f"> registry {registry_version}"
            )


def _npm_provenance_requested(configs):
    """Return True if any config in *configs* has an npm pipeline with
    ``provenance: true``.

    Config validation guarantees npm pipelines carry a boolean ``provenance``
    key, so this reads it directly. *configs* is an iterable of project/member
    config dicts.
    """
    for config in configs:
        pipelines = (config or {}).get("pipelines") or {}
        if not isinstance(pipelines, dict):
            continue
        for entry in pipelines.values():
            if (
                isinstance(entry, dict)
                and entry.get("type") == "npm"
                and entry.get("provenance") is True
            ):
                return True
    return False


def _abort_on_npm_provenance(configs, *, gh_config):
    """Abort the release if an npm pipeline requests provenance on a repo
    that cannot support it.

    npm build-provenance attestations (``npm publish --provenance``) require a
    PUBLIC GitHub source repository and GitHub Actions OIDC. When any config in
    *configs* declares an npm pipeline with ``provenance: true``, this probes
    the repository visibility via ``gh repo view --json isPrivate`` (using
    *gh_config* for GH_REPO resolution) and aborts when the repo is private or
    when visibility cannot be resolved (e.g. a non-GitHub remote).

    When no npm pipeline requests provenance, NO network call is made at all.

    Runs PRE-MUTATION: nothing has been modified yet when this aborts.
    """
    if not _npm_provenance_requested(configs):
        return

    try:
        out = run_gh(["repo", "view", "--json", "isPrivate"], gh_config)
        data = json.loads(out)
        is_private = data["isPrivate"]
    except Exception as exc:
        raise ReleaseValidationError(
            "npm provenance check: could not determine repository visibility "
            f"via 'gh repo view --json isPrivate' ({exc}). npm build-provenance "
            "requires a public GitHub source repository. If this repository is "
            "not hosted on GitHub, set \"provenance\": false in the npm "
            "pipeline config -- provenance is impossible off GitHub Actions."
        ) from exc

    if not isinstance(is_private, bool):
        raise ReleaseValidationError(
            "npm provenance check: 'gh repo view --json isPrivate' returned an "
            f"unexpected isPrivate value ({is_private!r}). npm build-provenance "
            "requires a public GitHub source repository; set \"provenance\": "
            "false in the npm pipeline config for non-GitHub hosts."
        )

    if is_private:
        raise ReleaseValidationError(
            "npm provenance check: the npm pipeline declares \"provenance\": "
            "true, but this repository is PRIVATE. npm build-provenance "
            "requires a public source repository. Three ways forward: "
            "(1) make the repository public; "
            "(2) set \"provenance\": false in the npm pipeline config; or "
            "(3) drop the npm pipeline from .rlsbl/config.json."
        )


def _schema_dump_command(entry_point: str, lang: str) -> list[str]:
    """Build the command list for running --dump-schema based on language."""
    if lang == "python":
        return ["uv", "run", entry_point, "--dump-schema"]
    elif lang == "go":
        return ["go", "run", entry_point, "--dump-schema"]
    else:
        raise ValueError(f"unsupported strictcli language: {lang}")


def _run_strictcli_schema_dump(flags, log, project_dir=".", version=None):
    """Run --dump-schema for strictcli projects to regenerate .strictcli/schema.json.

    Detects strictcli usage via pyproject.toml or go.mod, runs the entry point
    with --dump-schema, and logs the result. The generated file is picked up by
    the hook-generated file mechanism (pre/post hook dirty snapshots).

    When *version* is given, the ``version`` key in the generated schema.json
    is replaced with *version* after a successful dump (atomic write).

    A project that requires strictcli but whose entry point cannot be
    detected aborts validation (ReleaseValidationError) -- a silent skip
    would ship a stale schema.
    """
    from . import subprocess as _subprocess
    from ...strictcli_detect import StrictcliDetectError

    try:
        result = detect_strictcli(project_dir)
    except StrictcliDetectError as e:
        raise ReleaseValidationError(str(e)) from e

    if flags.get("dry-run"):
        if result:
            entry_point, lang = result
            cmd = _schema_dump_command(entry_point, lang)
            log(f"Would run: {' '.join(cmd)}")
        return

    if not result:
        return

    entry_point, lang = result
    cmd = _schema_dump_command(entry_point, lang)
    log(f"Dumping strictcli schema ({entry_point})...")

    try:
        _subprocess.run(
            cmd,
            cwd=project_dir,
            timeout=_SCHEMA_DUMP_TIMEOUT,
            check=True,
        )
    except _subprocess.TimeoutExpired:
        raise ReleaseValidationError(
            f"strictcli schema dump timed out after {_SCHEMA_DUMP_TIMEOUT}s"
        )
    except (_subprocess.CalledProcessError, OSError) as e:
        raise ReleaseValidationError(
            f"strictcli schema dump failed: {e}"
        ) from e

    if version is not None:
        _patch_schema_version(project_dir, version)


def _patch_schema_version(project_dir, version):
    """Replace the ``version`` key in .strictcli/schema.json with *version*.

    Writes atomically via a temp file + os.replace.
    """
    schema_path = os.path.join(project_dir, ".strictcli", "schema.json")
    if not os.path.isfile(schema_path):
        raise ReleaseValidationError(
            f"strictcli schema dump succeeded but {schema_path} does not exist"
        )

    with open(schema_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "version" not in data:
        raise ReleaseValidationError(
            f"{schema_path} has no 'version' key"
        )

    data["version"] = version

    schema_dir = os.path.dirname(schema_path)
    fd, tmp_path = tempfile.mkstemp(dir=schema_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, schema_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def validate_blog_body(project_dir, blog_enabled, *, releases_dir=None):
    """Validate the blog body file for a release.

    ``releases_dir`` overrides the default ``.rlsbl/releases/`` location --
    releasable releases keep the blog body (unreleased.md) in the
    releasable's own releases dir, alongside unreleased.toml.

    Returns (body_path, warning_message) where body_path is the path if it exists
    and warning_message is set if the file is missing.
    Raises ReleaseValidationError if blog_enabled and file is empty.
    """
    if not blog_enabled:
        return None, None
    if releases_dir is None:
        releases_dir = os.path.join(project_dir, ".rlsbl", "releases")
    blog_body_path = os.path.join(releases_dir, "unreleased.md")
    if os.path.exists(blog_body_path):
        with open(blog_body_path, "r", encoding="utf-8") as f:
            body_content = f.read()
        if not body_content.strip():
            print(
                f"Error: blog body file at {blog_body_path} exists but is empty.",
                file=sys.stderr,
            )
            raise ReleaseValidationError("Blog body validation failed")
        return blog_body_path, None
    return None, f"blog = true but no body file at {blog_body_path} (post will be changelog-only)"
