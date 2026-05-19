"""Monorepo workspace management commands for adding, removing, listing, and synchronizing projects in a multi-package repository."""

import os
import re
import subprocess
import sys
import time

from ..action_versions import format_action
from ..utils import commit_files
from ..workspace import find_workspace_root, load_workspace, save_workspace, WORKSPACE_DIR, WORKSPACE_FILE
from ..workspace_graph import WorkspaceGraph
from ..targets import detect_targets, TARGETS, TargetEntry


def _cmd_init(flags):
    ws_file = os.path.join(".", WORKSPACE_DIR, WORKSPACE_FILE)
    if os.path.isfile(ws_file):
        print("Error: Workspace already initialized.", file=sys.stderr)
        sys.exit(1)
    save_workspace(".", [])
    print("Initialized monorepo workspace in .rlsbl-monorepo/")

    rel_ws_file = os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)
    if flags.get("no-commit"):
        print(f"Skipped commit (--no-commit). Run `safegit commit -- {rel_ws_file}` manually.")
        return

    # Auto-commit workspace.toml
    commit_files("monorepo: init workspace", [rel_ws_file], allow_failure=True)


def _cmd_add(args, flags):
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

    root = find_workspace_root(".")
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
    if watch_raw:
        project["watch"] = [w.strip() for w in watch_raw.split(",")]
    if subtree_remote:
        project["subtree_remote"] = subtree_remote
    if depends_on:
        project["depends_on"] = depends_on
    if library is True:
        project["library"] = True
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


def _cmd_remove(args, flags):
    if not args:
        print("Error: Usage: rlsbl monorepo remove <path>", file=sys.stderr)
        sys.exit(1)

    path = args[0]

    root = find_workspace_root(".")
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


def _cmd_list(flags):
    root = find_workspace_root(".")
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

def _rewrite_trigger(content):
    """Replace the on: trigger block with workflow_call.

    Handles both multi-line triggers (on: alone on a line, with indented
    sub-keys up to jobs:) and single-line triggers (on: push, on: [push, ...]).
    """
    lines = content.splitlines()
    on_idx = None
    single_line = False
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if on_idx is None and (stripped == "on:" or stripped.startswith("on: ")):
            on_idx = i
            single_line = stripped.startswith("on: ")
            break

    if on_idx is None:
        print("Warning: no 'on:' trigger found in workflow, skipping rewrite", file=sys.stderr)
        return content

    if single_line:
        new_lines = lines[:on_idx] + ["on:", "  workflow_call:", ""] + lines[on_idx + 1:]
        return "\n".join(new_lines) + "\n"

    # Multi-line: find the next top-level key after on: (unindented, non-empty, non-comment)
    next_key_idx = None
    for i in range(on_idx + 1, len(lines)):
        line = lines[i]
        if line and not line[0].isspace() and not line.startswith("#"):
            next_key_idx = i
            break

    if next_key_idx is None:
        print("Warning: no top-level key found after 'on:' in workflow, skipping rewrite", file=sys.stderr)
        return content

    new_lines = lines[:on_idx] + ["on:", "  workflow_call:", ""] + lines[next_key_idx:]
    return "\n".join(new_lines) + "\n"


def _inject_working_directory(content, path):
    """Insert a defaults.run.working-directory block before the jobs: line."""
    clean_path = path.rstrip("/")
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            block = [
                "defaults:",
                "  run:",
                f"    working-directory: {clean_path}",
                "",
            ]
            new_lines = lines[:i] + block + lines[i:]
            return "\n".join(new_lines) + "\n"
    # No jobs: line found; return content unchanged
    return content


def _rewrite_version_file_inputs(content, project_path):
    """Prepend project_path to known *-version-file action inputs.

    Actions like actions/setup-go resolve ``go-version-file`` relative to
    the repo root, not ``working-directory``.  When a workflow is copied
    into a monorepo sub-project we must adjust these inputs so the runner
    can still find the file.

    Known inputs: go-version-file, python-version-file, node-version-file.
    """
    known_inputs = ("go-version-file", "python-version-file", "node-version-file")
    lines = content.splitlines()
    new_lines = []
    for line in lines:
        replaced = False
        for inp in known_inputs:
            # Match lines like "          go-version-file: go.mod"
            pattern = re.compile(
                r'^(\s+)' + re.escape(inp) + r':\s+(.+)$'
            )
            m = pattern.match(line)
            if m:
                indent = m.group(1)
                value = m.group(2).strip()
                # Only rewrite relative paths not already prefixed
                if not value.startswith(project_path + "/") and not value.startswith("/"):
                    new_lines.append(f"{indent}{inp}: {project_path}/{value}")
                    replaced = True
                break
        if not replaced:
            new_lines.append(line)
    return "\n".join(new_lines) + "\n" if content.endswith("\n") else "\n".join(new_lines)


