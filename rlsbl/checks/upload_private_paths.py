"""Upload check (tags: project, preflight): no upload carries a private path.

Checks: upload-private-paths.

Every target that can list its published upload offline
(:meth:`~rlsbl.targets.base.BaseTarget.offline_upload_listing`: the npm
package, the Go module zip) is listed and matched
against :mod:`rlsbl.private_paths`. A Python upload has to be built, which
needs the network, so the pypi CI template builds and checks it on the
candidate commit instead; this check answers nothing for it.
"""

import os

from ..check_context import WorkspaceCheckContext
from ..errors import ConfigError


def _publishing_roots(ctx):
    """``(label, directory, releasable config dir)`` for each publishing project.

    Workspace-wide when the context carries the workspace's members, so a
    member's upload is checked whichever position the check runs from.
    """
    from ..checks.nested import _uploads
    from ..config import read_project_config, suppresses_publish
    from ..targets import resolve_releasable_config_dir, resolve_releasable_config_dir_for_ctx

    if isinstance(ctx, WorkspaceCheckContext) and ctx.projects:
        root = os.path.realpath(str(ctx.workspace_root))
        return [
            (proj["name"], os.path.join(root, proj["path"]),
             resolve_releasable_config_dir(proj, root))
            for proj in ctx.projects if _uploads(proj, root)
        ]
    config_dir = resolve_releasable_config_dir_for_ctx(ctx)
    config = ctx.config
    if config is None:
        config = read_project_config(str(ctx.project_root), releasable_config_dir=config_dir)
    try:
        if suppresses_publish(config):
            return []
    except ConfigError:
        # An unreadable publish_mode is the publish-mode check's finding; the
        # upload is listed as though it publishes.
        pass
    return [(None, str(ctx.project_root), config_dir)]


def upload_private_path_problems(ctx):
    """One finding per private path an offline-listable upload would carry."""
    from . import targets_for_check
    from ..private_paths import private_paths_in
    from ..targets import TARGETS, detect_targets

    scope = targets_for_check("upload-private-paths")
    problems = []
    seen = set()
    for name, directory, config_dir in _publishing_roots(ctx):
        entries = detect_targets(directory, releasable_config_dir=config_dir)
        for entry in entries:
            if entry.name not in scope:
                continue
            target = TARGETS[entry.name]
            key = (entry.name, os.path.realpath(entry.path))
            if key in seen:
                continue
            seen.add(key)
            listing = target.offline_upload_listing(entry.path)
            if listing is None:
                continue
            prefix = f"{name}: " if name else ""
            for rel, rule, private_dir in private_paths_in(listing.files):
                problems.append(
                    f"{prefix}{listing.label} would ship {rel}, a private path "
                    f"({rule}): {listing.private_path_fix(rel, rule, private_dir)}."
                )
    return problems


def register_upload_private_path_checks(app):
    """Register the upload-private-paths check on *app*."""

    @app.error_check("upload-private-paths")
    def check_upload_private_paths(ctx, reporter):
        """No npm package or Go module zip carries a private path."""
        try:
            problems = upload_private_path_problems(ctx)
        except (ConfigError, ValueError, OSError) as exc:
            reporter.error(str(exc))
            return reporter.found("an upload's contents could not be listed")
        if not problems:
            return reporter.passed(
                "no npm package or Go module zip carries a private path"
            )
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} private path(s) an upload would carry; a registry "
            f"keeps every upload permanently"
        )
