"""Pre-push checks (tag: prepush) enforcing changelog coverage for pushed commits, gitignore guards, and manual release push warnings.

Checks: prepush-changelog-coverage, prepush-gitignore-guard,
prepush-manual-warning.
"""

import os

from ..workspace import project_is_releasable


def register_prepush_checks(app):
    """Register prepush-tag checks on *app*."""

    @app.error_check("prepush-changelog-coverage")
    def check_prepush_changelog_coverage(ctx, reporter):
        """Every pushed commit must have a JSONL changelog entry."""
        from ..changelog import changes_dir_exists
        from ..prepush_utils import (
            _check_jsonl_changelog,
            _get_pushed_commits,
            _parse_stdin_refs,
        )
        from ..git_util import affected_members, filter_commits_for_scope, get_push_changed_files
        from ..ownership import OwnershipScope

        if ctx.push_stdin is None:
            return reporter.skipped("not in push context")

        stdin_lines = ctx.push_stdin.strip().splitlines()
        refs = _parse_stdin_refs(stdin_lines)
        if refs is None:
            return reporter.skipped("no refs parsed from push stdin")

        # Monorepo mode: check each affected project independently
        if ctx.workspace_root is not None:
            from ..workspace import (
                get_releasable_changes_dir,
                load_workspace,
                members_of,
                resolve_releasable_for_project,
            )

            ws_root = str(ctx.workspace_root)
            changed_files = get_push_changed_files(refs)
            if changed_files is None:
                return reporter.skipped("could not determine changed files")

            projects = load_workspace(ws_root)
            affected = affected_members(changed_files, projects)
            if not affected:
                return reporter.passed("no affected projects")

            all_pushed = _get_pushed_commits(refs)
            if all_pushed is None:
                return reporter.skipped("could not determine pushed commits")

            failures = []

            # Coverage is per releasable: a member's commits are checked
            # against its releasable's changelog. A member that belongs to no
            # releasable (releasable = false) has no changelog to cover.
            checked_releasables = set()
            for proj in affected:
                if not project_is_releasable(proj):
                    continue
                rel = resolve_releasable_for_project(proj, ctx.releasables)
                if rel is None or rel.name in checked_releasables:
                    continue
                checked_releasables.add(rel.name)
                rel_changes_dir = get_releasable_changes_dir(ws_root, rel.name)
                if not os.path.isdir(rel_changes_dir):
                    continue
                member_projs = members_of(rel.name, projects)
                rel_commits = filter_commits_for_scope(
                    all_pushed,
                    OwnershipScope.for_members(projects, member_projs),
                    operation="pre-push changelog coverage",
                )
                if not rel_commits:
                    continue
                error = _check_jsonl_changelog(
                    ws_root, refs, pushed_commits=rel_commits,
                    changes_dir=rel_changes_dir,
                )
                if error:
                    failures.append(f"{rel.name}: {error}")

            if failures:
                msg = "; ".join(failures)
                reporter.error(msg)
                return reporter.found(msg)
            return reporter.passed(f"all {len(affected)} affected project(s) covered")

        # Single-project mode
        root_str = str(ctx.project_root)
        if not changes_dir_exists(root_str):
            return reporter.skipped("JSONL changelog not set up")

        error = _check_jsonl_changelog(root_str, refs)
        if error is not None:
            reporter.error(error)
            return reporter.found(error)
        return reporter.passed("all pushed commits covered")

    @app.error_check("prepush-gitignore-guard")
    def check_prepush_gitignore_guard(ctx, reporter):
        """rlsbl-managed files must not be gitignored."""
        from ..prepush_utils import _check_gitignore_guard

        extra_paths = None
        if (
            ctx.workspace_root is not None
            and getattr(ctx, "releasables", None)
        ):
            from ..workspace import (
                get_releasable_changes_dir,
                resolve_project,
                resolve_releasable_for_project,
            )

            ws_root = str(ctx.workspace_root)
            proj = resolve_project(ws_root, str(ctx.project_root))
            if proj is not None:
                rel = resolve_releasable_for_project(proj, ctx.releasables)
                if rel is not None:
                    rel_changes_dir = get_releasable_changes_dir(ws_root, rel.name)
                    extra_paths = [
                        os.path.join(rel_changes_dir, "unreleased.jsonl"),
                        os.path.join(rel_changes_dir, ".validated"),
                    ]

        error = _check_gitignore_guard(str(ctx.project_root), extra_paths=extra_paths)
        if error is not None:
            reporter.error(error)
            return reporter.found(error)
        return reporter.passed("no rlsbl-managed files are gitignored")

    @app.error_check("prepush-manual-warning")
    def check_prepush_manual_warning(ctx, reporter):
        """Hard-error when pushing to a release branch outside rlsbl release.

        Release-internal pushes never reach this check: they run
        ``git push --no-verify`` and skip the hook entirely. So any push to a
        release branch that DOES run the hook is by construction a manual one.
        """
        from ..prepush_utils import _get_release_branches
        from ..git_util import detect_manual_push_branches

        if ctx.push_stdin is None:
            return reporter.skipped("not in push context")

        stdin_lines = ctx.push_stdin.strip().splitlines()
        release_branches = _get_release_branches(ctx)
        pushed_release_branches = detect_manual_push_branches(
            stdin_lines, release_branches,
        )

        if pushed_release_branches:
            branches_str = ", ".join(sorted(set(pushed_release_branches)))
            msg = (
                f"manual push to release branch ({branches_str}) -- "
                f"not via 'rlsbl release'. Use `rlsbl release run` to publish; "
                f"never push a release branch by hand."
            )
            reporter.error(msg)
            return reporter.found(msg)
        return reporter.passed("not pushing to a release branch")
