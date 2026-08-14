"""rlsbl: Release orchestration and project scaffolding for npm, PyPI, Go, Deno, Zig, Swift, Hex, Docker, Maven, and more, automating version bumps, changelogs, tags, GitHub Releases, and CI/CD."""

import os
import subprocess
import sys
from pathlib import Path

import strictcli

from . import observe_allowlist as _observe_allowlist
from .context import create_context
from .errors import ReleaseFileError


def _detect_version():
    """Detect package version, preferring pyproject.toml over installed metadata.

    Order: pyproject.toml in the source tree (accurate during editable installs)
    -> importlib.metadata (works for regular installs) -> "unknown".
    """
    # Try reading version from pyproject.toml next to the package source
    try:
        pyproject_path = os.path.realpath(
            os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        )
        if os.path.isfile(pyproject_path):
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
            return data["project"]["version"]
    except Exception:
        pass

    # Fall back to installed dist-info metadata
    try:
        from importlib.metadata import version as _get_version
        return _get_version("rlsbl")
    except Exception:
        pass

    return "unknown"


__version__ = _detect_version()

# Module-level storage for variadic positional args that strictcli cannot
# handle natively. Populated by main() before app.run().
_variadic_args: list[str] = []

# The WorkspaceProject resolved by _require_sub_project_root(), or None in
# standalone mode. Command handlers can pass this to create_context().
_resolved_project = None


def detect_registries():
    """Detect all registries/targets applicable in the current directory.

    Returns a list of name strings, e.g. ["npm"], ["pypi"], or ["npm", "pypi"].
    Delegates to detect_targets() so all registered targets (including docker,
    deno, hex, maven, etc.) are auto-detected when no config exists.
    """
    from .targets import detect_targets
    return [entry.name for entry in detect_targets(".")]


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _require_project_root():
    """Find the rlsbl project root or exit with an error."""
    from .utils import find_project_root
    root = find_project_root()
    if root is None:
        print("Error: not in an rlsbl project (no .rlsbl/ found in any ancestor directory).", file=sys.stderr)
        sys.exit(1)
    return Path(root)


def _require_sub_project_root(workspace_root_guidance=None):
    """Find the project root, resolving to the sub-project in monorepo mode.

    In standalone mode: same as _require_project_root().
    In monorepo mode: uses resolve_project() to find which sub-project CWD is in,
    returns the sub-project path instead of the monorepo root.

    ``workspace_root_guidance``: optional error message printed when CWD is
    the monorepo workspace root itself. Per-sub-project commands (dev sync,
    dev install) pass this so a workspace-root invocation says "cd into a
    sub-project" instead of the misleading default "run 'rlsbl monorepo add'"
    (the workspace root is not an unregistered project).

    Side effect: sets module-level ``_resolved_project`` to the
    WorkspaceProject when in monorepo mode, or None in standalone mode.
    Command handlers can pass this to ``create_context(project=...)``.
    """
    global _resolved_project
    _resolved_project = None
    root = _require_project_root()
    from .workspace import find_workspace_root, resolve_project
    ws_root = find_workspace_root(str(root))
    if ws_root:
        project = resolve_project(ws_root, str(Path.cwd()))
        if project:
            _resolved_project = project
            sub_path = Path(ws_root) / project["path"]
            return sub_path
        if (
            workspace_root_guidance is not None
            and Path(ws_root).resolve() == Path.cwd().resolve()
        ):
            print(workspace_root_guidance, file=sys.stderr)
            sys.exit(1)
        # CWD is inside the monorepo but not in any registered project
        print(f"Error: CWD is inside monorepo at {ws_root} but not inside any registered project.", file=sys.stderr)
        print("Run 'rlsbl monorepo add <path>' to register this project.", file=sys.stderr)
        sys.exit(1)
    return root


def _resolve_target(target):
    """Validate and resolve a --target flag value.

    If target is a string, validates it against TARGETS. If None,
    auto-detects from the current directory. Returns the resolved
    registry name string or exits on error.
    """
    from .targets import TARGETS
    if target:
        if target not in TARGETS:
            print(
                f"Error: unknown target '{target}'. Valid: {', '.join(TARGETS.keys())}",
                file=sys.stderr,
            )
            sys.exit(1)
        return target
    # Auto-detect
    regs = detect_registries()
    if not regs:
        print("Error: no package.json, pyproject.toml, or go.mod found.", file=sys.stderr)
        sys.exit(1)
    return regs[0]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = strictcli.App(
    name="rlsbl",
    version=__version__,
    # The command-count sentence is appended after all registrations (see
    # "Derived help counts" near the bottom of this module) so it is computed
    # from the live registry instead of hand-maintained literals that drift.
    help="Release orchestration and project scaffolding CLI. Automates version bumping, changelog validation, tagging, GitHub Releases, and CI/CD scaffolding across 17 release targets (npm, PyPI, Go, Deno, Zig, Swift, Hex, Docker, Maven, Dart, Flutter, and more).",
    # --dry-run, --approve-consequential, --quiet and --verbose are
    # framework-owned: strictcli registers them on every app, strips them from
    # argv, and delivers them on the Context (ctx.dry_run /
    # ctx.approve_consequential / ctx.quiet / ctx.verbose).  rlsbl declared
    # --dry-run/--yes/--quiet itself until the effects regime landed; naming
    # any of them here is now a registration-time hard error, and `yes` stays
    # banned so nobody restates --approve-consequential in the old spelling.
    #
    # Every argv prefix is a READ: under --dry-run these still execute and
    # return real values, which is what lets the release engine's
    # read-then-branch code walk a preview end to end.  The list, the written
    # standard every entry must satisfy ("no user-visible mutation"), and each
    # entry's declared category live in :mod:`rlsbl.observe_allowlist`;
    # ``tests/test_observe_allowlist.py`` asserts the list against it.
    proc_observe_allowlist=_observe_allowlist.prefixes(),
    # A cross-tool protocol signal, read live at call time: the pre-push check
    # reads the pushed refs git feeds its hook on stdin, and a caller that has
    # already consumed that stdin hands the content over here instead.
    # Declaring it puts it in --help under Infrastructure, where an
    # undocumented env read belongs.
    handshake_env={
        "RLSBL_PUSH_STDIN": (
            "Pre-push ref lines (`<local ref> <local sha> <remote ref> "
            "<remote sha>`) for `rlsbl check --tag prepush`, when the caller "
            "has already consumed git's hook stdin."
        ),
    },
    checks_path=Path(__file__).parent / "data" / "checks.toml",
    test_coverage=True,
)


def _check_context_factory():
    """Create the appropriate check context for the current project.

    Returns WorkspaceCheckContext if in a monorepo, otherwise ProjectCheckContext.
    Imports are deferred to avoid circular dependencies and to keep the factory lazy.
    """
    import os
    from pathlib import Path

    from .check_context import WorkspaceCheckContext
    from .context import ProjectContext, create_context
    from .workspace import find_workspace_root, load_workspace

    push_stdin = os.environ.get("RLSBL_PUSH_STDIN")

    workspace_root = find_workspace_root()
    if workspace_root is not None:
        from .workspace_graph import WorkspaceGraph
        from .workspace import is_explicit_mode, load_releasables

        projects = load_workspace(workspace_root)
        graph = WorkspaceGraph(workspace_root, projects)
        # load_releasables raises WorkspaceError when [[releasables]] is
        # missing (implicit mode).  Only call it in explicit mode.
        if is_explicit_mode(workspace_root):
            releasables = load_releasables(workspace_root, projects=projects)
        else:
            releasables = []
        ctx = create_context(Path.cwd(), workspace_root=Path(workspace_root))
        wctx = WorkspaceCheckContext(
            project_root=ctx.project_root,
            workspace_root=ctx.workspace_root,
            config=ctx.config,
            projects=projects,
            graph=graph,
            releasables=releasables,
        )
        wctx.push_stdin = push_stdin
        return wctx
    from .workspace import create_standalone_releasable

    ctx = create_context(Path.cwd())
    ctx.push_stdin = push_stdin
    ctx.releasable = create_standalone_releasable(ctx.project_root)
    return ctx


def _read_config_for_cwd():
    """Read the rlsbl config for the current working directory.

    Used by the external check provider to read config at materialization
    time (called lazily by strictcli, memoized by cwd).
    """
    from .config import read_project_config
    return read_project_config(os.getcwd())


app.set_check_context(_check_context_factory)

# Register the external check provider (lazily reads config per cwd).
from .external_checks import make_external_check_provider
app.register_check_provider(make_external_check_provider(_read_config_for_cwd))

# Register check implementations on the strictcli check system.
from .checks import register_checks
from . import effects
register_checks(app)


# ---------------------------------------------------------------------------
# release group
# ---------------------------------------------------------------------------

# The subcommand-count sentence is appended after all registrations (see
# "Derived help counts" near the bottom of this module) so it is computed from
# the live registry instead of a hand-maintained literal that drifts -- it said
# "9 subcommands" and omitted `reconcile` long after reconcile shipped.
release_group = app.group("release", help="Release orchestration commands covering the full release lifecycle.")


