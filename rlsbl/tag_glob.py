"""Shared git tag-glob resolution for monorepo projects and releasables.

A single source of truth for deriving the ``git tag`` match pattern of a
monorepo project. Two code paths previously derived this independently
(``rlsbl monorepo status`` and ``rlsbl status``), and one of them ignored the
releasable's ``tag_format`` -- so releasable members reported no tag. Both now
call :func:`resolve_monorepo_tag_glob`.
"""

import os


def releasable_tag_glob(tag_format: str, releasable_name: str) -> str:
    """Derive a glob from a releasable's ``tag_format``.

    Replaces ``{version}`` with ``*`` and fills in ``{name}`` with the literal
    releasable name so ``git tag -l`` matches all versions
    (e.g. ``"{name}@v{version}"`` -> ``"www@v*"``).
    """
    return tag_format.replace("{version}", "*").format(name=releasable_name)


def resolve_monorepo_tag_glob(project, workspace_root, releasable=None) -> str:
    """Return the ``git tag`` glob for a monorepo *project*.

    When the project belongs to a *releasable* (explicit mode), the glob is
    derived from the releasable's ``tag_format`` -- this is the fix for
    releasable members previously falling back to the per-member target glob
    and reporting no tag. Otherwise the glob comes from the project's first
    detected target's ``monorepo_tag_glob``, or ``"{name}@v*"`` when no target
    is detected.

    Args:
        project: a WorkspaceProject or dict with ``name``/``path``.
        workspace_root: path to the monorepo root (str or Path).
        releasable: optional Releasable the project belongs to; when provided,
            its ``tag_format`` drives the glob.
    """
    if releasable is not None:
        return releasable_tag_glob(releasable.tag_format, releasable.name)

    from .targets import TARGETS, detect_targets, resolve_releasable_config_dir

    rel_dir = resolve_releasable_config_dir(project, workspace_root)
    proj_dir = os.path.join(str(workspace_root), project["path"])
    target_entries = detect_targets(proj_dir, releasable_config_dir=rel_dir)
    if target_entries and target_entries[0].name in TARGETS:
        return TARGETS[target_entries[0].name].monorepo_tag_glob(
            project["name"], path=project["path"]
        )
    return f"{project['name']}@v*"
