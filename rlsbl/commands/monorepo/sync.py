"""Monorepo sync command and all sync helpers: working-directory injection, inline CI router generation."""

import glob
import os
import subprocess
import sys
from ruamel.yaml.scalarstring import LiteralScalarString

import tomlkit

from ...action_versions import format_action
from ...ci_yaml import (
    parse_ci_workflow,
    emit_ci_workflow,
    inject_working_directory,
    rewrite_version_file_inputs,
)
from ...commands.init_cmd import check_unreplaced_vars, process_template
from ...context import create_context
from ...errors import ConfigError
from ...targets.utils import detect_python_package_root
from ...utils import commit_files_if_changed
from ...workspace import find_workspace_root, load_workspace, WORKSPACE_DIR, WORKSPACE_FILE
from ...targets import detect_targets, resolve_releasable_config_dir, TARGETS

# Backward-compatible aliases (private names used by monorepo __init__.py re-exports)
_inject_working_directory = inject_working_directory
_rewrite_version_file_inputs = rewrite_version_file_inputs


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


# GitHub rejects a workflow file once it references 20 reusable workflows
# (job-level ``uses:``): the workflow is silently invalid and never runs.
# Generated routers inline jobs instead, so any reusable call in a router is
# a regression; the guard hard-errors well before GitHub would reject it.
GITHUB_MAX_REUSABLE_CALLS = 20


def count_reusable_workflow_calls(jobs):
    """Count job-level ``uses:`` entries (reusable workflow calls) in a jobs mapping.

    Step-level ``uses:`` (actions) are not counted -- only jobs whose mapping
    carries a top-level ``uses:`` key, which is what GitHub treats as a
    reusable workflow call.
    """
    return sum(
        1 for job in jobs.values() if isinstance(job, dict) and "uses" in job
    )


def validate_router_reusable_calls(jobs, router_name):
    """Hard-error when a generated router carries >= 20 reusable workflow calls.

    GitHub rejects workflow files with too many reusable-workflow calls
    outright -- the router never runs and CI silently stops. Generated
    routers must inline jobs instead of calling reusable workflows.

    Raises:
        ConfigError: when *jobs* contains ``GITHUB_MAX_REUSABLE_CALLS`` or
            more job-level ``uses:`` entries.
    """
    count = count_reusable_workflow_calls(jobs)
    if count >= GITHUB_MAX_REUSABLE_CALLS:
        raise ConfigError(
            f"{router_name} would contain {count} reusable-workflow calls "
            f"(job-level 'uses:'). GitHub rejects workflows that reference "
            f"{GITHUB_MAX_REUSABLE_CALLS} or more reusable workflows -- the "
            "router would be silently invalid and CI would never run. "
            "Generated routers must inline jobs instead of calling reusable "
            "workflows; this is a generator bug, not a workspace-size problem."
        )


def _strip_expression_wrapper(expr):
    """Strip an optional ``${{ ... }}`` wrapper from a GitHub expression."""
    expr = str(expr).strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        return expr[3:-2].strip()
    return expr