@release_group.command(
    name="run",
    effect="mutating",
    # Tags, pushes, creates a GitHub Release and triggers the registry
    # publish. A published version cannot be unpublished.
    consequential=True,
    help="Bump version, validate the JSONL changelog, run tests and lint, commit, tag, push, and create a GitHub Release. Reads the bump type (patch, minor, major, or infra) and target selection from .rlsbl/releases/unreleased.toml, which can be scaffolded with rlsbl release init. Supports dry-run preview, --approve-consequential to skip the confirmation prompt in non-interactive contexts, and --allow-dirty to skip the clean working tree check.",
)
@strictcli.flag(name="push-timeout", type=int, default=0, help="Timeout in seconds for each git push. Overrides the push_timeout config key; 0 (the default) means use push_timeout, else the shipped default.")
@strictcli.flag(name="ci-timeout", type=int, default=0, help="Timeout in seconds for the release CI gate (the wait for CI to conclude on the pushed release candidate). Overrides the ci_timeout config key; 0 (the default) means use ci_timeout, else the shipped default.")
@strictcli.flag(name="check-timeout", type=int, default=0, help="Timeout in seconds for each preflight check subprocess (tests, lint, external checks). Overrides the check_timeout config key; 0 (the default) means use check_timeout, else the shipped default.")
@strictcli.flag(name="hook-timeout", type=int, default=0, help="Timeout in seconds for each release hook. Overrides the hook_timeout config key; 0 (the default) means use hook_timeout, else no timeout.")
@strictcli.flag(name="watch", type=bool, help="After release, automatically watch CI runs to completion (--no-watch to skip)")
@strictcli.flag(name="allow-dirty", type=bool, help="Skip the clean working tree check and allow releasing with uncommitted changes")
@strictcli.flag(name="bump", type=str, help="Bump type: patch, minor, major, infra, prerelease. Skips the release file.", default="")
@strictcli.flag(name="description", type=str, help="Short release description summarizing the changes (required with --bump)", default="")
@strictcli.flag(name="preid", type=str, help="Pre-release identifier: alpha, beta, rc, stable. Only valid with --bump.", default="")
@effects.handler
def cmd_release_run(ctx, allow_dirty, watch, bump, description, preid, push_timeout, ci_timeout, check_timeout, hook_timeout):
    """Execute the release flow: validate, bump, test, commit, tag, push, and create GitHub Release."""
    dry_run = ctx.dry_run
    quiet = ctx.quiet
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl release run` must run inside a sub-project, not "
            "at the monorepo workspace root. cd into the sub-project you "
            "want to release."
        )
    )

    from .release_file import read_release_file, get_release_file_path, ReleaseConfig, VALID_BUMP_TYPES
    from .workspace import find_workspace_root, resolve_project

    # In monorepo mode, the release file lives in the package's directory
    project_dir = "."
    monorepo_root = find_workspace_root(str(root))
    ctx = create_context(root, workspace_root=Path(monorepo_root) if monorepo_root else None)
    if monorepo_root:
        project = resolve_project(monorepo_root, ".")
        if project is None:
            print(
                "Error: cannot release from monorepo root. "
                "Use `rlsbl monorepo release run` for batch releases, "
                "or cd to a package directory.",
                file=sys.stderr,
            )
            sys.exit(1)
        project_dir = os.path.join(monorepo_root, project["path"])

    # Releasable releases keep their release file under the releasable's
    # own dir (.rlsbl-monorepo/releasables/<name>/releases/), never under
    # the representative member's .rlsbl/ (same home as in-progress.json).
    _releasable_dir = None
    if monorepo_root:
        from .commands.release.release_state import resolve_releasable_dir
        _releasable_dir = resolve_releasable_dir(project_dir, monorepo_root)

    # --- Quick bump mode: --bump + --description bypass the release file ---
    if preid and not bump:
        print("Error: --preid requires --bump", file=sys.stderr)
        sys.exit(1)
    if bump and not description:
        print("Error: --description is required when --bump is used", file=sys.stderr)
        sys.exit(1)
    if description and not bump:
        print("Error: --bump is required when --description is used", file=sys.stderr)
        sys.exit(1)
    if bump and description:
        if bump not in VALID_BUMP_TYPES:
            print(
                f"Error: invalid bump type {bump!r} (must be one of {VALID_BUMP_TYPES})",
                file=sys.stderr,
            )
            sys.exit(1)
        if preid:
            from .release_file import VALID_PREIDS
            if preid not in VALID_PREIDS:
                print(
                    f"Error: invalid preid {preid!r} (must be one of {VALID_PREIDS})",
                    file=sys.stderr,
                )
                sys.exit(1)
        release_path = get_release_file_path(project_dir, releasable_dir=_releasable_dir)
        if os.path.exists(release_path):
            print(
                "Error: release file exists — use `rlsbl release run` without --bump, "
                "or delete the file first.",
                file=sys.stderr,
            )
            sys.exit(1)
        from .targets import detect_targets
        from .errors import ConfigError
        try:
            targets = detect_targets(project_dir)
        except ConfigError:
            print(
                "Error: cannot auto-detect targets — use `rlsbl release init` "
                "for projects with custom target config",
                file=sys.stderr,
            )
            sys.exit(1)
        target_names = [t.name for t in targets]
        if "flutter" in target_names:
            print(
                "Error: Flutter projects require a release file for the mode "
                "setting — use `rlsbl release init`",
                file=sys.stderr,
            )
            sys.exit(1)
        release_config = ReleaseConfig(
            bump=bump,
            include=target_names,
            exclude=[],
            description=description,
            preid=preid,
        )
        from .commands.release.shared import build_release_flags
        flags = build_release_flags(dry_run, quiet, allow_dirty, watch=watch,
                                    push_timeout=push_timeout or None,
                                    ci_timeout=ci_timeout or None,
                                    check_timeout=check_timeout or None,
                                    hook_timeout=hook_timeout or None)
        from .commands.release import run_cmd
        run_cmd(release_config, flags, ctx=ctx)
        return

    # --- File-based flow ---
    release_path = get_release_file_path(project_dir, releasable_dir=_releasable_dir)
    if not os.path.exists(release_path):
        print(
            "No release file found. Run `rlsbl release init` to create one.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        release_config = read_release_file(release_path)
    except ReleaseFileError as e:
        print(f"Error in release file: {e}", file=sys.stderr)
        sys.exit(1)

    from .commands.release.shared import build_release_flags
    flags = build_release_flags(dry_run, quiet, allow_dirty, watch=watch,
                                push_timeout=push_timeout or None,
                                ci_timeout=ci_timeout or None,
                                check_timeout=check_timeout or None,
                                hook_timeout=hook_timeout or None)
    from .commands.release import run_cmd
    run_cmd(release_config, flags, ctx=ctx)


@release_group.command(
    name="resume",
    effect="mutating",
    # Re-enters the release flow: the steps that remain are the same push,
    # tag and publish `release run` performs.
    consequential=True,
    help="Resume a previously failed release from where it left off. Reads the in-progress state file (.rlsbl/releases/in-progress.json, or .rlsbl-monorepo/releasables/<name>/releases/in-progress.json for releasable releases), validates that the current branch matches the saved state, and re-enters the release flow, skipping already-completed steps.",
)
@strictcli.flag(name="push-timeout", type=int, default=0, help="Timeout in seconds for each git push. Overrides the push_timeout config key; 0 (the default) means use push_timeout, else the shipped default.")
@strictcli.flag(name="ci-timeout", type=int, default=0, help="Timeout in seconds for the release CI gate (the wait for CI to conclude on the pushed release candidate). Overrides the ci_timeout config key; 0 (the default) means use ci_timeout, else the shipped default.")
@strictcli.flag(name="check-timeout", type=int, default=0, help="Timeout in seconds for each preflight check subprocess (tests, lint, external checks). Overrides the check_timeout config key; 0 (the default) means use check_timeout, else the shipped default.")
@strictcli.flag(name="hook-timeout", type=int, default=0, help="Timeout in seconds for each release hook. Overrides the hook_timeout config key; 0 (the default) means use hook_timeout, else no timeout.")
@strictcli.flag(name="watch", type=bool, help="After release, automatically watch CI runs to completion (--no-watch to skip)")
@effects.handler
def cmd_release_resume(ctx, watch, push_timeout, ci_timeout, check_timeout, hook_timeout):
    """Resume a previously failed release from its last completed step."""
    dry_run = ctx.dry_run
    quiet = ctx.quiet
    from .commands.release.release_state import (
        StateResolutionError,
        load_release_state,
        resolve_resume_source,
    )
    from .workspace import find_workspace_root

    # Resolve the project dir and the (releasable-aware) state path via the
    # single resume-source resolver. At a workspace root, this finds the one
    # releasable with in-flight state (error on none or ambiguity) -- so the
    # workspace must be detected BEFORE requiring a sub-project root, which
    # would sys.exit at a workspace root that is not itself a member.
    monorepo_root = find_workspace_root()
    root = None
    if monorepo_root is None:
        root = _require_project_root()
    try:
        project_dir, state_path = resolve_resume_source(monorepo_root, cwd=".")
    except StateResolutionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if project_dir == "." and root is not None:
        ctx_root = root
    else:
        ctx_root = Path(project_dir)
    ctx = create_context(
        ctx_root,
        workspace_root=Path(monorepo_root) if monorepo_root else None,
    )

    # Load in-progress state
    saved = load_release_state(state_path)
    if saved is None:
        print(
            "No release in progress. Use `rlsbl release run` to start a new release.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate: same branch
    from .utils import get_current_branch, run as _run
    current_branch = get_current_branch(cwd=str(ctx.project_root))
    saved_branch = saved.get("branch", "")
    if current_branch != saved_branch:
        print(
            f"Error: release was started on branch {saved_branch!r} "
            f"but current branch is {current_branch!r}. "
            f"Switch to {saved_branch!r} before resuming.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate: HEAD is descendant of saved pre_release_sha
    pre_release_sha = saved.get("pre_release_sha", "").strip()
    if pre_release_sha:
        try:
            from .git_util import is_ancestor
            if not is_ancestor(pre_release_sha, "HEAD"):
                print(
                    f"Error: HEAD is not a descendant of the pre-release commit "
                    f"({pre_release_sha[:10]}). The release state may be stale. "
                    f"Use `rlsbl release undo` to roll back.",
                    file=sys.stderr,
                )
                sys.exit(1)
        except Exception as e:
            print(f"Error: could not verify commit ancestry: {e}", file=sys.stderr)
            sys.exit(1)

    completed_steps = saved.get("completed_steps", [])
    ip_version = saved.get("new_version", "unknown")
    ip_done = len(completed_steps)
    if not quiet:
        print(
            f"Resuming release v{ip_version} "
            f"({ip_done} step(s) completed: {', '.join(completed_steps) or 'none'})"
        )

    from .commands.release.shared import build_release_flags
    flags = build_release_flags(dry_run, quiet, allow_dirty=False, watch=watch,
                                push_timeout=push_timeout or None,
                                ci_timeout=ci_timeout or None,
                                check_timeout=check_timeout or None,
                                hook_timeout=hook_timeout or None)
    from .commands.release import resume_cmd
    resume_cmd(saved, flags, ctx=ctx)


@release_group.command(
    name="init",
    help="Scaffold a .rlsbl/releases/unreleased.toml file by auto-detecting project targets. The generated file contains a default bump type (patch), an include list of all detected targets, and per-target configuration sections for Flutter targets.",
    effect="mutating",
    dry_run_supported=False,
    dry_run_unsupported_reason=(
        "the command scaffolds a file whose point is that you edit it before "
        "releasing; printing that file instead of writing it leaves nothing "
        "to edit"
    ),
)
@effects.handler
def cmd_release_init(ctx):
    """Scaffold unreleased.toml with auto-detected targets for the next release."""
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl release init` must run inside a sub-project, not "
            "at the monorepo workspace root. cd into the sub-project you "
            "want to release."
        )
    )
    from .commands.release_init import run_cmd
    run_cmd(project_root=root)


@release_group.command(
    name="retry",
    effect="mutating",
    # Dispatches the publish workflows, so it causes the same irreversible
    # registry publish the release itself would have.
    consequential=True,
    help="Dispatch CI/CD workflows for a completed release via gh workflow run. Reads the dispatch list and ref from .rlsbl/releases/retry.toml, which is auto-scaffolded with sensible defaults if missing. Verifies the GitHub Release exists before dispatching. Each workflow in the dispatch list is triggered against the configured ref (defaults to the release tag).",
)
@strictcli.flag(name="watch", type=bool, help="After retry, automatically watch CI runs to completion (--no-watch to skip)")
@effects.handler
def cmd_release_retry(ctx, watch):
    """Dispatch CI workflows for a completed release via gh workflow run."""
    dry_run = ctx.dry_run
    quiet = ctx.quiet
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl release retry` must run inside a sub-project, "
            "not at the monorepo workspace root. cd into the sub-project "
            "you want to retry."
        )
    )

    from .release_file import get_retry_file_path, read_retry_file

    # Releasable members keep retry.toml under the releasable's releases dir.
    _retry_releasable_dir = None
    from .workspace import find_workspace_root as _find_ws_root
    _retry_ws_root = _find_ws_root(str(root))
    if _retry_ws_root:
        from .commands.release.release_state import resolve_releasable_dir
        _retry_releasable_dir = resolve_releasable_dir(str(root), _retry_ws_root)

    retry_path = get_retry_file_path(".", releasable_dir=_retry_releasable_dir)
    retry_config = None
    if os.path.exists(retry_path):
        try:
            retry_config = read_retry_file(retry_path)
        except ReleaseFileError as e:
            print(f"Error in retry file: {e}", file=sys.stderr)
            # Clean up the invalid file so it doesn't block subsequent
            # `rlsbl release run` with a dirty working tree.
            from .release_file import discard_invalid_retry_file
            discard_invalid_retry_file(retry_path)
            print(
                "Hint: `rlsbl release retry` is for dispatching CI after a "
                "completed release. To re-run a failed release, use "
                "`rlsbl release run`.",
                file=sys.stderr,
            )
            sys.exit(1)

    flags = {
        "dry-run": dry_run,
        "quiet": quiet,
        "watch": bool(watch),
    }
    from .commands.release_retry import run_cmd
    run_cmd(retry_config, flags, project_root=root)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

# Every key `_collect_status` builds is always present; the nullable ones are
# what an unavailable git repo, an untagged project or a skipped registry query
# leave behind. `drift` is only set when --registry was passed.
_STATUS_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "version": {"type": ["string", "null"]},
        "target": {"type": "string"},
        "branch": {"type": ["string", "null"]},
        "tag": {"type": ["string", "null"]},
        "clean": {"type": ["boolean", "null"]},
        "changelog": {"type": ["boolean", "null"]},
        "jsonl_coverage": {"type": "string"},
        "commits_ahead": {"type": ["integer", "null"]},
        "commits_ahead_tag": {"type": ["string", "null"]},
        "ci": {"type": "array", "items": {"type": "string"}},
        "publish": {"type": "boolean"},
        "registry_version": {"type": ["string", "null"]},
        "drift": {
            "type": ["string", "null"],
            "enum": [
                "AHEAD", "BEHIND", "SAME", "ERROR",
                "PRIVATE", "UNPUBLISHED", None,
            ],
        },
    },
    "required": [
        "name", "version", "target", "branch", "tag", "clean", "changelog",
        "jsonl_coverage", "commits_ahead", "commits_ahead_tag", "ci",
        "publish", "registry_version", "drift",
    ],
    "additionalProperties": False,
}


@app.command(name="status", help="Display the current project version, branch, last release tag, unreleased commit count, and changelog coverage. Outputs plain text by default or structured JSON with the --json flag.", effect="read_only", payload_schema=_STATUS_PAYLOAD_SCHEMA)
@strictcli.flag(name="target", type=str, help="Target a specific registry (auto-detected if omitted)", default="")
@strictcli.flag(name="registry", type=bool, default=False, help="Query the package registry for the latest published version")
@effects.handler
def cmd_status(ctx, target, registry):
    """Display project version, branch, last tag, and changelog coverage."""
    # --json is framework-owned: strictcli reserves the name at every level and
    # delivers the value on the Context.
    json = ctx.json
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl status` must run inside a sub-project, not at "
            "the monorepo workspace root. cd into a sub-project, or use "
            "`rlsbl monorepo status` for workspace-wide status."
        )
    )
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    # The project context is a different object from the dispatch context, so
    # it gets its own name: `ctx` stays the strictcli one the payload goes to.
    project_ctx = create_context(root, workspace_root=Path(ws_root) if ws_root else None)
    target_name = _resolve_target(target or None)
    flags = {"json": json, "registry": registry}
    from .commands.status import run_cmd
    ctx.payload(run_cmd(target_name, [], flags, ctx=project_ctx))


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------

