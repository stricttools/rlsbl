"""Monorepo sync command and all sync helpers: trigger rewriting, working-directory injection, router generation."""

import glob
import os
import sys
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from ...action_versions import format_action
from ...commands.init_cmd import process_template
from ...context import create_context
from ...utils import commit_files, commit_files_if_changed
from ...workspace import find_workspace_root, load_workspace
from ...targets import detect_targets, TARGETS


def parse_ci_workflow(content):
    """Parse CI workflow YAML content using round-trip mode (preserves comments, ordering).

    Returns the parsed document, or None if the content is empty or has no
    ``jobs:`` key.
    """
    yaml = YAML(typ='rt')
    doc = yaml.load(content)
    if doc is None or 'jobs' not in doc:
        return None
    return doc


def emit_ci_workflow(doc):
    """Serialize a parsed workflow document back to YAML string.

    Uses round-trip mode to preserve comment annotations and key order
    from the original parse.
    """
    yaml = YAML(typ='rt')
    yaml.default_flow_style = False
    stream = StringIO()
    yaml.dump(doc, stream)
    return stream.getvalue()


def _rewrite_trigger(doc):
    """Replace the on: trigger with workflow_call: in a parsed YAML document.

    YAML 1.1 treats bare ``on`` as boolean True, so the key may appear as
    either the string ``'on'`` or the boolean ``True`` depending on the
    parser mode.  Round-trip mode preserves the original text, but we
    check both forms defensively.
    """
    # Determine which key form is present
    if 'on' in doc:
        key = 'on'
    elif True in doc:
        key = True
    else:
        print("Warning: no 'on:' trigger found in workflow, skipping rewrite", file=sys.stderr)
        return doc

    doc[key] = {'workflow_call': None}
    return doc


def _inject_working_directory(doc, path):
    """Add defaults.run.working-directory to each job in a parsed YAML document.

    Merges with existing ``defaults.run`` (e.g. preserving ``shell``) rather
    than overwriting.  Only adds ``working-directory`` if it is not already
    present.
    """
    path = path.rstrip('/')
    for job_name, job in doc.get('jobs', {}).items():
        defaults = job.setdefault('defaults', {})
        run_section = defaults.setdefault('run', {})
        if 'working-directory' not in run_section:
            run_section['working-directory'] = path
    return doc


def _rewrite_version_file_inputs(doc, project_path):
    """Prefix version-file values with project path in setup actions.

    Actions like actions/setup-go resolve ``go-version-file`` relative to
    the repo root, not ``working-directory``.  When a workflow is copied
    into a monorepo sub-project we must adjust these inputs so the runner
    can still find the file.

    Known inputs: go-version-file, python-version-file, node-version-file.
    """
    known_inputs = ('go-version-file', 'python-version-file', 'node-version-file')
    for job in doc.get('jobs', {}).values():
        for step in job.get('steps', []):
            with_block = step.get('with', {})
            if not with_block:
                continue
            for inp in known_inputs:
                if inp in with_block:
                    val = str(with_block[inp])
                    if not val.startswith('/') and not val.startswith(project_path + '/'):
                        with_block[inp] = f"{project_path}/{val}"
    return doc


def _inject_packages_dir(doc, project_path):
    """Add packages-dir to PyPI publish steps.

    When ``working-directory`` is set, ``uv build`` creates artifacts in
    ``{project_path}/dist/`` but the publish action looks for ``dist/``
    at the repo root.  We inject ``packages-dir`` so it finds the right
    directory.
    """
    for job in doc.get('jobs', {}).values():
        for step in job.get('steps', []):
            uses = step.get('uses', '')
            if 'pypa/gh-action-pypi-publish' in str(uses):
                with_block = step.setdefault('with', {})
                if 'packages-dir' not in with_block:
                    with_block['packages-dir'] = f"{project_path}/dist/"
    return doc