def _inject_packages_dir(content, project_path):
    """Add packages-dir to pypa/gh-action-pypi-publish steps.

    When ``working-directory`` is set, ``uv build`` creates artifacts in
    ``{project_path}/dist/`` but the publish action looks for ``dist/``
    at the repo root.  We inject ``packages-dir`` so it finds the right
    directory.

    Step lines in YAML workflows look like ``      - uses: action@v1``
    (list item syntax).  The ``with:`` block is indented relative to the
    ``- uses:`` marker -- specifically at the same column as ``uses:``.
    """
    lines = content.splitlines()
    new_lines = []
    i = 0
    while i < len(lines):
        new_lines.append(lines[i])
        # Detect "- uses: pypa/gh-action-pypi-publish@..." or "uses: ..."
        stripped = lines[i].strip()
        is_pypi_publish = "pypa/gh-action-pypi-publish" in stripped and "uses:" in stripped
        if is_pypi_publish:
            # Determine the indentation for with:/packages-dir.
            # For "      - uses: ...", the with: block sits at the level of
            # "uses:" (i.e. after the "- ").  Find where "uses:" starts.
            uses_col = lines[i].index("uses:")
            with_indent = " " * uses_col
            value_indent = with_indent + "  "
            packages_dir_line = f"{value_indent}packages-dir: {project_path}/dist/"

            # Check if next line is "with:" block
            if i + 1 < len(lines) and lines[i + 1].strip().startswith("with:"):
                # Check if packages-dir is already present in the with block
                has_packages_dir = False
                j = i + 2
                while j < len(lines):
                    l = lines[j]
                    ls = l.strip()
                    if ls == "":
                        j += 1
                        continue
                    # If dedented to or past the with: level, we left the block
                    if len(l) - len(l.lstrip()) <= uses_col:
                        break
                    if ls.startswith("packages-dir:"):
                        has_packages_dir = True
                        break
                    j += 1
                if not has_packages_dir:
                    # Add packages-dir after "with:" line
                    new_lines.append(lines[i + 1])  # the "with:" line
                    new_lines.append(packages_dir_line)
                    i += 2
                    continue
            else:
                # No with: block -- create one
                new_lines.append(f"{with_indent}with:")
                new_lines.append(packages_dir_line)
        i += 1
    result = "\n".join(new_lines)
    if content.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _generate_router(projects):
    """Generate ci-router.yml content from project list."""
    lines = []
    lines.append("# DO NOT EDIT -- generated by rlsbl monorepo sync")
    lines.append("name: CI Router")
    lines.append("")
    lines.append("on:")
    lines.append("  push:")
    lines.append("    branches: [main]")
    lines.append("  pull_request:")
    lines.append("")
    lines.append("jobs:")

    # detect job
    lines.append("  detect:")
    lines.append("    runs-on: ubuntu-latest")
    lines.append("    outputs:")
    for p in projects:
        lines.append(f"      {p['name']}: ${{{{ steps.changes.outputs.{p['name']} }}}}")
    lines.append("    steps:")
    lines.append(f"      - uses: {format_action('actions/checkout')}")
    lines.append(f"      - uses: {format_action('dorny/paths-filter')}")
    lines.append("        id: changes")
    lines.append("        with:")
    lines.append("          filters: |")
    for p in projects:
        clean_path = p['path'].rstrip('/')
        watch = p.get("watch", [])
        if watch:
            lines.append(f"            {p['name']}:")
            lines.append(f"              - '{clean_path}/**'")
            for w in watch:
                lines.append(f"              - '{w}'")
        else:
            lines.append(f"            {p['name']}: '{clean_path}/**'")

    # per-project jobs
    for p in projects:
        lines.append("")
        lines.append(f"  {p['name']}:")
        lines.append("    needs: detect")
        lines.append(f"    if: needs.detect.outputs.{p['name']} == 'true'")
        lines.append(f"    uses: ./.github/workflows/{p['name']}-ci.yml")

    return "\n".join(lines) + "\n"