@app.command(name="scaffold", help="Generate or update CI/CD workflows, git hooks, changelog, and license files. Safe to run repeatedly -- three-way merges template changes with your customizations. Existing files with no stored merge base are healed from their last scaffold commit before merging.", effect="mutating")
@strictcli.flag(name="target", type=str, help="Declare an additional registry this project publishes to (for targets auto-detection cannot find, e.g. plain). Added to the project's target set; scaffold always covers every target, never just this one.", default="")
@strictcli.flag(name="publish-mode", type=str, default="", help='Publish mode: "ci" to publish via CI pipelines, or "none" to suppress publishing. Required for private repos; public repos default to "ci".')
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit scaffolded files after writing them to disk")
@strictcli.flag(name="skip-shared", type=bool, default=False, help="Skip processing of shared workflow templates across targets")
@strictcli.flag(name="auto-tag", type=bool, default=True, help="Add or update the rlsbl GitHub topic tag on this invocation")
@effects.handler
def cmd_scaffold(ctx, target, publish_mode, auto_commit, skip_shared, auto_tag):
    """Generate or update CI/CD workflows, hooks, and changelog infrastructure."""
    dry_run = ctx.dry_run
    # Scaffold is special: if a project root exists, resolve it for use as
    # scaffold_root; if not, stay in cwd (for new projects).
    # If the current directory has project markers (pyproject.toml,
    # package.json, go.mod, etc.), use cwd -- the user is in a sub-project
    # and wants to scaffold in place. This prevents walking up to a monorepo
    # root when inside a sub-project.
    # When --target is explicitly passed (e.g., --target plain), always use
    # cwd -- the user is declaring what to scaffold and where. Without this,
    # plain-target projects (whose detect() always returns False) would walk
    # up to the monorepo root, causing _is_non_releasable_project() to fail.
    from .utils import find_project_root
    cwd_has_project = bool(detect_registries())
    # Already-scaffolded projects (e.g. plain targets whose detect() returns
    # False) are recognised by the presence of .rlsbl/config.json in cwd.
    cwd_has_rlsbl_config = (Path.cwd() / ".rlsbl" / "config.json").is_file()
    scaffold_root = None
    if target:
        scaffold_root = Path.cwd()
    elif cwd_has_rlsbl_config:
        # Re-scaffold: cwd is already an rlsbl project regardless of
        # whether detect_registries() finds manifest files.
        scaffold_root = Path.cwd()
    elif not cwd_has_project:
        root = find_project_root()
        if root is not None:
            scaffold_root = Path(root)
    else:
        scaffold_root = Path.cwd()

    flags = {
        "publish-mode": publish_mode,
        "auto-commit": auto_commit,
        "skip-shared": skip_shared,
        "auto-tag": auto_tag,
        "dry-run": dry_run,
    }

    ctx = create_context(scaffold_root) if scaffold_root else None

    resolved_target = target or None
    if resolved_target:
        from .targets import TARGETS
        if resolved_target not in TARGETS:
            print(
                f"Error: unknown target '{resolved_target}'. Valid: {', '.join(TARGETS.keys())}",
                file=sys.stderr,
            )
            sys.exit(1)

    regs = detect_registries()
    if not regs and ctx and ctx.config.get("targets"):
        # Plain targets (and others whose detect() returns False) won't
        # appear in detect_registries(), but the config records them.
        regs = [t.get("name") if isinstance(t, dict) else t
                for t in ctx.config["targets"]]
    # --target DECLARES a target; it never narrows the run. Scaffold's outputs
    # are whole-project -- the merged publish.yml, the managed-files registry
    # that drives orphan deletion, the publish gate's CI check regex -- so a run
    # covering a subset of the project's targets treats the rest as orphans and
    # deletes their files (and their stored merge bases). Unioning makes that
    # class impossible: every scaffold run always covers every known target.
    if resolved_target and resolved_target not in regs:
        regs = [*regs, resolved_target]
    if not regs:
        print("Error: no package.json, pyproject.toml, or go.mod found.", file=sys.stderr)
        sys.exit(1)
    # Warn when auto-detection is used without explicit config
    if ctx and "targets" not in ctx.config:
        print(
            f"Note: Auto-detected target(s): {', '.join(regs)}. "
            "Run 'rlsbl scaffold' again after reviewing .rlsbl/config.json.",
            file=sys.stderr,
        )
    if len(regs) > 1:
        from .commands.init_cmd import run_cmd_multi
        run_cmd_multi(regs, [], flags, ctx=ctx)
    else:
        from .commands.init_cmd import run_cmd
        run_cmd(regs[0], [], flags, ctx=ctx)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

# One availability result, as `_result_to_json` builds it. `note`, `error` and
# `github_count` appear only when the underlying check set them.
_CHECK_NAME_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "target": {"type": "string"},
        "status": {"type": "string"},
        "reason": {"type": ["string", "null"]},
        "structured_conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "rule": {"type": "string"},
                },
                "required": ["name", "rule"],
                "additionalProperties": False,
            },
        },
        "rule_sentences": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "exit_code": {"type": "integer"},
        "note": {"type": "string"},
        "error": {"type": "string"},
        "github_count": {"type": "integer"},
    },
    "required": [
        "name", "target", "status", "reason",
        "structured_conflicts", "rule_sentences", "exit_code",
    ],
    "additionalProperties": False,
}

# The payload is one result object for a single name+target, and an array of
# them for any other combination -- so the declaration carries both forms: the
# object keywords describe the single-result shape, `items` the array's.
_CHECK_NAME_PAYLOAD_SCHEMA = {
    "type": ["object", "array"],
    "properties": _CHECK_NAME_RESULT_SCHEMA["properties"],
    "required": _CHECK_NAME_RESULT_SCHEMA["required"],
    "additionalProperties": False,
    "items": _CHECK_NAME_RESULT_SCHEMA,
}


@app.command(name="check-name", help="Query npm, PyPI, or other registries to check whether one or more package names are available. Accepts multiple names as positional arguments and respects a configurable delay between checks.", effect="read_only", payload_schema=_CHECK_NAME_PAYLOAD_SCHEMA)
@strictcli.flag(name="target", type=str, help="Registry to query for name availability (npm, pypi, go, or github); repeatable", repeatable=True, unique=True)
@strictcli.flag(name="delay", type=str, help="Milliseconds to wait between consecutive registry API queries (default: 200)", default="200")
@effects.handler
def cmd_check_name(ctx, target, delay):
    """Query package registries to check name availability."""
    # --json is framework-owned: strictcli reserves the name at every level and
    # delivers the value on the Context.
    json = ctx.json
    # --target is required for check-name; with repeatable=True, target is a list
    targets = target if target else []
    if not targets:
        print(
            "Error: --target is required. "
            "Usage: rlsbl check-name <name> [<name2> ...] --target <npm|pypi|go|github>",
            file=sys.stderr,
        )
        sys.exit(1)
    # Validate ALL targets upfront before any network calls
    valid_targets = {"npm", "pypi", "go", "github"}
    invalid = [t for t in targets if t not in valid_targets]
    if invalid:
        print(
            f"Error: unknown target(s): {', '.join(repr(t) for t in invalid)}. "
            f"Valid: npm, pypi, go, github",
            file=sys.stderr,
        )
        sys.exit(1)
    # Names come from _variadic_args (extracted before strictcli parsing)
    names = _variadic_args
    flags = {"delay": delay, "json": json}
    from .commands.check import run_cmd
    # run_cmd no longer exits; loop every target, accumulate, and exit with the
    # highest exit code across targets (a taken/error on any target propagates).
    max_exit = 0
    payloads = []
    for tgt in targets:
        exit_code, payload = run_cmd(tgt, names, flags)
        max_exit = max(max_exit, exit_code)
        payloads.extend(payload)
    # One object for a single name+target; a JSON array otherwise. The call is
    # mode-independent -- the framework emits it only in machine mode.
    ctx.payload(payloads[0] if len(payloads) == 1 else payloads)
    sys.exit(max_exit)


# ---------------------------------------------------------------------------
# claim-name
# ---------------------------------------------------------------------------

@app.command(
    name="claim-name",
    help="Claim a name on a package registry by publishing a minimal placeholder package. Runs check-name first, then publishes if available.",
    effect="mutating",
    # The one command in rlsbl that takes a permanent, unrecoverable name on a
    # public registry -- and the command whose "dry run that published for
    # real" incident started the effects campaign.
    consequential=True,
    # The publish is irreversible on both registries rlsbl supports, so the
    # preview says why it is there rather than listing it like any other step.
    grants=[strictcli.Grant(
        "publish",
        "claiming a name publishes a real package to a public registry, and neither npm nor PyPI lets you take it back",
        strictcli.PROC_MUTATE,
    )],
)
@strictcli.flag(name="target", type=str, help="Target package registry to publish the placeholder to (npm or pypi)", default="")
@strictcli.flag(name="force-publish", type=bool, negatable=False, default=False, help="Publish even when the availability check reports the name as taken or returns an ambiguous status. Distinct from the framework's --approve-consequential, which only skips the confirmation prompt.")
@effects.handler
def cmd_claim_name(ctx, target, force_publish):
    """Claim a package name on a registry by publishing a minimal placeholder."""
    dry_run = ctx.dry_run
    if not target:
        print(
            "Error: --target is required. "
            "Usage: rlsbl claim-name <name> --target <npm|pypi>",
            file=sys.stderr,
        )
        sys.exit(1)
    valid_targets = {"npm", "pypi"}
    if target not in valid_targets:
        print(
            f"Error: unknown target: {target!r}. Valid: npm, pypi",
            file=sys.stderr,
        )
        sys.exit(1)
    names = _variadic_args
    if len(names) != 1:
        print(
            "Error: expected exactly one package name. "
            "Usage: rlsbl claim-name <name> --target <npm|pypi>",
            file=sys.stderr,
        )
        sys.exit(1)
    flags = {"dry-run": dry_run, "force-publish": force_publish}
    from .commands.claim_name import run_cmd
    run_cmd(target, names, flags)


# ---------------------------------------------------------------------------
# release edit (was: edit-release)
# ---------------------------------------------------------------------------

@release_group.command(name="edit", help="Sync the GitHub Release notes for a given version with the corresponding CHANGELOG.md entry. Defaults to the current version if none is specified. Use --dry-run to preview changes without updating GitHub.", effect="mutating")
@strictcli.arg(name="version", help="Version whose GitHub Release notes to sync (defaults to current version)", required=False)
@effects.handler
def cmd_release_edit(ctx, version=None):
    """Sync GitHub Release notes with the CHANGELOG.md entry for a version."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl release edit` must run inside a sub-project, not "
            "at the monorepo workspace root. cd into the sub-project whose "
            "release notes you want to edit."
        )
    )
    args = [version] if version else []
    flags = {"dry-run": dry_run}
    from .commands.edit_release import run_cmd
    run_cmd(args, flags, project_root=root)


# ---------------------------------------------------------------------------
# release undo (was: undo)
# ---------------------------------------------------------------------------

@release_group.command(name="undo", help="Revert a release. Without --version, reverts the latest release (deletes GitHub Release, removes git tag, reverts version bump commit). With --version, reverts a non-latest release if it is provably unpublished (probes registries for evidence, deletes GitHub Release + tag only, un-finalizes changelog).", effect="mutating", consequential=True)  # deletes the GitHub Release and the remote tag
@strictcli.flag(name="target", type=str, help="Target a specific registry for version detection (auto-detected if omitted)", default="")
@strictcli.flag(name="version", type=str, help="Version to undo (for non-latest releases that are provably unpublished)", default="")
@effects.handler
def cmd_release_undo(ctx, target, version):
    """Revert a release by deleting the GitHub Release, tag, and version commit."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .workspace import find_workspace_root
    monorepo_root = find_workspace_root(str(root))
    ctx = create_context(root, workspace_root=Path(monorepo_root) if monorepo_root else None)
    flags = {"version": version or None, "dry-run": dry_run}
    from .commands.undo import run_cmd
    run_cmd(target or None, [], flags, ctx=ctx)


