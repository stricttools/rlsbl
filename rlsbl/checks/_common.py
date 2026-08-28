"""Shared helpers for rlsbl check modules: version and tag resolution, changelog context, sibling directory exclusion, and reporter text hygiene."""

import os

from ..check_context import WorkspaceCheckContext

# Universal project indicator: every scaffolded rlsbl project has this file.
RLSBL_CONFIG = os.path.join(".rlsbl", "config.json")

# Fallback text for a reporter call whose intended text turned out to be empty.
# The reporter rejects empty problem/outcome text with a ValueError that
# propagates out of the whole check run, so no rlsbl check may ever hand it one.
_NO_TEXT = "(no message)"


def reportable_lines(text, *, limit=None):
    """Return the non-blank lines of *text*, right-stripped, optionally capped.

    Tool output is not safe to feed to a reporter line by line: linters like
    ruff separate findings with blank lines, and an empty problem text is a
    hard error that aborts the entire check run with no attribution to the
    check that produced it.  Blank lines carry no finding, so they are dropped
    before the reporter ever sees them.  *limit* caps the number of lines
    returned (applied after filtering, so a cap of 20 yields 20 real findings).
    """
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if limit is not None:
        return lines[:limit]
    return lines


def summary_line(text, *, limit=200, fallback=_NO_TEXT):
    """Return the first non-blank line of *text*, truncated to *limit* chars.

    Used for the one-line outcome message that accompanies a set of problems.
    Never returns an empty string: text that is empty or entirely blank yields
    *fallback*, because the reporter rejects an empty outcome message.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return fallback


def exception_text(exc, *, fallback=None):
    """Return a non-empty description of *exc* suitable for a reporter call.

    An exception raised with no message (or a whitespace-only one) stringifies
    to ``""``, which the reporter rejects.  Falling back to the exception's
    class name keeps the failure attributable to the check that hit it instead
    of aborting the run with an internal error.
    """
    text = str(exc).strip()
    if text:
        return text
    return fallback or f"{type(exc).__name__} {_NO_TEXT}"


def _resolve_version_and_tag(ctx):
    """Detect version and tag from project targets rooted at *ctx*.

    In explicit releasable mode, uses the releasable's version file and
    tag_format instead of the per-target version and tag format.

    Returns ``(version, tag)``; either may be ``None``.
    """
    from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx

    # In explicit releasable mode, version and tag come from the releasable
    if isinstance(ctx, WorkspaceCheckContext) and getattr(ctx, "releasables", None):
        from ..workspace import (
            is_explicit_mode,
            read_releasable_version,
            resolve_project,
            resolve_releasable_for_project,
        )

        ws_root = str(ctx.workspace_root)
        if is_explicit_mode(ws_root):
            proj = resolve_project(ws_root, str(ctx.project_root))
            if proj is not None:
                rel = resolve_releasable_for_project(proj, ctx.releasables)
                if rel is not None:
                    try:
                        version = read_releasable_version(ws_root, rel.name)
                    except Exception:
                        version = None
                    if version:
                        tag = rel.effective_tag_format.replace("{version}", version).replace("{name}", rel.name)
                    else:
                        tag = None
                    return version, tag

    rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
    target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
    if not target_entries:
        return None, None

    first_name, first_path = target_entries[0]
    target = TARGETS[first_name]
    try:
        version = target.read_version(first_path)
    except Exception as e:
        import sys
        print(f"Warning: could not read version from {first_name}: {e}", file=sys.stderr)
        version = None
    tag = target.tag_format(version) if version else None
    return version, tag


def _get_changelog_context(ctx):
    """Resolve changes_dir, tag_glob, scope, and entries for changelog checks.

    Returns ``(changes_dir, tag_glob, scope, entries)`` or ``None`` when the
    changes directory does not exist (caller should return skip).

    The ``scope`` value is an :class:`~rlsbl.ownership.OwnershipScope` -- the
    whole workspace member list plus the names of the members whose files this
    changelog covers -- or ``None`` for a standalone project with no workspace.
    Attribution needs the whole list even when only some members are in scope,
    because a file's owner is decided against every member.

    In explicit mode, ``changes_dir`` points to the releasable's changes
    directory (``.rlsbl-monorepo/releasables/{name}/changes/``) and
    ``tag_glob`` is derived from the releasable's ``tag_format``.
    """
    from ..ownership import OwnershipScope
    from ..changelog.files import get_changes_dir, read_unreleased
    from ..workspace import (
        get_releasable_changes_dir,
        is_explicit_mode,
        members_of,
        resolve_project,
        resolve_releasable_for_project,
    )

    if not isinstance(ctx, WorkspaceCheckContext):
        # Standalone project
        changes_dir = get_changes_dir(str(ctx.project_root))
        if not os.path.isdir(changes_dir):
            return None
        entries = read_unreleased(changes_dir)
        return changes_dir, None, None, entries

    # Monorepo mode
    ws_root = str(ctx.workspace_root)
    proj = resolve_project(ws_root, str(ctx.project_root))
    if proj is None:
        # CWD is not inside any workspace project.
        # In explicit mode with releasables, return None here -- callers
        # should use _get_all_changelog_contexts() to iterate releasables.
        if is_explicit_mode(ws_root) and getattr(ctx, "releasables", None):
            return None
        # Otherwise fall back to per-project (standalone-like)
        changes_dir = get_changes_dir(str(ctx.project_root))
        if not os.path.isdir(changes_dir):
            return None
        entries = read_unreleased(changes_dir)
        return changes_dir, None, None, entries

    if is_explicit_mode(ws_root) and getattr(ctx, "releasables", None):
        # Explicit mode: resolve the releasable for this project
        rel = resolve_releasable_for_project(proj, ctx.releasables)
        if rel is None:
            # Project is not releasable (releasable = false)
            return None
        changes_dir = get_releasable_changes_dir(ws_root, rel.name)
        if not os.path.isdir(changes_dir):
            return None
        # tag_glob from releasable's tag_format: replace {version} with *
        tag_glob = rel.effective_tag_format.replace("{version}", "*").replace("{name}", rel.name)
        # All member projects of this releasable for commit scoping
        member_projects = members_of(rel.name, ctx.projects)
        scope = OwnershipScope.for_members(ctx.projects, member_projects)
        entries = read_unreleased(changes_dir)
        return changes_dir, tag_glob, scope, entries

    # Implicit mode: per-project changes dir
    changes_dir = get_changes_dir(str(ctx.project_root))
    if not os.path.isdir(changes_dir):
        return None

    # Use the target's monorepo_tag_glob() to get the correct
    # tag pattern (e.g. Go uses "path/v*" not "name@v*").
    from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx
    rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
    target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
    if target_entries:
        target = TARGETS[target_entries[0].name]
        tag_glob = target.monorepo_tag_glob(proj['name'], path=proj['path'])
    else:
        tag_glob = f"{proj['name']}@v*"

    entries = read_unreleased(changes_dir)
    return changes_dir, tag_glob, OwnershipScope.for_member(ctx.projects, proj), entries


def _resolve_tag_glob(ctx):
    """Return the git tag glob that scopes *ctx*'s project to its own releases.

    The single derivation used by the changelog checks
    (:func:`_get_changelog_context`): the releasable's ``tag_format`` in
    explicit monorepo mode, the target's ``monorepo_tag_glob`` in implicit
    mode, and the plain ``v*`` for a standalone repo. Reused by the
    external-check release-context env so a consumer check resolves the same
    last tag rlsbl itself would.
    """
    resolved = _get_changelog_context(ctx)
    if resolved is None:
        return "v*"
    _changes_dir, tag_glob, _project, _entries = resolved
    return tag_glob or "v*"


def _resolve_ledger_dir(ctx):
    """Return the release-archive directory that is *ctx*'s project's ledger.

    Derived from the same context resolution :func:`_resolve_tag_glob` uses, so
    the range a check computes and the archives it computes it from always
    describe the same project. Falls back to the project's own
    ``.rlsbl/releases/`` when there is no changelog context at all -- a project
    with no changes dir still has a releases dir, and an empty one honestly
    reports "nothing released here".
    """
    from ..ledger import releases_dir_for_changes_dir

    resolved = _get_changelog_context(ctx)
    if resolved is None:
        return os.path.join(str(ctx.project_root), ".rlsbl", "releases")
    changes_dir, _tag_glob, _project, _entries = resolved
    return releases_dir_for_changes_dir(changes_dir)


def _resolve_release_identity(ctx):
    """Return ``(target, ref_context, releases_dir)`` for *ctx*'s project.

    The one resolution the ref checks work from: which target answers ref
    questions, the context ``expected_refs`` reads, and the ledger whose
    archives say which versions exist.  All three describe the SAME project by
    construction -- the releases dir comes from :func:`_resolve_ledger_dir`,
    and the context is built from the same explicit-mode/implicit-mode branch
    that resolved it.

    Returns None when the project has no detectable target, so there is nothing
    to name refs for.
    """
    from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx
    from ..targets.refs import ref_context

    rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
    entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
    if not entries:
        return None
    target = TARGETS[entries[0].name]
    releases_dir = _resolve_ledger_dir(ctx)

    if not isinstance(ctx, WorkspaceCheckContext):
        return target, ref_context(repo_root=str(ctx.project_root)), releases_dir

    from ..workspace import (
        get_releasable_dir,
        is_explicit_mode,
        members_of,
        resolve_project,
        resolve_releasable_for_project,
    )

    ws_root = str(ctx.workspace_root)
    proj = resolve_project(ws_root, str(ctx.project_root))
    if proj is None:
        return target, ref_context(repo_root=str(ctx.project_root)), releases_dir

    if is_explicit_mode(ws_root) and getattr(ctx, "releasables", None):
        rel = resolve_releasable_for_project(proj, ctx.releasables)
        if rel is None:
            return None
        members = members_of(rel.name, ctx.projects)
        return target, ref_context(
            repo_root=ws_root,
            project_path=proj["path"],
            primary_tag_format=rel.effective_tag_format,
            releasable_name=rel.name,
            member_package_paths=[m["path"] for m in members],
            releasable_config_dir=get_releasable_dir(ws_root, rel.name),
        ), releases_dir

    return target, ref_context(
        repo_root=ws_root,
        project_path=proj["path"],
        monorepo_name=proj["name"],
    ), releases_dir


def _get_all_changelog_contexts(ctx):
    """Return changelog contexts for ALL releasables when CWD is the workspace root.

    When CWD is inside a specific project, delegates to ``_get_changelog_context``
    and wraps the result in a list.  When CWD is the workspace root in an
    explicit-mode workspace, iterates all releasables and returns a context
    tuple for each one that has a changes directory.

    Returns a list of ``(changes_dir, tag_glob, scope, entries)`` tuples,
    or an empty list when no contexts are available (caller should skip).
    """
    from ..changelog.files import read_unreleased
    from ..ownership import OwnershipScope
    from ..workspace import (
        get_releasable_changes_dir,
        is_explicit_mode,
        members_of,
        resolve_project,
    )

    # Non-workspace or CWD is inside a specific project: delegate
    if not isinstance(ctx, WorkspaceCheckContext):
        single = _get_changelog_context(ctx)
        return [single] if single is not None else []

    from ..ownership import is_root_member

    ws_root = str(ctx.workspace_root)
    proj = resolve_project(ws_root, str(ctx.project_root))

    # The root member's directory IS the workspace root, so standing there
    # means "the whole workspace", not "this one member" -- every releasable's
    # changelog is in scope, including the root member's own.
    at_workspace_root = proj is None or is_root_member(proj)

    if not at_workspace_root or not is_explicit_mode(ws_root) or not getattr(ctx, "releasables", None):
        # CWD is inside a member package -- single context
        single = _get_changelog_context(ctx)
        return [single] if single is not None else []

    # CWD is workspace root in explicit mode: iterate all releasables
    contexts = []
    for rel in ctx.releasables:
        changes_dir = get_releasable_changes_dir(ws_root, rel.name)
        if not os.path.isdir(changes_dir):
            continue
        tag_glob = rel.effective_tag_format.replace("{version}", "*").replace("{name}", rel.name)
        member_projects = members_of(rel.name, ctx.projects)
        scope = OwnershipScope.for_members(ctx.projects, member_projects)
        entries = read_unreleased(changes_dir)
        contexts.append((changes_dir, tag_glob, scope, entries))
    return contexts


def _sibling_exclude_dirs(root, project_path, all_projects):
    """Compute sibling project directories to exclude from a scan.

    For a project at ``project_path``, returns a list of other
    workspace project directories that are subdirectories of this
    project's path.  This prevents walk_source_files from descending
    into sibling projects when the current project is at a parent
    path (e.g. ``path = "."``).
    """
    project_abs = os.path.realpath(os.path.join(root, project_path))
    exclude = []
    for other in all_projects:
        other_path = other["path"]
        if other_path == project_path:
            continue
        other_abs = os.path.realpath(os.path.join(root, other_path))
        # Only exclude if the other project is strictly inside this
        # project's directory tree.
        if other_abs.startswith(project_abs + os.sep):
            exclude.append(other_abs)
    return exclude


def _build_dep_import_cache(ctx):
    """Build per-project import scan cache for dependency checks.

    Returns a dict mapping project name to (lib_imports, test_imports,
    guarded_imports). All dep checks (unused, undeclared, runtime-test-only,
    dev-in-lib) share one scan pass via this cache. The result is memoized
    on the context object so that multiple checks in the same run reuse a
    single scan instead of re-walking every project's source tree.
    """
    cached = getattr(ctx, "_dep_import_cache", None)
    if cached is not None:
        return cached

    from ..dep_validation import _get_imported_workspace_packages
    from ..import_scanners import build_jvm_package_map, build_namespace_map
    from ..utils import read_go_module_path

    root = str(ctx.workspace_root)
    workspace_names = {p["name"] for p in ctx.projects}

    # Build Go module path mapping for all Go projects in the workspace
    module_path_map: dict[str, str] = {}
    for proj in ctx.projects:
        project_dir = os.path.join(root, proj["path"])
        mod_path = read_go_module_path(project_dir)
        if mod_path is not None:
            module_path_map[proj["name"]] = mod_path

    # Build namespace map for Python namespace package detection
    namespace_map = build_namespace_map(ctx.projects, root)

    # Collect import_names from workspace projects
    import_names: dict[str, str] = {}
    for proj in ctx.projects:
        imp_name = proj.get("import_name", "")
        if imp_name:
            import_names[proj["name"]] = imp_name

    # Build JVM package prefix map for Java/Kotlin import detection
    jvm_package_map = build_jvm_package_map(ctx.projects, root)

    cache = {}
    for proj in ctx.projects:
        project_dir = os.path.join(root, proj["path"])
        exclude = _sibling_exclude_dirs(root, proj["path"], ctx.projects)
        cache[proj["name"]] = _get_imported_workspace_packages(
            project_dir, workspace_names,
            exclude_dirs=exclude or None,
            module_path_map=module_path_map or None,
            namespace_map=namespace_map or None,
            import_names=import_names or None,
            jvm_package_map=jvm_package_map or None,
        )
    ctx._dep_import_cache = cache
    return cache