def _get_monorepo_tag_prefix(project, root):
    """Return the tag prefix for a monorepo project's publish router condition.

    Uses the target's monorepo_tag_glob to derive the prefix (glob minus
    trailing ``*``). For Go projects this yields ``go/v``, for others
    ``name@v``.
    """
    target_entries = detect_targets(os.path.join(root, project["path"]))
    if target_entries and target_entries[0].name in TARGETS:
        glob = TARGETS[target_entries[0].name].monorepo_tag_glob(
            project["name"], path=project["path"]
        )
        # Strip trailing * to get the prefix for startsWith
        return glob.rstrip("*")
    return f"{project['name']}@v"


# Per-target publishing requirements for the monorepo publish router.
#
# Background: the router uses local reusable workflows
# (uses: ./.github/workflows/<name>-publish.yml). Reusable workflows do NOT
# inherit secrets automatically; they also can only use the permissions the
# caller grants them (the called workflow cannot elevate). Without these set
# correctly the called workflow either runs with empty secrets (publish step
# fails) or fails at startup (e.g., OIDC requires id-token: write at the
# caller level, otherwise the run cannot start).
#
# - secrets_inherit: True if the target's publish step references repo/org
#   secrets at runtime (NPM_TOKEN, CARGO_REGISTRY_TOKEN, HEX_API_KEY, etc.).
#   PyPI uses OIDC (no secrets) and must NOT have secrets: inherit.
# - permissions: mapping injected at the caller-job level so the called
#   workflow can actually use these permissions. Reusable workflows can
#   only DOWNGRADE the GITHUB_TOKEN permissions handed to them.
_PUBLISH_TARGET_REQUIREMENTS = {
    "npm":   {"secrets_inherit": True,  "permissions": {"contents": "read",  "id-token": "write"}},
    "pypi":  {"secrets_inherit": False, "permissions": {"contents": "read",  "id-token": "write"}},
    "go":    {"secrets_inherit": True,  "permissions": {"contents": "write"}},
    "hex":   {"secrets_inherit": True,  "permissions": {"contents": "read"}},
    "deno":  {"secrets_inherit": False, "permissions": {"contents": "read",  "id-token": "write"}},
    "cargo": {"secrets_inherit": True,  "permissions": {"contents": "read"}},
    "docker": {"secrets_inherit": True, "permissions": {"contents": "read",  "packages": "write"}},
    "maven": {"secrets_inherit": True,  "permissions": {"contents": "read",  "packages": "write"}},
    "zig":   {"secrets_inherit": True,  "permissions": {"contents": "write"}},
}


def _get_publish_requirements(project, root):
    """Return (secrets_inherit, permissions) for a project's publish job.

    Defaults to no secret inheritance and read-only contents when the target
    type is unknown -- explicit is better than guessing.
    """
    target_entries = detect_targets(os.path.join(root, project["path"]))
    if target_entries and target_entries[0].name in _PUBLISH_TARGET_REQUIREMENTS:
        req = _PUBLISH_TARGET_REQUIREMENTS[target_entries[0].name]
        return req["secrets_inherit"], req["permissions"]
    return False, {"contents": "read"}