# ---------------------------------------------------------------------------
# release deprecate (was: release yank)
# ---------------------------------------------------------------------------

@release_group.command(name="deprecate", help="Mark a past release as deprecated. Sets the GitHub Release pre-release flag and prepends a deprecation notice to the release notes. Use --reason to explain why and --use to suggest a replacement version.", effect="mutating", consequential=True)  # a public, consumer-visible statement about a shipped version
@strictcli.flag(name="reason", type=str, help="Human-readable explanation of why this version is being deprecated", default="")
@strictcli.flag(name="use", type=str, help="Suggest this version as a replacement in the deprecation notice", default="")
@strictcli.arg(name="version", help="Semver string of the release to deprecate, with or without v prefix (e.g. 0.9.1)")
@effects.handler
def cmd_release_deprecate(ctx, reason, use, version):
    """Mark a past release as deprecated on GitHub."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl release deprecate` must run inside a sub-project, not "
            "at the monorepo workspace root. cd into the sub-project whose "
            "release you want to deprecate."
        )
    )
    args = [version]
    flags = {
        "reason": reason or None,
        "use": use or None,
        "dry-run": dry_run,
    }
    from .commands.deprecate import run_cmd
    run_cmd(args, flags, project_root=root)


# ---------------------------------------------------------------------------
# release yank (registry-aware removal)
# ---------------------------------------------------------------------------

@release_group.command(name="yank", help="Remove a published version from package registries. Probes each configured target's registry to determine publication status, then executes registry-specific removal: npm deprecate, Go retract, or PyPI manual checklist. Also marks the GitHub Release as pre-release with a yank notice.", effect="mutating", consequential=True)  # removes a published version from public registries
@strictcli.flag(name="reason", type=str, help="Human-readable explanation of why this version is being yanked", default="")
@strictcli.flag(name="use", type=str, help="Suggest this version as a replacement in the yank notice", default="")
@strictcli.arg(name="version", help="Semver string of the release to yank, with or without v prefix (e.g. 0.9.1)")
@effects.handler
def cmd_release_yank(ctx, reason, use, version):
    """Remove a published version from package registries."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl release yank` must run inside a sub-project, not "
            "at the monorepo workspace root. cd into the sub-project whose "
            "release you want to yank."
        )
    )
    args = [version]
    flags = {
        "reason": reason or None,
        "use": use or None,
        "dry-run": dry_run,
    }
    from .commands.yank import run_cmd
    run_cmd(args, flags, project_root=root)


# ---------------------------------------------------------------------------
# release scrub
# ---------------------------------------------------------------------------

@release_group.command(
    name="scrub",
    effect="mutating",
    # Rewrites git history and force-pushes it. Every clone of the repo is
    # invalidated and the old history is gone from the remote.
    consequential=True,
    help="Scrub sensitive content from git history and update release metadata to match the rewritten commits. Supports 3 modes: match (--pattern), file (--file), or recipe (--recipe). After rewriting, remaps commit hashes in JSONL changelog files, regenerates CHANGELOG.md, force-pushes, and recreates GitHub Releases on the new tags.",
    mutex=[
        strictcli.MutexGroup(flags=[
            strictcli.Flag(name="pattern", type=str, help="Match mode: regex pattern to match against file contents (mutually exclusive with --file and --recipe)"),
            strictcli.Flag(name="file", type=str, help="File mode: path of the file to rewrite throughout history; it is replaced with its current on-disk content, or removed if absent (mutually exclusive with --pattern and --recipe; requires --from-commit)"),
            strictcli.Flag(name="recipe", type=str, help="Recipe mode: path to a scrub recipe TOML file executed via safegit scrub run; per-operation pattern/replace/mangle live inside the recipe (mutually exclusive with --pattern and --file)"),
        ]),
        strictcli.MutexGroup(flags=[
            strictcli.Flag(name="from-commit", type=str, help="SHA of the earliest commit to rewrite (all descendants are also rewritten)"),
            strictcli.Flag(name="entire-history", type=bool, negatable=False, default=False, help="Rewrite every commit in the repository from the initial commit onward (match and recipe modes only; file mode requires --from-commit)"),
        ]),
    ],
    dependencies=[
        # replace/mangle are match-mode-only knobs; file mode has no
        # replace/mangle concept and recipe mode defines them per-operation
        # inside the TOML. Enforced at parse time so the other modes never
        # have to carry (or reject) them. Their mutual exclusion cannot be a
        # MutexGroup (strictcli mutex is exactly-one-REQUIRED, which would
        # make file/recipe modes unreachable), so it is checked in the
        # handler below.
        strictcli.Requires(flag="replace", depends_on="pattern"),
        strictcli.Requires(flag="mangle", depends_on="pattern"),
    ],
)
@strictcli.flag(name="replace", type=str, help="Match mode: literal text to substitute for each match (mutually exclusive with --mangle)", default="")
@strictcli.flag(name="mangle", type=bool, negatable=False, default=False, help="Match mode: replace matched content with random ASCII of same length (mutually exclusive with --replace)")
@strictcli.flag(name="reason", type=str, help="Reason for scrubbing (required, used in commit message)", default="")
@effects.handler
def cmd_release_scrub(ctx, pattern, file, recipe, replace, mangle, from_commit, entire_history, reason):
    """Scrub sensitive content from git history and update release metadata."""
    dry_run = ctx.dry_run
    if replace and mangle:
        print("Error: --replace and --mangle are mutually exclusive", file=sys.stderr)
        sys.exit(1)
    root = _require_project_root()
    from .workspace import find_workspace_root
    monorepo_root = find_workspace_root(str(root))
    ctx = create_context(root, workspace_root=Path(monorepo_root) if monorepo_root else None)
    flags = {
        "pattern": pattern or None,
        "file": file or None,
        "recipe": recipe or None,
        "replace": replace or None,
        "mangle": mangle,
        "from-commit": from_commit or None,
        "entire-history": entire_history,
        "reason": reason or None,
        "dry-run": dry_run,
    }
    from .commands.release_scrub import run_cmd
    run_cmd(flags, ctx=ctx)


@release_group.command(
    name="reconcile",
    effect="mutating",
    # Force-pushes tags and recreates the GitHub Releases attached to them.
    consequential=True,
    help="Reconcile release metadata with a rewritten history: re-push the tags a rewrite moved and recreate the GitHub Releases attached to them. Reads safegit's rewrite journal (.git/safegit/rewrite-maps.jsonl) to determine what moved, so it works after ANY out-of-band rewrite, not just one driven by rlsbl release scrub. Fail-closed: a tag whose divergence from the remote the journal does not explain is a hard error, never a force-push.",
)
@strictcli.flag(name="push-timeout", type=int, default=0, help="Timeout in seconds for each tag push. Overrides the push_timeout config key; 0 (the default) means use push_timeout, else the shipped default.")
@effects.handler
def cmd_release_reconcile(ctx, push_timeout):
    """Re-push rewritten tags and recreate their GitHub Releases."""
    dry_run = ctx.dry_run
    quiet = ctx.quiet
    root = _require_project_root()
    from .workspace import find_workspace_root
    monorepo_root = find_workspace_root(str(root))
    ctx = create_context(
        root, workspace_root=Path(monorepo_root) if monorepo_root else None,
    )
    from .commands.release_reconcile import run_cmd
    run_cmd(
        {
            "dry-run": dry_run,
            "quiet": quiet,
            "push-timeout": push_timeout or None,
        },
        ctx=ctx,
    )


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

@app.command(name="discover", help="Search GitHub for repositories tagged with the rlsbl topic and list them. Use --mine to filter results to only your own repositories. Requires the gh CLI to be authenticated.", effect="read_only")
@strictcli.flag(name="mine", type=bool, default=False, help="Filter results to only show repositories owned by the authenticated GitHub user")
@effects.handler
def cmd_discover(ctx, mine):
    """Search GitHub for repositories tagged with the rlsbl topic."""
    flags = {"mine": mine}
    from .commands.discover import run_cmd
    run_cmd(None, [], flags)


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

# `mutating`, not `read_only`: watching a FAILED run auto-retries it with
# `gh run rerun`, which re-dispatches CI -- a real change to state on GitHub.
# While this was declared read_only the effects handle refused that argv at
# call time and the retry path swallowed the refusal, so a watch over a failed
# run silently gave up instead of re-dispatching. Not `consequential`: a rerun
# of a run that already failed is cheap and reversible, and the command is
# invoked constantly for monitoring.
@app.command(name="watch", help="Poll GitHub Actions CI workflow runs for a specific commit SHA and report pass or fail status. Defaults to HEAD if no SHA is provided. Useful after rlsbl release to monitor the publish pipeline.", effect="mutating")
@strictcli.flag(name="target", type=str, help="Registry whose CI workflow to watch (auto-detected if omitted)", default="")
@strictcli.flag(name="run-id", type=str, help="GitHub Actions workflow run ID to poll directly instead of searching by SHA", repeatable=True, unique=True)
@strictcli.arg(name="sha", help="Git commit SHA whose CI workflows to monitor (defaults to HEAD if omitted)", required=False)
@effects.handler
def cmd_watch(ctx, target, run_id, sha=None):
    """Poll GitHub Actions CI workflow runs for a commit and report status."""
    if sha and run_id:
        print("Error: cannot use both SHA and --run-id", file=sys.stderr)
        sys.exit(1)
    flags = {"run-id": run_id or []}
    args = [sha] if sha else []
    from .commands.watch import run_cmd
    run_cmd(target or None, args, flags)


# ---------------------------------------------------------------------------
# pre-push-check
# ---------------------------------------------------------------------------

@app.command(name="pre-push-check", help="Removed. This command no longer performs any check: it always exits 1 with instructions. The pre-push hook now runs `rlsbl check --tag prepush` instead, so a repo whose hook still calls pre-push-check needs `rlsbl scaffold` to regenerate it.", effect="read_only")
@effects.handler
def cmd_pre_push_check(ctx):
    """Removed command stub that directs users to re-scaffold."""
    print(
        "Error: pre-push-check was removed. Run 'rlsbl scaffold' to update your hook.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# prs
# ---------------------------------------------------------------------------

@app.command(name="prs", help="List all open pull requests for the current repository using the GitHub CLI. Shows PR number, title, author, and branch for a quick overview of pending work.", effect="read_only")
@effects.handler
def cmd_prs(ctx):
    """List open pull requests for the current repository."""
    from .commands.prs import run_cmd
    run_cmd(None, [], {})


# ---------------------------------------------------------------------------
# unreleased
# ---------------------------------------------------------------------------

# Three shapes, one declaration: the normal report carries the commit list and
# its coverage counts; the empty report carries the same with an empty list;
# a non-releasable project carries the commit COUNT instead of the list, plus
# the two flags that say why it has no changelog. `tag` is null before the
# project's first release tag.
_UNRELEASED_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "tag": {"type": ["string", "null"]},
        "commits": {
            "type": ["array", "integer"],
            "items": {
                "type": "object",
                "properties": {
                    "hash": {"type": "string"},
                    "subject": {"type": "string"},
                    "author": {"type": "string"},
                    "date": {"type": "string"},
                    "exempt": {"type": "boolean"},
                    "covered": {"type": "boolean"},
                },
                "required": [
                    "hash", "subject", "author", "date", "exempt", "covered",
                ],
                "additionalProperties": False,
            },
        },
        "coverage": {
            "type": "object",
            "properties": {
                "covered": {"type": "integer"},
                "total": {"type": "integer"},
                "exempted": {"type": "integer"},
            },
            "required": ["covered", "total", "exempted"],
            "additionalProperties": False,
        },
        "non_releasable": {"type": "boolean"},
        "dev_only": {"type": "boolean"},
    },
    "required": ["tag", "commits"],
    "additionalProperties": False,
}


@app.command(name="unreleased", help="List commits between the latest release tag and HEAD, and check whether each has a corresponding changelog entry. Outputs a coverage report in plain text or JSON to help prepare the next release.", effect="read_only", payload_schema=_UNRELEASED_PAYLOAD_SCHEMA)
@effects.handler
def cmd_unreleased(ctx):
    """List unreleased commits and their changelog coverage status."""
    # --json is framework-owned: strictcli reserves the name at every level and
    # delivers the value on the Context.
    json = ctx.json
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl unreleased` must run inside a sub-project, not "
            "at the monorepo workspace root. cd into a sub-project, or use "
            "`rlsbl monorepo status` for workspace-wide status."
        )
    )
    flags = {"json": json}
    from .commands.unreleased import run_cmd
    ctx.payload(run_cmd(None, [], flags, project_root=root))


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

@app.command(name="targets", help="List all release targets detected in the current project directory, showing which ecosystems (npm, PyPI, Go, etc.) are active based on manifest files found.", effect="read_only")
@effects.handler
def cmd_targets(ctx):
    """List all release targets detected in the current project."""
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl targets` must run inside a sub-project, not at "
            "the monorepo workspace root. cd into a sub-project."
        )
    )
    from .commands.targets_cmd import run_cmd
    run_cmd(None, [], {}, project_root=root)


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

