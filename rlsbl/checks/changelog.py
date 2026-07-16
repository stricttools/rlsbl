"""Changelog checks (tag: changelog) validating JSONL entry schema, commit hash resolution, coverage, orphans, and batch size limits.

Checks: changelog-entry, changelog-hashes, changelog-range,
changelog-coverage, changelog-orphans, changelog-schema,
changelog-user-facing, changelog-batch-commits, changelog-batch-entries.
"""

import os

from ..changelog.files import read_coverage_unit
from ._common import _resolve_version_and_tag, _get_all_changelog_contexts


def register_changelog_checks(app):
    """Register changelog-tag checks on *app*."""

    @app.error_check("changelog-entry")
    def check_changelog_entry(ctx, reporter):
        """CHANGELOG.md must have an entry for the current version."""
        from ..utils import extract_changelog_entry
        from ..changelog.home import get_changelog_home
        from ..check_context import WorkspaceCheckContext
        from ..workspace import (
            get_releasable_dir,
            is_explicit_mode,
            resolve_project,
            resolve_releasable_for_project,
        )

        version, _tag = _resolve_version_and_tag(ctx)
        if not version:
            return reporter.skipped("no version detected")

        # The canonical CHANGELOG.md location comes from the single home
        # resolver: releasable dir in explicit releasable mode, project
        # root otherwise.
        releasable_dir = None
        if isinstance(ctx, WorkspaceCheckContext) and ctx.workspace_root is not None:
            ws_root = str(ctx.workspace_root)
            if is_explicit_mode(ws_root) and getattr(ctx, "releasables", None):
                proj = resolve_project(ws_root, str(ctx.project_root))
                if proj is not None:
                    rel = resolve_releasable_for_project(proj, ctx.releasables)
                    if rel is not None:
                        releasable_dir = get_releasable_dir(ws_root, rel.name)
        changelog_path = get_changelog_home(
            str(ctx.project_root), releasable_dir=releasable_dir,
        )

        if not os.path.exists(changelog_path):
            reporter.error("CHANGELOG.md not found")
            return reporter.found("CHANGELOG.md not found")

        entry = extract_changelog_entry(changelog_path, version)
        if entry:
            return reporter.passed(f"entry for {version}")
        reporter.warn(f"no entry for {version}")
        return reporter.found(f"no entry for {version}")

    @app.error_check("changelog-hashes")
    def check_changelog_hashes(ctx, reporter):
        """Every hash in unreleased.jsonl must resolve via git rev-parse."""
        if read_coverage_unit(ctx.config) == "changeset-file":
            return reporter.skipped("not applicable in changeset-file mode")
        from ..changelog.validate import check_hashes_resolve

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _project, entries in all_contexts:
            passed, details = check_hashes_resolve(entries)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return reporter.passed("all hashes resolve")
        for detail in all_details:
            reporter.error(detail)
        return reporter.found(f"{len(all_details)} hash(es) failed to resolve")

    @app.error_check("changelog-range")
    def check_changelog_range(ctx, reporter):
        """Every resolved hash must be in the unreleased commit range."""
        if read_coverage_unit(ctx.config) == "changeset-file":
            return reporter.skipped("not applicable in changeset-file mode")
        from ..changelog.validate import check_in_range

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, tag_glob, project, entries in all_contexts:
            passed, details = check_in_range(entries, tag_glob, project=project)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return reporter.passed("all hashes in unreleased range")
        for detail in all_details:
            reporter.error(detail)
        return reporter.found(f"{len(all_details)} hash(es) out of range")

    @app.error_check("changelog-coverage")
    def check_changelog_coverage(ctx, reporter):
        """Every unreleased commit must appear in at least one entry."""
        if read_coverage_unit(ctx.config) == "changeset-file":
            return reporter.skipped("not applicable in changeset-file mode")
        from ..changelog.validate import check_coverage

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        checked_any = False
        for _changes_dir, tag_glob, project, entries in all_contexts:
            # In implicit mode, project is a single WorkspaceProject; skip if non-releasable.
            # In explicit mode, project is a list of members (always releasable).
            if project is not None and not isinstance(project, list) and not project.is_releasable:
                continue

            checked_any = True
            passed, details = check_coverage(entries, tag_glob, project=project)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if not checked_any:
            return reporter.skipped("non-releasable project")

        if all_passed:
            return reporter.passed("all unreleased commits covered")
        # Filter out informational "skipped N ..." lines from the fail count
        fail_details = [d for d in all_details if not d.startswith("skipped ")]
        for detail in all_details:
            if detail.startswith("skipped "):
                reporter.warn(detail)
            else:
                reporter.error(detail)
        return reporter.found(f"{len(fail_details)} uncovered commit(s)")

    @app.error_check("changelog-orphans")
    def check_changelog_orphans(ctx, reporter):
        """No entry should have ALL hashes unresolvable (stale/rebased)."""
        if read_coverage_unit(ctx.config) == "changeset-file":
            return reporter.skipped("not applicable in changeset-file mode")
        from ..changelog.validate import check_no_orphans

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, tag_glob, project, entries in all_contexts:
            passed, details = check_no_orphans(entries, tag_glob, project=project)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return reporter.passed("no orphaned entries")
        for detail in all_details:
            reporter.error(detail)
        return reporter.found(f"{len(all_details)} orphaned entry(ies)")

    @app.error_check("changelog-schema")
    def check_changelog_schema(ctx, reporter):
        """Every entry must pass schema validation."""
        from ..changelog.validate import check_schema

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _project, entries in all_contexts:
            passed, details = check_schema(entries)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return reporter.passed("all entries valid")
        for detail in all_details:
            reporter.error(detail)
        return reporter.found(f"{len(all_details)} schema error(s)")

    @app.warn_check("changelog-user-facing")
    def check_changelog_user_facing(ctx, reporter):
        """At least one entry must be user-facing."""
        # In changeset-file mode, check pending files instead
        if read_coverage_unit(ctx.config) == "changeset-file":
            from ..changelog.files import get_changes_dir, get_pending_dir, read_pending_files
            changes_dir = get_changes_dir(str(ctx.project_root))
            pending_dir = get_pending_dir(changes_dir)
            entries = read_pending_files(pending_dir)
            if not entries:
                return reporter.skipped("no pending changelog files")
            has_uf = any(e.user_facing for e in entries)
            if has_uf:
                return reporter.passed("has user-facing entries")
            reporter.warn('no user-facing entries (use bump = "hotfix" for infrastructure-only releases)')
            return reporter.found('no user-facing entries (use bump = "hotfix" for infrastructure-only releases)')

        from ..changelog.validate import check_has_user_facing

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        any_failed = False
        checked_any = False
        for _changes_dir, _tag_glob, project, entries in all_contexts:
            # In implicit mode, project is a single WorkspaceProject; skip if non-releasable.
            # In explicit mode, project is a list of members (always releasable).
            if project is not None and not isinstance(project, list) and not project.is_releasable:
                continue

            checked_any = True
            passed, details = check_has_user_facing(entries)
            if not passed:
                any_failed = True
                all_details.extend(details)

        if not checked_any:
            return reporter.skipped("non-releasable project")

        if not any_failed:
            return reporter.passed("has user-facing entries")
        for detail in all_details:
            reporter.warn(detail)
        return reporter.found('no user-facing entries (use bump = "hotfix" for infrastructure-only releases)')

    @app.error_check("changelog-batch-commits")
    def check_changelog_batch_commits(ctx, reporter):
        """No entry should have more commits than max_commits_per_entry."""
        if read_coverage_unit(ctx.config) == "changeset-file":
            return reporter.skipped("not applicable in changeset-file mode")
        from ..changelog.validate import check_batch_size_commits, _get_batch_limits_config

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        batch_config = _get_batch_limits_config(ctx.config)

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _project, entries in all_contexts:
            passed, details = check_batch_size_commits(entries, batch_config, version="unreleased")
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return reporter.passed("all entries within commit batch limit")
        for detail in all_details:
            reporter.error(detail)
        return reporter.found(f"{len(all_details)} entry(ies) exceed commit limit")

    @app.error_check("changelog-batch-entries")
    def check_changelog_batch_entries(ctx, reporter):
        """No commit should appear in more entries than max_entries_per_commit."""
        if read_coverage_unit(ctx.config) == "changeset-file":
            return reporter.skipped("not applicable in changeset-file mode")
        from ..changelog.validate import (
            check_batch_size_entries,
            _get_batch_limits_config,
            _read_all_versioned_entries,
        )

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        batch_config = _get_batch_limits_config(ctx.config)

        all_details = []
        all_passed = True
        for changes_dir, _tag_glob, _project, _entries in all_contexts:
            entries_by_version = _read_all_versioned_entries(changes_dir)
            passed, details = check_batch_size_entries(entries_by_version, batch_config)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return reporter.passed("all commits within entry batch limit")
        for detail in all_details:
            reporter.error(detail)
        return reporter.found(f"{len(all_details)} commit(s) exceed entry limit")
