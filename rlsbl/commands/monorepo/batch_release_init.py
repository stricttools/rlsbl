"""Scaffold a batch release file for the workspace's releasables, auto-detecting targets and commenting out releasables with no unreleased commits."""

import os
import subprocess
import sys

import tomlkit

from ...release_file import get_batch_release_file_path, is_pristine_batch_release_file
from ...targets import collect_releasable_targets, detect_targets, resolve_releasable_config_dir, TARGETS
from ...utils import commit_scaffold_file
from ...workspace import find_workspace_root, load_workspace
from ... import effects


def _handle_existing_batch(batch_path):
    """Refuse-unless-pristine for an existing batch release file.

    Returns normally (caller should stop) if the file is a still-pristine
    scaffold (idempotent no-op). Calls sys.exit(1) if any section has been
    filled in by an operator -- never overwriting operator data.
    """
    with open(batch_path, "r", encoding="utf-8") as f:
        existing = f.read()
    if is_pristine_batch_release_file(existing):
        print(
            f"{batch_path} already exists and is pristine "
            f"(no bump/description filled in); nothing to do."
        )
        return
    print(
        f"Error: {batch_path} already exists and has been filled in. "
        f"Refusing to overwrite it. Edit it directly, or delete it to "
        f"re-scaffold.",
        file=sys.stderr,
    )
    sys.exit(1)


def _get_unreleased_commit_count(proj, workspace_root, all_projects):
    """Return the number of unreleased commits inside a project's scope.

    The baseline comes from the project's LEDGER -- the highest archived
    release this checkout contains -- and commits since that release are
    counted when they touch a file *proj* owns, or (for a releasable member)
    that releasable's own state directory.  Ownership is resolved against
    *all_projects* -- the whole member list -- so a file under a nested member
    counts for that member alone.  Returns ``(count, last_release)`` where
    ``last_release`` is the version string, or None when nothing is released.

    This used to read ``git tag -l <glob> --sort=-v:refname``, which named
    whatever the tag namespace held rather than what was released.
    """
    from ...changelog.files import get_changes_dir
    from ...changelog.validate import filter_exempt_commits
    from ...git_util import filter_commits_for_scope
    from ...ledger import range_anchor, releases_dir_for_changes_dir
    from ...ownership import OwnershipScope
    from ...workspace import get_releasable_changes_dir

    name = proj["name"]
    path = proj["path"]

    # Determine tag glob for this project
    project_dir = os.path.join(workspace_root, path)
    rel_dir = resolve_releasable_config_dir(proj, workspace_root)
    target_entries = detect_targets(project_dir, releasable_config_dir=rel_dir)
    if target_entries and target_entries[0].name in TARGETS:
        tag_glob = TARGETS[target_entries[0].name].monorepo_tag_glob(name, path=path)
    else:
        tag_glob = f"{name}@v*"

    # The project's own ledger: a releasable member's archives live under the
    # releasable, everyone else's under the package.
    releasable = proj.get("releasable")
    if isinstance(releasable, str) and releasable:
        changes_dir = get_releasable_changes_dir(workspace_root, releasable)
    else:
        changes_dir = get_changes_dir(project_dir)
    anchor = range_anchor(
        releases_dir_for_changes_dir(changes_dir),
        tag_glob=tag_glob, cwd=workspace_root,
    )
    last_release = anchor.version if anchor else None

    # Get commits in range
    range_spec = f"{anchor.candidate_sha}..HEAD" if anchor else "HEAD"
    try:
        result = effects.run(
            ["git", "log", "--format=%H", range_spec],
            capture_output=True, text=True, timeout=30,
            cwd=workspace_root,
        )
        if result.returncode != 0:
            return (0, last_release)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return (0, last_release)

    all_commits = [
        line.strip()
        for line in result.stdout.strip().splitlines()
        if line.strip()
    ]

    # Scope first, then exempt -- the order every other consumer of these two
    # filters uses. The scope is the RELEASABLE's when the member has one, so
    # its state directory (which no member path claims) is inside the scope
    # that reads its ledger, rather than outside every scope.
    if isinstance(releasable, str) and releasable:
        scope = OwnershipScope.for_releasable(all_projects, [proj], releasable)
    else:
        scope = OwnershipScope.for_member(all_projects, proj)
    in_scope = filter_commits_for_scope(
        set(all_commits), scope,
        operation="batch release init commit count",
    )
    relevant = [c for c in all_commits if c in in_scope]
    relevant, _stats = filter_exempt_commits(relevant)

    return (len(relevant), last_release)


def _build_pkg_section(workspace_root, target_names):
    """Build a tomlkit table for a single releasable section."""
    pkg_table = tomlkit.table()
    pkg_table.add(tomlkit.comment("Version bump type: patch, minor, major, infra, or prerelease"))
    pkg_table.add("bump", "")
    pkg_table.add(tomlkit.comment("Short description of this release (required)"))
    pkg_table.add("description", "")
    pkg_table.add(tomlkit.comment("Optional context explaining why these changes were made"))
    pkg_table.add("context", "")
    pkg_table.add(tomlkit.comment("Pre-release channel: alpha, beta, rc, or stable (ordered;"))
    pkg_table.add(tomlkit.comment("demotion is an error). Per package, so one workspace release"))
    pkg_table.add(tomlkit.comment("can ship some packages stable and others as alphas. See"))
    pkg_table.add(tomlkit.comment("docs/release-workflow.md#the-pre-release-channel."))
    pkg_table.add(tomlkit.comment('preid = ""'))
    pkg_table.add("include", target_names)
    pkg_table.add("exclude", [])

    # Add per-target config sections for Flutter target
    flutter_targets = [n for n in target_names if n == "flutter"]
    if flutter_targets:
        targets_table = tomlkit.table(is_super_table=True)
        for ft in flutter_targets:
            t = tomlkit.table()
            t.add("mode", "build")
            targets_table.add(ft, t)
        pkg_table.add("targets", targets_table)

    return pkg_table


