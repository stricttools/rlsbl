"""Validation helpers for release: test runner, lint, selfdoc gen/check, scaffold conflict detection, strictcli schema dump, and blog post body validation.

Also contains extracted validation steps from run_cmd: target validation, OTA mode,
config integrity, pipeline config, gh CLI, clean tree, branch/remote, monorepo context,
version/tag computation, and changelog state validation.
"""

import json
import os
import re
import sys

from ...strictcli_detect import detect_strictcli
from ...utils import run_gh, working_tree_paths


class ReleaseValidationError(Exception):
    """Raised when a pre-release validation check fails."""
    pass


class HookError(Exception):
    """Raised when a built-in hook (tests, lint, selfdoc) fails."""
    pass


from ...release_file import VALID_BUMP_TYPES
from ... import effects


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


def validate_no_authored_release_commit(release_config):
    """Refuse a release whose editable release file carries any flow-owned field.

    ``candidate_sha`` and ``tree_hashes`` record which commit and tree a version
    shipped from. The release flow writes them into the ARCHIVED
    ``v{X.Y.Z}.toml`` at the archive step, from the commit its own CI verified,
    and rlsbl rewrites that archive only through its own documented unlock
    paths. There is no path by which a pre-release file can know either value:
    the candidate does not exist yet.

    The version-fate fields are refused on the same ground from the other
    direction -- each states something about a version whose fate is already
    settled, which a file describing the NEXT release cannot know:

    * ``unrecoverable`` is the backfill pass's permanent record that an ALREADY
      SHIPPED version's commit could not be recovered. The candidate has not
      been created here, let alone lost.
    * ``never_released`` says a version NUMBER exists that no release ever used.
      The file it appears on is the one preparing that very release.
    * ``shipped_as`` names the tag spelling a version already shipped under.

    So any of them in ``unreleased.toml`` is either a hand-authored claim about
    something that has not happened, or an archive that was copied back without
    being un-finalized properly. Both would make the archive assert something
    the release never verified, which is the one thing the release commit exists
    to prevent -- hence a hard error rather than an overwrite. (``release undo``
    strips every flow-owned field when it restores an archive, so its output
    passes here.)

    Raises ReleaseValidationError naming every flow-owned field present.
    """
    from ...release_file import (
        FLOW_OWNED_FIELDS,
        NEVER_RELEASED_FIELD,
        UNRECOVERABLE_FIELD,
    )

    present = [
        name for name in FLOW_OWNED_FIELDS
        if getattr(release_config, name, None) is not None
    ]
    if not present:
        return
    raise ReleaseValidationError(
        f"the release file carries flow-owned field(s) "
        f"{', '.join(present)}, which only the release flow may write. The "
        f"release commit records the commit CI verified and the tree each released "
        f"path shipped -- neither exists before the release runs -- while "
        f"{UNRECOVERABLE_FIELD} and {NEVER_RELEASED_FIELD} record the fate of a "
        f"version that is already settled. All of them belong in the archived "
        f"v{{version}}.toml, never in unreleased.toml. Remove "
        f"{'them' if len(present) > 1 else 'it'} from "
        f"the release file and re-run."
    )


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
    """Validate ``publish_mode`` and that suppressed repos have no local pipelines.

    Raises ReleaseValidationError on failure.
    """
    from ...config import get_publish_mode, ConfigError

    try:
        mode = get_publish_mode(config)
    except ConfigError as e:
        raise ReleaseValidationError(f"{e}\nQuick fix: rlsbl scaffold")

    if mode == "none":
        pipelines_cfg = config.get("pipelines", {})
        if isinstance(pipelines_cfg, dict):
            for pipeline_name, pipeline_cfg in pipelines_cfg.items():
                if isinstance(pipeline_cfg, dict) and pipeline_cfg.get("local"):
                    raise ReleaseValidationError(
                        'publish_mode "none" cannot publish to public registries.\n'
                        f'Remove pipelines.{pipeline_name}.local or set '
                        '"publish_mode": "ci".'
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

    # A MALFORMED `pipelines` -- present, but not a map -- used to be diagnosed
    # nowhere on this path: it is truthy, so the absent-key check above lets it
    # through, and validate_pipeline_target_links returns early on a non-dict
    # (deferring the shape error to validate_pipelines_config, which the release
    # flow does not run). It reached load_pipelines and died there on .items().
    # load_pipelines now raises ConfigError for it -- still before the mutating
    # phase, and caught by the release run_cmd wrapper -- so the diagnosis lives
    # at the chokepoint every caller shares rather than being restated here.
    #
    # Deliberately NOT validate_pipelines_config: that validator also enforces
    # per-entry rules (npm provenance, go artifact, asset size limits) which are
    # the config check's business, not a gate this preflight has ever applied.

    # Enforce the explicit pipeline->target link (separate-but-linked shape):
    # every pipeline must declare a target name or null, and named refs must
    # resolve to a configured target. Raises ConfigError, caught by the
    # release run_cmd wrapper. Runs at the same scope as this validator
    # (per publishing member in releasable mode; representative otherwise).
    from ...config import validate_pipeline_target_links
    validate_pipeline_target_links(config)

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
            # --method GET spells the read out: the app's observe allowlist
            # matches the ["gh","api","--method","GET"] prefix, so a preview
            # really performs this probe instead of recording it as a change.
            ["api", "--method", "GET", f"repos/{repo}", "--jq", ".permissions.push"],
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
        user = run_gh(["api", "--method", "GET", "user", "--jq", ".login"], config=config)
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


def is_tool_owned_state_path(path) -> bool:
    """True if *path* is one of rlsbl's own untracked release-state files.

    rlsbl writes ``in-progress.json`` (release state) and
    ``scrub-result.json`` (scrub resume state) into the releases dir and
    leaves them there precisely when a release failed and must be resumed.
    They are the tool's own scratch state, not the operator's work, so the
    clean-tree gate must not treat them as uncommitted changes -- otherwise
    rlsbl blocks its own ``release resume``.

    The match is STRUCTURAL (on the path itself), never gitignore-derived: a stale
    consumer ``.gitignore`` must not be able to defeat the exemption.

    ``path`` is a repo-root-relative path as ``git status --porcelain``
    reports it. The two canonical homes (see
    :func:`rlsbl.release_file.get_releases_dir`) are:

    - ``[<member>/].rlsbl/releases/<file>`` -- standalone projects
    - ``.rlsbl-monorepo/releasables/<name>/releases/<file>`` -- releasables

    ``unreleased.plan.json`` and the ``unreleased.toml`` family share those
    directories and are DELIBERATELY committed; they are not exempt.
    """
    from .release_state import STATE_FILENAME, SCRUB_RESULT_FILENAME

    parts = str(path).replace(os.sep, "/").split("/")
    if len(parts) < 3 or parts[-1] not in (STATE_FILENAME, SCRUB_RESULT_FILENAME):
        return False
    if parts[-2] != "releases":
        return False
    if parts[-3] == ".rlsbl":
        return True
    return (
        len(parts) >= 5
        and parts[-4] == "releasables"
        and parts[-5] == ".rlsbl-monorepo"
    )


def validate_no_stash(cwd=None):
    """Refuse a release while the repository has a stash.

    Unlike the clean-tree check, this one is not waived by ``--allow-dirty``:
    that flag says the operator accounted for the dirty paths git can name,
    and a stash is exactly the work git names nowhere. The release commits,
    tags and pushes this tree; a stash would either be silently left behind or
    quietly outlive the release with nothing recording what it belonged to.
    """
    from ...git_util import refuse_present_stash

    refuse_present_stash(
        cwd, operation="release",
        detail=(
            "The release commits, tags and pushes this working tree, and a "
            "stash rides along in none of it."
        ),
        error=ReleaseValidationError,
    )


def validate_clean_tree(flags):
    """Validate working tree is clean (or record pre-existing dirty files).

    Returns set of pre-existing dirty file paths.
    Raises ReleaseValidationError if tree is dirty and --allow-dirty not set.
    """
    from . import is_clean_tree

    pre_existing_dirty = set()
    if flags.get("allow-dirty"):
        return set(working_tree_paths())

    if is_clean_tree():
        return pre_existing_dirty

    # Something is dirty. Classify it: rlsbl's own release-state files
    # (in-progress.json, scrub-result.json) are the tool's scratch state and
    # never block -- refusing over them is rlsbl blocking its own `release
    # resume`, which is exactly when those files exist.
    #
    # ``--untracked-files=all`` is required: the default collapses a wholly
    # untracked directory into a single ``?? .rlsbl/releases/`` entry, which
    # cannot be classified per-file (and must NOT be exempted wholesale --
    # unreleased.toml lives there and is deliberately committed).
    try:
        dirty_paths = working_tree_paths(untracked="all")
    except Exception:
        # Fail closed: an unreadable status is never "clean enough".
        raise ReleaseValidationError(
            "working tree is not clean. Commit your changes first."
        )

    blocking = sorted(
        path for path in dirty_paths
        if not is_tool_owned_state_path(path)
    )
    if blocking:
        listed = "\n".join(f"  {path}" for path in blocking)
        raise ReleaseValidationError(
            "working tree is not clean. Commit your changes first.\n"
            f"Uncommitted:\n{listed}"
        )
    return pre_existing_dirty


def validate_branch_and_remote(flags, *, config=None, cwd):
    """Validate branch state and return the release branch name.

    A release may only be started from a **release branch** (listed in the
    ``release_branches`` config, default ``["main", "master"]``). Any other
    branch is a hard error: dev-branch releases (and the fast-forward merge
    they required) no longer exist. Land your work on the release branch
    first, then release from there.

    On a release branch this validates that local is not behind origin and
    returns the branch name.

    ``cwd`` is REQUIRED (keyword-only): the repo directory all git operations
    (branch lookup, fetch, ancestry, rev-list) run from. No process-cwd default.

    Returns the release branch name as a string.
    Raises :class:`ReleaseValidationError` on failure.
    """
    from . import run, get_current_branch, remote_branch_exists
    from ...prepush_utils import _get_release_branches

    branch = get_current_branch(cwd=cwd)

    # Determine which branches are release-only
    if config is not None:
        from ...context import ProjectContext
        # Build a minimal context to pass to _get_release_branches
        _ctx = ProjectContext(project_root=None, workspace_root=None, config=config)
        release_branches = _get_release_branches(_ctx)
    else:
        from ...prepush_utils import DEFAULT_RELEASE_BRANCHES
        release_branches = list(DEFAULT_RELEASE_BRANCHES)

    if branch not in release_branches:
        raise ReleaseValidationError(
            f'cannot release from "{branch}": it is not a release branch '
            f'(release branch(es): {", ".join(release_branches)}). '
            f'Merge "{branch}" into a release branch, check it out, and '
            f"release from there."
        )

    try:
        run("git", ["fetch", "origin", "--quiet"], cwd=cwd)
    except Exception:
        print("Warning: could not fetch from origin. Skipping remote-ahead check.", file=sys.stderr)
        return branch

    if not remote_branch_exists(branch, cwd=cwd):
        print(
            f"Remote branch origin/{branch} does not exist yet. Skipping remote-ahead check.",
            file=sys.stderr,
        )
        return branch

    try:
        behind_count = int(run("git", ["rev-list", "--count", f"HEAD..origin/{branch}"], cwd=cwd))
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

    Returns (monorepo_name, monorepo_project_path, is_library, is_non_releasable, releasable_name).
    All values are None/False/None when not in a monorepo.
    ``releasable_name`` is a string when the project belongs to a named
    releasable (``releasable = "name"``), or None when it belongs to none.
    Raises ReleaseValidationError if inside a monorepo but not a recognized project,
    or if the project is non-releasable.
    """
    from . import resolve_project

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
            "should be releasable, or keep releasable = false to confirm it "
            "is non-releasable."
        )

    # The project's releasable field names the releasable whose version file
    # is the canonical version source. Every member of a workspace declares
    # one (a member that declares `releasable = false` was refused above), so
    # a missing field is a malformed workspace, not a second release mode.
    rel_val = project.releasable
    if not isinstance(rel_val, str):
        raise ReleaseValidationError(
            f"project '{monorepo_name}' has no 'releasable' field in "
            f"workspace.toml. Every workspace declares its releasables in "
            f"[[releasables]], and every member sets releasable = \"<name>\" "
            f"or releasable = false."
        )
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

    When ``workspace_root`` and ``releasable_name`` are both provided, the
    version is read from the releasable's version file at
    ``.rlsbl-monorepo/releasables/<name>/version`` instead of from the
    target's manifest file. This is the canonical version source for
    multi-package releasables.

    When ``releasable_tag_fmt`` is provided, tags are constructed from the
    releasable's tag format instead of the target's monorepo tag format.

    When either parameter is None (a standalone repo), the version is read
    from the target's manifest instead.

    ``project_dir`` locates the project's ``.rlsbl/`` state -- the release
    archives that decide whether this version already shipped, and the changes
    directory the destroyed-tag guard reads (see
    :func:`_abort_on_destroyed_tag`). When omitted it falls back to
    ``primary_path``, which coincides with the project root for standalone
    repos.

    Whether this is a first release is decided by the RELEASE RECORD, not by the tag
    namespace. A tag read is still consulted, but only as corroboration, and
    its third answer is respected: under ``--dry-run`` past the first recorded
    mutation the tag read is UNANSWERABLE, and reading that as "no tag" is what
    made every preview of an already-released project abort with a
    destroyed-tag diagnosis (see :class:`~rlsbl.utils.LocalTagState`).

    Returns (current_version, new_version, bump_type, tag).
    Raises ReleaseValidationError on invalid bump type or duplicate tag.
    """
    from . import bump_version, tag_exists_locally
    from ...release_record import (
        require_checkout_contains_latest,
        version_is_archived,
    )
    from ...utils import LocalTagState, local_tag_state

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

    _guard_project_dir = project_dir if project_dir is not None else primary_path
    releases_dir = _resolve_releases_dir(
        _guard_project_dir, releasable_name=releasable_name,
        workspace_root=workspace_root,
    )

    # Pre-mutation: refuse to release on a history the latest release is not
    # in. Such a release would revert it, and its changelog range would cover
    # commits that already shipped.
    tag_glob = None
    if releasable_tag_fmt is not None and releasable_name is not None:
        tag_glob = _releasable_tag_glob(releasable_tag_fmt, releasable_name)
    elif monorepo_name:
        tag_glob = target.monorepo_tag_glob(
            monorepo_name, path=monorepo_project_path
        )
    # The ancestry question is asked IN the project's own directory: git
    # answers it from any path inside the repository, and the process cwd is
    # not something this function should depend on.
    require_checkout_contains_latest(
        releases_dir, tag_glob=tag_glob, cwd=_guard_project_dir,
    )

    current_tag = _make_tag(current_version)
    released_before = version_is_archived(releases_dir, current_version)
    tag_state = local_tag_state(current_tag, _guard_project_dir)

    if released_before and tag_state is LocalTagState.ABSENT:
        # The release record says this version shipped and the tag is genuinely gone
        # (not merely unanswerable): a destroyed tag. Abort PRE-MUTATION
        # rather than run the whole pipeline and crash at finalization.
        _abort_on_destroyed_tag(
            _guard_project_dir, current_version, current_tag,
            releasable_name=releasable_name, workspace_root=workspace_root,
        )

    first_release = not released_before and tag_state is not LocalTagState.PRESENT
    if first_release:
        # No archive and no tag. One more record can still contradict "never
        # released": a finalized, immutable <version>.jsonl, which a repository
        # whose archives predate release-commit recording still has.
        if tag_state is LocalTagState.ABSENT:
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

    # Check tag doesn't already exist. Read in the project's own directory,
    # the same place the current-version tag was read, so the two cannot
    # disagree about which repository they are describing.
    if tag_exists_locally(tag, _guard_project_dir):
        raise ReleaseValidationError(f'tag "{tag}" already exists.')

    # Pre-mutation remote collision check. The computed tag must not already
    # exist on the remote at ANY commit. A remote-only stale tag (e.g. left by
    # an interrupted or partially-undone release, possibly on another machine)
    # would otherwise only surface at push time -- after the version bump and
    # release commit have already mutated local state. Catching it here aborts
    # cleanly before any mutation. An inconclusive remote probe is also fatal:
    # a release must not start blind about whether its target tag is free.
    from . import remote_tag_commit, RemoteTagState
    _remote = remote_tag_commit(tag)
    if _remote.state is RemoteTagState.PRESENT:
        raise ReleaseValidationError(
            f'tag "{tag}" already exists on origin at {_remote.commit}; the '
            f'version may already be released or a stale remote tag is present. '
            f'Investigate and delete the remote tag before releasing.'
        )
    if _remote.state is RemoteTagState.INCONCLUSIVE:
        raise ReleaseValidationError(
            f'could not verify whether tag "{tag}" exists on origin '
            f'(ls-remote failed): {_remote.error}. A release must not start '
            f'without confirming its target tag is free -- resolve the remote '
            f'access issue and retry.'
        )

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


def _resolve_releases_dir(project_dir, *, releasable_name=None,
                          workspace_root=None):
    """The release archives -- the RELEASE RECORD -- for this project or releasable.

    A releasable keeps its archives beside its changelog under
    ``.rlsbl-monorepo/releasables/<name>/releases/``; everyone else under
    ``<project>/.rlsbl/releases/``. Derived from the changes-dir resolution so
    the two never disagree about which project's state is being read.
    """
    from ...release_record import releases_dir_for_changes_dir

    if releasable_name and workspace_root:
        from ...workspace import get_releasable_changes_dir
        changes_dir = get_releasable_changes_dir(str(workspace_root), releasable_name)
    else:
        from ...changelog.files import get_changes_dir
        changes_dir = get_changes_dir(project_dir)
    return releases_dir_for_changes_dir(changes_dir)


def _abort_on_destroyed_tag(project_dir, current_version, tag, *,
                            releasable_name=None, workspace_root=None):
    """Abort a release of a version the record says already shipped, untagged.

    "Never released", "released and the tag was later destroyed" (an
    interrupted or undone release), and "released under an old tag format" are
    indistinguishable from the tag alone. Two records can tell them apart, and
    either one is enough:

    * the RELEASE RECORD -- ``.rlsbl/releases/v<version>.toml``, written by the release
      at its archive step and rewritten by rlsbl only through its own
      documented unlock paths. This is the authority, and it is checked first.
    * the finalized, immutable ``.rlsbl/changes/<version>.jsonl``, locked at
      release time. Still consulted, because a repository whose releases
      predate archiving has that record and no archive.

    Without this guard the release would run the entire pipeline (checks,
    tests, secret scan, version-bump commit) and only crash at the finalize
    step ("refusing to finalize changelog ... already exists"), triggering a
    rollback. This guard fires PRE-MUTATION -- nothing has been modified yet
    when it aborts, so no rollback is needed.

    Directories are resolved the same way the rest of the release flow resolves
    them, so releasable-mode monorepos read the releasable's own state. A
    project with neither record is a genuine first release and the guard is a
    no-op.
    """
    from ...release_record import archived_release_path, version_is_archived

    releases_dir = _resolve_releases_dir(
        project_dir, releasable_name=releasable_name,
        workspace_root=workspace_root,
    )
    if version_is_archived(releases_dir, current_version):
        record = archived_release_path(releases_dir, current_version)
        record_kind = "release archive"
    else:
        try:
            changes_dir = resolve_changes_dir(
                project_dir, releasable_name=releasable_name,
                workspace_root=workspace_root,
            )
        except ReleaseValidationError:
            # No changes directory and no archive -> nothing was ever released
            # here, so nothing contradicts a first release.
            return
        finalized = os.path.join(changes_dir, f"{current_version}.jsonl")
        if not os.path.isfile(finalized):
            return
        record = finalized
        record_kind = "finalized changelog"

    raise ReleaseValidationError(
        f"version {current_version} appears to have been released before: its "
        f"{record_kind} {record} exists, but no tag \"{tag}\" is "
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
                          releasable_config_dir=None,
                          releasable_name=None, releasable_tag_fmt=None):
    """Print the release's identity summary: which release this is.

    The first of the preview's three parts. It answers "what release is this?"
    -- registry, bump, tag, branch, changelog -- from values the release flow
    has already resolved, before Phase A issues anything. The recorded Phase-A
    log, the boundary line and the declared Phase-B table follow it (see
    :func:`print_release_preview`), except on the library path, where there is
    no effects handle to record onto and this summary is the whole preview.
    """
    from . import TARGETS, load_workspace
    from .execute import release_ref_context

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
    # Show the rest of the ref set the release would create -- the same
    # expected_refs answer the tag step acts on, so the preview cannot show a
    # different set from the one that would be tagged.
    if member_package_paths is not None and monorepo_root:
        expected = TARGETS[registry].expected_refs(
            new_version,
            release_ref_context(
                monorepo_root=monorepo_root, git_root=project_dir,
                monorepo_name=monorepo_name,
                monorepo_project_path=monorepo_project_path,
                releasable_name=releasable_name,
                releasable_tag_format=releasable_tag_fmt,
                member_package_paths=member_package_paths,
                releasable_config_dir=releasable_config_dir,
            ),
        )
        if expected.companions:
            log(f"Companion tags: {', '.join(expected.companions)}")
        if expected.aliases:
            log(f"Recorded alias tags: {', '.join(expected.aliases)}")
    # Show subtree publishing info
    if monorepo_name:
        target = TARGETS[registry]
        try:
            from ...workspace import load_releasables, mirror_remote_for

            projects = load_workspace(monorepo_root)
            proj_dict = next((p for p in projects if p["name"] == monorepo_name), None)
            subtree_remote = (
                mirror_remote_for(proj_dict, load_releasables(monorepo_root, projects))
                if proj_dict else None
            )
        except Exception as e:
            from ...utils import warn_exception
            warn_exception("could not load workspace for subtree info", e)
            subtree_remote = None
        if subtree_remote:
            plain_tag = target.tag_format(new_version)
            log(f"Subtree:   {subtree_remote} (tag: {plain_tag})")
    log(f"Changelog:\n{changelog_entry or '(none)'}")


# The one line a reader must not miss in a release preview. Above it: work the
# release can describe exactly, because every operand was derived before
# anything was issued. Below it: work whose very existence depends on a verdict
# CI has not given yet.
BOUNDARY_LINE = (
    "──────── everything below depends on CI's verdict ────────"
)


def _phase_b_rows(state, *, registry):
    """The declared Phase-B plan: one (step, what it does) row per release step.

    Phase B stays imperative -- it waits on CI, then publishes -- so it has no
    plan to issue. What it CAN do honestly is declare itself: name each step
    and what that step would do to this particular release, with the operands
    the builder already resolved (the version, the tag, the changes dir). No
    row here is recorded, and the boundary line above says so.
    """
    version = state.new_version
    tag = state.tag
    branch = state.branch
    changes = state.changes_dir or ".rlsbl/changes"
    rows = [
        ("CI_VERIFIED",
         f"wait for CI to go green on the candidate pushed to origin/{branch}"),
        ("CHANGELOG_FINALIZED",
         f"rename {changes}/unreleased.jsonl to {version}.jsonl (read-only), "
         f"write {version}.md, regenerate CHANGELOG.md, commit"),
        ("RELEASE_FILE_FINALIZED",
         f"archive the release file as releases/v{version}.toml, commit"),
        ("TAGGED", f"create {tag} on the CI-verified candidate"),
        ("PUSHED", f"push origin/{branch} and {tag}"),
        ("GITHUB_RELEASE",
         f"create the GitHub Release for {tag} from the {version} "
         f"changelog section"),
    ]
    if state.monorepo_name and state.monorepo_project_path:
        rows.append(("SUBTREE_PUBLISHED",
                     "converge the subtree mirror's branch, if one is configured"))
        rows.append(("MIRROR_RELEASED",
                     "publish this version's tag and GitHub Release on the mirror"))
    rows += [
        ("ASSETS_UPLOADED", f"build and upload release assets to {tag}"),
        ("PIPELINES_PUBLISHED",
         f"publish {registry} (and any other configured pipeline) for {version}"),
        ("DEPLOYED", "run the configured deploy targets"),
        ("POST_HOOKS_RUN", "run .rlsbl/hooks/post-release.sh"),
    ]
    return rows


def print_release_preview(log, plan, state, *, registry, files_to_commit):
    """Render the release preview: recorded Phase A, the boundary, declared Phase B.

    The order is the point. Phase A's steps really were issued -- as recorded
    effects, which the framework's would-do log lists verbatim at the end of the
    run -- so they are reported first, as the plan that produced them. Then the
    boundary line. Then Phase B, which is declared and not recorded, because
    nothing below the line is knowable until CI has judged the candidate.

    ``plan`` is None on the one path where Phase A is not owed at all: a resume
    past the CI gate, or a batch member the orchestrator already gated. Nothing
    was built and nothing was issued, so there is no table to print -- and an
    empty one would read as "Phase A does nothing" rather than the truth,
    "Phase A is already done". The preview says which.
    """
    from .phase_a import render_plan_table

    log("")
    if plan is None:
        log("--- Phase A (version bump -> candidate push): ALREADY DONE ---")
        log("This release's candidate is already committed, pushed and "
            "CI-verified (a resume past the gate, or a batch member the "
            "orchestrator gated), so Phase A had nothing to issue and nothing "
            "was recorded for it.")
    else:
        log("--- Recorded: Phase A (version bump -> candidate push) ---")
        log("Every effect below was RECORDED, not performed; the would-do log "
            "at the end of this run lists them verbatim.")
        log(render_plan_table(plan))
        log(f"Files in the release commit: {len(files_to_commit)}")
    log("")
    log(BOUNDARY_LINE)
    log("")
    log("--- Declared: Phase B (CI gate -> publish), NOT recorded ---")
    for step, what in _phase_b_rows(state, registry=registry):
        log(f"  {step:<22} {what}")
    log("")
    # The framework writes its own structured would-do log on the way out of
    # every dispatch, so it lands after this block no matter what the handler
    # prints. Say what it is, or a reader meets an effect log below a line that
    # just told them everything below it waits on CI.
    log(
        "(The would-do log that follows is the framework's own record of the "
        + ("preflight effects this run recorded. Nothing from Phase A -- which "
           "was already done -- or Phase B appears in it.)"
           if plan is None else
           "Phase-A effects above. Nothing from Phase B appears in it.)")
    )
    log("")


def print_resume_dry_run_summary(log, saved_state, *, verified_sha=None,
                                 head=None):
    """Print what ``rlsbl release resume`` WOULD do, and change nothing.

    The fresh-release preview (:func:`print_dry_run_summary`) describes a
    release that has not started.  A resume is the opposite situation: part of
    the release already happened, and the only useful preview is which steps
    are left, which commit the tag would land on, and whether the CI gate is
    already satisfied.

    This exists because the dry-run gate used to live ONLY in the fresh-release
    entry point.  ``rlsbl release resume --dry-run`` therefore executed the
    whole release for real -- commits, tag, push to the release branch, GitHub
    Release, publish dispatches.
    """
    from .release_state import RELEASE_STEPS, get_failed_steps, get_missing_steps

    completed = [s for s in RELEASE_STEPS
                 if s in set(saved_state.get("completed_steps") or ())]
    remaining = get_missing_steps(saved_state)
    failed = get_failed_steps(saved_state)

    log("\n--- Dry run summary (resume) ---")
    log(f"Version:   {saved_state.get('new_version', '(unknown)')}")
    log(f"Tag:       {saved_state.get('tag', '(unknown)')}")
    log(f"Branch:    {saved_state.get('branch', '(unknown)')}")
    log(f"Done:      {len(completed)}/{len(RELEASE_STEPS)} "
        f"({', '.join(completed) or 'none'})")
    if failed:
        for step, message in failed.items():
            log(f"Failed:    {step}: {message}")
    if verified_sha:
        log(f"Would tag: {verified_sha[:12]} (the CI-verified candidate "
            f"recorded by the earlier attempt)")
    elif head:
        log(f"Would tag: {head[:12]} (the current tip -- it would be pushed "
            f"as a new candidate and re-gated by CI first)")
    log(f"Would run: {', '.join(remaining) or 'nothing (state is complete)'}")
    log("--- No changes made ---")


_SCHEMA_DUMP_TIMEOUT = 30


def _selfdoc_version_args(version):
    """Return the ``--version-override`` argv fragment for *version*, if any.

    selfdoc resolves version-bearing content (the CLI index's Version line,
    root-file version directives) from the project's CURRENT version. During a
    release the version on disk is still the OLD one when selfdoc runs -- the
    bump happens later, in the mutating phase -- so every generated
    version-bearing line shipped exactly one release stale, and the same churn
    tripped the doc-staleness check on the very next release.

    Passing the about-to-be-released version closes that loop: generated
    content is written for the version this release is producing.
    """
    return ["--version-override", str(version)] if version else []


def _run_selfdoc_gen(flags, project_dir=None, version=None):
    """Run selfdoc gen if selfdoc.json exists in the project directory.

    Regenerates documentation pages from source before the selfdoc check step,
    ensuring the check validates fresh content rather than stale pages.

    ``version`` is the version this release is producing; it is forwarded as
    ``--version-override`` so version-bearing generated content is written for
    the new version rather than the (still un-bumped) one on disk.
    """
    from . import require_tool, effects as _effects, subprocess as _subprocess

    check_dir = project_dir if project_dir else "."
    selfdoc_config = os.path.join(check_dir, "selfdoc.json")
    if not os.path.exists(selfdoc_config):
        return True

    version_args = _selfdoc_version_args(version)

    # No dry-run branch: the child below is ``effects.run``, so a preview
    # records it and prints it in the would-do log. The hand-rolled
    # "Would run: ..." line that used to sit here restated -- by hand, and
    # only for this one call -- what the regime already reports for every
    # call, and it drifted from the argv it claimed to describe.
    if not require_tool("selfdoc", fatal=False):
        print(
            "Note: selfdoc.json found but selfdoc is not installed. Skipping docs generation."
        )
        return True

    print("Running selfdoc gen...")
    try:
        _effects.run(
            # No confirm-skip flag: strictcli's confirm protocol keys on a
            # declared `consequential`, not on `mutating`. selfdoc's `gen` and
            # `check` are mutating but not consequential -- regenerating docs
            # in the working tree is ordinary, git-recoverable work -- so they
            # never prompt. (`selfdoc deploy` IS consequential and the
            # cloudflare-pages pipeline passes the flag; these two are not.)
            # `--yes` no longer exists on a strictcli app at all -- it is a
            # banned flag name.
            ["selfdoc", "gen", "--no-auto-commit"] + version_args,
            cwd=project_dir, check=True,
        )
    except _subprocess.CalledProcessError as e:
        print(
            f"Error: selfdoc gen failed (exit code {e.returncode}).",
            file=sys.stderr,
        )
        raise HookError("selfdoc gen failed")
    return True


def _run_selfdoc_check(flags, project_dir=None, version=None):
    """Run selfdoc check if selfdoc.json exists in the project directory.

    Checks documentation consistency before releasing. Non-fatal if selfdoc
    is not installed; fatal if it is installed and the check fails.
    When project_dir is set (monorepo mode), checks are resolved relative to it.

    ``version`` is forwarded as ``--version-override`` so the check judges the
    generated content against the version the gen step just wrote, not the
    still-un-bumped one on disk.
    """
    from . import require_tool, effects as _effects, subprocess as _subprocess

    # No dry-run branch: the child below is ``effects.run`` and records itself.
    check_dir = project_dir if project_dir else "."
    selfdoc_config = os.path.join(check_dir, "selfdoc.json")
    if not os.path.exists(selfdoc_config):
        return True

    if not require_tool("selfdoc", fatal=False):
        print("Note: selfdoc.json found but selfdoc is not installed. Skipping docs check.")
        return True

    print("Running selfdoc check...")
    try:
        _effects.run(
            ["selfdoc", "check", "--no-auto-commit"]
            + _selfdoc_version_args(version),
            cwd=project_dir, check=True,
        )
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
    """Build the command list for running --dump-schema based on language.

    One branch per strictcli implementation. TypeScript apps are npm packages
    whose ``bin`` entry names a built JS file, so the dump runs that file
    directly with node -- there is no install step to depend on, exactly as
    ``go run`` needs no built binary.
    """
    if lang == "python":
        return ["uv", "run", entry_point, "--dump-schema"]
    elif lang == "go":
        return ["go", "run", entry_point, "--dump-schema"]
    elif lang == "typescript":
        return ["node", entry_point, "--dump-schema"]
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
    from . import effects as _effects, subprocess as _subprocess
    from ...strictcli_detect import StrictcliDetectError

    try:
        result = detect_strictcli(project_dir)
    except StrictcliDetectError as e:
        raise ReleaseValidationError(str(e)) from e

    # No dry-run branch for the dump itself: it is an ``effects.run`` and
    # records itself in the would-do log.
    if not result:
        return

    entry_point, lang = result
    cmd = _schema_dump_command(entry_point, lang)
    log(f"Dumping strictcli schema ({entry_point})...")

    try:
        _effects.run(
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


# The top-level ``version`` member of a canonically-encoded schema document:
# two spaces of indent (depth 1), the key, ``": "``, a JSON string literal, and
# an optional comma. Pinned at exactly two spaces so a ``version`` key nested
# deeper -- a flag NAMED version, a nested object with its own -- can never
# match, and pinned at the line start so it cannot match inside a string.
_SCHEMA_VERSION_LINE = re.compile(
    r'^  "version": "(?:[^"\\]|\\.)*"(,?)$', re.MULTILINE,
)


def _patch_schema_version(project_dir, version):
    """Replace the top-level ``version`` value in .strictcli/schema.json.

    The patch is TEXTUAL: it rewrites exactly one line and preserves every
    other byte. strictcli writes this file in its own canonical encoding
    (schema v2) -- raw UTF-8, no HTML escaping, canonical floats, two-space
    indent, one trailing newline -- and a decode/re-encode round trip through
    ``json.dumps`` silently produces a different document. Most visibly,
    ``json.dumps`` defaults to ``ensure_ascii=True``, so every non-ASCII
    character in any help text came back as a ``\\uXXXX`` escape and every
    consumer release rewrote its schema file into something no strictcli
    implementation would ever write.

    Writes atomically via a temp file + os.replace.
    """
    schema_path = os.path.join(project_dir, ".strictcli", "schema.json")
    if not os.path.isfile(schema_path):
        if effects.previewing():
            # The dump above was RECORDED, not run, so a project whose schema
            # file does not exist yet has nothing here to patch. Absence is
            # then a statement about the preview, not about the project, and
            # must not be reported as a failed dump.
            return
        raise ReleaseValidationError(
            f"strictcli schema dump succeeded but {schema_path} does not exist"
        )

    with open(schema_path, "r", encoding="utf-8") as f:
        content = f.read()

    match = _SCHEMA_VERSION_LINE.search(content)
    if match is None:
        raise ReleaseValidationError(
            f"{schema_path} has no top-level 'version' key on a line of its "
            "own. Either the schema declares no version, or the file is not in "
            "strictcli's canonical encoding -- which means something other "
            "than a strictcli dump wrote it."
        )

    # The value is re-encoded as a JSON string literal in the same canonical
    # form the document uses (``ensure_ascii=False``), never spliced in raw.
    replacement = f'  "version": {json.dumps(version, ensure_ascii=False)}{match.group(1)}'
    patched = content[:match.start()] + replacement + content[match.end():]

    # file_mode pins the 0o600 the mkstemp-based hand-rolled write produced
    # here before the chokepoint absorbed it (see the effects module).
    effects.atomic_write_text(schema_path, patched, file_mode=0o600)


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