def _generate_router(projects):
    """Generate ci-router.yml content with every project's CI jobs inlined.

    Each project dict must carry ``_ci_docs``: a list of ``(job_prefix, doc)``
    pairs where *doc* is a parsed CI workflow (working-directory already
    injected) and *job_prefix* is the per-file key (``{name}-ci`` or
    ``{name}-ci-{target}``).

    GitHub rejects workflow files with 20+ reusable-workflow calls, so the
    router inlines every project's CI jobs directly instead of ``uses:``
    calls. Each inlined job:

    - gets its key prefixed with the CI file's job prefix (unique per file),
    - gets an explicit ``name: "{prefix} / {job}"`` so check-run names stay
      identical to the reusable-workflow era (publish gate regexes and
      branch protection rules keep matching),
    - is gated on ``needs: detect`` + ``if: needs.detect.outputs.{project}``,
    - keeps intra-workflow ``needs:`` (rewritten to the prefixed keys).
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

    # Inline every project's CI jobs, prefixed per CI file.
    jobs = {'detect': detect_job}
    for p in projects:
        name = p['name']
        detect_cond = f"needs.detect.outputs.{name} == 'true'"
        for prefix, doc in p.get('_ci_docs', []):
            src_jobs = doc.get('jobs') or {}
            workflow_env = doc.get('env')
            workflow_defaults_run = (doc.get('defaults') or {}).get('run') or {}
            key_map = {orig: f"{prefix}-{orig}" for orig in src_jobs}

            for orig_key, job in src_jobs.items():
                new_key = key_map[orig_key]
                if new_key in jobs:
                    raise ConfigError(
                        f"CI router job key collision: '{new_key}' is produced "
                        f"by more than one project CI file. Rename the job "
                        f"'{orig_key}' in the CI workflow of project '{name}'."
                    )

                # Preserve reusable-workflow-era check-run names so publish
                # gate regexes and branch protection rules keep matching.
                display = job.get('name', orig_key)
                job['name'] = f"{prefix} / {display}"

                # Rewrite intra-workflow needs to prefixed keys, then gate
                # every job on the detect job.
                needs = job.get('needs')
                if needs is None:
                    rewritten = []
                elif isinstance(needs, str):
                    rewritten = [key_map.get(needs, f"{prefix}-{needs}")]
                else:
                    rewritten = [key_map.get(n, f"{prefix}-{n}") for n in needs]
                job['needs'] = ['detect', *rewritten]

                existing_if = job.get('if')
                if existing_if is None:
                    job['if'] = detect_cond
                else:
                    job['if'] = (
                        f"{detect_cond} && "
                        f"({_strip_expression_wrapper(existing_if)})"
                    )

                # Push workflow-level env/defaults.run down (job keys win) --
                # they would otherwise be lost when only jobs are extracted.
                if workflow_env:
                    job_env = job.setdefault('env', {})
                    for env_key, env_val in workflow_env.items():
                        if env_key not in job_env:
                            job_env[env_key] = env_val
                if workflow_defaults_run:
                    run_block = job.setdefault('defaults', {}).setdefault('run', {})
                    for run_key, run_val in workflow_defaults_run.items():
                        if run_key not in run_block:
                            run_block[run_key] = run_val

                jobs[new_key] = job

    validate_router_reusable_calls(jobs, "ci-router.yml")

    workflow = {
        'name': 'CI Router',
        'on': {
            'push': {'branches': ['main']},
            'pull_request': None,
            'workflow_dispatch': None,
        },
        # Per-SHA group: re-runs of the same commit dedupe, but a new commit
        # never cancels an earlier commit's in-flight run (release CI
        # conclusions stay intact during back-to-back batch pushes).
        'concurrency': {
            'group': '${{ github.workflow_ref }}-${{ github.sha }}',
            'cancel-in-progress': True,
        },
        'jobs': jobs,
    }

    yaml_str = emit_ci_workflow(workflow)
    return f"# DO NOT EDIT -- generated by rlsbl monorepo sync\n{yaml_str}"


def _get_monorepo_tag_prefix(project, root, releasables=None):
    """Return the tag prefix for a monorepo project's publish router condition.

    When *releasables* are provided and the project belongs to a releasable
    (``releasable = "X"``), the prefix is derived from the releasable's
    ``tag_format`` (e.g. ``"{name}@v{version}"`` -> ``"X@v"``).

    Otherwise falls back to the target's ``monorepo_tag_glob`` (glob minus
    trailing ``*``). For Go projects this yields ``go/v``, for others
    ``name@v``.
    """
    # In explicit releasable mode, derive prefix from the releasable's tag_format
    if releasables:
        from ...workspace import WorkspaceProject
        rel_name = None
        if isinstance(project, WorkspaceProject):
            rel_name = project.releasable if isinstance(project.releasable, str) else None
        elif isinstance(project, dict):
            val = project.get("releasable")
            rel_name = val if isinstance(val, str) else None

        if rel_name is not None:
            for rel in releasables:
                if rel.name == rel_name:
                    # Format with empty version to get the prefix
                    return rel.tag_format.format(name=rel.name, version="")

    rel_dir = resolve_releasable_config_dir(project, root)
    target_entries = detect_targets(os.path.join(root, project["path"]), releasable_config_dir=rel_dir)
    if target_entries and target_entries[0].name in TARGETS:
        glob = TARGETS[target_entries[0].name].monorepo_tag_glob(
            project["name"], path=project["path"]
        )
        # Strip trailing * to get the prefix for startsWith
        return glob.rstrip("*")
    return f"{project['name']}@v"


def _root_is_publisher(project, root):
    """Return True when the root project (path='.') actually publishes.

    A root publisher has publish_mode != "none" and at least one detectable
    publish target. Detection is config-based (not based on the on-disk
    publish.yml, which is the generated router itself). A ConfigError during
    publish_mode or target detection means the root is not a resolvable
    publisher -- treat it as a non-publisher rather than crashing sync.
    """
    from ...config import read_project_config, suppresses_publish, ConfigError

    rel_dir = resolve_releasable_config_dir(project, root)
    project_dir = os.path.join(root, project["path"])
    config = read_project_config(project_dir, releasable_config_dir=rel_dir)
    try:
        if suppresses_publish(config):
            return False
    except ConfigError:
        return False
    try:
        entries = detect_targets(project_dir, releasable_config_dir=rel_dir)
    except ConfigError:
        return False
    return any(e.name in TARGETS for e in entries)


def _build_project_template_vars(project_dir, root):
    """Build a template vars dict for a project, with both namespaced and un-namespaced keys.

    Detects the project's targets, calls each target's template_vars(), and
    returns a merged dict where each target's vars appear under both their
    bare names and ``{target_name}.{key}`` namespaced names. This allows
    process_template to resolve patterns like ``{{pypi.minRequiredPython}}``
    in workflow comments.
    """
    from pathlib import Path
    from ...context import resolve_releasable_config_dir

    rel_dir = resolve_releasable_config_dir(Path(project_dir), Path(root))
    target_entries = detect_targets(project_dir, releasable_config_dir=rel_dir)
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
        # TemplateVars already contains both bare and namespaced keys.
        # First target wins for bare keys; namespaced keys always added.
        for key, value in tvars.items():
            if "." in key:
                # Namespaced: always add
                merged[key] = value
            elif key not in merged:
                # Bare: first target wins
                merged[key] = value
    return merged


def scaffold_releasable_dirs(workspace_root):
    """Create the directory structure for each releasable in explicit mode.

    In explicit mode (when ``[[releasables]]`` exists in workspace.toml),
    each releasable gets:

    - ``.rlsbl-monorepo/releasables/{name}/version`` (user-owned, never overwritten)
    - ``.rlsbl-monorepo/releasables/{name}/changes/unreleased.jsonl`` (user-owned)

    Hook scripts are no longer scaffolded here -- hooks are config-driven
    (see ``hooks`` key in config.json).
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

    return created_files


