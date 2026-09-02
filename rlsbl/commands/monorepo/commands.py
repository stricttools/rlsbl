"""Monorepo workspace management commands: init, add, remove, list, status, outdated, and check-names."""

import os
import sys
import time

from ...ownership import OwnershipScope
from ...utils import commit_files
from ...workspace import find_workspace_root, load_workspace, save_workspace, WorkspaceProject, WORKSPACE_DIR, WORKSPACE_FILE
from ...workspace_graph import WorkspaceGraph
from ...targets import detect_targets, resolve_releasable_config_dir, TARGETS, TargetEntry


def _cmd_init(flags, project_root):
    root_dir = str(project_root)
    ws_file = os.path.join(root_dir, WORKSPACE_DIR, WORKSPACE_FILE)
    if os.path.isfile(ws_file):
        print("Error: Workspace already initialized.", file=sys.stderr)
        sys.exit(1)

    from ...ownership import ROOT_MEMBER_NAME, ROOT_MEMBER_PATH
    from ...workspace import Releasable

    # Every workspace has a root member, and its KIND is a decision only the
    # operator can make: a dev node whose root files need no changelog
    # coverage, or a member of a named releasable whose root files do.
    root_releasable = flags.get("root-releasable") or None
    root_tag_format = flags.get("root-tag-format") or None
    root_dev_node = bool(flags.get("root-dev-node"))

    if not root_dev_node and not root_releasable:
        print(
            "Error: the root member's kind must be declared. Every workspace "
            "declares the repository root as a member, and it is either:\n"
            "  --root-dev-node                  a dev node -- root files are "
            "exempt from changelog coverage\n"
            "  --root-releasable <name> --tag-format <fmt>\n"
            "                                   a member of a named releasable "
            "-- root files get changelog coverage,\n"
            "                                   and the releasable's tags use "
            "the format you name\n"
            "There is no default: which one a repository wants depends on "
            "whether its root files ship to users.",
            file=sys.stderr,
        )
        sys.exit(1)

    if root_releasable and not root_tag_format:
        print(
            "Error: --tag-format is required with --root-releasable. A "
            "releasable that owns the repository root must never inherit a "
            "default tag format: pass \"v{version}\" for bare version tags, or "
            "\"{name}@v{version}\" for the workspace scheme.",
            file=sys.stderr,
        )
        sys.exit(1)

    root_member = {"path": ROOT_MEMBER_PATH, "name": ROOT_MEMBER_NAME}
    if root_releasable:
        root_member["releasable"] = root_releasable
        releasables = [Releasable(name=root_releasable, tag_format=root_tag_format)]
    else:
        # A dev-node root member is both: dev_only (nothing user-facing may
        # depend on it) and outside every releasable (it is never released).
        root_member["dev_only"] = True
        root_member["releasable"] = False
        releasables = []

    save_workspace(root_dir, [root_member], releasables=releasables)
    print("Initialized monorepo workspace in .rlsbl-monorepo/")
    if root_releasable:
        print(
            f"Root member '{ROOT_MEMBER_NAME}' belongs to releasable "
            f"'{root_releasable}' (tag format: {root_tag_format})."
        )
    else:
        print(f"Root member '{ROOT_MEMBER_NAME}' is a dev node.")

    rel_ws_file = os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)
    if not flags.get("auto-commit", True):
        print(f"Skipped commit (--no-auto-commit). Run `safegit commit -- {rel_ws_file}` manually.")
        return

    # Auto-commit workspace.toml
    commit_files("monorepo: init workspace", [rel_ws_file], allow_failure=True)


def _create_releasable(name, tag_format_flag, target_entries, path, *, adding_root):
    """The Releasable an add naming an undeclared group creates.

    Its ``tag_format`` is written out explicitly, either as the operator stated
    it (``--tag-format``) or as the member's primary target implies -- the same
    derivation ``monorepo absorb`` uses for its auto-singleton
    (:func:`rlsbl.tag_glob.derive_releasable_tag_format`), so a member's targets
    imply one format with one answer whichever command creates the releasable.

    The ROOT member is the exception: a releasable that owns the repository root
    is commonly tagged under the scheme the repository used while it was
    standalone, and only the operator can read the existing tags. It is required
    to state the format, exactly as ``monorepo init --root-releasable`` requires
    it.
    """
    from ...errors import MixedTagSchemeError
    from ...tag_glob import derive_releasable_tag_format
    from ...workspace import Releasable

    if tag_format_flag:
        return Releasable(name=name, tag_format=tag_format_flag)

    if adding_root:
        print(
            f"Error: --tag-format is required to create releasable '{name}' "
            f"for the root member. A releasable that owns the repository root "
            f"must never inherit a derived tag format: a repository that used "
            f"to be standalone already has tags, and only you can read which "
            f"scheme they use. Pass --tag-format \"v{{version}}\" for bare "
            f"version tags, or --tag-format \"{{name}}@v{{version}}\" for the "
            f"workspace scheme.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        tag_format = derive_releasable_tag_format(
            target_entries, name, path, subject=f"member dir '{path}'",
        )
    except MixedTagSchemeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    return Releasable(name=name, tag_format=tag_format)


