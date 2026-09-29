"""Nested-member checks (tags: workspace, preflight): a member's tools leave the members inside it alone.

Checks: nested-member-runner-exclusion.
"""

import os

from ..check_context import WorkspaceCheckContext


def runner_exclusion_problems(workspace_root):
    """One finding per member whose test runner would collect a nested member."""
    from ..nested_exclusions import (
        CHECK_NAME,
        missing_exclusions,
        nested_paths_inside,
    )
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