@app.command(name="deploy", help="Run the configured deployment pipeline for the project. Supports named deploy targets and dry-run preview of what would be deployed. Branch restrictions are always enforced.", effect="mutating", consequential=True)  # ships to a live environment
@strictcli.flag(name="target", type=str, help="Registry whose deploy pipeline to run (auto-detected if omitted)", default="")
@strictcli.arg(name="target_name", help="Named deploy target from the project's deploy configuration to execute", required=False)
@effects.handler
def cmd_deploy(ctx, target, target_name=None):
    """Run the configured deployment pipeline for the project."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl deploy` must run inside a sub-project, not at "
            "the monorepo workspace root. cd into the sub-project you want "
            "to deploy."
        )
    )
    ctx = create_context(root)
    args = [target_name] if target_name else []
    flags = {"dry-run": dry_run}
    from .commands.deploy_cmd import run_cmd
    run_cmd(target or None, args, flags, ctx=ctx)


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

@app.command(
    name="commit",
    help="Commit one or more files with an Autogenerated trailer, marking the commit as machine-generated so it is automatically exempted from changelog coverage checks.",
    effect="mutating",
    dry_run_supported=False,
    dry_run_unsupported_reason=(
        "the whole command is one safegit commit, and safegit -- not rlsbl -- "
        "decides what gets staged, which hooks run and whether anything "
        "changes, so a preview could only echo the argv you just typed"
    ),
)
@strictcli.flag(name="message", short="m", type=str, help="Commit message for the autogenerated-file commit (added with Autogenerated trailer)")
@effects.handler
def cmd_commit(ctx, message):
    """Commit files with an Autogenerated trailer for changelog exemption."""
    # Files come from _variadic_args (extracted before strictcli parsing)
    files = _variadic_args
    if not files:
        print("Error: no files specified. Usage: rlsbl commit -m <message> -- <file1> [file2 ...]", file=sys.stderr)
        sys.exit(1)
    from .commands.commit_cmd import run_cmd
    run_cmd(message, files)


# ---------------------------------------------------------------------------
# changelog group
# ---------------------------------------------------------------------------

chlog = app.group("changelog", help="Structured changelog management using JSONL entries with 3 entry types (feature, fix, breaking). Add and generate CHANGELOG.md from per-commit changelog entries stored in unreleased.jsonl for precise, auditable release notes.")


@chlog.command(name="add", help="Append a structured changelog entry to the project's unreleased.jsonl file. Each entry includes a human-readable description, an entry type (feature, fix, or breaking), and optional commit hashes linking it to specific changes. The file is auto-committed by default. Use --no-user-facing to mark internal changes that should not appear in the published changelog.", effect="mutating")
@strictcli.flag(name="commits", type=str, help="Comma-separated list of commit hashes to associate with this changelog entry", default="")
@strictcli.flag(name="description", type=str, help="Human-readable description of the change, shown in the generated CHANGELOG.md", default="")
@strictcli.flag(name="type", type=str, help="Classification of the change: feature, fix, or breaking (required if user-facing)", default="")
@strictcli.flag(name="user-facing", type=bool, default=True, help="Mark this entry as user-facing (included in generated CHANGELOG.md output)")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit unreleased.jsonl after appending the entry")
@strictcli.flag(name="allow-batch", type=bool, default=False, help="Auto-create an exclusion if this entry exceeds the commit batch limit")
@effects.handler
def cmd_chlog_add(ctx, commits, description, type, user_facing, auto_commit, allow_batch):
    """Append a structured changelog entry to unreleased.jsonl."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl changelog add` must run inside a sub-project, "
            "not at the monorepo workspace root. cd into the sub-project "
            "whose changelog you want to modify."
        )
    )
    flags = {
        "commits": commits,
        "description": description,
        "type": type,
        "user-facing": user_facing,
        "auto-commit": auto_commit,
        "allow-batch": allow_batch,
        "dry-run": dry_run,
    }
    from .commands.changelog_cmd import cmd_add
    cmd_add(flags, project_root=root)



@chlog.command(name="generate", help="Compile all validated JSONL changelog entries into a formatted CHANGELOG.md file. Groups entries by type (features, fixes, breaking changes) under the appropriate version heading, preserving existing changelog content for previous releases. Use --dry-run to preview the generated Markdown output without writing to disk, which is useful for reviewing before committing.", effect="mutating")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit generated CHANGELOG.md and per-version .md files")
@effects.handler
def cmd_chlog_generate(ctx, auto_commit):
    """Generate CHANGELOG.md from all JSONL changelog files."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl changelog generate` must run inside a "
            "sub-project, not at the monorepo workspace root. cd into the "
            "sub-project whose changelog you want to generate."
        )
    )
    flags = {"dry-run": dry_run, "auto-commit": auto_commit}
    from .commands.changelog_cmd import cmd_generate
    cmd_generate(flags, project_root=root)


@chlog.command(name="amend", help="Append a changelog entry to a released version's JSONL file. Temporarily unlocks the read-only file, appends the entry, re-locks it, regenerates CHANGELOG.md, and syncs GitHub Release notes. Use --no-validate-hashes to skip hash validation for old or amended commits.", effect="mutating")
@strictcli.flag(name="version", type=str, help="Semver of the already-released version whose JSONL to amend (e.g. 0.39.0)")
@strictcli.flag(name="commits", type=str, help="Comma-separated commit hashes to associate with the amended changelog entry")
@strictcli.flag(name="id", type=str, help="Entry ID (ULID) to select the target entry for amendment", default="")
@strictcli.flag(name="description", type=str, help="Human-readable description for the amended entry in CHANGELOG.md", default="")
@strictcli.flag(name="type", type=str, help="Classification for the amended entry: feature, fix, or breaking (required if user-facing)", default="")
@strictcli.flag(name="user-facing", type=bool, default=True, help="Mark the amended entry as user-facing (included in CHANGELOG.md output)")
@strictcli.flag(name="validate-hashes", type=bool, default=True, help="Validate commit hashes via git rev-parse before appending")
@effects.handler
def cmd_chlog_amend(ctx, version, commits, id, description, type, user_facing, validate_hashes):
    """Append a changelog entry to a released version's JSONL file."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl changelog amend` must run inside a sub-project, "
            "not at the monorepo workspace root. cd into the sub-project "
            "whose changelog you want to amend."
        )
    )
    flags = {
        "version": version,
        "commits": commits,
        "id": id,
        "description": description,
        "type": type,
        "user-facing": user_facing,
        "validate-hashes": validate_hashes,
        "dry-run": dry_run,
    }
    from .commands.changelog_cmd import cmd_amend
    cmd_amend(flags, project_root=root)


@chlog.command(name="edit", help="Modify an existing changelog entry in unreleased or released JSONL files. Finds the entry by commit hash or entry ID, applies field changes (type, description, user-facing status), and rewrites the file atomically. For released files, temporarily unlocks the read-only file, regenerates CHANGELOG.md, and syncs GitHub Release notes.", effect="mutating")
@strictcli.flag(name="commits", type=str, help="Comma-separated commit hashes identifying the target entry", default="")
@strictcli.flag(name="id", type=str, help="Entry ID (ULID) identifying the target entry to edit in the JSONL file", default="")
@strictcli.flag(name="type", type=str, help="New type value (feature, fix, breaking); also disambiguates multi-entry commits", default="")
@strictcli.flag(name="description", type=str, help="Replacement description text for the matched changelog entry", default="")
@strictcli.flag(name="user-facing", type=bool, default=None, help="Set user_facing status on the matched entry (--user-facing to set true, --no-user-facing to set false)")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Automatically commit the edited JSONL changelog file to git after modification")
@effects.handler
def cmd_chlog_edit(ctx, commits, id, type, description, user_facing, auto_commit):
    """Modify an existing changelog entry by commit hash or entry ID."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl changelog edit` must run inside a sub-project, "
            "not at the monorepo workspace root. cd into the sub-project "
            "whose changelog you want to edit."
        )
    )
    flags = {
        "commits": commits,
        "id": id,
        "type": type,
        "description": description,
        "user-facing": user_facing,
        "auto-commit": auto_commit,
        "dry-run": dry_run,
    }
    from .commands.changelog_cmd import cmd_edit
    cmd_edit(flags, project_root=root)


@chlog.command(name="remap", help="Remap stale commit hashes in JSONL changelog files using a mapping of old SHAs to new SHAs. Reads the mapping from a file (--map-file), the safegit rewrite journal (--from-journal), or stdin (--stdin). At least one source is required. Auto-commits with Autogenerated trailer.", effect="mutating")
@strictcli.flag(name="map-file", type=str, help="Path to a file of 'old_sha new_sha' lines (same format as git's post-rewrite hook)", default="")
@strictcli.flag(name="from-journal", type=bool, default=False, help="Read the commit map from safegit's rewrite journal (.git/safegit/rewrite-maps.jsonl)")
@strictcli.flag(name="stdin", type=bool, default=False, help="Read the old/new SHA map from stdin (for piping from git's post-rewrite hook)")
@effects.handler
def cmd_chlog_remap(ctx, map_file, from_journal, stdin):
    """Remap stale commit hashes in JSONL files using an old-to-new SHA mapping."""
    dry_run = ctx.dry_run
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl changelog remap` must run inside a sub-project, "
            "not at the monorepo workspace root. cd into the sub-project "
            "whose changelog you want to remap."
        )
    )
    flags = {
        "map-file": map_file,
        "from-journal": from_journal,
        "stdin": stdin,
        "dry-run": dry_run,
    }
    from .commands.changelog_cmd import cmd_remap
    cmd_remap(flags, project_root=root)


# ---------------------------------------------------------------------------
# monorepo group
# ---------------------------------------------------------------------------

# Same derived-count treatment as the release group: the literal said "16
# monorepo subcommands" while 18 were registered.
mono = app.group("monorepo", help="Manage monorepo workspaces with multiple independently-versioned projects. Initialize workspaces, add or remove projects, sync CI workflows, check name availability, and analyze dependency graphs. Supports all 18 release targets in a single workspace.toml.")


@mono.command(
    name="init",
    help="Create a new monorepo workspace by generating the .rlsbl-monorepo directory and an empty workspace.toml configuration file at the current directory. This must be run at the repository root before adding individual projects with the add subcommand. Each workspace tracks multiple independently-versioned projects that share a single git repository.",
    effect="mutating",
    dry_run_supported=False,
    dry_run_unsupported_reason=(
        "it bootstraps the workspace every other command reads, and there is "
        "no workspace to preview against until it exists"
    ),
)
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Automatically commit the generated workspace.toml configuration file to git")
@effects.handler
def cmd_mono_init(ctx, auto_commit):
    """Create a new monorepo workspace with .rlsbl-monorepo/ and workspace.toml."""
    # monorepo init does NOT require a pre-existing .rlsbl/ marker --
    # it bootstraps a fresh workspace. Resolve to CWD instead of
    # _require_project_root(), but refuse if CWD is inside an existing
    # workspace (which would create a nested workspace).
    from .workspace import find_workspace_root
    root = Path.cwd()
    existing_ws = find_workspace_root(str(root))
    if existing_ws is not None:
        print(
            f"Error: CWD is inside an existing workspace at {existing_ws}. "
            f"Cannot create a nested workspace.",
            file=sys.stderr,
        )
        sys.exit(1)
    from .utils import find_project_root
    existing_project = find_project_root(str(root))
    if existing_project is not None and Path(existing_project) != root:
        print(f"Error: CWD is inside existing project at {existing_project}. "
              f"Use monorepo init from {existing_project} to convert it.", file=sys.stderr)
        sys.exit(1)
    from .commands.monorepo import _cmd_init
    _cmd_init({"auto-commit": auto_commit}, project_root=root)


@mono.command(name="add", help="Register a project directory in the monorepo workspace.toml configuration. The path argument specifies the project's location relative to the repo root. Supports 6 optional settings: display name, target registry, glob patterns for change detection, subtree remote URL, inter-project dependencies, and a library flag to mark shared code packages.", effect="mutating")
@strictcli.flag(name="name", type=str, help="Display name for the project in workspace.toml (defaults to directory name)", default="")
@strictcli.flag(name="target", type=str, help="Registry this project publishes to (e.g. npm, pypi, go)", default="")
@strictcli.flag(name="watch", type=str, help="Comma-separated glob patterns for change detection in CI workflows", default="")
@strictcli.flag(name="subtree-remote", type=str, help="Git remote URL for split-publishing this project as a standalone repo", default="")
@strictcli.flag(name="depends-on", type=str, help="Comma-separated names of workspace projects this project depends on", default="")
@strictcli.flag(name="library", type=str, help="Mark as a shared library consumed by other workspace projects (true/false)", default="")
@strictcli.flag(name="dev-only", type=str, help="Mark as a dev-only leaf node excluded from the dependency boundary guardrail (true/false)", default="")
@strictcli.flag(name="releasable", type=str, help="Releasable group this project belongs to (name of a [[releasables]] entry, or 'false' to opt out of versioning)", default="")
@strictcli.flag(name="registry-name", type=str, help="Package registry identity for this project (used verbatim for name checks; overrides prefix/suffix)", default="")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit workspace.toml and trigger scaffold/sync commits")
@strictcli.arg(name="path", help="Relative path from the repo root to the project directory to register")
@effects.handler
def cmd_mono_add(ctx, name, target, watch, subtree_remote, depends_on, library, dev_only, releasable, registry_name, auto_commit, path):
    """Register a project directory in the monorepo workspace.toml."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    flags = {}
    if name:
        flags["name"] = name
    if target:
        flags["target"] = target
    if watch:
        flags["watch"] = watch
    if subtree_remote:
        flags["subtree-remote"] = subtree_remote
    if depends_on:
        flags["depends-on"] = depends_on
    if library:
        flags["library"] = library
    if dev_only:
        flags["dev_only"] = dev_only
    if releasable:
        flags["releasable"] = releasable
    if registry_name:
        flags["registry-name"] = registry_name
    if not auto_commit:
        flags["auto-commit"] = False
    from .commands.monorepo import _cmd_add
    _cmd_add([path], flags, project_root=root, dry_run=dry_run)


