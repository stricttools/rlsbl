"""Nested-member checks (tags: workspace, preflight): a member's tools leave the members inside it alone.

Checks: nested-member-runner-exclusion, nested-member-upload-contents.
"""

import os

from ..check_context import WorkspaceCheckContext
from ..errors import ConfigError


def runner_exclusion_problems(workspace_root):
    """One finding per member whose test runner would collect a nested member."""
    from ..nested_exclusions import missing_exclusions, nested_paths_inside
    from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir
    from ..workspace import load_workspace, nested_member_dirs

    root = str(workspace_root)
    problems = []
    # The whole member list, read from the workspace: a release's preflight
    # hands each member a context listing that member alone.
    for proj in load_workspace(root):
        proj_dir = os.path.join(root, proj["path"])
        nested = nested_member_dirs(proj_dir)
        if not nested:
            continue
        entries = detect_targets(
            proj_dir, releasable_config_dir=resolve_releasable_config_dir(proj, root),
        )
        seen = set()
        for entry in entries:
            target = TARGETS.get(entry.name)
            if target is None:
                continue
            key = (target.scratch_test_exclusion, os.path.realpath(entry.path))
            if key in seen:
                continue
            seen.add(key)
            nested_rel = nested_paths_inside(entry.path, nested)
            found = missing_exclusions(
                target.scratch_test_exclusion, entry.path, nested_rel,
            )
            if found is None:
                continue
            config_file, missing = found
            config_rel = os.path.relpath(config_file, root).replace(os.sep, "/")
            problems.append(
                f"{proj['name']}: its {entry.name} test runner would collect "
                f"the members nested inside it; {config_rel} must exclude "
                f"{', '.join(missing)}. Run `rlsbl scaffold` in {proj['path']} "
                f"to write them (a file rlsbl cannot write into, such as a "
                f"pytest.ini or a deno.jsonc, takes them by hand). pytest "
                f"resolves --ignore against the directory it starts in: run it "
                f"from {proj['path']}, as rlsbl and CI do."
            )
    return problems


def register_nested_checks(app):
    """Register the nested-member checks on *app*."""
    _register_upload_contents(app)

    @app.error_check("nested-member-runner-exclusion")
    def check_nested_member_runner_exclusion(ctx, reporter):
        """A member's test runner must exclude every member nested inside it."""
        if not isinstance(ctx, WorkspaceCheckContext) or ctx.workspace_root is None:
            return reporter.skipped("not a workspace")
        problems = runner_exclusion_problems(ctx.workspace_root)
        if not problems:
            return reporter.passed(
                "every member's test runner excludes the members nested inside it"
            )
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} member(s) whose test runner would collect a nested member"
        )


# ---------------------------------------------------------------------------
# What a member's upload carries
# ---------------------------------------------------------------------------


def upload_content_problems(workspace_root):
    """One finding per file a member's upload would take from a nested member.

    Each target answers what its own upload carries
    (:meth:`~rlsbl.targets.base.BaseTarget.offline_upload_listing`); a target
    that cannot list it offline -- a Python sdist has to be built -- answers
    None and is checked in CI instead.
    """
    from ..ownership import member_name, owner_of
    from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir
    from ..workspace import load_workspace, nested_member_dirs

    root = os.path.realpath(str(workspace_root))
    projects = load_workspace(root)
    problems = []
    for proj in projects:
        proj_dir = os.path.join(root, proj["path"])
        if not nested_member_dirs(proj_dir):
            continue
        entries = detect_targets(
            proj_dir, releasable_config_dir=resolve_releasable_config_dir(proj, root),
        )
        for entry in entries:
            target = TARGETS.get(entry.name)
            listing = target.offline_upload_listing(entry.path) if target else None
            if listing is None:
                continue
            base = os.path.relpath(os.path.realpath(entry.path), root).replace(os.sep, "/")
            for rel in listing.files:
                path = rel if base == "." else f"{base}/{rel}"
                owner = owner_of(path, projects)
                if owner is None or member_name(owner) == member_name(proj):
                    continue
                problems.append(
                    f"{proj['name']}: {listing.label} would ship {path}, which "
                    f"member '{member_name(owner)}' owns. {listing.remedy}"
                )
    return problems


def _register_upload_contents(app):
    @app.error_check("nested-member-upload-contents")
    def check_nested_member_upload_contents(ctx, reporter):
        """A member's npm package and Go module zip carry no nested member's files."""
        if not isinstance(ctx, WorkspaceCheckContext) or ctx.workspace_root is None:
            return reporter.skipped("not a workspace")
        try:
            problems = upload_content_problems(ctx.workspace_root)
        except (ConfigError, ValueError, OSError) as exc:
            reporter.error(str(exc))
            return reporter.found("an upload's contents could not be listed")
        if not problems:
            return reporter.passed(
                "no npm package or Go module zip carries a nested member's files"
            )
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} file(s) a member's upload would take from a nested member"
        )