def _generate_router(projects):
    """Generate ci-router.yml content from project list.

    Builds the router as a structured dict and serializes with
    ``emit_ci_workflow`` for consistent YAML output.
    """
    # Build the filters block as a multi-line string (dorny/paths-filter format)
    filter_lines = []
    for p in projects:
        clean_path = p['path'].rstrip('/')
        watch = p.get("watch", [])
        if watch:
            filter_lines.append(f"{p['name']}:")
            filter_lines.append(f"  - '{clean_path}/**'")
            for w in watch:
                filter_lines.append(f"  - '{w}'")
        else:
            filter_lines.append(f"{p['name']}: '{clean_path}/**'")
    filters_str = LiteralScalarString("\n".join(filter_lines) + "\n")

    # detect job
    detect_outputs = {}
    for p in projects:
        detect_outputs[p['name']] = f"${{{{ steps.changes.outputs.{p['name']} }}}}"

    detect_job = {
        'runs-on': 'ubuntu-latest',
        'outputs': detect_outputs,
        'steps': [
            {'uses': format_action('actions/checkout')},
            {
                'uses': format_action('dorny/paths-filter'),
                'id': 'changes',
                'with': {'filters': filters_str},
            },
        ],
    }

    # per-project jobs (one job per CI file; projects may have multiple)
    jobs = {'detect': detect_job}
    for p in projects:
        ci_files = p.get('_ci_files', [f"{p['name']}-ci.yml"])
        if len(ci_files) == 1:
            # Single CI file: use project name as job key (backward compat)
            jobs[p['name']] = {
                'needs': 'detect',
                'if': f"needs.detect.outputs.{p['name']} == 'true'",
                'uses': f"./.github/workflows/{ci_files[0]}",
            }
        else:
            # Multiple CI files: one job per file
            for ci_file in ci_files:
                job_key = ci_file.removesuffix('.yml')
                jobs[job_key] = {
                    'needs': 'detect',
                    'if': f"needs.detect.outputs.{p['name']} == 'true'",
                    'uses': f"./.github/workflows/{ci_file}",
                }

    workflow = {
        'name': 'CI Router',
        'on': {
            'push': {'branches': ['main']},
            'pull_request': None,
            'workflow_dispatch': None,
        },
        'jobs': jobs,
    }

    yaml_str = emit_ci_workflow(workflow)
    return f"# DO NOT EDIT -- generated by rlsbl monorepo sync\n{yaml_str}"


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


def _build_project_template_vars(project_dir, root):
    """Build a template vars dict for a project, with both namespaced and un-namespaced keys.

    Detects the project's targets, calls each target's template_vars(), and
    returns a merged dict where each target's vars appear under both their
    bare names and ``{target_name}.{key}`` namespaced names. This allows
    process_template to resolve patterns like ``{{pypi.minRequiredPython}}``
    in workflow comments.
    """
    from pathlib import Path

    target_entries = detect_targets(project_dir)
    if not target_entries:
        return {}

    ctx = create_context(Path(project_dir), workspace_root=Path(root))
    merged = {}
    for entry in target_entries:
        if entry.name not in TARGETS:
            continue
        target = TARGETS[entry.name]
        try:
            tvars = target.template_vars(entry.path, ctx)
        except Exception as e:
            from ...utils import warn_exception
            warn_exception(f"template_vars failed for target {entry.name}", e)
            continue
        # Un-namespaced (first target wins for collisions)
        for key, value in tvars.items():
            if key not in merged:
                merged[key] = value
        # Namespaced: always add
        for key, value in tvars.items():
            merged[f"{entry.name}.{key}"] = value
    return merged


