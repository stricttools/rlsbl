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
    help="Release orchestration and project scaffolding CLI. Automates version bumping, changelog validation, tagging, GitHub Releases, and CI/CD scaffolding across 18 release targets (npm, PyPI, Go, Cargo, Deno, Zig, Swift, Hex, Docker, Maven, Dart, Flutter, and more). Ships 32 commands organized into 13 top-level commands and 4 command groups (release, changelog, monorepo, dev).",
    flags=[
        strictcli.Flag(name="dry-run", type=bool, help="Preview changes without applying them"),
        strictcli.Flag(name="yes", type=bool, short="y", help="Skip confirmation prompts"),
        strictcli.Flag(name="quiet", type=bool, help="Suppress non-essential output"),
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

        projects = load_workspace(workspace_root)
        graph = WorkspaceGraph(workspace_root, projects)
        ctx = create_context(Path.cwd(), workspace_root=Path(workspace_root))
        wctx = WorkspaceCheckContext(
            project_root=ctx.project_root,
            workspace_root=ctx.workspace_root,
            config=ctx.config,
            projects=projects,
            graph=graph,
        )
        wctx.push_stdin = push_stdin
        return wctx
    ctx = create_context(Path.cwd())
    ctx.push_stdin = push_stdin
    return ctx


app.set_check_context(_check_context_factory)

# Register check implementations on the strictcli check system.
from .checks import register_checks
register_checks(app)


# ---------------------------------------------------------------------------
# release group
# ---------------------------------------------------------------------------

release_group = app.group("release", help="Release orchestration commands. Provides 6 subcommands covering the full release lifecycle: run, init, retry, edit, undo, and yank.")


@release_group.command(
    name="run",
    help="Bump version, validate the JSONL changelog, run tests and lint, commit, tag, push, and create a GitHub Release. Reads the bump type (patch, minor, or major) and target selection from .rlsbl/releases/unreleased.toml, which can be scaffolded with rlsbl release init. Supports dry-run preview, non-interactive mode with --yes, and --allow-dirty to skip the clean working tree check.",
    mutex=[
        strictcli.MutexGroup(flags=[
            strictcli.Flag(name="watch", type=bool, negatable=False, help="After release, automatically watch CI runs to completion"),
            strictcli.Flag(name="no-watch", type=bool, negatable=False, help="After release, print the watch command hint without watching"),
        ]),
    ],
)
@strictcli.flag(name="allow-dirty", type=bool, help="Allow releasing with a dirty working tree")
def cmd_release_run(dry_run, yes, quiet, allow_dirty, watch, no_watch, **_kwargs):
    root = _require_sub_project_root()

    from .release_file import read_release_file, get_release_file_path
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
                "Use `rlsbl monorepo release` for batch releases, "
                "or cd to a package directory.",
                file=sys.stderr,
            )
            sys.exit(1)
        project_dir = os.path.join(monorepo_root, project["path"])

    release_path = get_release_file_path(project_dir)
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

    flags = {
        "dry-run": dry_run,
        "yes": yes,
        "quiet": quiet,
        "allow-dirty": allow_dirty,
        "watch": bool(watch),
    }
    from .commands.release import run_cmd
    run_cmd(release_config, flags, ctx=ctx)


@release_group.command(name="init", help="Scaffold a .rlsbl/releases/unreleased.toml file by auto-detecting project targets. The generated file contains a default bump type (patch), an include list of all detected targets, and per-target configuration sections for Flutter targets.")
def cmd_release_init(**_kwargs):
    root = _require_sub_project_root()
    from .commands.release_init import run_cmd
    run_cmd(project_root=root)


@release_group.command(
    name="retry",
    help="Dispatch CI/CD workflows for a completed release via gh workflow run. Reads the dispatch list and ref from .rlsbl/releases/retry.toml, which is auto-scaffolded with sensible defaults if missing. Verifies the GitHub Release exists before dispatching. Each workflow in the dispatch list is triggered against the configured ref (defaults to the release tag).",
    mutex=[
        strictcli.MutexGroup(flags=[
            strictcli.Flag(name="watch", type=bool, negatable=False, help="After retry, automatically watch CI runs to completion"),
            strictcli.Flag(name="no-watch", type=bool, negatable=False, help="After retry, print the watch command hint without watching"),
        ]),
    ],
)
def cmd_release_retry(dry_run, yes, quiet, watch, no_watch, **_kwargs):
    root = _require_sub_project_root()

    from .release_file import get_retry_file_path, read_retry_file

    retry_path = get_retry_file_path(".")
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
@strictcli.flag(name="json", type=bool, help="Output status as JSON")
def cmd_status(target, json, **_kwargs):
    root = _require_sub_project_root()
    from .workspace import find_workspace_root
    ws_root = find_workspace_root(str(root))
    ctx = create_context(root, workspace_root=Path(ws_root) if ws_root else None)
    registry = _resolve_target(target or None)
    flags = {"json": json}
    from .commands.status import run_cmd
    run_cmd(registry, [], flags, ctx=ctx)


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------

