"""Yank command for registry-level removal of a published version, probing each configured target and executing registry-specific yank actions.

For each configured target, probes the registry to determine publication
status, then executes the registry-specific yank action:
- npm: ``npm deprecate``
- Go: retract directive + tag deletion
- PyPI: human-in-the-loop checklist (no yank API)

Also sets the GitHub pre-release flag and adds a yank notice to the release.
"""

import os
import sys
import time

from ..member_context import resolve_member_context
from ..publication_probe import PublicationStatus
from ..release_publication import read_release_body
from ..targets import TARGETS, resolve_releasable_config_dir
from ..utils import run_gh, check_gh_installed, check_gh_auth
from ..workspace import find_workspace_root, resolve_project
from .. import effects


def run_cmd(args, flags, project_root):
    """Yank a version from registries and mark the GitHub Release.

    For each configured target:
    - PUBLISHED: execute registry-specific yank
    - UNPUBLISHED: skip with a message
    - UNPROBEABLE: hard error

    Args:
        args: Positional args; first element is the version to yank.
        flags: dict with keys ``dry-run``, ``reason``, ``use``. There is no
            confirmation key: ``release yank`` declares itself consequential,
            so the framework owns the prompt and ``--approve-consequential``.
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

            # Resolve the releasable this member belongs to.
            from ..workspace import load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
            ws_projects = _load_ws(monorepo_root)
            releasables = load_releasables(monorepo_root, ws_projects)
            rel = resolve_releasable_for_project(project, releasables)
            if rel:
                releasable_name = rel.name
                releasable_tag_fmt = rel.effective_tag_format

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

    # Probe registries for publication status. Each entry is probed in ITS OWN
    # directory: a target declared with a ``path`` has its manifest there, and
    # the project root either holds a different package or none at all.
    probe_results = []
    if entries:
        for entry in entries:
            t = TARGETS[entry.name]
            if t.supports_publication_probe:
                result = t.publication_probe(entry.path, version)
                probe_results.append((t, entry.path, result))
            else:
                # Target without a registry probe: UNPROBEABLE
                from ..publication_probe import PublicationProbeResult
                probe_results.append((t, entry.path, PublicationProbeResult(
                    status=PublicationStatus.UNPROBEABLE,
                    registry=t.name,
                    version=version,
                    message=f"target '{t.name}' does not support publication probing",
                )))

    # Check for hard errors: any UNPROBEABLE with no other evidence
    unprobeable_targets = [
        (t, r) for t, _p, r in probe_results if r.status == PublicationStatus.UNPROBEABLE
    ]
    if unprobeable_targets:
        print("Error: cannot determine publication status for:", file=sys.stderr)
        for t, r in unprobeable_targets:
            print(f"  {t.name}: {r.message}", file=sys.stderr)
        print("\nYank requires certainty about publication status. Cannot proceed.", file=sys.stderr)
        sys.exit(1)

    published = [
        (t, p, r) for t, p, r in probe_results
        if r.status == PublicationStatus.PUBLISHED
    ]
    unpublished = [
        (t, r) for t, _p, r in probe_results
        if r.status == PublicationStatus.UNPUBLISHED
    ]

    # Display plan
    if published:
        print(f"Will yank {tag} from {len(published)} registry(ies):")
        for t, _p, r in published:
            print(f"  {t.name}: {r.message}")
    if unpublished:
        for t, r in unpublished:
            print(f"  {t.name}: skipping ({r.message})")

    # No hand-rolled prompt: `release yank` declares itself `consequential`,
    # so strictcli confirms before dispatch and --approve-consequential skips
    # it, with one prompt wording and one non-interactive error across every
    # rlsbl command.  The per-registry breakdown above is still printed.

    # Execute registry-specific yank for each published target, in the
    # directory that target was detected in.
    for t, target_path, r in published:
        _yank_target(t, target_path, version, tag, reason, dry_run)

    # Mark GitHub release as pre-release with yank notice
    _mark_github_release(tag, reason, use, dry_run)

    if dry_run:
        print(f"\nDry run complete for {tag}.")
    else:
        print(f"\nYanked {tag}.")


def _yank_target(target, target_dir, version, tag, reason, dry_run):
    """Execute the target's own registry-removal action and print its outcome.

    *target_dir* is the directory this target was detected in, not the project
    root: the two differ for a target declared with a ``path``, and the yank
    action reads that target's manifest.

    The dispatch is the target protocol: every target answers ``yank()``, and a
    target with no registry-removal action answers UNSUPPORTED naming itself.
    There is no name comparison here and no fallthrough that could pass over a
    target in silence.
    """
    from ..targets.outcomes import YankStatus

    outcome = target.yank(
        target_dir, version, tag, reason=reason, dry_run=dry_run,
    )
    if outcome.status is YankStatus.DONE:
        print(f"  {outcome.message}")
    elif outcome.status is YankStatus.UNSUPPORTED:
        print(f"  {target.name}: no yank implementation (skipping)", file=sys.stderr)
    else:
        print(f"  {outcome.message}", file=sys.stderr)
    return outcome


def _mark_github_release(tag, reason, use, dry_run):
    """Mark GitHub release as pre-release with a yank notice."""
    notice = _build_yank_notice(reason, use)

    # Read through the one Release-document reader; an unreadable body leaves
    # the notice standing alone rather than aborting the yank.
    try:
        current_body = read_release_body(tag, gh=run_gh)
    except Exception:
        current_body = ""

    new_body = notice + "\n\n" + current_body if current_body else notice

    if dry_run:
        print(f"  github: would mark {tag} as pre-release with yank notice")
        return

    notes_file = f".rlsbl-yank-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    try:
        with effects.open_write(writing_file, "w", encoding="utf-8") as f:
            f.write(new_body)
        effects.rename(writing_file, notes_file)
        run_gh(["release", "edit", tag, "--prerelease", "--notes-file", notes_file])
        print(f"  github: marked {tag} as pre-release")
    finally:
        for tmp in (notes_file, writing_file):
            if os.path.exists(tmp):
                effects.remove(tmp)


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