def scaffold_releasable_dirs(workspace_root):
    """Create the directory structure for each releasable in explicit mode.

    In explicit mode (when ``[[releasables]]`` exists in workspace.toml),
    each releasable gets:

    - ``.rlsbl-monorepo/releasables/{name}/version`` (user-owned, never overwritten)
    - ``.rlsbl-monorepo/releasables/{name}/changes/unreleased.jsonl`` (user-owned)
    - ``.rlsbl-monorepo/releasables/{name}/hooks/pre-checks.sh`` (scaffold-managed)
    - ``.rlsbl-monorepo/releasables/{name}/hooks/pre-release.sh`` (scaffold-managed)
    - ``.rlsbl-monorepo/releasables/{name}/hooks/post-release.sh`` (scaffold-managed)

    Hook scripts are scaffold-managed: they use three-way merge when updated.
    Version and unreleased.jsonl are user-owned: created once, never overwritten.

    Args:
        workspace_root: path to the monorepo root.

    Returns:
        A list of file paths that were created or updated (for commit tracking).
    """
    from ...workspace import is_explicit_mode, load_releasables, load_workspace
    from ...workspace import get_releasable_dir, get_releasable_version_path, get_releasable_changes_dir

    if not is_explicit_mode(workspace_root):
        return []

    projects = load_workspace(workspace_root)
    releasables = load_releasables(workspace_root, projects)
    if not releasables:
        return []

    # Load hook templates
    templates_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "templates", "shared", "hooks",
    )
    hook_templates = {}
    for hook_name in ("pre-checks.sh", "pre-release.sh", "post-release.sh"):
        tpl_path = os.path.join(templates_dir, f"{hook_name}.tpl")
        if os.path.isfile(tpl_path):
            with open(tpl_path, "r", encoding="utf-8") as f:
                hook_templates[hook_name] = f.read()

    created_files = []

    for rel in releasables:
        rel_dir = get_releasable_dir(workspace_root, rel.name)
        os.makedirs(rel_dir, exist_ok=True)

        # Version file (user-owned: create if missing, never overwrite)
        version_path = get_releasable_version_path(workspace_root, rel.name)
        if not os.path.isfile(version_path):
            with open(version_path, "w", encoding="utf-8") as f:
                f.write("0.0.0\n")
            created_files.append(version_path)

        # Changes directory + unreleased.jsonl (user-owned)
        changes_dir = get_releasable_changes_dir(workspace_root, rel.name)
        os.makedirs(changes_dir, exist_ok=True)
        unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
        if not os.path.isfile(unreleased_path):
            with open(unreleased_path, "w", encoding="utf-8") as f:
                pass  # empty file
            created_files.append(unreleased_path)

        # Hook scripts (scaffold-managed: three-way merge on update)
        hooks_dir = os.path.join(rel_dir, "hooks")
        os.makedirs(hooks_dir, exist_ok=True)

        # Store merge bases under the monorepo directory (not cwd's .rlsbl/).
        bases_dir = os.path.join(workspace_root, ".rlsbl-monorepo", "bases")

        for hook_name, template_content in hook_templates.items():
            hook_path = os.path.join(hooks_dir, hook_name)
            if not os.path.isfile(hook_path):
                # First scaffold: write template and make executable
                with open(hook_path, "w", encoding="utf-8") as f:
                    f.write(template_content)
                os.chmod(hook_path, 0o755)
                created_files.append(hook_path)
            else:
                # Existing hook: three-way merge
                from ...commands.init_cmd import _three_way_merge

                base_key = os.path.relpath(hook_path, workspace_root)
                base_path = os.path.join(bases_dir, base_key)

                # Load base
                base = None
                if os.path.isfile(base_path):
                    with open(base_path, "r", encoding="utf-8") as f:
                        base = f.read()

                with open(hook_path, "r", encoding="utf-8") as f:
                    ours = f.read()

                if ours == template_content:
                    # No change needed
                    pass
                elif base is None:
                    # No base stored: seed the base for future merges
                    os.makedirs(os.path.dirname(base_path), exist_ok=True)
                    with open(base_path, "w", encoding="utf-8") as f:
                        f.write(template_content)
                elif ours == base:
                    # User hasn't customized; update to new template
                    with open(hook_path, "w", encoding="utf-8") as f:
                        f.write(template_content)
                    os.chmod(hook_path, 0o755)
                    with open(base_path, "w", encoding="utf-8") as f:
                        f.write(template_content)
                    created_files.append(hook_path)
                elif base != template_content:
                    # Both sides changed: three-way merge
                    merged, has_conflicts = _three_way_merge(ours, base, template_content)
                    with open(hook_path, "w", encoding="utf-8") as f:
                        f.write(merged)
                    os.chmod(hook_path, 0o755)
                    with open(base_path, "w", encoding="utf-8") as f:
                        f.write(template_content)
                    created_files.append(hook_path)
                    if has_conflicts:
                        print(
                            f"Warning: merge conflicts in {hook_path}, resolve manually",
                            file=sys.stderr,
                        )

    return created_files


