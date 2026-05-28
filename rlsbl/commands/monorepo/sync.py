"""Monorepo sync command and all sync helpers: trigger rewriting, working-directory injection, router generation."""

import os
import re
import sys

from ...action_versions import format_action
from ...utils import commit_files, commit_files_if_changed
from ...workspace import find_workspace_root, load_workspace
from ...targets import detect_targets, TARGETS


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
    lines.append("  workflow_dispatch:")
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



def _cmd_sync(flags, project_root=None):
    start = str(project_root) if project_root else "."
    root = find_workspace_root(start)
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

    # Pre-compute router paths so we can detect self-references
    ci_router_output = os.path.realpath(os.path.join(workflows_dir, "ci-router.yml"))
    publish_router_output = os.path.realpath(os.path.join(workflows_dir, "publish.yml"))

    for proj in projects:
        name = proj["name"]
        path = proj["path"]
        clean_path = path.rstrip("/")
        current_project_names.add(name)

        # --- CI workflow: copy + transform ---
        ci_src = os.path.join(root, path, ".github", "workflows", "ci.yml")
        ci_dest = os.path.join(workflows_dir, f"{name}-ci.yml")

        if not os.path.isfile(ci_src):
            print(f"Warning: {path} has no CI workflow ({ci_src})", file=sys.stderr)
        else:
            # Skip if the source is a generated router (path="." self-reference)
            ci_src_real = os.path.realpath(ci_src)
            if ci_src_real == ci_router_output or ci_src_real == publish_router_output:
                print(
                    f"Warning: {name} ci workflow is the generated router itself "
                    f"(path='{path}'), skipping",
                    file=sys.stderr,
                )
            else:
                with open(ci_src, "r", encoding="utf-8") as f:
                    content = f.read()

                rewritten = _rewrite_trigger(content)
                rewritten = _inject_working_directory(rewritten, clean_path)
                rewritten = _rewrite_version_file_inputs(rewritten, clean_path)
                rewritten = _inject_packages_dir(rewritten, clean_path)

                header = (
                    f"# DO NOT EDIT -- generated by rlsbl monorepo sync\n"
                    f"# Source: {clean_path}/.github/workflows/ci.yml\n"
                )
                final = header + rewritten

                if os.path.isfile(ci_dest):
                    os.chmod(ci_dest, 0o644)
                with open(ci_dest, "w", encoding="utf-8") as f:
                    f.write(final)
                os.chmod(ci_dest, 0o444)
                written_files.append(ci_dest)
                projects_with_ci.append(proj)

        # --- Publish workflow: just verify existence for inline router ---
        publish_src = os.path.join(root, path, ".github", "workflows", "publish.yml")
        if os.path.isfile(publish_src):
            publish_src_real = os.path.realpath(publish_src)
            if publish_src_real == ci_router_output or publish_src_real == publish_router_output:
                print(
                    f"Warning: {name} publish workflow is the generated router itself "
                    f"(path='{path}'), skipping",
                    file=sys.stderr,
                )
            else:
                projects_with_publish.append(proj)

    # Generate CI router (only for projects that have CI workflows)
    router_path = os.path.join(workflows_dir, "ci-router.yml")
    if os.path.isfile(router_path):
        os.chmod(router_path, 0o644)
    with open(router_path, "w", encoding="utf-8") as f:
        f.write(_generate_router(projects_with_ci))
    os.chmod(router_path, 0o444)
    written_files.append(router_path)

    # Generate inline publish router (only if any project has publish.yml)
    publish_router_path = os.path.join(workflows_dir, "publish.yml")
    if projects_with_publish:
        from .publish_inline import (
            compute_publish_hashes,
            generate_inline_publish_router,
            load_publish_cache,
            save_publish_cache,
            should_regenerate_router,
        )

        monorepo_dir = os.path.join(root, ".rlsbl-monorepo")
        current_hashes = compute_publish_hashes(projects, root)
        cached_hashes = load_publish_cache(monorepo_dir)

        if should_regenerate_router(cached_hashes, current_hashes, publish_router_path):
            if os.path.isfile(publish_router_path):
                os.chmod(publish_router_path, 0o644)
            with open(publish_router_path, "w", encoding="utf-8") as f:
                f.write(generate_inline_publish_router(projects_with_publish, root))
            os.chmod(publish_router_path, 0o444)
            written_files.append(publish_router_path)

            cache_path = save_publish_cache(monorepo_dir, current_hashes)
            written_files.append(cache_path)
        else:
            print("Publish router up to date, skipping regeneration")

    # Remove stale workflows
    stale_removed = 0
    deleted_files = []
    for filename in os.listdir(workflows_dir):
        filepath = os.path.join(workflows_dir, filename)
        if filepath in written_files:
            continue

        # All *-publish.yml files are stale (we no longer generate per-project wrappers)
        if filename.endswith("-publish.yml"):
            os.chmod(filepath, 0o644)
            os.remove(filepath)
            deleted_files.append(filepath)
            stale_removed += 1
            print(f"Removed stale publish wrapper: {filename}")
            continue

        # Stale *-ci.yml from removed projects
        if filename.endswith("-ci.yml"):
            proj_name = filename[: -len("-ci.yml")]
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
            commit_files_if_changed("monorepo: sync CI workflows", all_files, skip_message="No workflow changes to commit.")

    # written_files includes CI workflows + CI router + publish router (if any)
    router_count = 1 + (1 if projects_with_publish else 0)
    wf_count = len(written_files) - router_count
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