def _render_commented_section(name, target_names, reason):
    """Render a releasable section as TOML comments.

    Returns a string of comment lines (each prefixed with '# ') that
    represent the section the user would uncomment if they want to include it.
    """
    lines = [
        f"# {name}: {reason}",
        f"# [releasables.{name}]",
        "# bump = \"\"",
        "# description = \"\"",
        "# context = \"\"",
        f"# include = {tomlkit.item(target_names).as_string()}",
        "# exclude = []",
    ]
    return "\n".join(lines)


def _collect_releasable_targets(releasable_name, member_projects, workspace_root):
    """Collect targets for a releasable.

    Delegates to ``collect_releasable_targets`` in ``rlsbl.targets``.
    Kept as a thin wrapper for backward compatibility with existing callers
    that import this private name.
    """
    return collect_releasable_targets(releasable_name, member_projects, workspace_root)


def _cmd_batch_release_init(project_root, releasables=None):
    """Create .rlsbl-monorepo/releases/unreleased.toml.

    Scaffolds one ``[releasables.<name>]`` section per releasable declared in
    workspace.toml, skipping non-releasable projects, detecting targets for
    each, and writing sections with an empty bump/description and the detected
    include list.

    Items with zero unreleased commits are included as commented-out sections.

    Args:
        project_root: Path to the project root directory.
        releasables: Optional comma-separated string of releasable names to
            include.
    """
    start = str(project_root)
    workspace_root = find_workspace_root(start)
    if workspace_root is None:
        print(
            "Error: No workspace found. Run 'rlsbl monorepo init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    batch_path = get_batch_release_file_path(workspace_root)
    if os.path.exists(batch_path):
        _handle_existing_batch(batch_path)
        return

    projects = load_workspace(workspace_root)
    if not projects:
        print("Error: no projects in workspace.", file=sys.stderr)
        sys.exit(1)

    _scaffold_releasable_sections(
        workspace_root, projects, batch_path, releasables,
    )


def _scaffold_releasable_sections(workspace_root, projects, batch_path, filter_names):
    """Scaffold the [releasables.<name>] sections of a batch release file."""
    from ...workspace import load_releasables, members_of

    releasables = load_releasables(workspace_root, projects)

    # Parse filter
    requested_names = None
    if filter_names:
        requested_names = [n.strip() for n in filter_names.split(",") if n.strip()]
        all_names = {r.name for r in releasables}
        unknown = [n for n in requested_names if n not in all_names]
        if unknown:
            print(
                f"Error: unknown releasable(s): {', '.join(unknown)}. "
                f"Available: {', '.join(sorted(all_names))}",
                file=sys.stderr,
            )
            sys.exit(1)

    doc = tomlkit.document()
    section_table = tomlkit.table(is_super_table=True)

    any_added = False
    commented_sections = []

    for rel in releasables:
        if requested_names is not None and rel.name not in requested_names:
            continue

        member_projs = members_of(rel.name, projects)
        if not member_projs:
            print(f"Warning: releasable '{rel.name}' has no member projects, skipping.", file=sys.stderr)
            continue

        target_names = _collect_releasable_targets(rel.name, member_projs, workspace_root)
        if not target_names:
            print(f"Warning: no targets detected for releasable '{rel.name}', skipping.", file=sys.stderr)
            continue

        # Check unreleased commits across all member projects
        total_commits = 0
        last_release = None
        for proj in member_projs:
            count, released = _get_unreleased_commit_count(proj, workspace_root, projects)
            total_commits += count
            if released is not None:
                last_release = released

        if total_commits == 0 and last_release is not None:
            reason = f"no unreleased commits since {last_release}"
            commented_sections.append((rel.name, target_names, reason))
            any_added = True
            continue

        pkg_table = _build_pkg_section(workspace_root, target_names)
        section_table.add(rel.name, pkg_table)
        any_added = True

    if not any_added:
        print("Error: no eligible releasables with detected targets.", file=sys.stderr)
        sys.exit(1)

    doc.add("releasables", section_table)

    releases_dir = os.path.dirname(batch_path)
    effects.makedirs(releases_dir, exist_ok=True)

    toml_text = tomlkit.dumps(doc)

    if commented_sections:
        comment_blocks = []
        for name, target_names, reason in commented_sections:
            comment_blocks.append(
                _render_commented_section(name, target_names, reason)
            )
        toml_text = toml_text.rstrip("\n") + "\n\n" + "\n\n".join(comment_blocks) + "\n"

    # Atomic exclusive-create closes the TOCTOU (see _cmd_batch_release_init's
    # exists() check); on collision, re-run refuse-unless-pristine.
    try:
        f = effects.open_exclusive(batch_path, file_mode=0o644)
    except FileExistsError:
        _handle_existing_batch(batch_path)
        return
    with f:
        f.write(toml_text)

    commit_scaffold_file(
        "release: scaffold unreleased.toml", [batch_path],
        cwd=workspace_root, expected_root=workspace_root,
    )

    print(batch_path)