def _cmd_sync(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace. Nothing to sync.")
        return

    # Scaffold releasable directory structure (explicit mode only).
    # This runs early so that releasable dirs exist before CI workflow
    # generation, which may need to reference releasable paths.
    releasable_files = scaffold_releasable_dirs(root)
    if releasable_files and not flags.get("no-commit"):
        commit_files_if_changed(
            "monorepo: scaffold releasable directories",
            releasable_files,
            skip_message=None,
        )

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

        # --- CI workflow(s): copy + transform ---
        # Support both single ci.yml and per-target ci-{target}.yml files
        proj_wf_dir = os.path.join(root, path, ".github", "workflows")
        ci_sources = []
        if os.path.isdir(proj_wf_dir):
            single = os.path.join(proj_wf_dir, "ci.yml")
            if os.path.isfile(single):
                ci_sources.append(single)
            for f in sorted(glob.glob(os.path.join(proj_wf_dir, "ci-*.yml"))):
                if os.path.basename(f) != "ci-router.yml":
                    ci_sources.append(f)

        if not ci_sources:
            print(f"Warning: {path} has no CI workflow(s) in {proj_wf_dir}", file=sys.stderr)
        else:
            project_dir = os.path.join(root, path)
            tvars = _build_project_template_vars(project_dir, root)
            ci_dest_names_for_proj = []

            for ci_src in ci_sources:
                ci_basename = os.path.basename(ci_src)

                # Determine destination name
                if ci_basename == "ci.yml":
                    ci_dest_name = f"{name}-ci.yml"
                else:
                    # ci-{target}.yml -> {name}-ci-{target}.yml
                    ci_dest_name = f"{name}-{ci_basename}"
                ci_dest = os.path.join(workflows_dir, ci_dest_name)

                # Skip if the source is a generated router (path="." self-reference)
                ci_src_real = os.path.realpath(ci_src)
                if ci_src_real == ci_router_output or ci_src_real == publish_router_output:
                    print(
                        f"Warning: {name} ci workflow is the generated router itself "
                        f"(path='{path}'), skipping",
                        file=sys.stderr,
                    )
                    continue

                with open(ci_src, "r", encoding="utf-8") as f:
                    content = f.read()

                # Resolve any remaining {{...}} template variables in the
                # workflow content (e.g. {{pypi.minRequiredPython}} in comments
                # that scaffold left unresolved).
                if tvars:
                    content, _ = process_template(content, tvars)

                doc = parse_ci_workflow(content)
                if doc is None:
                    print(f"Warning: {ci_src} has no jobs: key, skipping", file=sys.stderr)
                    continue
                _rewrite_trigger(doc)
                _inject_working_directory(doc, clean_path)
                _rewrite_version_file_inputs(doc, clean_path)
                _inject_packages_dir(doc, clean_path)
                rewritten = emit_ci_workflow(doc)

                header = (
                    f"# DO NOT EDIT -- generated by rlsbl monorepo sync\n"
                    f"# Source: {clean_path}/.github/workflows/{ci_basename}\n"
                )
                final = header + rewritten

                if os.path.isfile(ci_dest):
                    os.chmod(ci_dest, 0o644)
                with open(ci_dest, "w", encoding="utf-8") as f:
                    f.write(final)
                os.chmod(ci_dest, 0o444)
                written_files.append(ci_dest)
                ci_dest_names_for_proj.append(ci_dest_name)

            if ci_dest_names_for_proj:
                proj['_ci_files'] = ci_dest_names_for_proj
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

        # Stale CI workflows: any {name}-ci.yml or {name}-ci-{target}.yml not
        # freshly written this sync. The written_files check above already
        # skips files that were just written, so anything reaching here is
        # stale -- either from a removed project or a project whose CI file
        # set changed (e.g. switched from ci.yml to ci-pypi.yml + ci-go.yml).
        if "-ci" in filename and filename.endswith(".yml"):
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