@mono.command(
    name="remove",
    help="Unregister a project from the monorepo workspace.toml by its path. This removes the project entry from the workspace configuration file but does not delete any files, directories, or git history on disk. The project's code remains intact and can be re-added later with the add subcommand if needed.",
    effect="mutating",
    dry_run_supported=False,
    dry_run_unsupported_reason=(
        "the whole edit is deleting the one workspace.toml entry you just "
        "named, and a preview of it would restate the path back to you"
    ),
)
@strictcli.arg(name="path", help="Relative path from the repo root of the project to unregister from workspace.toml")
@effects.handler
def cmd_mono_remove(ctx, path):
    """Unregister a project from the monorepo workspace.toml by path."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_remove
    _cmd_remove([path], {}, project_root=root)


@mono.command(name="list", help="Display all projects registered in the monorepo workspace.toml file. For each project, shows the project name, relative path from the repo root, target registry for publishing, and any configured options such as watch patterns, subtree remotes, inter-project dependencies, and whether the project is marked as a library.", effect="read_only")
@effects.handler
def cmd_mono_list(ctx):
    """Display all projects registered in the monorepo workspace."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_list
    _cmd_list({}, project_root=root)


@mono.command(name="sync", help="Inline every project's CI jobs into a single generated ci-router.yml (and publish jobs into publish.yml) in the shared .github/workflows directory at the repository root. Jobs are inlined rather than routed via reusable-workflow calls because GitHub rejects workflows that reference 20 or more reusable workflows. Stale per-project workflow copies at the root are removed via saferm.", effect="mutating")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit merged workflow files in .github/workflows/")
@effects.handler
def cmd_mono_sync(ctx, auto_commit):
    """Inline per-project CI jobs into shared workflow files at the repo root."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_sync
    _cmd_sync({"auto-commit": auto_commit}, project_root=root)


@mono.command(name="status", help="Show the current version, last release tag, and changelog coverage for every project in the monorepo workspace. Coverage is the real JSONL figure -- the commits since the project's last tag, scoped to the project and minus the exempt ones, rendered covered/tracked with an (N exempted) suffix, or 'no changelog' when the project has no changes directory. Provides a quick overview of which projects have pending changes and are ready for their next release.", effect="read_only")
@effects.handler
def cmd_mono_status(ctx):
    """Show version, last tag, and changelog coverage for all workspace projects."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_status
    _cmd_status({}, project_root=root)


@mono.command(name="check-names", help="Check package name availability on a target registry for all projects in the monorepo workspace. Queries the registry API for each project name and reports whether it is available or already taken. Supports optional prefix and suffix arguments to test naming conventions like scoped packages, with a configurable delay between registry queries to avoid rate limiting.", effect="read_only")
@strictcli.flag(name="target", type=str, help="Registry to query for name availability across all workspace projects (npm, pypi, go, or github)")
@strictcli.flag(name="prefix", type=str, help="String to prepend to each project name before checking availability", default="")
@strictcli.flag(name="suffix", type=str, help="String to append to each project name before checking availability", default="")
@strictcli.flag(name="delay", type=str, help="Milliseconds to wait between consecutive registry API queries (default: 200)", default="200")
@effects.handler
def cmd_mono_check_names(ctx, target, prefix, suffix, delay):
    """Check package name availability across registries for all workspace projects."""
    root = _require_project_root()
    flags = {"target": target, "prefix": prefix, "suffix": suffix, "delay": delay}
    from .commands.monorepo import _cmd_check_names
    _cmd_check_names(_variadic_args, flags, project_root=root)


@mono.command(name="outdated", help="Scan all projects in the monorepo workspace for intra-workspace dependencies that reference older versions than what is currently available in the workspace. Lists each outdated dependency with the referenced version and the latest available version, helping identify which downstream projects need a version bump after upstream releases.", effect="read_only")
@effects.handler
def cmd_mono_outdated(ctx):
    """Scan workspace projects for outdated intra-workspace dependency versions."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_outdated
    _cmd_outdated({}, project_root=root)


@mono.command(name="snapshot", help="Regenerate the committed JSON artifact at .rlsbl-monorepo/snapshot.json summarizing all packages, versions, dependencies, and graph structure, and commit it. Verifying without regenerating is a separate command, `rlsbl monorepo snapshot-check`. Under --dry-run the artifact is computed but neither written nor committed, and the preview names both steps.", effect="mutating")
@effects.handler
def cmd_mono_snapshot(ctx):
    """Regenerate and commit the workspace snapshot.json artifact."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_snapshot
    _cmd_snapshot({}, project_root=root)


@mono.command(name="snapshot-check", help="Verify that .rlsbl-monorepo/snapshot.json matches the workspace it describes, without regenerating it. Exits 1 when the artifact is stale or missing. This is the read-only half of the former `monorepo snapshot --check` flag; `rlsbl monorepo snapshot` is the half that writes.", effect="read_only")
@effects.handler
def cmd_mono_snapshot_check(ctx):
    """Verify the workspace snapshot.json artifact is up to date."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_snapshot_check
    _cmd_snapshot_check({}, project_root=root)


@mono.command(name="mirror", help="Reconcile a monorepo project's subtree mirror toward its desired state. The mirror is a tool-owned, derived artifact: it observes the remote, then converges it to exactly one scaffold commit atop the current deterministic subtree split, force-pushing (with lease) as the routine write. A tripwire refuses to touch a mirror carrying foreign (hand-authored) commits. Use --dry-run to print a plan (converged, behind, scaffold-missing, contract-violated, or virgin) without writing.", effect="mutating", consequential=True)  # force-pushes the mirror remote
@strictcli.arg(name="project", help="Name of the workspace project to split and push as a standalone mirror repo")
@effects.handler
def cmd_mono_mirror(ctx, project):
    """Reconcile a workspace project's subtree mirror with the remote."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .commands.monorepo import _cmd_mirror
    _cmd_mirror({"project": project, "dry-run": dry_run}, project_root=root)


# The graph as the workspace scan produced it: one entry per package, keyed by
# package name, plus one edge per dependency. The DOT and text renderings are
# built from exactly this data.
_MONO_GRAPH_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "packages": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "deps": {"type": "array", "items": {"type": "string"}},
                    "rdeps": {"type": "array", "items": {"type": "string"}},
                    "targets": {"type": "array", "items": {"type": "string"}},
                    "version": {"type": ["string", "null"]},
                    "dev_only": {"type": "boolean"},
                    "library": {"type": "boolean"},
                    "has_runtime_dependents": {"type": "boolean"},
                    "is_leaf": {"type": "boolean"},
                },
                "required": [
                    "deps", "rdeps", "targets", "version", "dev_only",
                    "library", "has_runtime_dependents", "is_leaf",
                ],
                "additionalProperties": False,
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "type": {"type": "string"},
                    "constraint": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": ["from", "to", "type", "constraint", "scope"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["packages", "edges"],
    "additionalProperties": False,
}


@mono.command(name="graph", help="Export the monorepo dependency graph as DOT (Graphviz) or an indented text tree; the framework-owned --json yields the same graph as a structured document. Supports filtering by a root package (transitive deps) or reverse package (transitive rdeps), with optional depth limiting. Use --output to write the rendering to a file instead of stdout.", effect="mutating", payload_schema=_MONO_GRAPH_PAYLOAD_SCHEMA)
@strictcli.flag(name="format", type=str, help="Rendering for the dependency graph: dot (Graphviz) or text", default="text")
@strictcli.flag(name="output", type=str, help="File path to write the graph output to instead of printing to stdout", default="")
@strictcli.flag(name="root", type=str, help="Filter to show only transitive dependencies reachable from this package", default="")
@strictcli.flag(name="reverse", type=str, help="Filter to show only transitive reverse dependencies of this package", default="")
@strictcli.flag(name="depth", type=int, help="Maximum number of dependency hops to traverse from the root or reverse node")
@effects.handler
def cmd_mono_graph(ctx, format, output, root, reverse, depth=None):
    """Export the monorepo dependency graph as DOT or a text tree."""
    flags = {"format": format, "json": ctx.json}
    if output:
        flags["output"] = output
    if root:
        flags["root"] = root
    if reverse:
        flags["reverse"] = reverse
    if depth is not None:
        flags["depth"] = depth
    root = _require_project_root()
    from .commands.monorepo import _cmd_graph
    graph_data = _cmd_graph(flags, project_root=root)
    if graph_data is not None:
        ctx.payload(graph_data)


# The impact report, exactly as `_compute_impact` builds it. `input` is the
# comma-joined set of packages the analysis started from.
_MONO_IMPACT_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "input": {"type": "string"},
        "direct_dependents": {"type": "array", "items": {"type": "string"}},
        "transitive_dependents": {"type": "array", "items": {"type": "string"}},
        "test_scope": {"type": "array", "items": {"type": "string"}},
        "release_candidates": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "input", "direct_dependents", "transitive_dependents",
        "test_scope", "release_candidates",
    ],
    "additionalProperties": False,
}


@mono.command(name="impact", help="Analyze the impact of changes to a package, file, or git diff range on the monorepo dependency graph. Shows direct and transitive dependents, test scope, and release candidates as a human report, or as a structured document under the framework-owned --json. Supports package names, file paths, and --since for git-based change detection.", effect="read_only", payload_schema=_MONO_IMPACT_PAYLOAD_SCHEMA)
@strictcli.flag(name="depth", type=int, help="Maximum number of dependency hops to traverse when computing transitive impact")
@strictcli.flag(name="since", type=str, help="Git ref to diff against HEAD (e.g. HEAD~3, v1.0.0)", default="")
@effects.handler
def cmd_mono_impact(ctx, depth=None, since=""):
    """Analyze the impact of changes on the monorepo dependency graph."""
    args = _variadic_args
    flags = {"json": ctx.json}
    if depth is not None:
        flags["depth"] = depth
    if since:
        flags["since"] = since
    root = _require_project_root()
    from .commands.monorepo import _cmd_impact
    impact_data = _cmd_impact(args, flags, project_root=root)
    if impact_data is not None:
        ctx.payload(impact_data)


mono_release = mono.group("release", help="Release commands for monorepo workspaces. Provides 3 subcommands: run (batch release), init (scaffold release file), and order (topological release order).")


