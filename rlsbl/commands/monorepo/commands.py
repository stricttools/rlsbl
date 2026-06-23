"""Monorepo workspace management commands: init, add, remove, list, status, outdated, release-order, and check-names."""

import os
import re
import subprocess
import sys
import time

from ...git_util import validate_subtree_remote_ssh_host
from ...utils import commit_files
from ...workspace import find_workspace_root, load_workspace, save_workspace, WORKSPACE_DIR, WORKSPACE_FILE
from ...workspace_graph import WorkspaceGraph
from ...targets import detect_targets, TARGETS, TargetEntry


def _cmd_init(flags, project_root):
    root_dir = str(project_root)
    ws_file = os.path.join(root_dir, WORKSPACE_DIR, WORKSPACE_FILE)
    if os.path.isfile(ws_file):
        print("Error: Workspace already initialized.", file=sys.stderr)
        sys.exit(1)
    save_workspace(root_dir, [])
    print("Initialized monorepo workspace in .rlsbl-monorepo/")

    rel_ws_file = os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)
    if flags.get("no-commit"):
        print(f"Skipped commit (--no-commit). Run `safegit commit -- {rel_ws_file}` manually.")
        return

    # Auto-commit workspace.toml
    commit_files("monorepo: init workspace", [rel_ws_file], allow_failure=True)