def _generate_publish_router(projects, root):
    """Generate publish router content for projects with publish workflows.

    Each per-project job declares the permissions its reusable workflow
    needs and opts into secrets: inherit when the target publishes with a
    runtime token. Without these, jobs fail at startup or publish silently
    with missing credentials.
    """
    lines = []
    lines.append("# DO NOT EDIT -- generated by rlsbl monorepo sync")
    lines.append("name: Publish Router")
    lines.append("")
    lines.append("on:")
    lines.append("  release:")
    lines.append("    types: [published]")
    lines.append("")
    lines.append("jobs:")

    for p in projects:
        tag_prefix = _get_monorepo_tag_prefix(p, root)
        secrets_inherit, permissions = _get_publish_requirements(p, root)
        lines.append(f"  {p['name']}:")
        lines.append(f"    if: startsWith(github.event.release.tag_name, '{tag_prefix}')")
        lines.append("    permissions:")
        for perm, level in permissions.items():
            lines.append(f"      {perm}: {level}")
        lines.append(f"    uses: ./.github/workflows/{p['name']}-publish.yml")
        if secrets_inherit:
            lines.append("    secrets: inherit")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _cmd_sync(flags):
    root = find_workspace_root(".")
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace. Nothing to sync.")
        return

    workflows_dir = os.path.join(root, ".github", "workflows")
    os.makedirs(workflows_dir, exist_ok=True)

    written_files = []
    current_project_names = set()

    # Track which projects have CI and publish workflows
    projects_with_ci = []
    projects_with_publish = []

    for proj in projects:
        name = proj["name"]
        path = proj["path"]
        clean_path = path.rstrip("/")
        current_project_names.add(name)

        for wf_type in ("ci", "publish"):
            src = os.path.join(root, path, ".github", "workflows", f"{wf_type}.yml")
            dest = os.path.join(workflows_dir, f"{name}-{wf_type}.yml")

            if not os.path.isfile(src):
                if wf_type == "ci":
                    print(f"Warning: {path} has no CI workflow ({src})", file=sys.stderr)
                continue

            with open(src, "r", encoding="utf-8") as f:
                content = f.read()

            # Rewrite trigger and inject working directory
            rewritten = _rewrite_trigger(content)
            rewritten = _inject_working_directory(rewritten, clean_path)

            # Rewrite action with: inputs that reference file paths
            rewritten = _rewrite_version_file_inputs(rewritten, clean_path)

            # Inject packages-dir for pypa/gh-action-pypi-publish
            rewritten = _inject_packages_dir(rewritten, clean_path)

            # Prepend header
            header = (
                f"# DO NOT EDIT -- generated by rlsbl monorepo sync\n"
                f"# Source: {clean_path}/.github/workflows/{wf_type}.yml\n"
            )
            final = header + rewritten

            # Write destination
            if os.path.isfile(dest):
                os.chmod(dest, 0o644)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(final)
            os.chmod(dest, 0o444)
            written_files.append(dest)

            if wf_type == "ci":
                projects_with_ci.append(proj)
            if wf_type == "publish":
                projects_with_publish.append(proj)

    # Generate CI router (only for projects that have CI workflows)
    router_path = os.path.join(workflows_dir, "ci-router.yml")
    if os.path.isfile(router_path):
        os.chmod(router_path, 0o644)
    with open(router_path, "w", encoding="utf-8") as f:
        f.write(_generate_router(projects_with_ci))
    os.chmod(router_path, 0o444)
    written_files.append(router_path)

    # Generate publish router (only if any project has publish.yml)
    publish_router_path = os.path.join(workflows_dir, "publish.yml")
    if projects_with_publish:
        if os.path.isfile(publish_router_path):
            os.chmod(publish_router_path, 0o644)
        with open(publish_router_path, "w", encoding="utf-8") as f:
            f.write(_generate_publish_router(projects_with_publish, root))
        os.chmod(publish_router_path, 0o444)
        written_files.append(publish_router_path)

    # Remove stale workflows
    stale_removed = 0
    deleted_files = []
    for filename in os.listdir(workflows_dir):
        filepath = os.path.join(workflows_dir, filename)
        if filepath in written_files:
            continue
        # Check if this is a generated per-project workflow
        for suffix in ("-ci.yml", "-publish.yml"):
            if filename.endswith(suffix):
                proj_name = filename[: -len(suffix)]
                if proj_name not in current_project_names:
                    os.chmod(filepath, 0o644)
                    os.remove(filepath)
                    deleted_files.append(filepath)
                    stale_removed += 1

    # Auto-commit
    all_files = written_files + deleted_files
    if all_files:
        if flags.get("no-commit"):
            quoted = " ".join(all_files)
            print(f"Skipped commit (--no-commit). Run `safegit commit -- {quoted}` manually.")
        else:
            commit_files("monorepo: sync CI workflows", all_files, allow_failure=True)

    wf_count = len(written_files) - 1  # subtract router(s)
    if projects_with_publish:
        wf_count -= 1
    router_count = 1 + (1 if projects_with_publish else 0)
    msg = f"Synced {wf_count} workflow(s), generated {router_count} router(s)."
    if stale_removed:
        msg += f" Removed {stale_removed} stale workflow(s)."
    print(msg)

    # Warn about Swift projects without subtree_remote
    for proj in projects:
        proj_targets = detect_targets(os.path.join(root, proj["path"]))
        if any(te.name in ("swift", "swift-apple") for te in proj_targets):
            if not proj.get("subtree_remote"):
                print(
                    f"Warning: Swift project '{proj['name']}' has no subtree_remote configured. "
                    "SPM consumers won't be able to resolve monorepo tags.",
                    file=sys.stderr,
                )