@mono_release.command(
    name="run",
    effect="mutating",
    # `release run` once per package, in one uninterruptible sweep.
    consequential=True,
    help="Execute a batch release of multiple monorepo packages in topological order. Reads package configurations from .rlsbl-monorepo/releases/unreleased.toml. Each package is released sequentially using the single-package release flow, with leaves (no dependencies) released first. Supports --dry-run, --approve-consequential and --allow-dirty flags.",
)
@strictcli.flag(name="push-timeout", type=int, default=0, help="Timeout in seconds for each git push. Overrides the push_timeout config key; 0 (the default) means use push_timeout, else the shipped default.")
@strictcli.flag(name="ci-timeout", type=int, default=0, help="Timeout in seconds for the release CI gate (the wait for CI to conclude on the pushed release candidate). Overrides the ci_timeout config key; 0 (the default) means use ci_timeout, else the shipped default.")
@strictcli.flag(name="check-timeout", type=int, default=0, help="Timeout in seconds for each preflight check subprocess (tests, lint, external checks). Overrides the check_timeout config key; 0 (the default) means use check_timeout, else the shipped default.")
@strictcli.flag(name="hook-timeout", type=int, default=0, help="Timeout in seconds for each release hook. Overrides the hook_timeout config key; 0 (the default) means use hook_timeout, else no timeout.")
@strictcli.flag(name="watch", type=bool, help="After batch release, automatically watch CI runs to completion (--no-watch to skip)")
@strictcli.flag(name="allow-dirty", type=bool, help="Skip the clean working tree check and allow releasing with uncommitted changes")
@effects.handler
def cmd_mono_release_run(ctx, allow_dirty, watch, push_timeout, ci_timeout, check_timeout, hook_timeout):
    """Execute a batch release of multiple monorepo packages in topological order."""
    dry_run = ctx.dry_run
    quiet = ctx.quiet
    from .commands.release.shared import build_release_flags
    flags = build_release_flags(dry_run, quiet, allow_dirty, watch=watch,
                                push_timeout=push_timeout or None,
                                ci_timeout=ci_timeout or None,
                                check_timeout=check_timeout or None,
                                hook_timeout=hook_timeout or None)
    root = _require_project_root()
    from .commands.monorepo import _cmd_batch_release
    _cmd_batch_release(flags, project_root=root)


@mono_release.command(
    name="init",
    help="Scaffold a batch release file for all workspace projects by auto-detecting each project's release targets and generating per-package configuration sections. Creates .rlsbl-monorepo/releases/unreleased.toml with a [packages.<name>] section for each non-dev-node project, pre-populated with bump type, description, and include lists. Packages with no unreleased commits since their last tag are rendered as commented-out sections.",
    effect="mutating",
    dry_run_supported=False,
    dry_run_unsupported_reason=(
        "the command scaffolds a batch release file whose point is that you "
        "edit it before releasing; printing it instead of writing it leaves "
        "nothing to edit"
    ),
)
@strictcli.flag(name="packages", type=str, help="Comma-separated package names to include (default: all)", default="")
@effects.handler
def cmd_mono_release_init(ctx, packages):
    """Scaffold a batch release file for all workspace projects."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_batch_release_init
    _cmd_batch_release_init(project_root=root, packages=packages or None)


@mono_release.command(name="order", help="Compute and display the topological release order for all projects in the monorepo workspace based on their declared depends-on relationships. Projects with no dependencies are listed first, followed by projects that depend on them, ensuring each project is released only after its dependencies. Detects and reports circular dependency errors.", effect="read_only")
@effects.handler
def cmd_mono_release_order(ctx):
    """Display the topological release order for workspace projects."""
    root = _require_project_root()
    from .commands.monorepo import _cmd_release_order
    _cmd_release_order({}, project_root=root)


# NOT consequential: the filter-repo rewrite runs on a throwaway clone at
# target_path, so nothing anyone has ever pulled is rewritten. Nothing is
# pushed, no registry or public artifact is touched, and the only mutation to
# the repo you are standing in is one removed workspace.toml entry. Everything
# it does is local and trivially undone.
@mono.command(name="extract", help="Extract a package from the monorepo into a new standalone repository. Clones the monorepo, runs git filter-repo to keep only the package's history, migrates changelog entries, creates .rlsbl/ config in the new repo, and removes the project from workspace.toml.", effect="mutating")
@strictcli.arg(name="target_path", help="Filesystem path where the new standalone repository will be created")
@strictcli.arg(name="package_name", help="Name of the package as defined in workspace.toml to extract into a standalone repo")
@effects.handler
def cmd_mono_extract(ctx, package_name, target_path):
    """Extract a package from the monorepo into a new standalone repository."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    from .commands.monorepo import cmd_extract
    try:
        result = cmd_extract(ws_root, package_name, target_path, dry_run=dry_run)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if dry_run:
        print(f"Would extract '{result['package_name']}' (path: {result['package_path']}) to {result['target_path']}")
    else:
        print(f"Extracted '{result['package_name']}' to {result['target_path']}")
        print(f"  Changelog: {result['entries_migrated']} entries in {result['files_written']} files")


@mono.command(name="absorb", help="Absorb an external repository as a package in the monorepo. Rewrites the source's history to live under the destination path, fetch-merges it (preserving full history with rewritten paths), imports its version tags under the monorepo tag scheme, and remaps its JSONL changelog hashes to the new commits.", effect="mutating", consequential=True)  # rewrites another repo's history and merges it in
@strictcli.flag(name="name", type=str, help="Workspace project name for the absorbed package (default: basename of the destination path)", default="")
@strictcli.flag(name="registry-name", type=str, help="Package registry identity recorded in workspace.toml (used verbatim for name checks)", default="")
@strictcli.flag(name="releasable", type=str, help="Releasable group to assign the absorbed package to", default="")
@strictcli.arg(name="dest_path", help="Destination directory (and workspace path) the source repo's history is rewritten under")
@strictcli.arg(name="source_repo", help="Filesystem path to the external git repository to absorb")
@effects.handler
def cmd_mono_absorb(ctx, name, registry_name, releasable, source_repo, dest_path):
    """Absorb an external repository as a monorepo package with history rewriting."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    from .commands.monorepo import cmd_absorb
    try:
        result = cmd_absorb(
            ws_root, source_repo, dest_path,
            name=name or None,
            registry_name=registry_name,
            releasable_name=releasable or None,
            dry_run=dry_run,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if dry_run:
        tags = ", ".join(result["tags_to_import"]) or "none"
        print(f"Would absorb '{result['name']}' from {result['source_path']} into {result['dest_path']}")
        print(f"  Version tags to import: {tags}")
    else:
        print(f"Absorbed '{result['name']}' from {result['source_path']} into {result['dest_path']}")
        print(f"  Changelog: {result['entries_migrated']} entries migrated")
        print(f"  Tags imported: {', '.join(result['tags_imported']) or 'none'}")
        skipped = result.get("skipped_tags") or []
        if skipped:
            print(f"  Skipped {len(skipped)} non-version tag(s): {', '.join(skipped)}")


# NOT consequential, for the same reason as `monorepo extract`: the rewrite is
# confined to a throwaway clone, nothing is pushed, and the source workspace
# loses only the extracted members' workspace.toml entries.
@mono.command(name="extract-releasable", help="Extract all member packages of a releasable into a new repository. If the releasable has one member, creates a single-project repo. If it has multiple members, creates a new monorepo with workspace.toml. Migrates changelog entries for each member and removes all extracted projects from the source workspace.", effect="mutating")
@strictcli.arg(name="target_path", help="Filesystem path where the new repository will be created")
@strictcli.arg(name="releasable_name", help="Name of the releasable group in workspace.toml to extract")
@effects.handler
def cmd_mono_extract_releasable(ctx, releasable_name, target_path):
    """Extract all member packages of a releasable into a new repository."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    from .commands.monorepo import cmd_extract_releasable
    try:
        result = cmd_extract_releasable(ws_root, releasable_name, target_path, dry_run=dry_run)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if dry_run:
        members = ", ".join(result["member_packages"])
        kind = "monorepo" if result["is_monorepo"] else "single-project repo"
        print(f"Would extract releasable '{result['releasable_name']}' ({kind}) to {result['target_path']}")
        print(f"  Members: {members}")
    else:
        members = ", ".join(result["member_packages"])
        kind = "monorepo" if result["is_monorepo"] else "single-project repo"
        print(f"Extracted releasable '{result['releasable_name']}' ({kind}) to {result['target_path']}")
        print(f"  Members: {members}")
        print(f"  Changelog: {result['entries_migrated']} entries in {result['files_written']} files")


@mono.command(name="cleanup", help="Remove per-package release-state residue from releasable member packages: .rlsbl/changes/, .rlsbl/releases/, .rlsbl/bases/, .rlsbl/lint/, .rlsbl/version, per-package CHANGELOG.md, and .rlsbl/config.json when identical to the releasable-level config. Per-package hooks/ directories are preserved (live feature), and members whose path is the workspace root are exempt. Deletions go through saferm (audit trail, recoverable) and are committed automatically. Requires an explicit-mode workspace ([[releasables]] in workspace.toml). Detect residue first with `rlsbl check --name releasable-residue`.", effect="mutating")
@effects.handler
def cmd_mono_cleanup(ctx):
    """Remove per-package release-state residue from releasable members."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .workspace import find_workspace_root, is_explicit_mode
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    if not is_explicit_mode(ws_root):
        print(
            "Error: cleanup only applies to explicit-mode workspaces "
            "([[releasables]] in workspace.toml).",
            file=sys.stderr,
        )
        sys.exit(1)
    from .releasable_cleanup import run_cleanup_command
    run_cleanup_command(ws_root, dry_run=dry_run)


@mono.command(name="migrate-releasable", help="Migrate a releasable from per-package release state to the releasable model. Detects current state, consolidates per-package changelogs and versions into the releasable directory, creates a releasable-format migration tag, and removes orphaned per-package .rlsbl/changes/ and .rlsbl/releases/ directories. Requires the workspace to be in explicit mode (with [[releasables]] in workspace.toml).", effect="mutating")
@strictcli.arg(name="releasable_name", help="Name of the releasable group in workspace.toml to migrate")
@effects.handler
def cmd_mono_migrate_releasable(ctx, releasable_name):
    """Migrate a releasable from per-package state to the releasable model."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    from .releasable_migration import cmd_migrate_releasable
    try:
        result = cmd_migrate_releasable(
            ws_root, releasable_name, dry_run=dry_run,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        members = ", ".join(result.get("members", []))
        print(f"Would migrate releasable '{releasable_name}'")
        print(f"  Members: {members}")
        print(f"  Tag format: {result.get('tag_format', 'N/A')}")
        # Summarize state
        state = result.get("state", {})
        for proj in state.get("projects", []):
            if proj.get("has_changelog"):
                print(f"  {proj['name']}: {proj['unreleased_entry_count']} unreleased entries")
    else:
        changelogs = result.get("changelogs") or {}
        versions = result.get("versions") or {}
        tag = result.get("tag") or {}
        cleanup = result.get("cleanup") or []

        print(f"Migrated releasable '{releasable_name}'")
        print(f"  Changelogs merged: {changelogs.get('entries_merged', 0)} entries from {', '.join(changelogs.get('source_projects', []))}")
        print(f"  Version: {versions.get('version', 'N/A')} ({versions.get('status', 'N/A')})")
        if tag.get("tag"):
            print(f"  Tag created: {tag['tag']} (from {tag.get('source_tag', 'N/A')})")
        elif tag.get("status") == "no_tags":
            print("  Tag: no per-package tags found, skipped")
        if tag.get("skipped_members"):
            print(f"  Skipped members (no scoped tag): {', '.join(tag['skipped_members'])}")
        if cleanup:
            print(f"  Cleaned up: {len(cleanup)} per-package directories")


@mono.command(name="rename-releasable", help="Rename a releasable group. Rewrites the [[releasables]] name and every member's releasable field in workspace.toml (preserving comments), moves the state directory, drops the stale changelog validation cache, re-runs monorepo sync, and commits it all as one commit. When tag_format contains {name}, a boundary alias tag for the current version is created at the old tag's commit and pushed; historical releases stay under the old prefix. Idempotent: re-running heals a crash between the commit and the tag push.", effect="mutating")
@strictcli.arg(name="new_name", help="New name for the releasable group in workspace.toml and state directories")
@strictcli.arg(name="old_name", help="Current name of the releasable group in workspace.toml")
@effects.handler
def cmd_mono_rename_releasable(ctx, old_name, new_name):
    """Rename a releasable group, updating workspace.toml and state directories."""
    dry_run = ctx.dry_run
    root = _require_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    from .commands.monorepo.releasable_rename import rename_releasable
    try:
        result = rename_releasable(
            ws_root, old_name, new_name, dry_run=dry_run,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"Would rename releasable '{old_name}' -> '{new_name}'")
        for step in result.get("plan", []):
            print(f"  {step}")
        if result.get("note"):
            print(result["note"])
        return

    if result.get("aborted"):
        print("Aborted.")
        return

    print(f"Renamed releasable '{old_name}' -> '{new_name}'")
    if result.get("members"):
        print(f"  Members updated: {', '.join(result['members'])}")
    tag = result.get("tag")
    if tag:
        status = tag.get("status")
        if status == "created":
            print(f"  Alias tag pushed: {tag['tag']}")
        elif status == "already_done":
            print(f"  Alias tag already present: {tag['tag']}")
        elif status == "no_source_tag":
            print(f"  No current-version tag '{tag['old_tag']}' to alias; skipped tag step")
    elif result.get("name_only"):
        print("  Name-only rename (tag_format has no {name}); no alias tag needed")
    if result.get("note"):
        print(result["note"])


# ---------------------------------------------------------------------------
# dev group
# ---------------------------------------------------------------------------

dev = app.group("dev", help="Developer utilities for locally working with rlsbl projects, including editable installs that mirror the project's release target (pypi -> uv tool install -e, npm -> npm link, go -> go install).")


@dev.command(name="install", help="Install the project locally for development by running each detected target's own install command. --target is required and names the install mode. --target global is supported by 7 targets: pypi (uv tool install -e), npm (npm link), go, deno, zig, swift, and hex. --target venv installs into the project's local environment instead and covers pypi, npm, deno, and hex; other targets are skipped with a reason. --uninstall reverses a previous install on pypi, npm, and deno. In monorepo mode, pair with --all, --include, or --exclude.", effect="mutating")
@strictcli.flag(name="all", type=bool, default=False, help="In monorepo mode, install every project in the workspace")
@strictcli.flag(name="include", type=str, help="In monorepo mode, comma-separated project names to include", default="")
@strictcli.flag(name="exclude", type=str, help="In monorepo mode, comma-separated project names to exclude", default="")
@strictcli.flag(name="uninstall", type=bool, default=False, help="Reverse a previous dev install (where supported by the target)")
@strictcli.flag(name="target", type=str, choices=["global", "venv"], help="Install mode: 'global' installs as a global tool/symlink, 'venv' installs into the project's local environment only (e.g. uv sync, npm install). Required -- there is no default mode.")
@effects.handler
def cmd_dev_install(ctx, all, include, exclude, uninstall, target):
    """Install the project locally using the detected target's editable install."""
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl dev install` must run inside a sub-project, not "
            "at the monorepo workspace root. cd into a sub-project and "
            "re-run; from within any sub-project, --all/--include/--exclude "
            "select which workspace projects to install."
        )
    )
    flags = {
        "all": all,
        "include": include or None,
        "exclude": exclude or None,
        "uninstall": uninstall,
        "target": target,
    }
    from .commands.dev import run_install
    rc = run_install(flags, project_root=root)
    if rc:
        sys.exit(rc)


@dev.command(name="sync", help="Overlay local editable checkouts of sibling projects onto this project's locked environment. Reads dev-sources.toml.local-only for overlay entries, runs uv sync --inexact excluding overlaid packages, then uv pip install -e per entry. Requires UV_NO_SYNC=1 in the environment to prevent bare uv run from reverting overlays.", effect="mutating")
@effects.handler
def cmd_dev_sync(ctx):
    """Overlay local editable checkouts of sibling projects onto the locked environment."""
    from .commands.dev_sync import OVERRIDES_FILENAME, run_sync
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl dev sync` must run inside a sub-project, not at "
            "the monorepo workspace root. cd into the sub-project (its "
            f"{OVERRIDES_FILENAME} lives at the sub-project root) and re-run."
        )
    )
    rc = run_sync(root)
    if rc:
        sys.exit(rc)


