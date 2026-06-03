"""Monorepo sync command and all sync helpers: trigger rewriting, working-directory injection, router generation."""

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

    # per-project jobs
    jobs = {'detect': detect_job}
    for p in projects:
        jobs[p['name']] = {
            'needs': 'detect',
            'if': f"needs.detect.outputs.{p['name']} == 'true'",
            'uses': f"./.github/workflows/{p['name']}-ci.yml",
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
        except Exception:
            continue
        # Un-namespaced (first target wins for collisions)
        for key, value in tvars.items():
            if key not in merged:
                merged[key] = value
        # Namespaced: always add
        for key, value in tvars.items():
            merged[f"{entry.name}.{key}"] = value
    return merged


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

                # Resolve any remaining {{...}} template variables in the
                # workflow content (e.g. {{pypi.minRequiredPython}} in comments
                # that scaffold left unresolved).
                project_dir = os.path.join(root, path)
                tvars = _build_project_template_vars(project_dir, root)
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
