"""rlsbl: Release orchestration and project scaffolding for npm, PyPI, Go, Cargo, Deno, Zig, Swift, Hex, Docker, Maven, and more, automating version bumps, changelogs, tags, GitHub Releases, and CI/CD."""

import os
import subprocess
import sys
from pathlib import Path

import strictcli

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
    cargo, deno, hex, maven, etc.) are auto-detected when no config exists.
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


def _require_sub_project_root():
    """Find the project root, resolving to the sub-project in monorepo mode.

    In standalone mode: same as _require_project_root().
    In monorepo mode: uses resolve_project() to find which sub-project CWD is in,
    returns the sub-project path instead of the monorepo root.

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
    help="Release orchestration and project scaffolding CLI. Automates version bumping, changelog validation, tagging, GitHub Releases, and CI/CD scaffolding across 18 release targets (npm, PyPI, Go, Cargo, Deno, Zig, Swift, Hex, Docker, Maven, Dart, Flutter, and more). Ships 33 commands organized into 13 top-level commands and 4 command groups (release, changelog, monorepo, dev).",
    flags=[
        strictcli.Flag(name="dry-run", type=bool, default=False, help="Preview changes without applying them"),
        strictcli.Flag(name="yes", type=bool, short="y", default=False, help="Skip confirmation prompts"),
        strictcli.Flag(name="quiet", type=bool, default=False, help="Suppress non-essential output"),
    ],
    checks_path=Path(__file__).parent / "data" / "checks.toml",
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


app.set_check_context(_check_context_factory)

# Register check implementations on the strictcli check system.
from .checks import register_checks
register_checks(app)


# ---------------------------------------------------------------------------
# release group
# ---------------------------------------------------------------------------

release_group = app.group("release", help="Release orchestration commands. Provides 8 subcommands covering the full release lifecycle: run, resume, init, retry, edit, undo, yank, and scrub.")


@release_group.command(
    name="run",
    help="Bump version, validate the JSONL changelog, run tests and lint, commit, tag, push, and create a GitHub Release. Reads the bump type (patch, minor, major, or hotfix) and target selection from .rlsbl/releases/unreleased.toml, which can be scaffolded with rlsbl release init. Supports dry-run preview, non-interactive mode with --yes, and --allow-dirty to skip the clean working tree check.",
)
@strictcli.flag(name="watch", type=bool, help="After release, automatically watch CI runs to completion (--no-watch to skip)")
@strictcli.flag(name="allow-dirty", type=bool, help="Skip the clean working tree check and allow releasing with uncommitted changes")
@strictcli.flag(name="bump", type=str, help="Bump type: patch, minor, major, hotfix, prerelease. Skips the release file.", default="")
@strictcli.flag(name="description", type=str, help="Release description (required with --bump)", default="")
@strictcli.flag(name="preid", type=str, help="Pre-release identifier: alpha, beta, rc, stable. Only valid with --bump.", default="")
def cmd_release_run(dry_run, yes, quiet, allow_dirty, watch, bump, description, preid, **_kwargs):
    root = _require_sub_project_root()

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
        flags = build_release_flags(dry_run, yes, quiet, allow_dirty, watch=watch)
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
    flags = build_release_flags(dry_run, yes, quiet, allow_dirty, watch=watch)
    from .commands.release import run_cmd
    run_cmd(release_config, flags, ctx=ctx)


@release_group.command(
    name="resume",
    help="Resume a previously failed release from where it left off. Reads the in-progress state file (.rlsbl/releases/in-progress.json, or .rlsbl-monorepo/releasables/<name>/releases/in-progress.json for releasable releases), validates that the current branch matches the saved state, and re-enters the release flow, skipping already-completed steps.",
)
@strictcli.flag(name="watch", type=bool, help="After release, automatically watch CI runs to completion (--no-watch to skip)")
def cmd_release_resume(dry_run, yes, quiet, watch, **_kwargs):
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
    current_branch = get_current_branch()
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
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", pre_release_sha, "HEAD"],
                capture_output=True,
            )
            if result.returncode != 0:
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
    flags = build_release_flags(dry_run, yes, quiet, allow_dirty=False, watch=watch)
    from .commands.release import resume_cmd
    resume_cmd(saved, flags, ctx=ctx)


@release_group.command(name="init", help="Scaffold a .rlsbl/releases/unreleased.toml file by auto-detecting project targets. The generated file contains a default bump type (patch), an include list of all detected targets, and per-target configuration sections for Flutter targets.")
def cmd_release_init(**_kwargs):
    root = _require_sub_project_root()
    from .commands.release_init import run_cmd
    run_cmd(project_root=root)


@release_group.command(
    name="retry",
    help="Dispatch CI/CD workflows for a completed release via gh workflow run. Reads the dispatch list and ref from .rlsbl/releases/retry.toml, which is auto-scaffolded with sensible defaults if missing. Verifies the GitHub Release exists before dispatching. Each workflow in the dispatch list is triggered against the configured ref (defaults to the release tag).",
)
@strictcli.flag(name="watch", type=bool, help="After retry, automatically watch CI runs to completion (--no-watch to skip)")
def cmd_release_retry(dry_run, yes, quiet, watch, **_kwargs):
    root = _require_sub_project_root()

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
            if os.path.exists(retry_path):
                os.remove(retry_path)
            print(
                "Hint: `rlsbl release retry` is for dispatching CI after a "
                "completed release. To re-run a failed release, use "
                "`rlsbl release run`.",
                file=sys.stderr,
            )
            sys.exit(1)

    flags = {
        "dry-run": dry_run,
        "yes": yes,
        "quiet": quiet,
        "watch": bool(watch),
    }
    from .commands.release_retry import run_cmd
    run_cmd(retry_config, flags, project_root=root)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command(name="status", help="Display the current project version, branch, last release tag, unreleased commit count, and changelog coverage. Outputs plain text by default or structured JSON with the --json flag.")
@strictcli.flag(name="target", type=str, help="Target a specific registry (auto-detected if omitted)", default="")
@strictcli.flag(name="json", type=bool, default=False, help="Output version, branch, tag, and coverage as machine-readable JSON")
@strictcli.flag(name="registry", type=bool, default=False, help="Query the package registry for the latest published version")
def cmd_status(target, json, registry, **_kwargs):
    root = _require_sub_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    ctx = create_context(root, workspace_root=Path(ws_root) if ws_root else None)
    target_name = _resolve_target(target or None)
    flags = {"json": json, "registry": registry}
    from .commands.status import run_cmd
    run_cmd(target_name, [], flags, ctx=ctx)


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------

@app.command(name="scaffold", help="Generate or update CI/CD workflows, git hooks, changelog, and license files. Safe to run repeatedly -- merges template changes with your customizations. Use --force-overwrite to overwrite all files.")
@strictcli.flag(name="target", type=str, help="Target a specific registry (auto-detected if omitted)", default="")
@strictcli.flag(name="force-overwrite", type=bool, default=False, help="Overwrite all scaffold-managed files, discarding any user customizations")
@strictcli.flag(name="private", type=bool, default=False, help="Generate workflows without publish steps, suitable for private repositories")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit scaffolded files after writing them to disk")
@strictcli.flag(name="skip-shared", type=bool, default=False, help="Skip processing of shared workflow templates across targets")
@strictcli.flag(name="auto-tag", type=bool, default=True, help="Add or update the rlsbl GitHub topic tag on this invocation")
def cmd_scaffold(target, force_overwrite, private, auto_commit, skip_shared, auto_tag, dry_run, **_kwargs):
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
        "force": force_overwrite,
        "private": private,
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
        from .commands.init_cmd import run_cmd
        run_cmd(resolved_target, [], flags, ctx=ctx)
    else:
        regs = detect_registries()
        if not regs and ctx and ctx.config.get("targets"):
            # Plain targets (and others whose detect() returns False) won't
            # appear in detect_registries(), but the config records them.
            regs = list(ctx.config["targets"])
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

@app.command(name="check-name", help="Query npm, PyPI, or other registries to check whether one or more package names are available. Accepts multiple names as positional arguments and respects a configurable delay between checks.")
@strictcli.flag(name="target", type=str, help="Registry to query for name availability (npm, pypi, go, or github); repeatable", repeatable=True, unique=True)
@strictcli.flag(name="delay", type=str, help="Milliseconds to wait between consecutive registry API queries (default: 200)", default="200")
def cmd_check_name(target, delay, **_kwargs):
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
    flags = {"delay": delay}
    from .commands.check import run_cmd
    for tgt in targets:
        run_cmd(tgt, names, flags)


# ---------------------------------------------------------------------------
# claim-name
# ---------------------------------------------------------------------------

@app.command(name="claim-name", help="Claim a name on a package registry by publishing a minimal placeholder package. Runs check-name first, then publishes if available.")
@strictcli.flag(name="target", type=str, help="Target package registry to publish the placeholder to (npm or pypi)", default="")
def cmd_claim_name(target, yes, **_kwargs):
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
    flags = {"yes": yes}
    from .commands.claim_name import run_cmd
    run_cmd(target, names, flags)


# ---------------------------------------------------------------------------
# release edit (was: edit-release)
# ---------------------------------------------------------------------------

@release_group.command(name="edit", help="Sync the GitHub Release notes for a given version with the corresponding CHANGELOG.md entry. Defaults to the current version if none is specified. Use --dry-run to preview changes without updating GitHub.")
@strictcli.arg(name="version", help="Version whose GitHub Release notes to sync (defaults to current version)", required=False)
def cmd_release_edit(dry_run, version=None, **_kwargs):
    root = _require_sub_project_root()
    args = [version] if version else []
    flags = {"dry-run": dry_run}
    from .commands.edit_release import run_cmd
    run_cmd(args, flags, project_root=root)


# ---------------------------------------------------------------------------
# release undo (was: undo)
# ---------------------------------------------------------------------------

@release_group.command(name="undo", help="Revert the most recent release by deleting the GitHub Release, removing the git tag from local and remote, and reverting the version bump commit. Requires a manual git push afterward to finalize.")
@strictcli.flag(name="target", type=str, help="Target a specific registry for version detection (auto-detected if omitted)", default="")
def cmd_release_undo(target, yes, **_kwargs):
    root = _require_project_root()
    from .workspace import find_workspace_root
    monorepo_root = find_workspace_root(str(root))
    ctx = create_context(root, workspace_root=Path(monorepo_root) if monorepo_root else None)
    flags = {"yes": yes}
    from .commands.undo import run_cmd
    run_cmd(target or None, [], flags, ctx=ctx)


# ---------------------------------------------------------------------------
# release yank (was: yank)
# ---------------------------------------------------------------------------

@release_group.command(name="yank", help="Mark a past release as deprecated (soft yank) or delete it (hard yank). Soft yank marks the GitHub Release as pre-release and prepends a deprecation notice. Hard yank deletes the release entirely while preserving the git tag.")
@strictcli.flag(name="reason", type=str, help="Human-readable explanation of why this version is being yanked", default="")
@strictcli.flag(name="use", type=str, help="Suggest this version as a replacement in the deprecation notice", default="")
@strictcli.flag(name="hard", type=bool, help="Delete the release instead of marking as pre-release")
@strictcli.arg(name="version", help="Semver string of the release to yank, with or without v prefix (e.g. 0.9.1)")
def cmd_release_yank(reason, use, hard, dry_run, yes, version, **_kwargs):
    root = _require_sub_project_root()
    args = [version]
    flags = {
        "reason": reason or None,
        "use": use or None,
        "hard": hard,
        "dry-run": dry_run,
        "yes": yes,
    }
    from .commands.yank import run_cmd
    run_cmd(args, flags, project_root=root)


# ---------------------------------------------------------------------------
# release scrub
# ---------------------------------------------------------------------------

@release_group.command(
    name="scrub",
    help="Scrub sensitive content from git history and update release metadata to match the rewritten commits. Wraps safegit scrub in one of three modes: match (--pattern with --replace or --mangle), file (--file, which replaces the file throughout history with its current on-disk content or removes it if absent; requires --from-commit), or recipe (--recipe, a scrub recipe TOML executed via safegit scrub run). Afterwards remaps commit hashes in all JSONL changelog files, regenerates CHANGELOG.md, force-pushes the rewritten history, and recreates GitHub Releases on the new tags. A scrub-result.json file records the SHA mapping for recovery if any post-rewrite step fails.",
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
def cmd_release_scrub(pattern, file, recipe, replace, mangle, from_commit, entire_history, reason, dry_run, yes, **_kwargs):
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
        "yes": yes,
    }
    from .commands.release_scrub import run_cmd
    run_cmd(flags, ctx=ctx)


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

@app.command(name="discover", help="Search GitHub for repositories tagged with the rlsbl topic and list them. Use --mine to filter results to only your own repositories. Requires the gh CLI to be authenticated.")
@strictcli.flag(name="mine", type=bool, default=False, help="Filter results to only show repositories owned by the authenticated GitHub user")
def cmd_discover(mine, **_kwargs):
    flags = {"mine": mine}
    from .commands.discover import run_cmd
    run_cmd(None, [], flags)


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

@app.command(name="watch", help="Poll GitHub Actions CI workflow runs for a specific commit SHA and report pass or fail status. Defaults to HEAD if no SHA is provided. Useful after rlsbl release to monitor the publish pipeline.")
@strictcli.flag(name="target", type=str, help="Registry whose CI workflow to watch (auto-detected if omitted)", default="")
@strictcli.flag(name="run-id", type=str, help="GitHub Actions workflow run ID to poll directly instead of searching by SHA", repeatable=True, unique=True)
@strictcli.arg(name="sha", help="Git commit SHA whose CI workflows to monitor (defaults to HEAD if omitted)", required=False)
def cmd_watch(target, run_id, sha=None, **_kwargs):
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

@app.command(name="pre-push-check", help="Verify that CHANGELOG.md contains an entry matching the current project version. Designed to run as a git pre-push hook to prevent pushing releases without documented changes.")
def cmd_pre_push_check(**_kwargs):
    print(
        "Error: pre-push-check was removed. Run 'rlsbl scaffold' to update your hook.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# prs
# ---------------------------------------------------------------------------

@app.command(name="prs", help="List all open pull requests for the current repository using the GitHub CLI. Shows PR number, title, author, and branch for a quick overview of pending work.")
def cmd_prs(**_kwargs):
    from .commands.prs import run_cmd
    run_cmd(None, [], {})


# ---------------------------------------------------------------------------
# unreleased
# ---------------------------------------------------------------------------

@app.command(name="unreleased", help="List commits between the latest release tag and HEAD, and check whether each has a corresponding changelog entry. Outputs a coverage report in plain text or JSON to help prepare the next release.")
@strictcli.flag(name="json", type=bool, default=False, help="Output the unreleased commit list and coverage status as machine-readable JSON")
def cmd_unreleased(json, **_kwargs):
    root = _require_sub_project_root()
    flags = {"json": json}
    from .commands.unreleased import run_cmd
    run_cmd(None, [], flags, project_root=root)


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

@app.command(name="targets", help="List all release targets detected in the current project directory, showing which ecosystems (npm, PyPI, Go, Cargo, etc.) are active based on manifest files found.")
def cmd_targets(**_kwargs):
    root = _require_sub_project_root()
    from .commands.targets_cmd import run_cmd
    run_cmd(None, [], {}, project_root=root)


# ---------------------------------------------------------------------------
# record-gif
# ---------------------------------------------------------------------------

@app.command(name="record-gif", help="Record an animated GIF demo of rlsbl commands using the vhs terminal recorder. Configurable width, height, font size, and duration for consistent, reproducible demo recordings.")
@strictcli.flag(name="width", type=str, help="Width of the recorded GIF in pixels (default: 1200)", default="1200")
@strictcli.flag(name="height", type=str, help="Height of the recorded GIF in pixels (default: 600)", default="600")
@strictcli.flag(name="font-size", type=str, help="Terminal font size in pixels for the recording (default: 24)", default="24")
@strictcli.flag(name="duration", type=str, help="Total recording duration in seconds before auto-stop (default: 10)", default="10")
def cmd_record_gif(width, height, font_size, duration, **_kwargs):
    root = _require_sub_project_root()
    ctx = create_context(root)
    flags = {"width": width, "height": height, "font-size": font_size, "duration": duration}
    from .commands.record_gif import run_cmd
    run_cmd(None, [], flags, ctx=ctx)


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------

@app.command(name="migrate", help="Run pending configuration migrations to update .rlsbl config files to the latest schema. Use --dry-run to preview changes without applying, or --status to see which migrations are pending.")
@strictcli.flag(name="status", type=bool, default=False, help="Display which migrations are pending and which have already been applied")
def cmd_migrate(dry_run, status, **_kwargs):
    flags = {"dry-run": dry_run, "status": status}
    from .commands.migrate import run_cmd
    run_cmd(None, [], flags)


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

@app.command(name="deploy", help="Run the configured deployment pipeline for the project. Supports named deploy targets and dry-run preview of what would be deployed. Branch restrictions are always enforced.")
@strictcli.flag(name="target", type=str, help="Registry whose deploy pipeline to run (auto-detected if omitted)", default="")
@strictcli.arg(name="target_name", help="Named deploy target from the project's deploy configuration to execute", required=False)
def cmd_deploy(target, dry_run, target_name=None, **_kwargs):
    root = _require_sub_project_root()
    ctx = create_context(root)
    args = [target_name] if target_name else []
    flags = {"dry-run": dry_run}
    from .commands.deploy_cmd import run_cmd
    run_cmd(target or None, args, flags, ctx=ctx)


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

@app.command(name="commit", help="Commit one or more files with an Autogenerated trailer, marking the commit as machine-generated so it is automatically exempted from changelog coverage checks.")
@strictcli.flag(name="message", short="m", type=str, help="Commit message for the autogenerated-file commit (added with Autogenerated trailer)")
def cmd_commit(message, **_kwargs):
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

chlog = app.group("changelog", help="Structured changelog management using JSONL entries. Add and generate CHANGELOG.md from per-commit changelog entries stored in unreleased.jsonl for precise, auditable release notes.")


@chlog.command(name="add", help="Append a structured changelog entry to the project's unreleased.jsonl file. Each entry includes a human-readable description, an entry type (feature, fix, or breaking), and optional commit hashes linking it to specific changes. The file is auto-committed by default. Use --no-user-facing to mark internal changes that should not appear in the published changelog.")
@strictcli.flag(name="commits", type=str, help="Comma-separated list of commit hashes to associate with this changelog entry", default="")
@strictcli.flag(name="description", type=str, help="Human-readable description of the change, shown in the generated CHANGELOG.md", default="")
@strictcli.flag(name="type", type=str, help="Classification of the change: feature, fix, or breaking (required if user-facing)", default="")
@strictcli.flag(name="user-facing", type=bool, default=True, help="Mark this entry as user-facing (included in generated CHANGELOG.md output)")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit unreleased.jsonl after appending the entry")
@strictcli.flag(name="allow-batch", type=bool, default=False, help="Auto-create an exclusion if this entry exceeds the commit batch limit")
def cmd_chlog_add(commits, description, type, user_facing, auto_commit, allow_batch, dry_run, **_kwargs):
    root = _require_sub_project_root()
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



@chlog.command(name="generate", help="Compile all validated JSONL changelog entries into a formatted CHANGELOG.md file. Groups entries by type (features, fixes, breaking changes) under the appropriate version heading, preserving existing changelog content for previous releases. Use --dry-run to preview the generated Markdown output without writing to disk, which is useful for reviewing before committing.")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit generated CHANGELOG.md and per-version .md files")
def cmd_chlog_generate(dry_run, auto_commit, **_kwargs):
    root = _require_sub_project_root()
    flags = {"dry-run": dry_run, "auto-commit": auto_commit}
    from .commands.changelog_cmd import cmd_generate
    cmd_generate(flags, project_root=root)


@chlog.command(name="amend", help="Append a changelog entry to a released version's JSONL file. Temporarily unlocks the read-only file, appends the entry, re-locks it, regenerates CHANGELOG.md, and syncs GitHub Release notes. Use --no-validate-hashes to skip hash validation for old or amended commits.")
@strictcli.flag(name="version", type=str, help="Semver of the already-released version whose JSONL to amend (e.g. 0.39.0)")
@strictcli.flag(name="commits", type=str, help="Comma-separated commit hashes to associate with the amended changelog entry")
@strictcli.flag(name="description", type=str, help="Human-readable description for the amended entry in CHANGELOG.md", default="")
@strictcli.flag(name="type", type=str, help="Classification for the amended entry: feature, fix, or breaking (required if user-facing)", default="")
@strictcli.flag(name="user-facing", type=bool, default=True, help="Mark the amended entry as user-facing (included in CHANGELOG.md output)")
@strictcli.flag(name="validate-hashes", type=bool, default=True, help="Validate commit hashes via git rev-parse before appending")
def cmd_chlog_amend(version, commits, description, type, user_facing, validate_hashes, dry_run, **_kwargs):
    root = _require_sub_project_root()
    flags = {
        "version": version,
        "commits": commits,
        "description": description,
        "type": type,
        "user-facing": user_facing,
        "validate-hashes": validate_hashes,
        "dry-run": dry_run,
    }
    from .commands.changelog_cmd import cmd_amend
    cmd_amend(flags, project_root=root)


@chlog.command(name="edit", help="Modify an existing changelog entry in unreleased or released JSONL files. Finds the entry by commit hash, applies field changes (type, description, user-facing status), and rewrites the file atomically. For released files, temporarily unlocks the read-only file, regenerates CHANGELOG.md, and syncs GitHub Release notes.")
@strictcli.flag(name="commits", type=str, help="Comma-separated commit hashes identifying the target entry")
@strictcli.flag(name="type", type=str, help="New type value (feature, fix, breaking); also disambiguates multi-entry commits", default="")
@strictcli.flag(name="description", type=str, help="Replacement description text for the matched changelog entry", default="")
@strictcli.flag(name="user-facing", type=bool, default=None, help="Set user_facing status on the matched entry (--user-facing to set true, --no-user-facing to set false)")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit the edited JSONL file")
def cmd_chlog_edit(commits, type, description, user_facing, auto_commit, dry_run, **_kwargs):
    root = _require_sub_project_root()
    flags = {
        "commits": commits,
        "type": type,
        "description": description,
        "user-facing": user_facing,
        "auto-commit": auto_commit,
        "dry-run": dry_run,
    }
    from .commands.changelog_cmd import cmd_edit
    cmd_edit(flags, project_root=root)


# ---------------------------------------------------------------------------
# monorepo group
# ---------------------------------------------------------------------------

mono = app.group("monorepo", help="Manage monorepo workspaces with multiple independently-versioned projects. Initialize workspaces, add or remove projects, sync CI workflows, check name availability, and analyze dependency graphs. Provides 16 monorepo subcommands plus a release subgroup, and supports all 18 release targets in a single workspace.toml.")


@mono.command(name="init", help="Create a new monorepo workspace by generating the .rlsbl-monorepo directory and an empty workspace.toml configuration file at the current directory. This must be run at the repository root before adding individual projects with the add subcommand. Each workspace tracks multiple independently-versioned projects that share a single git repository.")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit workspace.toml after creating it")
def cmd_mono_init(auto_commit, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_init
    _cmd_init({"auto-commit": auto_commit}, project_root=root)


@mono.command(name="add", help="Register a project directory in the monorepo workspace.toml configuration. The path argument specifies the project's location relative to the repo root. Optionally set a display name, target registry for publishing, glob patterns for change detection, a subtree remote URL for split publishing, inter-project dependencies, and a library flag to mark shared code packages.")
@strictcli.flag(name="name", type=str, help="Display name for the project in workspace.toml (defaults to directory name)", default="")
@strictcli.flag(name="target", type=str, help="Registry this project publishes to (e.g. npm, pypi, go, cargo)", default="")
@strictcli.flag(name="watch", type=str, help="Comma-separated glob patterns for change detection in CI workflows", default="")
@strictcli.flag(name="subtree-remote", type=str, help="Git remote URL for split-publishing this project as a standalone repo", default="")
@strictcli.flag(name="depends-on", type=str, help="Comma-separated names of workspace projects this project depends on", default="")
@strictcli.flag(name="library", type=str, help="Mark as a shared library consumed by other workspace projects (true/false)", default="")
@strictcli.flag(name="dev-only", type=str, help="Mark as a dev-only leaf node excluded from the dependency boundary guardrail (true/false)", default="")
@strictcli.flag(name="releasable", type=str, help="Releasable group this project belongs to (name of a [[releasables]] entry, or 'false' to opt out of versioning)", default="")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit workspace.toml and trigger scaffold/sync commits")
@strictcli.arg(name="path", help="Relative path from the repo root to the project directory to register")
def cmd_mono_add(name, target, watch, subtree_remote, depends_on, library, dev_only, releasable, auto_commit, path, **_kwargs):
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
    if not auto_commit:
        flags["auto-commit"] = False
    from .commands.monorepo import _cmd_add
    _cmd_add([path], flags, project_root=root)


@mono.command(name="remove", help="Unregister a project from the monorepo workspace.toml by its path. This removes the project entry from the workspace configuration file but does not delete any files, directories, or git history on disk. The project's code remains intact and can be re-added later with the add subcommand if needed.")
@strictcli.arg(name="path", help="Relative path from the repo root of the project to unregister from workspace.toml")
def cmd_mono_remove(path, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_remove
    _cmd_remove([path], {}, project_root=root)


@mono.command(name="list", help="Display all projects registered in the monorepo workspace.toml file. For each project, shows the project name, relative path from the repo root, target registry for publishing, and any configured options such as watch patterns, subtree remotes, inter-project dependencies, and whether the project is marked as a library.")
def cmd_mono_list(**_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_list
    _cmd_list({}, project_root=root)


@mono.command(name="sync", help="Copy and merge CI workflow files from each project's individual scaffold into the shared .github/workflows directory at the repository root. This ensures that every project in the workspace has its publish and test pipelines properly configured as GitHub Actions workflows, even when projects use different target registries or have custom workflow steps.")
@strictcli.flag(name="auto-commit", type=bool, default=True, help="Auto-commit merged workflow files in .github/workflows/")
def cmd_mono_sync(auto_commit, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_sync
    _cmd_sync({"auto-commit": auto_commit}, project_root=root)


@mono.command(name="status", help="Show the current version, last release tag, and number of unreleased commits for every project in the monorepo workspace. Provides a quick overview of which projects have pending changes and are ready for their next release. Projects with zero unreleased commits are shown as up-to-date.")
def cmd_mono_status(**_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_status
    _cmd_status({}, project_root=root)


@mono.command(name="check-names", help="Check package name availability on a target registry for all projects in the monorepo workspace. Queries the registry API for each project name and reports whether it is available or already taken. Supports optional prefix and suffix arguments to test naming conventions like scoped packages, with a configurable delay between registry queries to avoid rate limiting.")
@strictcli.flag(name="target", type=str, help="Registry to query for name availability across all workspace projects (npm, pypi, go, or github)")
@strictcli.flag(name="prefix", type=str, help="String to prepend to each project name before checking availability", default="")
@strictcli.flag(name="suffix", type=str, help="String to append to each project name before checking availability", default="")
@strictcli.flag(name="delay", type=str, help="Milliseconds to wait between consecutive registry API queries (default: 200)", default="200")
def cmd_mono_check_names(target, prefix, suffix, delay, **_kwargs):
    root = _require_project_root()
    flags = {"target": target, "prefix": prefix, "suffix": suffix, "delay": delay}
    from .commands.monorepo import _cmd_check_names
    _cmd_check_names(_variadic_args, flags, project_root=root)


@mono.command(name="outdated", help="Scan all projects in the monorepo workspace for intra-workspace dependencies that reference older versions than what is currently available in the workspace. Lists each outdated dependency with the referenced version and the latest available version, helping identify which downstream projects need a version bump after upstream releases.")
def cmd_mono_outdated(**_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_outdated
    _cmd_outdated({}, project_root=root)


@mono.command(name="snapshot", help="Generate a committed JSON artifact at .rlsbl-monorepo/snapshot.json summarizing all packages, versions, dependencies, and graph structure. Use --check to verify the snapshot is up-to-date without regenerating it (exits 1 if stale).")
@strictcli.flag(name="check", type=bool, default=False, help="Verify snapshot.json is up-to-date (exit 1 if stale)")
def cmd_mono_snapshot(check, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_snapshot
    _cmd_snapshot({"check": check}, project_root=root)


@mono.command(name="mirror", help="Initialize a subtree mirror repository for a monorepo project by performing a full git subtree split of the project's history, pushing the extracted tree to the configured subtree_remote URL, cloning the resulting standalone mirror repository, running rlsbl scaffold to generate CI workflows for independent publishing, and pushing the scaffolded mirror to its remote.")
@strictcli.arg(name="project", help="Name of the workspace project to split and push as a standalone mirror repo")
def cmd_mono_mirror(project, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_mirror
    _cmd_mirror({"project": project}, project_root=root)


@mono.command(name="graph", help="Export the monorepo dependency graph in JSON, DOT (Graphviz), or indented text tree format. Supports filtering by a root package (transitive deps) or reverse package (transitive rdeps), with optional depth limiting. Use --output to write to a file instead of stdout.")
@strictcli.flag(name="format", type=str, help="Serialization format for the dependency graph: json, dot (Graphviz), or text", default="json")
@strictcli.flag(name="output", type=str, help="File path to write the graph output to instead of printing to stdout", default="")
@strictcli.flag(name="root", type=str, help="Filter to show only transitive dependencies reachable from this package", default="")
@strictcli.flag(name="reverse", type=str, help="Filter to show only transitive reverse dependencies of this package", default="")
@strictcli.flag(name="depth", type=int, help="Maximum number of dependency hops to traverse from the root or reverse node")
def cmd_mono_graph(format, output, root, reverse, depth=None, **_kwargs):
    flags = {"format": format}
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
    _cmd_graph(flags, project_root=root)


@mono.command(name="impact", help="Analyze the impact of changes to a package, file, or git diff range on the monorepo dependency graph. Shows direct and transitive dependents, test scope, and release candidates. Supports package names, file paths, and --since for git-based change detection.")
@strictcli.flag(name="format", type=str, help="Output serialization format for the impact report: json or text (default: text)", default="text")
@strictcli.flag(name="depth", type=int, help="Maximum number of dependency hops to traverse when computing transitive impact")
@strictcli.flag(name="since", type=str, help="Git ref to diff against HEAD (e.g. HEAD~3, v1.0.0)", default="")
def cmd_mono_impact(format, depth=None, since="", **_kwargs):
    args = _variadic_args
    flags = {"format": format}
    if depth is not None:
        flags["depth"] = depth
    if since:
        flags["since"] = since
    root = _require_project_root()
    from .commands.monorepo import _cmd_impact
    _cmd_impact(args, flags, project_root=root)


mono_release = mono.group("release", help="Release commands for monorepo workspaces. Provides 3 subcommands: run (batch release), init (scaffold release file), and order (topological release order).")


@mono_release.command(
    name="run",
    help="Execute a batch release of multiple monorepo packages in topological order. Reads package configurations from .rlsbl-monorepo/releases/unreleased.toml. Each package is released sequentially using the single-package release flow, with leaves (no dependencies) released first. Supports --dry-run, --yes, --allow-dirty flags.",
)
@strictcli.flag(name="watch", type=bool, help="After batch release, automatically watch CI runs to completion (--no-watch to skip)")
@strictcli.flag(name="allow-dirty", type=bool, help="Skip the clean working tree check and allow releasing with uncommitted changes")
def cmd_mono_release_run(dry_run, yes, quiet, allow_dirty, watch, **_kwargs):
    from .commands.release.shared import build_release_flags
    flags = build_release_flags(dry_run, yes, quiet, allow_dirty, watch=watch)
    root = _require_project_root()
    from .commands.monorepo import _cmd_batch_release
    _cmd_batch_release(flags, project_root=root)


@mono_release.command(name="init", help="Scaffold a batch release file for all workspace projects by auto-detecting each project's release targets and generating per-package configuration sections. Creates .rlsbl-monorepo/releases/unreleased.toml with a [packages.<name>] section for each non-dev-node project, pre-populated with bump type, description, and include lists. Packages with no unreleased commits since their last tag are rendered as commented-out sections.")
@strictcli.flag(name="packages", type=str, help="Comma-separated package names to include (default: all)", default="")
def cmd_mono_release_init(packages, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_batch_release_init
    _cmd_batch_release_init(project_root=root, packages=packages or None)


@mono_release.command(name="order", help="Compute and display the topological release order for all projects in the monorepo workspace based on their declared depends-on relationships. Projects with no dependencies are listed first, followed by projects that depend on them, ensuring each project is released only after its dependencies. Detects and reports circular dependency errors.")
def cmd_mono_release_order(**_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_release_order
    _cmd_release_order({}, project_root=root)


@mono.command(name="extract", help="Extract a package from the monorepo into a new standalone repository. Clones the monorepo, runs git filter-repo to keep only the package's history, migrates changelog entries, creates .rlsbl/ config in the new repo, and removes the project from workspace.toml.")
@strictcli.arg(name="package_name", help="Name of the package in workspace.toml to extract")
@strictcli.arg(name="target_path", help="Filesystem path where the new standalone repository will be created")
def cmd_mono_extract(dry_run, package_name, target_path, **_kwargs):
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


@mono.command(name="absorb", help="Absorb an external repository as a package in the monorepo. Runs git subtree add to import the source repo's history under the package name, adds the project to workspace.toml, and migrates changelog entries from the source repo's .rlsbl/changes/ directory.")
@strictcli.flag(name="releasable", type=str, help="Releasable group to assign the absorbed package to", default="")
@strictcli.arg(name="source_path", help="Filesystem path to the external git repository to absorb")
@strictcli.arg(name="package_name", help="Name for the package in the monorepo workspace")
def cmd_mono_absorb(dry_run, releasable, source_path, package_name, **_kwargs):
    root = _require_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    from .commands.monorepo import cmd_absorb
    try:
        result = cmd_absorb(
            ws_root, source_path, package_name,
            releasable_name=releasable or None,
            dry_run=dry_run,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if dry_run:
        print(f"Would absorb '{result['package_name']}' from {result['source_path']} (branch: {result['source_branch']})")
    else:
        print(f"Absorbed '{result['package_name']}' from {result['source_path']} (branch: {result['source_branch']})")
        print(f"  Changelog: {result['entries_migrated']} entries in {result['files_written']} files")


@mono.command(name="extract-releasable", help="Extract all member packages of a releasable into a new repository. If the releasable has one member, creates a single-project repo. If it has multiple members, creates a new monorepo with workspace.toml. Migrates changelog entries for each member and removes all extracted projects from the source workspace.")
@strictcli.arg(name="releasable_name", help="Name of the releasable group in workspace.toml to extract")
@strictcli.arg(name="target_path", help="Filesystem path where the new repository will be created")
def cmd_mono_extract_releasable(dry_run, releasable_name, target_path, **_kwargs):
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


@mono.command(name="cleanup", help="Remove per-package release-state residue from releasable member packages: .rlsbl/changes/, .rlsbl/releases/, .rlsbl/bases/, .rlsbl/lint/, .rlsbl/version, per-package CHANGELOG.md, and .rlsbl/config.json when identical to the releasable-level config. Per-package hooks/ directories are preserved (live feature), and members whose path is the workspace root are exempt. Deletions go through saferm (audit trail, recoverable) and are committed automatically. Requires an explicit-mode workspace ([[releasables]] in workspace.toml). Detect residue first with `rlsbl check --name releasable-residue`.")
def cmd_mono_cleanup(dry_run, yes, **_kwargs):
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
    run_cleanup_command(ws_root, dry_run=dry_run, yes=yes)


@mono.command(name="migrate-releasable", help="Migrate a releasable from per-package release state to the releasable model. Detects current state, consolidates per-package changelogs and versions into the releasable directory, creates a releasable-format migration tag, and removes orphaned per-package .rlsbl/changes/ and .rlsbl/releases/ directories. Requires the workspace to be in explicit mode (with [[releasables]] in workspace.toml).")
@strictcli.arg(name="releasable_name", help="Name of the releasable group in workspace.toml to migrate")
def cmd_mono_migrate_releasable(dry_run, yes, releasable_name, **_kwargs):
    root = _require_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    if ws_root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    from .releasable_migration import cmd_migrate_releasable
    try:
        result = cmd_migrate_releasable(
            ws_root, releasable_name, dry_run=dry_run, yes=yes,
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


# ---------------------------------------------------------------------------
# dev group
# ---------------------------------------------------------------------------

dev = app.group("dev", help="Developer utilities for locally working with rlsbl projects, including editable installs that mirror the project's release target (pypi -> uv tool install -e, npm -> npm link, go -> go install).")


@dev.command(name="install", help="Install the project locally for development using the detected target's editable install command. --global (default) installs system-wide across 8 supported targets (pypi, npm, go, cargo, zig, swift, deno, hex), while --venv installs into the project's local environment instead. In monorepo mode, pair with --all, --include, or --exclude. Use --uninstall to reverse a previous install.")
@strictcli.flag(name="all", type=bool, default=False, help="In monorepo mode, install every project in the workspace")
@strictcli.flag(name="include", type=str, help="In monorepo mode, comma-separated project names to include", default="")
@strictcli.flag(name="exclude", type=str, help="In monorepo mode, comma-separated project names to exclude", default="")
@strictcli.flag(name="uninstall", type=bool, default=False, help="Reverse a previous dev install (where supported by the target)")
@strictcli.flag(name="global", type=bool, help="Install as a global tool/symlink. This is the default behavior when neither --global nor --venv is passed. Mutually exclusive with --venv.", default=False)
@strictcli.flag(name="venv", type=bool, help="Install into the project's local environment only (e.g. uv sync, npm install). Mutually exclusive with --global.", default=False)
def cmd_dev_install(all, include, exclude, uninstall, global_, venv, **_kwargs):
    if global_ and venv:
        print(
            "Error: --global and --venv are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(2)
    root = _require_sub_project_root()
    # Both flags default to False (not True) so strictcli's mutex check doesn't
    # always fire when neither is passed. The user-visible default (no flags ->
    # global mode) is preserved by deriving install_global from --venv only.
    install_global = not venv
    flags = {
        "all": all,
        "include": include or None,
        "exclude": exclude or None,
        "uninstall": uninstall,
        "global": install_global,
        "venv": venv,
    }
    from .commands.dev import run_install
    rc = run_install(flags, project_root=root)
    if rc:
        sys.exit(rc)


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
        value_flags = {"format", "since", "depth"}
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

def main():
    global _variadic_args
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