@dev.command(name="status", help="Report the state of local dev-sync overlays: for each package recorded in the dev-overlays sentinel, show its declared editable checkout path and version alongside the venv's actual install (editable at the expected path, WIPED back to a registry wheel, or missing entirely). Exits 1 if any overlay drifted so scripts and pre-run guards can detect a silent wipe by a bare uv sync or uv run; exits 0 when all overlays are intact or none are declared.", effect="read_only")
@effects.handler
def cmd_dev_status(ctx):
    """Report the state of local dev-sync overlays and detect drift."""
    from .commands.dev_sync import SENTINEL_FILENAME, run_status
    root = _require_sub_project_root(
        workspace_root_guidance=(
            "Error: `rlsbl dev status` must run inside a sub-project, not at "
            "the monorepo workspace root. cd into the sub-project (its "
            f"{SENTINEL_FILENAME} lives at the sub-project root) and re-run."
        )
    )
    rc = run_status(root)
    if rc:
        sys.exit(rc)


# ---------------------------------------------------------------------------
# Derived help counts
# ---------------------------------------------------------------------------

def _count_leaf_commands(groups):
    """Recursively count leaf commands across nested group registries."""
    return sum(
        len(group.commands) + _count_leaf_commands(group._groups)
        for group in groups.values()
    )


# Appended after ALL command/group registrations above so the counts are
# derived from the live registry and can never drift (they used to be
# hand-maintained literals that went stale). strictcli renders app.help
# lazily (at --help / schema-dump time), so mutating it here is safe.
# Register new commands above this block, or test_app_help_counts.py fails.
_total_commands = len(app._commands) + _count_leaf_commands(app._groups)
app.help = (
    f"{app.help} Ships {_total_commands} commands organized into "
    f"{len(app._commands)} top-level commands and {len(app._groups)} "
    f"command groups ({', '.join(app._groups)})."
)


def _append_subcommand_sentence(group, noun):
    """Append a derived 'Provides N subcommands: ...' sentence to a group help.

    Same rationale as the app-level sentence: the release group's literal said
    "9 subcommands" and omitted `reconcile`, and the monorepo group's said 16
    when 18 were registered. Both are now recounted from the live registry.
    """
    names = list(group.commands)
    sentence = (
        f" Provides {len(names)} {noun}: {', '.join(names)}."
        if names else ""
    )
    subgroups = list(group._groups)
    if subgroups:
        label = "subgroup" if len(subgroups) == 1 else "subgroups"
        sentence += f" Plus {len(subgroups)} {label}: {', '.join(subgroups)}."
    group.help = f"{group.help}{sentence}"


_append_subcommand_sentence(release_group, "subcommands")
_append_subcommand_sentence(mono, "monorepo subcommands")


# ---------------------------------------------------------------------------
# Variadic arg extraction
# ---------------------------------------------------------------------------

def _extract_variadic_args():
    """Extract variadic positional args from sys.argv for commands that need them.

    For 'check', 'commit', and 'monorepo check-names', removes positional args
    from sys.argv and returns them. This must be called before app.run() since
    strictcli does not support variadic positional args.
    """
    argv = sys.argv[1:]
    if not argv:
        return []

    cmd = argv[0]

    if cmd == "commit":
        # For 'commit', everything after '--' is a file path.
        # Flags (-m/--message) are before '--'.
        new_argv = [sys.argv[0], "commit"]
        positionals = []
        found_separator = False
        i = 1  # index into argv (after 'commit')
        value_flags = {"message"}
        short_value_flags = {"m"}
        while i < len(argv):
            tok = argv[i]
            if found_separator:
                positionals.append(tok)
            elif tok == "--":
                found_separator = True
            elif tok.startswith("--"):
                key = tok[2:]
                if "=" in key:
                    new_argv.append(tok)
                elif key in value_flags and i + 1 < len(argv):
                    new_argv.append(tok)
                    new_argv.append(argv[i + 1])
                    i += 1
                else:
                    new_argv.append(tok)
            elif tok.startswith("-") and len(tok) == 2:
                short_key = tok[1:]
                if short_key in short_value_flags and i + 1 < len(argv):
                    new_argv.append(tok)
                    new_argv.append(argv[i + 1])
                    i += 1
                else:
                    new_argv.append(tok)
            else:
                # Positional without -- separator (shouldn't happen, but
                # treat as file for robustness)
                positionals.append(tok)
            i += 1
        sys.argv = new_argv
        return positionals

    if cmd == "check-name":
        # Everything after 'check-name' that doesn't start with '-' and isn't
        # a value following a flag is a positional name arg.
        positionals = []
        new_argv = [sys.argv[0], "check-name"]
        i = 1  # index into argv (after 'check-name')
        value_flags = {"target", "delay"}
        while i < len(argv):
            tok = argv[i]
            if tok.startswith("--"):
                key = tok[2:]
                if "=" in key:
                    new_argv.append(tok)
                elif key in value_flags and i + 1 < len(argv):
                    new_argv.append(tok)
                    new_argv.append(argv[i + 1])
                    i += 1
                else:
                    new_argv.append(tok)
            elif tok.startswith("-"):
                new_argv.append(tok)
            else:
                positionals.append(tok)
            i += 1
        sys.argv = new_argv
        return positionals

    if cmd == "claim-name":
        positionals = []
        new_argv = [sys.argv[0], "claim-name"]
        i = 1  # index into argv (after 'claim-name')
        value_flags = {"target"}
        while i < len(argv):
            tok = argv[i]
            if tok.startswith("--"):
                key = tok[2:]
                if "=" in key:
                    new_argv.append(tok)
                elif key in value_flags and i + 1 < len(argv):
                    new_argv.append(tok)
                    new_argv.append(argv[i + 1])
                    i += 1
                else:
                    new_argv.append(tok)
            elif tok.startswith("-"):
                new_argv.append(tok)
            else:
                positionals.append(tok)
            i += 1
        sys.argv = new_argv
        return positionals

    if cmd == "monorepo" and len(argv) > 1 and argv[1] == "check-names":
        # Same pattern for monorepo check-names
        positionals = []
        new_argv = [sys.argv[0], "monorepo", "check-names"]
        i = 2  # index into argv (after 'monorepo check-names')
        value_flags = {"target", "prefix", "suffix", "delay"}
        while i < len(argv):
            tok = argv[i]
            if tok.startswith("--"):
                key = tok[2:]
                if "=" in key:
                    new_argv.append(tok)
                elif key in value_flags and i + 1 < len(argv):
                    new_argv.append(tok)
                    new_argv.append(argv[i + 1])
                    i += 1
                else:
                    new_argv.append(tok)
            elif tok.startswith("-"):
                new_argv.append(tok)
            else:
                positionals.append(tok)
            i += 1
        sys.argv = new_argv
        return positionals

    if cmd == "monorepo" and len(argv) > 1 and argv[1] == "impact":
        positionals = []
        new_argv = [sys.argv[0], "monorepo", "impact"]
        i = 2  # index into argv (after 'monorepo impact')
        value_flags = {"since", "depth"}
        while i < len(argv):
            tok = argv[i]
            if tok.startswith("--"):
                key = tok[2:]
                if "=" in key:
                    new_argv.append(tok)
                elif key in value_flags and i + 1 < len(argv):
                    new_argv.append(tok)
                    new_argv.append(argv[i + 1])
                    i += 1
                else:
                    new_argv.append(tok)
            elif tok.startswith("-"):
                new_argv.append(tok)
            else:
                positionals.append(tok)
            i += 1
        sys.argv = new_argv
        return positionals

    return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _enable_line_buffering():
    """Put stdout and stderr in line-buffered mode for the whole process.

    Python block-buffers stdout as soon as it is not a TTY (a log file, a pipe,
    a CI step) while stderr stays unbuffered. A release then emitted its
    progress in 8KB gulps that landed AFTER the stderr warnings they belong
    next to -- and a run killed mid-block lost its last lines entirely, which
    is exactly the output an operator needs when a release dies. Line
    buffering makes the redirected transcript match the terminal one.

    Set once, here, so no individual command has to remember to flush.

    A stream substituted by a test harness (pytest capture, a StringIO) has no
    ``reconfigure`` -- there is no buffering knob on it to set, so there is
    nothing to do rather than something to fail on.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def main():
    """CLI entry point: extract variadic args and run the strictcli app."""
    global _variadic_args
    _enable_line_buffering()
    # strictcli recognizes --dry-run/--approve-consequential/--quiet/--verbose
    # anywhere in argv, so `rlsbl release run --approve-consequential` reaches
    # the framework as written and needs no argv rewriting here.
    _variadic_args = _extract_variadic_args()
    try:
        app.run()
    except subprocess.CalledProcessError as e:
        if e.stderr and e.stderr.strip():
            print(f"Error: {e.stderr.strip()}", file=sys.stderr)
        else:
            print(f"Error: Command failed: {e.cmd[0]}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