def _cmd_add(args, flags, project_root):
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

    name = flags.get("name") or os.path.basename(path.rstrip("/"))
    watch_raw = flags.get("watch")
    subtree_remote = flags.get("subtree-remote")
    depends_on_raw = flags.get("depends-on")
    library_raw = flags.get("library")
    dev_only_raw = flags.get("dev_only")
    releasable_raw = flags.get("releasable")

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

    # In explicit mode, --releasable is required
    from ...workspace import is_explicit_mode, load_releasables
    explicit = is_explicit_mode(root)
    if explicit and releasable_value is None:
        print(
            "Error: --releasable is required in explicit mode "
            "(workspace has [[releasables]] defined). "
            "Use --releasable <name> or --releasable false.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate releasable name exists in [[releasables]]
    if isinstance(releasable_value, str) and explicit:
        releasables = load_releasables(root, projects)
        defined_names = {r.name for r in releasables}
        if releasable_value not in defined_names:
            print(
                f"Error: releasable '{releasable_value}' is not defined in "
                f"[[releasables]]. Available: {sorted(defined_names)}",
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

    # Validate SSH host consistency between subtree_remote and origin
    if subtree_remote:
        validate_subtree_remote_ssh_host(subtree_remote, root)

    project = {"path": path, "name": name}
    if watch_raw:
        project["watch"] = [w.strip() for w in watch_raw.split(",")]
    if subtree_remote:
        project["subtree_remote"] = subtree_remote
    if depends_on:
        project["depends_on"] = depends_on
    if library is True:
        project["library"] = True
    if dev_only is True:
        project["dev_only"] = True
    if releasable_value is not None:
        project["releasable"] = releasable_value
    projects.append(project)
    save_workspace(root, projects)
    print(f"Added project '{name}' at {path}")

    no_commit = bool(flags.get("no-commit"))
    ws_file = os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)

    if no_commit:
        print(f"Skipped commit (--no-commit). Run `safegit commit -- {ws_file}` manually.")
    else:
        # Commit workspace.toml
        commit_files(f"monorepo: add {name}", [ws_file], allow_failure=True)

    # Auto-scaffold if not already scaffolded
    project_rlsbl = os.path.join(path, ".rlsbl", "config.json")
    if not os.path.exists(project_rlsbl):
        print(f"Scaffolding {name}...")
        try:
            cmd = [sys.executable, "-m", "rlsbl", "scaffold"]
            if explicit_target:
                cmd.extend(["--target", explicit_target])
            if no_commit:
                cmd.append("--no-commit")
            subprocess.run(
                cmd,
                cwd=path,
                check=False,
            )
        except Exception as e:
            print(f"Warning: scaffold failed: {e}", file=sys.stderr)

    # Sync CI workflows
    try:
        sync_cmd = [sys.executable, "-m", "rlsbl", "monorepo", "sync"]
        if no_commit:
            sync_cmd.append("--no-commit")
        subprocess.run(
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

    # Build dependency graph
    graph = WorkspaceGraph(root, projects)

    # Detect explicit releasable mode for column display
    from ...workspace import is_explicit_mode
    explicit = is_explicit_mode(root)
    releasable_map = {}  # project name -> releasable name
    if explicit:
        from ...workspace import load_releasables, resolve_releasable_for_project
        releasables = load_releasables(root, projects)
        for proj in projects:
            rel = resolve_releasable_for_project(proj, releasables)
            releasable_map[proj["name"]] = rel.name if rel else ""

    rows = []
    for proj in projects:
        name = proj["name"]
        path = proj["path"]

        # Detect targets
        target_entries = detect_targets(os.path.join(root, path))
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

        # Find latest tag using target-aware glob
        latest_tag = "(none)"
        latest_tag_version = None
        try:
            if first_target_name and first_target_name in TARGETS:
                tag_glob = TARGETS[first_target_name].monorepo_tag_glob(name, path=path)
            else:
                tag_glob = f"{name}@v*"
            result = subprocess.run(
                ["git", "tag", "-l", tag_glob, "--sort=-v:refname"],
                capture_output=True, text=True, check=True,
            )
            first_line = result.stdout.strip().split("\n")[0].strip() if result.stdout.strip() else ""
            if first_line:
                latest_tag = first_line
                # Extract version from tag (handles both name@v1.2.3 and path/v1.2.3)
                version_match = re.search(r"v(\d+\.\d+\.\d+)$", first_line)
                if version_match:
                    latest_tag_version = version_match.group(1)
        except Exception:
            pass

        # Count unreleased changelog entries
        changelog_path = os.path.join(path, "CHANGELOG.md")
        if not os.path.isfile(changelog_path):
            unreleased_str = "no changelog"
        else:
            with open(changelog_path, "r") as f:
                changelog_text = f.read()
            if latest_tag_version is None:
                # No tag: count all bullet lines across all ## sections
                count = sum(1 for line in changelog_text.splitlines() if line.startswith("- "))
            else:
                # Count bullet lines in ## sections above the tagged version
                tag_pattern = re.compile(r"^## " + re.escape(latest_tag_version) + r"(\s|$)", re.MULTILINE)
                match = tag_pattern.search(changelog_text)
                if match:
                    above = changelog_text[:match.start()]
                    count = sum(1 for line in above.splitlines() if line.startswith("- "))
                else:
                    # Tagged version not found in changelog: count all bullets
                    count = sum(1 for line in changelog_text.splitlines() if line.startswith("- "))
            if count == 0:
                unreleased_str = "0"
            elif count == 1:
                unreleased_str = "1 entry"
            else:
                unreleased_str = f"{count} entries"

        # Dependency counts
        deps_count = graph.dep_count(name)
        rdeps_count = graph.rdep_count(name)
        deps_str = str(deps_count) if deps_count else "0"
        rdeps_str = str(rdeps_count) if rdeps_count else "0"

        # Watch paths
        watch = proj.get("watch", [])
        watch_str = f"{len(watch)} paths" if watch else "-"

        # Library flag
        library_str = "yes" if proj.get("library", False) else ""

        # Dev-only flag
        dev_only_str = "yes" if proj.dev_only else ""

        # Subtree remote
        remote = proj.get("subtree_remote", "")
        remote_str = remote if remote else "-"

        # Releasable membership (explicit mode only)
        releasable_str = releasable_map.get(name, "") if explicit else ""

        rows.append((name, path, target_display, version, latest_tag, unreleased_str, library_str, dev_only_str, deps_str, rdeps_str, watch_str, remote_str, releasable_str))

    # Determine which dynamic columns to show
    any_library = any(row[6] != "" for row in rows)
    any_dev_only = any(row[7] != "" for row in rows)
    any_deps = any(row[8] != "0" for row in rows)
    any_rdeps = any(row[9] != "0" for row in rows)
    any_watch = any(row[10] != "-" for row in rows)
    any_remote = any(row[11] != "-" for row in rows)
    any_releasable = any(row[12] != "" for row in rows)

    # Calculate column widths
    base_headers = ("Project", "Path", "Target", "Version", "Tag", "Unreleased")
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
    if any_watch:
        base_headers = base_headers + ("Watch",)
    if any_remote:
        base_headers = base_headers + ("Remote",)
    headers = base_headers

    # Build display rows matching the dynamic header order
    display_rows = []
    for row in rows:
        cells = list(row[:6])  # base columns: name, path, target, version, tag, unreleased
        if any_releasable:
            cells.append(row[12])
        if any_library:
            cells.append(row[6])
        if any_dev_only:
            cells.append(row[7])
        if any_deps:
            cells.append(row[8])
        if any_rdeps:
            cells.append(row[9])
        if any_watch:
            cells.append(row[10])
        if any_remote:
            cells.append(row[11])
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


def _parse_version_tuple(version_str):
    """Parse a version string like '1.2.3' into a tuple of ints.

    Returns None if parsing fails.
    """
    parts = []
    for p in version_str.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            return None
    return tuple(parts) if parts else None


def _evaluate_constraint(constraint, current_version):
    """Evaluate a version constraint against a current version.

    Returns "ok" if the constraint is satisfied, "outdated" if not,
    or "versioned" if the constraint is too complex to parse.
    """
    current_tuple = _parse_version_tuple(current_version)
    if current_tuple is None:
        return "versioned"

    # Strip leading operator and extract version from simple constraints
    # Handles: >=1.2.0, ^1.2.0, ~1.2.0, <=1.2.0, >1.2.0, <1.2.0, ==1.2.0, =1.2.0, 1.2.0
    stripped = constraint.strip()
    if not stripped:
        return "versioned"

    # Reject complex constraints (multiple conditions with commas, ||, spaces)
    if "," in stripped or "||" in stripped:
        return "versioned"

    # Extract operator and version
    if stripped.startswith(">="):
        op, ver_str = ">=", stripped[2:].strip()
    elif stripped.startswith("<="):
        op, ver_str = "<=", stripped[2:].strip()
    elif stripped.startswith("=="):
        op, ver_str = "==", stripped[2:].strip()
    elif stripped.startswith("!="):
        return "versioned"
    elif stripped.startswith(">"):
        op, ver_str = ">", stripped[1:].strip()
    elif stripped.startswith("<"):
        op, ver_str = "<", stripped[1:].strip()
    elif stripped.startswith("~="):
        op, ver_str = "~=", stripped[2:].strip()
    elif stripped.startswith("^"):
        op, ver_str = "^", stripped[1:].strip()
    elif stripped.startswith("~"):
        op, ver_str = "~", stripped[1:].strip()
    elif stripped.startswith("="):
        op, ver_str = "==", stripped[1:].strip()
    else:
        # Bare version string like "1.2.0"
        op, ver_str = "==", stripped

    constraint_tuple = _parse_version_tuple(ver_str)
    if constraint_tuple is None:
        return "versioned"

    if op == ">=":
        return "ok" if current_tuple >= constraint_tuple else "outdated"
    elif op == ">":
        return "ok" if current_tuple > constraint_tuple else "outdated"
    elif op == "<=":
        return "ok" if current_tuple <= constraint_tuple else "outdated"
    elif op == "<":
        return "ok" if current_tuple < constraint_tuple else "outdated"
    elif op == "==":
        return "ok" if current_tuple == constraint_tuple else "outdated"
    elif op == "^":
        # Caret: >=ver and same major (for major>0), or same major.minor (for 0.x)
        if current_tuple < constraint_tuple:
            return "outdated"
        if constraint_tuple[0] > 0:
            return "ok" if current_tuple[0] == constraint_tuple[0] else "outdated"
        # 0.x.y: pin to minor
        if len(constraint_tuple) >= 2 and len(current_tuple) >= 2:
            return "ok" if current_tuple[0] == 0 and current_tuple[1] == constraint_tuple[1] else "outdated"
        return "versioned"
    elif op == "~" or op == "~=":
        # Tilde: >=ver and same major.minor
        if current_tuple < constraint_tuple:
            return "outdated"
        if len(constraint_tuple) >= 2 and len(current_tuple) >= 2:
            return "ok" if (current_tuple[0] == constraint_tuple[0] and
                           current_tuple[1] == constraint_tuple[1]) else "outdated"
        return "versioned"

    return "versioned"


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
        target_entries = detect_targets(os.path.join(root, path))
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

    rows = []
    for i, proj in enumerate(projects):
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