def _cmd_status(flags):
    root = find_workspace_root(".")
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    if not projects:
        print("No projects in workspace.")
        return

    # Build dependency graph
    graph = WorkspaceGraph(root, projects)

    rows = []
    for proj in projects:
        name = proj["name"]
        path = proj["path"]

        # Detect target
        target_entries = detect_targets(os.path.join(root, path))
        target_name = target_entries[0].name if target_entries else "none"

        # Read version
        version = "?"
        if target_name != "none" and target_name in TARGETS:
            target_path = target_entries[0].path if target_entries else os.path.join(root, path)
            try:
                version = TARGETS[target_name].read_version(target_path)
            except Exception:
                version = "?"

        # Find latest tag using target-aware glob
        latest_tag = "(none)"
        latest_tag_version = None
        try:
            if target_name != "none" and target_name in TARGETS:
                tag_glob = TARGETS[target_name].monorepo_tag_glob(name, path=path)
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

        # Subtree remote
        remote = proj.get("subtree_remote", "")
        remote_str = remote if remote else "-"

        rows.append((name, path, target_name, version, latest_tag, unreleased_str, library_str, deps_str, rdeps_str, watch_str, remote_str))

    # Determine which dynamic columns to show
    any_library = any(row[6] != "" for row in rows)
    any_deps = any(row[7] != "0" for row in rows)
    any_rdeps = any(row[8] != "0" for row in rows)
    any_watch = any(row[9] != "-" for row in rows)
    any_remote = any(row[10] != "-" for row in rows)

    # Calculate column widths
    base_headers = ("Project", "Path", "Target", "Version", "Tag", "Unreleased")
    if any_library:
        base_headers = base_headers + ("Library",)
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
        if any_library:
            cells.append(row[6])
        if any_deps:
            cells.append(row[7])
        if any_rdeps:
            cells.append(row[8])
        if any_watch:
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


def _cmd_outdated(flags):
    root = find_workspace_root(".")
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


def _cmd_release_order(flags):
    root = find_workspace_root(".")
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    from ..workspace_graph import CycleError

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


def _cmd_check_names(args, flags):
    target = flags.get("target")
    if not target:
        print("Error: --target is required. Usage: rlsbl monorepo check-names --target <npm|pypi|go>", file=sys.stderr)
        sys.exit(1)

    prefix = flags.get("prefix", "")
    suffix = flags.get("suffix", "")
    delay_ms = int(flags.get("delay", "200"))

    root = find_workspace_root(".")
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    from .check import _check_single_name, _format_table_row

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


# Manifests that indicate a directory is a project
_PROJECT_MANIFESTS = (
    "go.mod",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "mix.exs",
    "deno.json",
    "build.zig.zon",
)


def _cmd_lint(flags):
    root = find_workspace_root(".")
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    registered_paths = {proj["path"].rstrip("/") for proj in projects}

    # Determine which directories to skip via .gitignore
    gitignored = set()
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            gitignored.add(line.rstrip("/"))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Scan first-level directories for manifests
    found_project_dirs = set()
    try:
        entries = os.listdir(root)
    except OSError:
        entries = []

    for entry in sorted(entries):
        # Skip hidden dirs and .git
        if entry.startswith("."):
            continue
        # Skip gitignored directories
        if entry in gitignored:
            continue
        dir_path = os.path.join(root, entry)
        if not os.path.isdir(dir_path):
            continue
        # Check if the directory contains any recognized manifest
        for manifest in _PROJECT_MANIFESTS:
            if os.path.isfile(os.path.join(dir_path, manifest)):
                found_project_dirs.add(entry)
                break

    # Detect unregistered projects
    unregistered = sorted(found_project_dirs - registered_paths)

    # Detect stale entries (registered but dir missing or has no manifest)
    stale = []
    for path in sorted(registered_paths):
        dir_path = os.path.join(root, path)
        if not os.path.isdir(dir_path):
            stale.append(path)
            continue
        has_manifest = any(
            os.path.isfile(os.path.join(dir_path, m)) for m in _PROJECT_MANIFESTS
        )
        if not has_manifest:
            stale.append(path)

    # Report
    if not unregistered and not stale:
        return

    if unregistered:
        print("Unregistered projects:")
        for d in unregistered:
            print(f"  {d}")

    if stale:
        print("Stale entries:")
        for s in stale:
            print(f"  {s}")

    sys.exit(1)