def _cmd_add(args, flags, project_root, dry_run=False):
    if not args:
        print("Error: Usage: rlsbl monorepo add <path> [--name <name>]", file=sys.stderr)
        sys.exit(1)

    path = args[0]
    if not os.path.isdir(path):
        print(f"Error: '{path}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    explicit_target = flags.get("target")
    if explicit_target:
        if explicit_target not in TARGETS:
            print(f"Error: Unknown target '{explicit_target}'.", file=sys.stderr)
            valid = ", ".join(sorted(TARGETS))
            print(f"Valid targets: {valid}", file=sys.stderr)
            sys.exit(1)
        target_entries = [TargetEntry(name=explicit_target, path=path)]
    else:
        target_entries = detect_targets(path)
        if not target_entries:
            print(f"Error: No release target detected in '{path}'. Initialize a project first.", file=sys.stderr)
            print("Hint: create a project manifest (e.g., package.json, pyproject.toml, go.mod, version.json) in the directory.", file=sys.stderr)
            sys.exit(1)

    from ...ownership import ROOT_MEMBER_NAME, ROOT_MEMBER_PATH
    from ...workspace import is_root_path

    adding_root = is_root_path(path)
    if adding_root:
        path = ROOT_MEMBER_PATH
        name = flags.get("name") or ROOT_MEMBER_NAME
        if name != ROOT_MEMBER_NAME:
            print(
                f"Error: the root member (path \".\") is named "
                f"'{ROOT_MEMBER_NAME}' and nothing else -- job keys, router "
                f"filters and check regexes are derived from that name. Drop "
                f"--name, or pass --name {ROOT_MEMBER_NAME}.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        name = flags.get("name") or os.path.basename(path.rstrip("/"))
        if name == ROOT_MEMBER_NAME:
            print(
                f"Error: '{ROOT_MEMBER_NAME}' is reserved for the member that "
                f"owns the repository root (path \".\"). Choose a different "
                f"--name for the member at '{path}'.",
                file=sys.stderr,
            )
            sys.exit(1)

    depends_on_raw = flags.get("depends-on")
    library_raw = flags.get("library")
    dev_only_raw = flags.get("dev_only")
    releasable_raw = flags.get("releasable")
    registry_name = flags.get("registry-name") or ""
    tag_format_flag = flags.get("tag-format") or ""

    # Parse --library as boolean
    library = None
    if library_raw is not None:
        if library_raw == "true":
            library = True
        elif library_raw == "false":
            library = False
        else:
            print(f"Error: --library must be 'true' or 'false', got '{library_raw}'.", file=sys.stderr)
            sys.exit(1)

    # Parse --dev-only as boolean
    dev_only = None
    if dev_only_raw is not None:
        if dev_only_raw == "true":
            dev_only = True
        elif dev_only_raw == "false":
            dev_only = False
        else:
            print(f"Error: --dev-only must be 'true' or 'false', got '{dev_only_raw}'.", file=sys.stderr)
            sys.exit(1)

    # Parse --releasable as string name or "false"
    releasable_value = None  # None means "not set" (omit from project)
    if releasable_raw is not None:
        if releasable_raw == "false":
            releasable_value = False
        elif releasable_raw:
            releasable_value = releasable_raw
        # Empty string means flag not passed (default="")

    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    norm_path = path.rstrip("/")
    for proj in projects:
        if proj["path"].rstrip("/") == norm_path:
            print(f"Error: Project at '{path}' already exists in workspace.", file=sys.stderr)
            sys.exit(1)
        if proj["name"] == name:
            print(f"Error: Project named '{name}' already exists in workspace.", file=sys.stderr)
            sys.exit(1)

    # Every workspace declares its releasables, so --releasable is required
    from ...workspace import load_releasables
    if releasable_value is None:
        print(
            "Error: --releasable is required: every workspace declares its "
            "releasables in [[releasables]]. "
            "Use --releasable <name> or --releasable false.",
            file=sys.stderr,
        )
        sys.exit(1)

    # A releasable this add NAMES but the workspace does not declare is created
    # here, as the auto-singleton `monorepo absorb` creates for an arriving
    # member: one [[releasables]] entry, with its tag_format written out
    # explicitly rather than inherited by accident. A name the workspace already
    # declares is joined, and that releasable already owns its format.
    releasables = None
    created_releasable = None
    if isinstance(releasable_value, str):
        releasables = load_releasables(root, projects)
        defined_names = {r.name for r in releasables}
        if releasable_value in defined_names:
            if tag_format_flag:
                declared = next(
                    r for r in releasables if r.name == releasable_value
                ).effective_tag_format
                print(
                    f"Error: --tag-format applies only to the releasable this "
                    f"command creates. Releasable '{releasable_value}' already "
                    f"exists and declares its own tag format ('{declared}'); "
                    f"change it in workspace.toml if it is wrong.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            created_releasable = _create_releasable(
                releasable_value, tag_format_flag, target_entries, path,
                adding_root=adding_root,
            )
            releasables = list(releasables) + [created_releasable]
    elif tag_format_flag:
        print(
            "Error: --tag-format is the format of the releasable this command "
            "creates, and --releasable false creates none -- the member opts "
            "out of versioning entirely. Drop --tag-format, or name the "
            "releasable this member belongs to.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate --depends-on against existing project names
    depends_on = None
    if depends_on_raw:
        depends_on = [d.strip() for d in depends_on_raw.split(",")]
        existing_names = {proj["name"] for proj in projects}
        for dep_name in depends_on:
            if dep_name not in existing_names:
                print(f"Error: Dependency '{dep_name}' does not exist in workspace.", file=sys.stderr)
                sys.exit(1)

    project = {"path": path, "name": name}
    if depends_on:
        project["depends_on"] = depends_on
    if library is True:
        project["library"] = True
    if dev_only is True:
        project["dev_only"] = True
    if releasable_value is not None:
        project["releasable"] = releasable_value
    if registry_name:
        project["registry_name"] = registry_name

    # Honest plan boundary: validation above has fully run. In dry-run we report
    # exactly what would happen and make ZERO mutations -- no workspace write, no
    # scaffold, no sync. Anything below this point mutates state.
    if dry_run:
        print(f"Would add project '{name}' at {path}")
        print(f"  workspace.toml entry: {project}")
        if created_releasable is not None:
            print(
                f"  Would create releasable '{created_releasable.name}' "
                f"(tag format: {created_releasable.tag_format})"
            )
        project_rlsbl = os.path.join(path, ".rlsbl", "config.json")
        if not os.path.exists(project_rlsbl):
            print(f"  Would scaffold '{name}' (no .rlsbl/config.json present)")
        else:
            print(f"  Would skip scaffold ('{name}' already scaffolded)")
        print("  Would run: rlsbl monorepo sync (regenerate CI workflows)")
        return

    projects.append(project)
    # ``releasables`` is the full desired list only when this add created one;
    # otherwise it stays None so the existing section is preserved untouched.
    save_workspace(
        root, projects, releasables=releasables if created_releasable else None,
    )
    print(f"Added project '{name}' at {path}")
    if created_releasable is not None:
        print(
            f"Created releasable '{created_releasable.name}' "
            f"(tag format: {created_releasable.tag_format}). Its state "
            f"directory is scaffolded by the `rlsbl monorepo sync` below."
        )

    no_commit = not flags.get("auto-commit", True)
    ws_file = os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)

    if no_commit:
        print(f"Skipped commit (--no-auto-commit). Run `safegit commit -- {ws_file}` manually.")
    else:
        # Commit workspace.toml
        commit_files(f"monorepo: add {name}", [ws_file], allow_failure=True)

    # Auto-scaffold if not already scaffolded
    project_rlsbl = os.path.join(path, ".rlsbl", "config.json")
    if not os.path.exists(project_rlsbl):
        print(f"Scaffolding {name}...")
        try:
            # -P: suppress CWD injection from ``python -m`` run in a foreign dir
            # (a root module shadowing a stdlib/dep name would break rlsbl imports).
            # No confirmation-skip flag: `scaffold` is `mutating` but not
            # `consequential`, so strictcli never prompts for it.
            cmd = [sys.executable, "-P", "-m", "rlsbl", "scaffold"]
            if explicit_target:
                cmd.extend(["--target", explicit_target])
            if no_commit:
                cmd.append("--no-auto-commit")
            effects.run(
                cmd,
                cwd=path,
                check=False,
            )
        except Exception as e:
            print(f"Warning: scaffold failed: {e}", file=sys.stderr)

    # Sync CI workflows
    try:
        sync_cmd = [sys.executable, "-P", "-m", "rlsbl", "monorepo", "sync"]
        if no_commit:
            sync_cmd.append("--no-auto-commit")
        effects.run(
            sync_cmd,
            cwd=root,
            check=False,
        )
    except Exception:
        pass


def _cmd_remove(args, flags, project_root):
    if not args:
        print("Error: Usage: rlsbl monorepo remove <path>", file=sys.stderr)
        sys.exit(1)

    path = args[0]

    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    norm_path = path.rstrip("/")
    new_projects = [p for p in projects if p["path"].rstrip("/") != norm_path]

    if len(new_projects) == len(projects):
        print(f"Warning: Project at '{path}' not found in workspace.", file=sys.stderr)
        return

    save_workspace(root, new_projects)
    print(f"Removed project at {path}")


def _cmd_list(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    if not projects:
        print("No projects in workspace.")
        return

    name_width = max(len("Name"), max(len(p["name"]) for p in projects))
    header_name = "Name".ljust(name_width)
    print(f"{header_name}  Path")
    for proj in projects:
        name_col = proj["name"].ljust(name_width)
        print(f"{name_col}  {proj['path']}")


def _latest_release_for_row(changes_dir, tag_glob):
    """The release a status row reports, from that row's own LEDGER.

    Returns ``(fact, anchor)``: the project's latest archived release as a
    displayable fact (annotated when this checkout does not contain it), and
    the highest archived release this checkout DOES contain, which is what
    bounds the coverage range. The two differ exactly when the checkout
    predates a release, and the table shows both rather than collapsing them.

    This used to be ``git tag -l <glob> --sort=-v:refname``, which reported
    whatever the tag namespace happened to hold.
    """
    from ...ledger import (
        latest_release_fact,
        range_anchor,
        releases_dir_for_changes_dir,
    )

    releases_dir = releases_dir_for_changes_dir(changes_dir)
    return (
        latest_release_fact(releases_dir, tag_glob=tag_glob),
        range_anchor(releases_dir, tag_glob=tag_glob),
    )


def _coverage_column(anchor, changes_dir, scope):
    """Return the Coverage-column string for one status row.

    Real JSONL coverage: the commits since *anchor* -- the ledger entry for
    the highest archived release this checkout contains -- scoped to the row's
    members via *scope* (an :class:`~rlsbl.ownership.OwnershipScope`, which
    carries the whole member list), minus the exempt ones, cross-referenced
    against the row's
    ``unreleased.jsonl``. Rendered ``covered/tracked`` with a
    ``(N exempted)`` suffix, matching ``rlsbl status``.

    This column used to count CHANGELOG.md bullet lines above the last version
    heading. CHANGELOG.md is regenerated from the JSONL at release time, so
    that count described the *previous* release's prose, never whether the
    current unreleased commits had entries -- and it read as "documented" when
    no entry existed at all.

    ``"no changelog"`` when the changes directory is missing.
    """
    if not os.path.isdir(changes_dir):
        return "no changelog"

    from ...changelog.files import read_unreleased
    from ...changelog.resolve import _git_log_hashes, resolve_hashes
    from ...changelog.validate import filter_exempt_commits
    from ...git_util import filter_commits_for_scope

    range_spec = f"{anchor.candidate_sha}..HEAD" if anchor else "HEAD"
    commits = _git_log_hashes(range_spec)
    # Scope first, then exempt -- the order the authoritative coverage check
    # uses, so an unrelated package's changelog churn is never counted here.
    if scope is not None:
        in_scope = filter_commits_for_scope(
            set(commits), scope, operation="monorepo status coverage",
        )
        commits = [c for c in commits if c in in_scope]
    non_exempt, _stats = filter_exempt_commits(commits)
    exempted = len(commits) - len(non_exempt)

    all_hashes = []
    for entry in read_unreleased(changes_dir):
        all_hashes.extend(entry.commits)
    resolved = resolve_hashes(all_hashes)
    covered_shas = {full for full in resolved.values() if full is not None}

    covered = sum(1 for c in non_exempt if c in covered_shas)
    suffix = f" ({exempted} exempted)" if exempted else ""
    return f"{covered}/{len(non_exempt)}{suffix}"


def _cmd_status_explicit(root, projects):
    """Render per-releasable status rows for a workspace.

    One row per releasable (version, tag, coverage, member count + names),
    plus one row for each standalone project not belonging to any releasable.
    Tag globs come from the shared resolver so releasable members resolve
    their releasable's tag_format instead of a per-member glob.
    """
    from ...workspace import (
        get_releasable_changes_dir,
        load_releasables,
        members_of,
        read_releasable_version,
    )
    from ...changelog.files import get_changes_dir
    from ...tag_glob import resolve_monorepo_tag_glob

    releasables = load_releasables(root, projects)
    rows = []  # (name, kind, version, tag, coverage, members)
    claimed = set()

    for rel in releasables:
        members = members_of(rel.name, projects)
        for m in members:
            claimed.add(m["name"])
        try:
            version = read_releasable_version(root, rel.name) or "?"
        except Exception:
            version = "?"
        tag_glob = resolve_monorepo_tag_glob(None, root, releasable=rel)
        rel_changes = get_releasable_changes_dir(root, rel.name)
        fact, anchor = _latest_release_for_row(rel_changes, tag_glob)
        coverage = _coverage_column(
            anchor, rel_changes,
            OwnershipScope.for_releasable(projects, members, rel.name),
        )
        member_names = ", ".join(m["name"] for m in members)
        members_col = f"{len(members)} ({member_names})" if members else "0"
        rows.append((rel.name, "releasable", str(version), fact.label(), coverage, members_col))

    for proj in projects:
        if proj["name"] in claimed:
            continue
        name = proj["name"]
        path = proj["path"]
        rel_dir = resolve_releasable_config_dir(proj, root)
        target_entries = detect_targets(os.path.join(root, path), releasable_config_dir=rel_dir)
        version = "?"
        if target_entries and target_entries[0].name in TARGETS:
            try:
                version = TARGETS[target_entries[0].name].read_version(target_entries[0].path)
            except Exception:
                version = "?"
        tag_glob = resolve_monorepo_tag_glob(proj, root, releasable=None)
        proj_changes = get_changes_dir(os.path.join(root, path))
        fact, anchor = _latest_release_for_row(proj_changes, tag_glob)
        coverage = _coverage_column(
            anchor, proj_changes,
            OwnershipScope.for_member(projects, proj),
        )
        rows.append((name, "project", str(version), fact.label(), coverage, "-"))

    headers = ("Name", "Kind", "Version", "Released", "Coverage", "Members")
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(len(headers)):
            widths[i] = max(widths[i], len(str(row[i])))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def _cmd_status(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    if not projects:
        print("No projects in workspace.")
        return

    # Every workspace declares its releasables, so the per-releasable summary
    # (versions, tags, coverage, members) is always rendered first. The rich
    # per-project table below still follows it: the two answer different
    # questions -- what is released, and what each member is -- and the
    # per-project columns (target, path, deps, remote) exist nowhere else.
    _cmd_status_explicit(root, projects)
    print()

    # Build dependency graph
    graph = WorkspaceGraph(root, projects)

    # Releasable membership, for the column display
    from ...workspace import (
        load_releasables,
        mirror_remote_for,
        resolve_releasable_for_project,
    )
    releasable_map = {}  # project name -> releasable name
    releasables = load_releasables(root, projects)
    for proj in projects:
        rel = resolve_releasable_for_project(proj, releasables)
        releasable_map[proj["name"]] = rel.name if rel else ""

    rows = []
    for proj in projects:
        name = proj["name"]
        path = proj["path"]

        # Detect targets
        rel_dir = resolve_releasable_config_dir(proj, root)
        target_entries = detect_targets(os.path.join(root, path), releasable_config_dir=rel_dir)
        target_names = [e.name for e in target_entries]
        target_display = ", ".join(target_names) if target_names else "none"

        # Read version (use first target -- one version per project)
        version = "?"
        first_target_name = target_entries[0].name if target_entries else None
        if first_target_name and first_target_name in TARGETS:
            try:
                version = TARGETS[first_target_name].read_version(target_entries[0].path)
            except Exception:
                version = "?"

        # The tag glob names this package's tag scheme; the release itself
        # comes from its LEDGER. Releasable members read the releasable's
        # changes dir -- and therefore its archives -- not the package's.
        if first_target_name and first_target_name in TARGETS:
            tag_glob = TARGETS[first_target_name].monorepo_tag_glob(name, path=path)
        else:
            tag_glob = f"{name}@v*"

        from ...changelog.files import get_changes_dir
        _cl_changes_dir = None
        _cl_rel_name = releasable_map.get(name, "")
        if _cl_rel_name:
            from ...workspace import get_releasable_changes_dir
            _cl_changes_dir = get_releasable_changes_dir(root, _cl_rel_name)
        _changes_dir = _cl_changes_dir or get_changes_dir(os.path.join(root, path))
        fact, anchor = _latest_release_for_row(_changes_dir, tag_glob)
        coverage_str = _coverage_column(
            anchor, _changes_dir, OwnershipScope.for_member(projects, proj),
        )

        # Dependency counts
        deps_count = graph.dep_count(name)
        rdeps_count = graph.rdep_count(name)
        deps_str = str(deps_count) if deps_count else "0"
        rdeps_str = str(rdeps_count) if rdeps_count else "0"

        # Library flag
        library_str = "yes" if proj.get("library", False) else ""

        # Dev-only flag
        dev_only_str = "yes" if proj.dev_only else ""

        # Subtree remote -- declared by the releasable this member belongs to
        remote = mirror_remote_for(proj, releasables)
        remote_str = remote if remote else "-"

        # Releasable membership
        releasable_str = releasable_map.get(name, "")

        rows.append((name, path, target_display, version, fact.label(), coverage_str, library_str, dev_only_str, deps_str, rdeps_str, remote_str, releasable_str))

    # Determine which dynamic columns to show
    any_library = any(row[6] != "" for row in rows)
    any_dev_only = any(row[7] != "" for row in rows)
    any_deps = any(row[8] != "0" for row in rows)
    any_rdeps = any(row[9] != "0" for row in rows)
    any_remote = any(row[10] != "-" for row in rows)
    any_releasable = any(row[11] != "" for row in rows)

    # Calculate column widths
    base_headers = ("Project", "Path", "Target", "Version", "Released", "Coverage")
    if any_releasable:
        base_headers = base_headers + ("Releasable",)
    if any_library:
        base_headers = base_headers + ("Library",)
    if any_dev_only:
        base_headers = base_headers + ("DevOnly",)
    if any_deps:
        base_headers = base_headers + ("Deps",)
    if any_rdeps:
        base_headers = base_headers + ("Rdeps",)
    if any_remote:
        base_headers = base_headers + ("Remote",)
    headers = base_headers

    # Build display rows matching the dynamic header order
    display_rows = []
    for row in rows:
        cells = list(row[:6])  # base columns: name, path, target, version, tag, unreleased
        if any_releasable:
            cells.append(row[11])
        if any_library:
            cells.append(row[6])
        if any_dev_only:
            cells.append(row[7])
        if any_deps:
            cells.append(row[8])
        if any_rdeps:
            cells.append(row[9])
        if any_remote:
            cells.append(row[10])
        display_rows.append(tuple(cells))

    widths = [len(h) for h in headers]
    for cells in display_rows:
        for i in range(len(headers)):
            widths[i] = max(widths[i], len(cells[i]))

    # Print header
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)

    # Print rows
    for cells in display_rows:
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))
        print(line)


# Canonical definitions live in rlsbl.constraints; imported here for
# backward compatibility with callers that import via commands.monorepo.
from ...constraints import _evaluate_constraint, _parse_version_tuple  # noqa: F401
from ... import effects


def _cmd_outdated(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    graph = WorkspaceGraph(root, projects)

    # Build a lookup: project name -> (target_name, target_path) for version reading
    project_version_info = {}
    for proj in projects:
        name = proj["name"]
        path = proj["path"]
        rel_dir = resolve_releasable_config_dir(proj, root)
        target_entries = detect_targets(os.path.join(root, path), releasable_config_dir=rel_dir)
        if target_entries and target_entries[0].name in TARGETS:
            project_version_info[name] = (target_entries[0].name, target_entries[0].path)

    rows = []
    for proj in projects:
        name = proj["name"]
        deps = graph.dependencies(name)
        for dep in deps:
            # Read the dependency's current version
            current_version = "?"
            if dep.name in project_version_info:
                target_name, target_path = project_version_info[dep.name]
                try:
                    current_version = TARGETS[target_name].read_version(target_path)
                except Exception:
                    current_version = "?"

            # Determine status
            if dep.dep_type == "workspace":
                status = "workspace"
            elif dep.dep_type == "path":
                status = "path"
            elif dep.dep_type == "explicit":
                status = "explicit"
            else:
                status = _evaluate_constraint(dep.constraint, current_version)

            constraint_display = "(explicit)" if dep.dep_type == "explicit" else dep.constraint
            rows.append((name, dep.name, constraint_display, current_version, status))

    if not rows:
        print("No intra-workspace dependencies found.")
        return

    # Print table
    headers = ("Project", "Dependency", "Constraint", "Current", "Status")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)

    for row in rows:
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        print(line)


def _cmd_release_order(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    from ...workspace_graph import CycleError

    graph = WorkspaceGraph(root, projects)
    project_names = [p["name"] for p in projects]

    try:
        order = graph.topological_order()
    except CycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    all_independent = all(graph.dep_count(p) == 0 for p in project_names)

    if all_independent:
        print("All projects are independent (no intra-workspace dependencies).")
        print()
        for name in sorted(project_names):
            print(f"  {name}")
    else:
        print("Release order (leaves first):")
        print()
        for i, name in enumerate(order, 1):
            print(f"  {i}. {name}")


def _cmd_check_names(args, flags, project_root):
    target = flags.get("target")
    if not target:
        print("Error: --target is required. Usage: rlsbl monorepo check-names --target <npm|pypi|go|github>", file=sys.stderr)
        sys.exit(1)

    prefix = flags.get("prefix", "")
    suffix = flags.get("suffix", "")
    delay_ms = int(flags.get("delay", "200"))

    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    from ..check import _check_single_name, _format_table_row

    from ...workspace import project_is_dev_only

    # A dev node publishes nothing, so it has no registry identity to check --
    # and every workspace has at least one (its root member, when that member
    # is a dev node). Asking a registry about it is pointless contact.
    projects = [p for p in projects if not project_is_dev_only(p)]
    if not projects:
        print("No publishable projects in workspace.")
        return

    rows = []
    for i, proj in enumerate(projects):
        # A project's registry_name IS its registry identity: use it verbatim,
        # bypassing prefix/suffix. Only fall back to prefix+name+suffix when no
        # registry_name is declared.
        registry_name = proj.registry_name if isinstance(proj, WorkspaceProject) else proj.get("registry_name", "")
        if registry_name:
            checked_name = registry_name
        else:
            checked_name = prefix + proj["name"] + suffix
        result = _check_single_name(checked_name, target)
        table_row = _format_table_row(result)
        rows.append({
            "project": proj["name"],
            "checked_name": checked_name,
            "status": table_row["status"],
        })
        if i < len(projects) - 1:
            time.sleep(delay_ms / 1000)

    # Compute column widths
    proj_width = max(len("Project"), max(len(r["project"]) for r in rows))
    name_width = max(len("Checked Name"), max(len(r["checked_name"]) for r in rows))
    status_width = max(len("Status"), max(len(r["status"]) for r in rows))

    header = f"{'Project':<{proj_width}}  {'Checked Name':<{name_width}}  {'Status':<{status_width}}"
    print(header)
    for row in rows:
        line = f"{row['project']:<{proj_width}}  {row['checked_name']:<{name_width}}  {row['status']:<{status_width}}"
        print(line)

    # Summary line
    available_count = sum(1 for r in rows if r["status"] in ("available", "not found"))
    taken_count = sum(1 for r in rows if r["status"] in ("taken", "exists", "CONFLICT"))
    error_count = sum(1 for r in rows if r["status"] == "error")
    total = len(rows)
    if error_count:
        print(f"\nSummary: {available_count} available, {taken_count} taken, {error_count} error(s) ({total} total)")
    else:
        print(f"\nSummary: {available_count} available, {taken_count} taken ({total} total)")

    # Batch context note
    msg = f"Checked with {delay_ms}ms delay between names."
    if delay_ms == 200:
        msg += " Increase --delay if rate limited."
    print(msg)