@app.command(name="scaffold", help="Generate or update CI/CD workflows, git hooks, changelog, and license files. Safe to run repeatedly -- merges template changes with your customizations. Use --force to overwrite all files.")
@strictcli.flag(name="target", type=str, help="Target a specific registry (auto-detected if omitted)", default="")
@strictcli.flag(name="force", type=bool, help="Overwrite all files (ignore user customizations)")
@strictcli.flag(name="private", type=bool, help="Scaffold for private repos (skip publish)")
@strictcli.flag(name="no-commit", type=bool, help="Skip auto-commit of scaffolded files")
@strictcli.flag(name="skip-shared", type=bool, help="Skip shared template processing")
@strictcli.flag(name="no-tag", type=bool, help="Disable ecosystem tagging for this invocation")
def cmd_scaffold(target, force, private, no_commit, skip_shared, no_tag, dry_run, **_kwargs):
    # Scaffold is special: if a project root exists, resolve it for use as
    # scaffold_root; if not, stay in cwd (for new projects).
    # If the current directory has project markers (pyproject.toml,
    # package.json, go.mod, etc.), use cwd -- the user is in a sub-project
    # and wants to scaffold in place. This prevents walking up to a monorepo
    # root when inside a sub-project.
    # When --target is explicitly passed (e.g., --target plain), always use
    # cwd -- the user is declaring what to scaffold and where. Without this,
    # plain-target projects (whose detect() always returns False) would walk
    # up to the monorepo root, causing _is_dev_node_project() to fail.
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
        "force": force,
        "private": private,
        "no-commit": no_commit,
        "skip-shared": skip_shared,
        "no-tag": no_tag,
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
@strictcli.flag(name="target", type=str, help="Target registry (npm, pypi, or go)", repeatable=True, unique=True)
@strictcli.flag(name="delay", type=str, help="Delay between checks in ms", default="200")
def cmd_check_name(target, delay, **_kwargs):
    # --target is required for check-name; with repeatable=True, target is a list
    targets = target if target else []
    if not targets:
        print(
            "Error: --target is required. "
            "Usage: rlsbl check-name <name> [<name2> ...] --target <npm|pypi|go>",
            file=sys.stderr,
        )
        sys.exit(1)
    # Validate ALL targets upfront before any network calls
    valid_targets = {"npm", "pypi", "go"}
    invalid = [t for t in targets if t not in valid_targets]
    if invalid:
        print(
            f"Error: unknown target(s): {', '.join(repr(t) for t in invalid)}. "
            f"Valid: npm, pypi, go",
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
# release edit (was: edit-release)
# ---------------------------------------------------------------------------

@release_group.command(name="edit", help="Sync the GitHub Release notes for a given version with the corresponding CHANGELOG.md entry. Defaults to the current version if none is specified. Use --dry-run to preview changes without updating GitHub.")
@strictcli.arg(name="version", help="Version to update (defaults to current)", required=False)
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
@strictcli.flag(name="target", type=str, help="Target a specific registry", default="")
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
@strictcli.flag(name="reason", type=str, help="Why the version is being yanked", default="")
@strictcli.flag(name="use", type=str, help="Replacement version to recommend", default="")
@strictcli.flag(name="hard", type=bool, help="Delete the release instead of marking as pre-release")
@strictcli.arg(name="version", help="Version to yank (e.g. 0.9.1 or v0.9.1)")
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
    help="Scrub sensitive content from git history and update release metadata to match the rewritten commits. Wraps safegit scrub (match or file mode), remaps commit hashes in all JSONL changelog files, regenerates CHANGELOG.md, force-pushes the rewritten history, and recreates GitHub Releases on the new tags. A scrub-result.json file records the SHA mapping for recovery if any post-rewrite step fails.",
    mutex=[
        strictcli.MutexGroup(flags=[
            strictcli.Flag(name="pattern", type=str, help="Regex pattern to match (for scrub match)"),
            strictcli.Flag(name="file", type=str, help="File path to scrub (for scrub file)"),
        ]),
        strictcli.MutexGroup(flags=[
            strictcli.Flag(name="replace", type=str, help="Replacement text for matched content"),
            strictcli.Flag(name="mangle", type=bool, negatable=False, help="Replace matched content with random ASCII of same length"),
        ]),
        strictcli.MutexGroup(flags=[
            strictcli.Flag(name="from-commit", type=str, help="Start rewriting from this commit (inclusive)"),
            strictcli.Flag(name="entire-history", type=bool, negatable=False, help="Rewrite entire repository history"),
        ]),
    ],
)
@strictcli.flag(name="reason", type=str, help="Reason for scrubbing (required, used in commit message)", default="")
def cmd_release_scrub(pattern, file, replace, mangle, from_commit, entire_history, reason, dry_run, yes, **_kwargs):
    root = _require_project_root()
    from .workspace import find_workspace_root
    monorepo_root = find_workspace_root(str(root))
    ctx = create_context(root, workspace_root=Path(monorepo_root) if monorepo_root else None)
    flags = {
        "pattern": pattern or None,
        "file": file or None,
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
@strictcli.flag(name="mine", type=bool, help="Show only your own repositories")
def cmd_discover(mine, **_kwargs):
    flags = {"mine": mine}
    from .commands.discover import run_cmd
    run_cmd(None, [], flags)


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

@app.command(name="watch", help="Poll GitHub Actions CI workflow runs for a specific commit SHA and report pass or fail status. Defaults to HEAD if no SHA is provided. Useful after rlsbl release to monitor the publish pipeline.")
@strictcli.flag(name="target", type=str, help="Target a specific registry", default="")
@strictcli.flag(name="run-id", type=str, help="CI workflow run ID to watch", repeatable=True, unique=True)
@strictcli.arg(name="sha", help="Commit SHA to watch (defaults to HEAD)", required=False)
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
@strictcli.flag(name="json", type=bool, help="Output as JSON")
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
@strictcli.flag(name="width", type=str, help="GIF width in pixels", default="1200")
@strictcli.flag(name="height", type=str, help="GIF height in pixels", default="600")
@strictcli.flag(name="font-size", type=str, help="Font size in pixels", default="24")
@strictcli.flag(name="duration", type=str, help="Duration in seconds", default="10")
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
@strictcli.flag(name="status", type=bool, help="Show migration status")
def cmd_migrate(dry_run, status, **_kwargs):
    flags = {"dry-run": dry_run, "status": status}
    from .commands.migrate import run_cmd
    run_cmd(None, [], flags)



# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

@app.command(name="deploy", help="Run the configured deployment pipeline for the project. Supports named deploy targets, dry-run preview of what would be deployed, and a --force flag to override branch restrictions.")
@strictcli.flag(name="target", type=str, help="Target a specific registry", default="")
@strictcli.flag(name="force", type=bool, help="Override branch restrictions")
@strictcli.arg(name="target_name", help="Deploy target name", required=False)
def cmd_deploy(target, dry_run, force, target_name=None, **_kwargs):
    root = _require_sub_project_root()
    ctx = create_context(root)
    args = [target_name] if target_name else []
    flags = {"dry-run": dry_run, "force": force}
    from .commands.deploy_cmd import run_cmd
    run_cmd(target or None, args, flags, ctx=ctx)


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

@app.command(name="commit", help="Commit one or more files with an Autogenerated trailer, marking the commit as machine-generated so it is automatically exempted from changelog coverage checks.")
@strictcli.flag(name="message", short="m", type=str, help="Commit message")
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


@chlog.command(name="add", help="Append a structured changelog entry to the project's unreleased.jsonl file. Each entry includes a human-readable description, an entry type (feature, fix, or breaking), and optional commit hashes linking it to specific changes. The file is auto-committed unless --no-commit is passed. Use --no-user-facing to mark internal changes that should not appear in the published changelog.")
@strictcli.flag(name="commits", type=str, help="Comma-separated commit hashes", default="")
@strictcli.flag(name="description", type=str, help="Entry description", default="")
@strictcli.flag(name="type", type=str, help="Entry type (feature, fix, breaking)", default="")
@strictcli.flag(name="no-user-facing", type=bool, help="Mark as non-user-facing")
@strictcli.flag(name="no-commit", type=bool, help="Skip auto-commit of unreleased.jsonl")
@strictcli.flag(name="allow-batch", type=bool, help="Auto-create an exclusion if this entry exceeds the commit batch limit")
def cmd_chlog_add(commits, description, type, no_user_facing, no_commit, allow_batch, **_kwargs):
    root = _require_sub_project_root()
    flags = {
        "commits": commits,
        "description": description,
        "type": type,
        "no-user-facing": no_user_facing,
        "no-commit": no_commit,
        "allow-batch": allow_batch,
    }
    from .commands.changelog_cmd import cmd_add
    cmd_add(flags, project_root=root)



@chlog.command(name="generate", help="Compile all validated JSONL changelog entries into a formatted CHANGELOG.md file. Groups entries by type (features, fixes, breaking changes) under the appropriate version heading, preserving existing changelog content for previous releases. Use --dry-run to preview the generated Markdown output without writing to disk, which is useful for reviewing before committing.")
@strictcli.flag(name="no-commit", type=bool, help="Skip auto-commit of generated files")
def cmd_chlog_generate(dry_run, no_commit, **_kwargs):
    root = _require_sub_project_root()
    flags = {"dry-run": dry_run, "no-commit": no_commit}
    from .commands.changelog_cmd import cmd_generate
    cmd_generate(flags, project_root=root)


@chlog.command(name="amend", help="Append a changelog entry to a released version's JSONL file. Temporarily unlocks the read-only file, appends the entry, re-locks it, regenerates CHANGELOG.md, and syncs GitHub Release notes. Use --no-resolve to skip hash validation for old or amended commits.")
@strictcli.flag(name="version", type=str, help="Released version to amend (e.g., 0.39.0)")
@strictcli.flag(name="commits", type=str, help="Comma-separated commit hashes")
@strictcli.flag(name="description", type=str, help="Entry description", default="")
@strictcli.flag(name="type", type=str, help="Entry type (feature, fix, breaking)", default="")
@strictcli.flag(name="no-user-facing", type=bool, help="Mark as non-user-facing")
@strictcli.flag(name="no-resolve", type=bool, help="Skip hash validation")
def cmd_chlog_amend(version, commits, description, type, no_user_facing, no_resolve, **_kwargs):
    root = _require_sub_project_root()
    flags = {
        "version": version,
        "commits": commits,
        "description": description,
        "type": type,
        "no-user-facing": no_user_facing,
        "no-resolve": no_resolve,
    }
    from .commands.changelog_cmd import cmd_amend
    cmd_amend(flags, project_root=root)


@chlog.command(name="edit", help="Modify an existing changelog entry in unreleased or released JSONL files. Finds the entry by commit hash, applies field changes (type, description, user-facing status), and rewrites the file atomically. For released files, temporarily unlocks the read-only file, regenerates CHANGELOG.md, and syncs GitHub Release notes.")
@strictcli.flag(name="commits", type=str, help="Comma-separated commit hashes identifying the target entry")
@strictcli.flag(name="type", type=str, help="New type value (feature, fix, breaking); also disambiguates multi-entry commits", default="")
@strictcli.flag(name="description", type=str, help="New description text", default="")
@strictcli.flag(name="no-user-facing", type=bool, help="Set user_facing=false, clear description and type")
@strictcli.flag(name="user-facing", type=bool, help="Set user_facing=true (requires --description and --type if entry doesn't already have them)")
@strictcli.flag(name="no-commit", type=bool, help="Skip auto-commit")
def cmd_chlog_edit(commits, type, description, no_user_facing, user_facing, no_commit, **_kwargs):
    root = _require_sub_project_root()
    flags = {
        "commits": commits,
        "type": type,
        "description": description,
        "no-user-facing": no_user_facing,
        "user-facing": user_facing,
        "no-commit": no_commit,
    }
    from .commands.changelog_cmd import cmd_edit
    cmd_edit(flags, project_root=root)


# ---------------------------------------------------------------------------
# monorepo group
# ---------------------------------------------------------------------------

mono = app.group("monorepo", help="Manage monorepo workspaces with multiple independently-versioned projects. Initialize workspaces, add or remove projects, sync CI workflows, check name availability, and analyze dependency graphs. Provides 10 monorepo subcommands and supports all 18 release targets in a single workspace.toml.")


@mono.command(name="init", help="Create a new monorepo workspace by generating the .rlsbl-monorepo directory and an empty workspace.toml configuration file at the current directory. This must be run at the repository root before adding individual projects with the add subcommand. Each workspace tracks multiple independently-versioned projects that share a single git repository.")
@strictcli.flag(name="no-commit", type=bool, help="Skip auto-commit of workspace.toml")
def cmd_mono_init(no_commit, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_init
    _cmd_init({"no-commit": no_commit}, project_root=root)


@mono.command(name="add", help="Register a project directory in the monorepo workspace.toml configuration. The path argument specifies the project's location relative to the repo root. Optionally set a display name, target registry for publishing, glob patterns for change detection, a subtree remote URL for split publishing, inter-project dependencies, and a library flag to mark shared code packages.")
@strictcli.flag(name="name", type=str, help="Project name (defaults to directory name)", default="")
@strictcli.flag(name="target", type=str, help="Target registry", default="")
@strictcli.flag(name="watch", type=str, help="Comma-separated glob patterns to watch", default="")
@strictcli.flag(name="subtree-remote", type=str, help="Subtree remote URL", default="")
@strictcli.flag(name="depends-on", type=str, help="Comma-separated dependency project names", default="")
@strictcli.flag(name="library", type=str, help="Mark as library (true/false)", default="")
@strictcli.flag(name="dev-node", type=str, help="Mark as dev node (true/false)", default="")
@strictcli.flag(name="no-commit", type=bool, help="Skip auto-commit of workspace.toml and suppress commits from auto-triggered scaffold/sync")
@strictcli.arg(name="path", help="Path to the project directory")
def cmd_mono_add(name, target, watch, subtree_remote, depends_on, library, dev_node, no_commit, path, **_kwargs):
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
    if dev_node:
        flags["dev_node"] = dev_node
    if no_commit:
        flags["no-commit"] = True
    from .commands.monorepo import _cmd_add
    _cmd_add([path], flags, project_root=root)


@mono.command(name="remove", help="Unregister a project from the monorepo workspace.toml by its path. This removes the project entry from the workspace configuration file but does not delete any files, directories, or git history on disk. The project's code remains intact and can be re-added later with the add subcommand if needed.")
@strictcli.arg(name="path", help="Path to the project to remove")
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
@strictcli.flag(name="no-commit", type=bool, help="Skip auto-commit of synced workflow files")
def cmd_mono_sync(no_commit, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_sync
    _cmd_sync({"no-commit": no_commit}, project_root=root)


@mono.command(name="status", help="Show the current version, last release tag, and number of unreleased commits for every project in the monorepo workspace. Provides a quick overview of which projects have pending changes and are ready for their next release. Projects with zero unreleased commits are shown as up-to-date.")
def cmd_mono_status(**_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_status
    _cmd_status({}, project_root=root)


@mono.command(name="check-names", help="Check package name availability on a target registry for all projects in the monorepo workspace. Queries the registry API for each project name and reports whether it is available or already taken. Supports optional prefix and suffix arguments to test naming conventions like scoped packages, with a configurable delay between registry queries to avoid rate limiting.")
@strictcli.flag(name="target", type=str, help="Target registry (npm, pypi, or go)")
@strictcli.flag(name="prefix", type=str, help="Prefix to prepend to project names", default="")
@strictcli.flag(name="suffix", type=str, help="Suffix to append to project names", default="")
@strictcli.flag(name="delay", type=str, help="Delay between checks in ms", default="200")
def cmd_mono_check_names(target, prefix, suffix, delay, **_kwargs):
    root = _require_project_root()
    flags = {"target": target, "prefix": prefix, "suffix": suffix, "delay": delay}
    from .commands.monorepo import _cmd_check_names
    _cmd_check_names(_variadic_args, flags, project_root=root)


@mono.command(name="release-order", help="Compute and display the topological release order for all projects in the monorepo workspace based on their declared depends-on relationships. Projects with no dependencies are listed first, followed by projects that depend on them, ensuring each project is released only after its dependencies. Detects and reports circular dependency errors.")
def cmd_mono_release_order(**_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_release_order
    _cmd_release_order({}, project_root=root)


@mono.command(name="outdated", help="Scan all projects in the monorepo workspace for intra-workspace dependencies that reference older versions than what is currently available in the workspace. Lists each outdated dependency with the referenced version and the latest available version, helping identify which downstream projects need a version bump after upstream releases.")
def cmd_mono_outdated(**_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_outdated
    _cmd_outdated({}, project_root=root)


@mono.command(name="snapshot", help="Generate a committed JSON artifact at .rlsbl-monorepo/snapshot.json summarizing all packages, versions, dependencies, and graph structure. Use --check to verify the snapshot is up-to-date without regenerating it (exits 1 if stale).")
@strictcli.flag(name="check", type=bool, help="Verify snapshot.json is up-to-date (exit 1 if stale)")
def cmd_mono_snapshot(check, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_snapshot
    _cmd_snapshot({"check": check}, project_root=root)


@mono.command(name="mirror", help="Initialize a subtree mirror repository for a monorepo project by performing a full git subtree split of the project's history, pushing the extracted tree to the configured subtree_remote URL, cloning the resulting standalone mirror repository, running rlsbl scaffold to generate CI workflows for independent publishing, and pushing the scaffolded mirror to its remote.")
@strictcli.arg(name="project", help="Name of the workspace project to mirror")
def cmd_mono_mirror(project, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_mirror
    _cmd_mirror({"project": project}, project_root=root)


@mono.command(name="graph", help="Export the monorepo dependency graph in JSON, DOT (Graphviz), or indented text tree format. Supports filtering by a root package (transitive deps) or reverse package (transitive rdeps), with optional depth limiting. Use --output to write to a file instead of stdout.")
@strictcli.flag(name="format", type=str, help="Output format: json, dot, or text (default: json)", default="json")
@strictcli.flag(name="output", type=str, help="Write output to file instead of stdout", default="")
@strictcli.flag(name="root", type=str, help="Show only transitive deps from this package", default="")
@strictcli.flag(name="reverse", type=str, help="Show only transitive rdeps of this package", default="")
@strictcli.flag(name="depth", type=int, help="Limit traversal depth")
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
@strictcli.flag(name="format", type=str, help="Output format: json or text (default: text)", default="text")
@strictcli.flag(name="depth", type=int, help="Limit traversal depth")
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


@mono.command(name="release", help="Execute a batch release of multiple monorepo packages in topological order. Reads package configurations from .rlsbl-monorepo/releases/unreleased.toml. Each package is released sequentially using the single-package release flow, with leaves (no dependencies) released first. Supports --dry-run, --yes, --allow-dirty flags.")
@strictcli.flag(name="allow-dirty", type=bool, help="Allow releasing with a dirty working tree")
def cmd_mono_release(dry_run, yes, quiet, allow_dirty, **_kwargs):
    flags = {
        "dry-run": dry_run,
        "yes": yes,
        "quiet": quiet,
        "allow-dirty": allow_dirty,
    }
    root = _require_project_root()
    from .commands.monorepo import _cmd_batch_release
    _cmd_batch_release(flags, project_root=root)


@mono.command(name="release-init", help="Scaffold a batch release file for all workspace projects by auto-detecting each project's release targets and generating per-package configuration sections. Creates .rlsbl-monorepo/releases/unreleased.toml with a [packages.<name>] section for each non-dev-node project, pre-populated with bump type, description, and include lists. Packages with no unreleased commits since their last tag are rendered as commented-out sections.")
@strictcli.flag(name="packages", type=str, help="Comma-separated package names to include (default: all)", default="")
def cmd_mono_release_init(packages, **_kwargs):
    root = _require_project_root()
    from .commands.monorepo import _cmd_batch_release_init
    _cmd_batch_release_init(project_root=root, packages=packages or None)


# ---------------------------------------------------------------------------
# dev group
# ---------------------------------------------------------------------------

dev = app.group("dev", help="Developer utilities for locally working with rlsbl projects, including editable installs that mirror the project's release target (pypi -> uv tool install -e, npm -> npm link, go -> go install).")


@dev.command(name="install", help="Install the project locally for development using the detected target's editable install command. --global (default) installs system-wide across 8 supported targets (pypi, npm, go, cargo, zig, swift, deno, hex), while --venv installs into the project's local environment instead. In monorepo mode, pair with --all, --include, or --exclude. Use --uninstall to reverse a previous install.")
@strictcli.flag(name="all", type=bool, help="In monorepo mode, install every project in the workspace")
@strictcli.flag(name="include", type=str, help="In monorepo mode, comma-separated project names to include", default="")
@strictcli.flag(name="exclude", type=str, help="In monorepo mode, comma-separated project names to exclude", default="")
@strictcli.flag(name="uninstall", type=bool, help="Reverse a previous dev install (where supported by the target)")
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
