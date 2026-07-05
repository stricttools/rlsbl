"""Yank command that performs registry-level removal of a published version.

For each configured target, probes the registry to determine publication
status, then executes the registry-specific yank action:
- npm: ``npm deprecate``
- cargo: ``cargo yank``
- Go: retract directive + tag deletion
- PyPI: human-in-the-loop checklist (no yank API)

Also sets the GitHub pre-release flag and adds a yank notice to the release.
"""

import os
import sys
import time

from ..member_context import resolve_member_context
from ..publication_probe import PublicationStatus
from ..targets import TARGETS, resolve_releasable_config_dir
from ..utils import run_gh, check_gh_installed, check_gh_auth
from ..workspace import find_workspace_root, resolve_project


def run_cmd(args, flags, project_root):
    """Yank a version from registries and mark the GitHub Release.

    For each configured target:
    - PUBLISHED: execute registry-specific yank
    - UNPUBLISHED: skip with a message
    - UNPROBEABLE: hard error

    Args:
        args: Positional args; first element is the version to yank.
        flags: dict with keys ``dry-run``, ``yes``, ``reason``, ``use``.
        project_root: Path to the project root directory, or None for cwd.
    """
    dry_run = flags.get("dry-run", False)
    reason = flags.get("reason")
    use = flags.get("use")

    if not args:
        print("Error: version argument is required.", file=sys.stderr)
        sys.exit(1)

    # Normalize version: strip leading "v" for display
    raw_version = args[0]
    version = raw_version.lstrip("v")

    # Detect monorepo context and build tag accordingly
    monorepo_name = None
    monorepo_project_path = None
    releasable_name = None
    releasable_tag_fmt = None
    releasable_config_dir = None
    start_path = str(project_root)
    monorepo_root = find_workspace_root(start_path)
    if monorepo_root:
        project = resolve_project(monorepo_root, start_path)
        if project is not None:
            monorepo_name = project["name"]
            monorepo_project_path = project["path"]
            releasable_config_dir = resolve_releasable_config_dir(project, monorepo_root)

            # Detect explicit releasable mode
            from ..workspace import is_explicit_mode, load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
            if is_explicit_mode(monorepo_root):
                ws_projects = _load_ws(monorepo_root)
                releasables = load_releasables(monorepo_root, ws_projects)
                rel = resolve_releasable_for_project(project, releasables)
                if rel:
                    releasable_name = rel.name
                    releasable_tag_fmt = rel.tag_format

    # Project directory
    project_dir = start_path
    member = resolve_member_context(
        project_dir, releasable_config_dir=releasable_config_dir,
    )
    entries = member.targets
    if entries:
        target_obj = TARGETS[entries[0].name]
        if releasable_name and releasable_tag_fmt:
            from .release.validate import _format_releasable_tag
            tag = _format_releasable_tag(releasable_tag_fmt, releasable_name, version)
        elif monorepo_name:
            tag = target_obj.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
        else:
            tag = target_obj.tag_format(version)
    else:
        tag = f"v{version}"

    if not check_gh_installed():
        print("Error: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print("Error: gh CLI is not authenticated.", file=sys.stderr)
        sys.exit(1)

    # Verify the GitHub Release exists
    try:
        run_gh(["release", "view", tag])
    except Exception:
        print(f"Error: GitHub Release for {tag} not found.", file=sys.stderr)
        sys.exit(1)

    # Refuse to yank the latest release -- suggest rlsbl release undo instead
    try:
        latest_line = run_gh(["release", "list", "--limit", "1", "--json", "tagName", "--jq", ".[0].tagName"])
        if latest_line == tag:
            print(
                f"Error: {tag} is the latest release. Use 'rlsbl release undo' to revert it instead.",
                file=sys.stderr,
            )
            sys.exit(1)
    except Exception as e:
        print(f"Error: could not determine latest release: {e}", file=sys.stderr)
        print("Cannot verify whether this is the latest release. Aborting for safety.", file=sys.stderr)
        sys.exit(1)

    # Probe registries for publication status
    probe_results = []
    target_objects = []
    if entries:
        for entry in entries:
            t = TARGETS[entry.name]
            target_objects.append(t)
            if "publication_probe" in t.capabilities:
                result = t.publication_probe(project_dir, version)
                probe_results.append((t, result))
            else:
                # Target without probe capability: UNPROBEABLE
                from ..publication_probe import PublicationProbeResult
                probe_results.append((t, PublicationProbeResult(
                    status=PublicationStatus.UNPROBEABLE,
                    registry=t.name,
                    version=version,
                    message=f"target '{t.name}' does not support publication probing",
                )))

    # Check for hard errors: any UNPROBEABLE with no other evidence
    unprobeable_targets = [
        (t, r) for t, r in probe_results if r.status == PublicationStatus.UNPROBEABLE
    ]
    if unprobeable_targets:
        print("Error: cannot determine publication status for:", file=sys.stderr)
        for t, r in unprobeable_targets:
            print(f"  {t.name}: {r.message}", file=sys.stderr)
        print("\nYank requires certainty about publication status. Cannot proceed.", file=sys.stderr)
        sys.exit(1)

    published = [(t, r) for t, r in probe_results if r.status == PublicationStatus.PUBLISHED]
    unpublished = [(t, r) for t, r in probe_results if r.status == PublicationStatus.UNPUBLISHED]

    # Display plan
    if published:
        print(f"Will yank {tag} from {len(published)} registry(ies):")
        for t, r in published:
            print(f"  {t.name}: {r.message}")
    if unpublished:
        for t, r in unpublished:
            print(f"  {t.name}: skipping ({r.message})")

    # Confirmation prompt (skipped with --yes or --dry-run)
    if not dry_run and not flags.get("yes"):
        prompt = f"Proceed with yanking {tag}? [y/N] "
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # Execute registry-specific yank for each published target
    for t, r in published:
        _yank_target(t, project_dir, version, tag, reason, dry_run)

    # Mark GitHub release as pre-release with yank notice
    _mark_github_release(tag, reason, use, dry_run)

    if dry_run:
        print(f"\nDry run complete for {tag}.")
    else:
        print(f"\nYanked {tag}.")


def _yank_target(target, project_dir, version, tag, reason, dry_run):
    """Execute registry-specific yank for a single target."""
    name = target.name
    if name == "npm":
        _yank_npm(target, project_dir, version, reason, dry_run)
    elif name == "cargo":
        _yank_cargo(target, project_dir, version, dry_run)
    elif name == "go":
        _yank_go(target, project_dir, version, tag, dry_run)
    elif name == "pypi":
        _yank_pypi(target, project_dir, version, dry_run)
    else:
        print(f"  {name}: no yank implementation (skipping)", file=sys.stderr)


def _yank_npm(target, project_dir, version, reason, dry_run):
    """Deprecate a version on npm."""
    pkg_name = target.read_name(project_dir, None)
    if not pkg_name:
        print("  npm: cannot determine package name, skipping", file=sys.stderr)
        return

    deprecation_msg = reason or "This version has been yanked."
    spec = f"{pkg_name}@{version}"

    if dry_run:
        print(f"  npm: would run: npm deprecate {spec} \"{deprecation_msg}\"")
        return

    import subprocess
    try:
        subprocess.run(
            ["npm", "deprecate", spec, deprecation_msg],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(f"  npm: deprecated {spec}")
    except subprocess.CalledProcessError as e:
        print(f"  npm: deprecation failed: {e.stderr.strip()}", file=sys.stderr)
    except FileNotFoundError:
        print("  npm: npm CLI not found", file=sys.stderr)


def _yank_cargo(target, project_dir, version, dry_run):
    """Yank a version on crates.io."""
    crate_name = target.read_name(project_dir, None)
    if not crate_name:
        print("  cargo: cannot determine crate name, skipping", file=sys.stderr)
        return

    if dry_run:
        print(f"  cargo: would run: cargo yank --version {version} {crate_name}")
        return

    import subprocess
    try:
        subprocess.run(
            ["cargo", "yank", "--version", version, crate_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_dir,
        )
        print(f"  cargo: yanked {crate_name}@{version}")
    except subprocess.CalledProcessError as e:
        print(f"  cargo: yank failed: {e.stderr.strip()}", file=sys.stderr)
    except FileNotFoundError:
        print("  cargo: cargo CLI not found", file=sys.stderr)


def _yank_go(target, project_dir, version, tag, dry_run):
    """Retract a version in Go (add retract directive to go.mod)."""
    from ..utils import read_go_module_path
    module_path = read_go_module_path(project_dir)
    if not module_path:
        print("  go: cannot determine module path, skipping", file=sys.stderr)
        return

    if dry_run:
        print(f"  go: would add retract directive for v{version} to go.mod")
        print(f"  go: would require a new release to publish the retraction")
        return

    # Add retract directive to go.mod
    go_mod = os.path.join(project_dir, "go.mod")
    try:
        with open(go_mod, "r", encoding="utf-8") as f:
            content = f.read()

        retract_line = f"retract v{version}"
        if retract_line not in content:
            content = content.rstrip() + f"\n\n{retract_line}\n"
            with open(go_mod, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  go: added retract directive for v{version} to go.mod")
            print(f"  go: commit and release a new version to publish the retraction")
        else:
            print(f"  go: retract directive for v{version} already present")
    except Exception as e:
        print(f"  go: failed to update go.mod: {e}", file=sys.stderr)


def _yank_pypi(target, project_dir, version, dry_run):
    """PyPI has no yank API -- print manual steps and wait for confirmation."""
    pkg_name = target.read_name(project_dir, None)
    if not pkg_name:
        print("  pypi: cannot determine package name, skipping", file=sys.stderr)
        return

    pep440_version = target.format_version(version)

    print(f"\n  PyPI does not have a yank API. Manual steps required:")
    print(f"  1. Go to https://pypi.org/project/{pkg_name}/{pep440_version}/")
    print(f"  2. Click 'Options' -> 'Yank release'")
    print(f"  3. Enter a reason and confirm")
    print(f"  4. The version will be hidden from default pip installs")

    if dry_run:
        print(f"  pypi: would wait for interactive confirmation")
        return

    try:
        answer = input(f"\n  Have you completed the PyPI yank for {pkg_name}=={pep440_version}? [y/N] ").strip().lower()
        if answer == "y":
            print(f"  pypi: confirmed yanked {pkg_name}=={pep440_version}")
        else:
            print(f"  pypi: skipped -- remember to yank manually on PyPI")
    except (EOFError, KeyboardInterrupt):
        print(f"\n  pypi: skipped -- remember to yank manually on PyPI")


def _mark_github_release(tag, reason, use, dry_run):
    """Mark GitHub release as pre-release with a yank notice."""
    notice = _build_yank_notice(reason, use)

    try:
        current_body = run_gh(["release", "view", tag, "--json", "body", "--jq", ".body"])
    except Exception:
        current_body = ""

    new_body = notice + "\n\n" + current_body if current_body else notice

    if dry_run:
        print(f"  github: would mark {tag} as pre-release with yank notice")
        return

    notes_file = f".rlsbl-yank-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    try:
        with open(writing_file, "w", encoding="utf-8") as f:
            f.write(new_body)
        os.rename(writing_file, notes_file)
        run_gh(["release", "edit", tag, "--prerelease", "--notes-file", notes_file])
        print(f"  github: marked {tag} as pre-release")
    finally:
        for tmp in (notes_file, writing_file):
            if os.path.exists(tmp):
                os.unlink(tmp)


def _build_yank_notice(reason, use):
    """Build the yank notice string for the GitHub release body."""
    parts = []
    if reason:
        parts.append(reason)
    if use:
        use_version = use.lstrip("v")
        parts.append(f"Use v{use_version} instead")

    if parts:
        return "> **Yanked:** " + ". ".join(parts) + "."
    return "> **Yanked.**"