def _saferm_workflow(filepath, description):
    """Delete a stale generated workflow file via saferm (audit trail).

    Raises RuntimeError if saferm is not on PATH and propagates
    subprocess.CalledProcessError if saferm exits non-zero.
    """
    try:
        subprocess.run(
            ["saferm", "delete", "--description", description, filepath],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "saferm is not installed or not on PATH. "
            "Install saferm before running sync."
        ) from None


def _sync_import_names(root, projects):
    """Auto-populate import_name for Python projects whose import name differs from their project name.

    For each Python project (has pyproject.toml with [project]), detects the
    package root via detect_python_package_root and compares the derived import
    name against the underscored project name. When they differ and import_name
    is not already set in workspace.toml, writes import_name to the project's
    entry.

    Returns the workspace.toml path if it was modified, or None.
    """
    updates: list[tuple[str, str]] = []  # (project_name, import_name)

    for proj in projects:
        # Skip if already set
        if proj.import_name:
            continue

        project_dir = os.path.join(root, proj.path)
        pyproject_path = os.path.join(project_dir, "pyproject.toml")
        if not os.path.isfile(pyproject_path):
            continue

        pkg_root = detect_python_package_root(project_dir)
        if pkg_root is None:
            continue

        # Derive import name from detected package root
        detected_import_name = os.path.basename(pkg_root)

        # Compare against the underscored project name (the default Python
        # convention: project name "my-package" imports as "my_package")
        underscored_name = proj.name.replace("-", "_")
        if detected_import_name != underscored_name:
            updates.append((proj.name, detected_import_name))

    if not updates:
        return None

    # Read workspace.toml with tomlkit (preserves formatting)
    ws_path = os.path.join(root, WORKSPACE_DIR, WORKSPACE_FILE)
    with open(ws_path, encoding="utf-8") as f:
        doc = tomlkit.loads(f.read())

    # Build a name -> tomlkit table mapping for efficient lookup
    projects_array = doc.get("projects", [])
    name_to_table: dict[str, object] = {}
    for table in projects_array:
        name_to_table[table.get("name", "")] = table

    for proj_name, import_name in updates:
        table = name_to_table.get(proj_name)
        if table is not None:
            table["import_name"] = import_name
            print(f"Auto-detected import_name for {proj_name}: {import_name}")

    # Write back atomically
    tmp = ws_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    os.replace(tmp, ws_path)

    return ws_path


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

    # Auto-populate import_name for Python projects where the import name
    # differs from the project name (e.g., project "cloudflare" imports as "cf").
    ws_path = _sync_import_names(root, projects)
    if ws_path:
        if flags.get("auto-commit", True):
            commit_files_if_changed(
                "monorepo: auto-populate import_name",
                [ws_path],
                skip_message=None,
            )
        # Re-load projects so the rest of sync sees the updated import_names
        projects = load_workspace(root)

    # Scaffold releasable directory structure (explicit mode only).
    # This runs early so that releasable dirs exist before CI workflow
    # generation, which may need to reference releasable paths.
    releasable_files = scaffold_releasable_dirs(root)
    if releasable_files and flags.get("auto-commit", True):
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
            ci_docs_for_proj = []

            for ci_src in ci_sources:
                ci_basename = os.path.basename(ci_src)

                # Per-file job prefix: ci.yml -> {name}-ci,
                # ci-{target}.yml -> {name}-ci-{target}
                if ci_basename == "ci.yml":
                    ci_dest_name = f"{name}-ci.yml"
                else:
                    ci_dest_name = f"{name}-{ci_basename}"

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
                content, unreplaced = process_template(content, tvars)
                check_unreplaced_vars(ci_src, unreplaced)

                doc = parse_ci_workflow(content)
                if doc is None:
                    print(f"Warning: {ci_src} has no jobs: key, skipping", file=sys.stderr)
                    continue
                _inject_working_directory(doc, clean_path)
                _rewrite_version_file_inputs(doc, clean_path)
                _inject_packages_dir(doc, clean_path)

                # No root copy is written: the jobs are inlined into the CI
                # router (GitHub rejects routers with 20+ reusable-workflow
                # calls, so ``uses:``-based routing cannot scale).
                ci_dest_names_for_proj.append(ci_dest_name)
                ci_docs_for_proj.append((ci_dest_name.removesuffix(".yml"), doc))

            if ci_docs_for_proj:
                # _ci_files feeds the publish gate's check-run name regexes;
                # inline jobs keep the "{prefix} / {job}" naming, so the
                # prefix list stays the contract.
                proj['_ci_files'] = ci_dest_names_for_proj
                proj['_ci_docs'] = ci_docs_for_proj
                projects_with_ci.append(proj)

        # --- Publish workflow: verify existence / detect root publisher ---
        publish_src = os.path.join(root, path, ".github", "workflows", "publish.yml")
        if clean_path == ".":
            # Root publisher: its publish.yml IS the router output
            # (source==destination). It must NOT be read as a source (the
            # transform pipeline is non-idempotent on its own output).
            # Instead, when the root actually publishes, mark it so the
            # router generates its jobs from config/templates. Previously
            # this project self-excluded here, leaving projects_with_publish
            # empty so the gated router was never generated and the
            # hand-authored ungated publish.yml survived (a security bug).
            #
            # A publish.yml must already exist (hand-authored on first sync,
            # or the generated router thereafter): its presence is the signal
            # that the root publishes, exactly as for member projects.
            if os.path.isfile(publish_src) and _root_is_publisher(proj, root):
                proj['_root_publisher'] = True
                projects_with_publish.append(proj)
        elif os.path.isfile(publish_src):
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
        from ...workspace import is_explicit_mode, load_releasables

        # Load releasables so publish conditions use releasable tag prefixes
        releasables = None
        if is_explicit_mode(root):
            releasables = load_releasables(root, projects)

        monorepo_dir = os.path.join(root, ".rlsbl-monorepo")
        current_hashes = compute_publish_hashes(projects, root)
        cached_hashes = load_publish_cache(monorepo_dir)

        if should_regenerate_router(cached_hashes, current_hashes, publish_router_path):
            if os.path.isfile(publish_router_path):
                os.chmod(publish_router_path, 0o644)
            with open(publish_router_path, "w", encoding="utf-8") as f:
                f.write(generate_inline_publish_router(projects_with_publish, root, releasables=releasables))
            os.chmod(publish_router_path, 0o444)
            written_files.append(publish_router_path)

            cache_path = save_publish_cache(monorepo_dir, current_hashes)
            written_files.append(cache_path)
        else:
            print("Publish router up to date, skipping regeneration")

    # Remove stale workflows. Sync no longer writes per-project CI copies at
    # the root (their jobs are inlined into ci-router.yml), so every
    # {name}-ci*.yml and {name}-publish.yml at the root is stale.
    stale_removed = 0
    deleted_files = []
    for filename in os.listdir(workflows_dir):
        filepath = os.path.join(workflows_dir, filename)
        if filepath in written_files:
            continue

        # All *-publish.yml files are stale (we no longer generate per-project wrappers)
        if filename.endswith("-publish.yml"):
            os.chmod(filepath, 0o644)
            _saferm_workflow(
                filepath,
                f"Removing stale per-project publish wrapper {filename} "
                "-- publish jobs are inlined into the root publish.yml router",
            )
            deleted_files.append(filepath)
            stale_removed += 1
            print(f"Removed stale publish wrapper: {filename}")
            continue

        # Stale CI workflow copies: sync used to write {name}-ci.yml /
        # {name}-ci-{target}.yml reusable workflows at the root; the router
        # now inlines all CI jobs, so no such copy is ever current.
        if "-ci" in filename and filename.endswith(".yml"):
            os.chmod(filepath, 0o644)
            _saferm_workflow(
                filepath,
                f"Removing stale per-project CI workflow copy {filename} "
                "-- CI jobs are inlined into ci-router.yml",
            )
            deleted_files.append(filepath)
            stale_removed += 1
            print(f"Removed stale CI workflow copy: {filename}")

    # Auto-commit
    all_files = written_files + deleted_files
    if all_files:
        if not flags.get("auto-commit", True):
            quoted = " ".join(all_files)
            print(f"Skipped commit (--no-auto-commit). Run `safegit commit -- {quoted}` manually.")
        else:
            commit_files_if_changed("monorepo: sync CI workflows", all_files, skip_message="No workflow changes to commit.")

    inlined_count = sum(len(p.get('_ci_docs', [])) for p in projects_with_ci)
    router_count = 1 + (1 if projects_with_publish else 0)
    msg = (
        f"Inlined {inlined_count} CI workflow(s) into ci-router.yml, "
        f"generated {router_count} router(s)."
    )
    if stale_removed:
        msg += f" Removed {stale_removed} stale workflow(s)."
    print(msg)

    # Warn about Swift projects without subtree_remote
    for proj in projects:
        rel_dir = resolve_releasable_config_dir(proj, root)
        proj_targets = detect_targets(os.path.join(root, proj["path"]), releasable_config_dir=rel_dir)
        if any(te.name in ("swift", "swift-apple") for te in proj_targets):
            if not proj.get("subtree_remote"):
                print(
                    f"Warning: Swift project '{proj['name']}' has no subtree_remote configured. "
                    "SPM consumers won't be able to resolve monorepo tags.",
                    file=sys.stderr,
                )
