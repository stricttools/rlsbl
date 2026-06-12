"""Shared helpers for check modules."""

import os

from ..check_context import WorkspaceCheckContext

# Universal project indicator: every scaffolded rlsbl project has this file.
RLSBL_CONFIG = os.path.join(".rlsbl", "config.json")


def _resolve_version_and_tag(ctx):
    """Detect version and tag from project targets rooted at *ctx*.

    Returns ``(version, tag)``; either may be ``None``.
    """
    from ..targets import TARGETS, detect_targets

    target_entries = detect_targets(str(ctx.project_root))
    if not target_entries:
        return None, None

    first_name, first_path = target_entries[0]
    target = TARGETS[first_name]
    try:
        version = target.read_version(first_path)
    except Exception:
        version = None
    tag = target.tag_format(version) if version else None
    return version, tag


def _get_changelog_context(ctx):
    """Resolve changes_dir, tag_glob, project, and entries for changelog checks.

    Returns ``(changes_dir, tag_glob, project, entries)`` or ``None`` when the
    changes directory does not exist (caller should return skip).
    The ``project`` value is a dict with ``path`` and ``watch`` keys when
    running in monorepo mode, or ``None`` for standalone projects.
    """
    from ..changelog.files import get_changes_dir, read_unreleased

    changes_dir = get_changes_dir(str(ctx.project_root))
    if not os.path.isdir(changes_dir):
        return None

    tag_glob = None
    project = None
    if isinstance(ctx, WorkspaceCheckContext):
        # Derive tag_glob and project dict from workspace for monorepo scoping
        from ..workspace import resolve_project
        proj = resolve_project(str(ctx.workspace_root), str(ctx.project_root))
        if proj is not None:
            # Use the target's monorepo_tag_glob() to get the correct
            # tag pattern (e.g. Go uses "path/v*" not "name@v*").
            from ..targets import TARGETS, detect_targets
            target_entries = detect_targets(str(ctx.project_root))
            if target_entries:
                target = TARGETS[target_entries[0].name]
                tag_glob = target.monorepo_tag_glob(proj['name'], path=proj['path'])
            else:
                tag_glob = f"{proj['name']}@v*"
            project = proj

    entries = read_unreleased(changes_dir)
    return changes_dir, tag_glob, project, entries


def _sibling_exclude_dirs(root, project_path, all_projects):
    """Compute sibling project directories to exclude from a scan.

    For a project at ``project_path``, returns a list of other
    workspace project directories that are subdirectories of this
    project's path.  This prevents walk_source_files from descending
    into sibling projects when the current project is at a parent
    path (e.g. ``path = "."``).
    """
    project_abs = os.path.normpath(os.path.join(root, project_path))
    exclude = []
    for other in all_projects:
        other_path = other["path"]
        if other_path == project_path:
            continue
        other_abs = os.path.normpath(os.path.join(root, other_path))
        # Only exclude if the other project is strictly inside this
        # project's directory tree.
        if other_abs.startswith(project_abs + os.sep):
            exclude.append(other_abs)
    return exclude


def _build_dep_import_cache(ctx):
    """Build per-project import scan cache for dependency checks.

    Returns a dict mapping project name to (lib_imports, test_imports).
    All dep checks (unused, undeclared, runtime-test-only, dev-in-lib)
    share one scan pass via this cache.  The result is memoized on the
    context object so that multiple checks in the same run reuse a
    single scan instead of re-walking every project's source tree.
    """
    cached = getattr(ctx, "_dep_import_cache", None)
    if cached is not None:
        return cached

    from ..dep_validation import _get_imported_workspace_packages, _read_go_module_path

    root = str(ctx.workspace_root)
    workspace_names = {p["name"] for p in ctx.projects}

    # Build Go module path mapping for all Go projects in the workspace
    module_path_map: dict[str, str] = {}
    for proj in ctx.projects:
        project_dir = os.path.join(root, proj["path"])
        mod_path = _read_go_module_path(project_dir)
        if mod_path is not None:
            module_path_map[proj["name"]] = mod_path

    cache = {}
    for proj in ctx.projects:
        project_dir = os.path.join(root, proj["path"])
        exclude = _sibling_exclude_dirs(root, proj["path"], ctx.projects)
        cache[proj["name"]] = _get_imported_workspace_packages(
            project_dir, workspace_names,
            exclude_dirs=exclude or None,
            module_path_map=module_path_map or None,
        )
    ctx._dep_import_cache = cache
    return cache
