"""Changelog checks (tag: changelog) validating JSONL entry schema, commit hash resolution, coverage, orphans, and batch size limits.

Checks: changelog-entry, changelog-hashes, changelog-range,
changelog-coverage, changelog-orphans, changelog-schema,
changelog-user-facing, changelog-batch-commits, changelog-batch-entries.
"""

import os

from strictcli import CheckResult

from ._common import _resolve_version_and_tag, _get_changelog_context, _get_all_changelog_contexts


def register_changelog_checks(app):
    """Register changelog-tag checks on *app*."""

    @app.check("changelog-entry")
    def check_changelog_entry(ctx):
        """CHANGELOG.md must have an entry for the current version."""
        from ..utils import extract_changelog_entry
        from ..check_context import WorkspaceCheckContext
        from ..workspace import (
            get_releasable_dir,
            is_explicit_mode,
            resolve_project,
            resolve_releasable_for_project,
        )

        version, _tag = _resolve_version_and_tag(ctx)
        if not version:
            return CheckResult("skip", "no version detected")

        # In explicit releasable mode, look for CHANGELOG.md in the releasable dir
        changelog_path = os.path.join(str(ctx.project_root), "CHANGELOG.md")
        if isinstance(ctx, WorkspaceCheckContext) and ctx.workspace_root is not None:
            ws_root = str(ctx.workspace_root)
            if is_explicit_mode(ws_root) and getattr(ctx, "releasables", None):
                proj = resolve_project(ws_root, str(ctx.project_root))
                if proj is not None:
                    rel = resolve_releasable_for_project(proj, ctx.releasables)
                    if rel is not None:
                        changelog_path = os.path.join(
                            get_releasable_dir(ws_root, rel.name), "CHANGELOG.md",
                        )

        if not os.path.exists(changelog_path):
            return CheckResult("warn", "CHANGELOG.md not found")

        entry = extract_changelog_entry(changelog_path, version)
        if entry:
            return CheckResult("pass", f"entry for {version}")
        return CheckResult("warn", f"no entry for {version}")

    @app.check("changelog-hashes")
    def check_changelog_hashes(ctx):
        """Every hash in unreleased.jsonl must resolve via git rev-parse."""
        from ..changelog.validate import check_hashes_resolve

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _project, entries in all_contexts:
            passed, details = check_hashes_resolve(entries)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return CheckResult("pass", "all hashes resolve")
        return CheckResult("fail", f"{len(all_details)} hash(es) failed to resolve", details=all_details)

    @app.check("changelog-range")
    def check_changelog_range(ctx):
        """Every resolved hash must be in the unreleased commit range."""
        from ..changelog.validate import check_in_range

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, tag_glob, project, entries in all_contexts:
            passed, details = check_in_range(entries, tag_glob, project=project)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return CheckResult("pass", "all hashes in unreleased range")
        return CheckResult("fail", f"{len(all_details)} hash(es) out of range", details=all_details)

    @app.check("changelog-coverage")
    def check_changelog_coverage(ctx):
        """Every unreleased commit must appear in at least one entry."""
        from ..changelog.validate import check_coverage

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

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
            return CheckResult("skip", "non-releasable project")

        if all_passed:
            return CheckResult("pass", "all unreleased commits covered")
        # Filter out informational "skipped N ..." lines from the fail count
        fail_details = [d for d in all_details if not d.startswith("skipped ")]
        return CheckResult("fail", f"{len(fail_details)} uncovered commit(s)", details=all_details)

    @app.check("changelog-orphans")
    def check_changelog_orphans(ctx):
        """No entry should have ALL hashes unresolvable (stale/rebased)."""
        from ..changelog.validate import check_no_orphans

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _project, entries in all_contexts:
            passed, details = check_no_orphans(entries)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return CheckResult("pass", "no orphaned entries")
        return CheckResult("fail", f"{len(all_details)} orphaned entry(ies)", details=all_details)

    @app.check("changelog-schema")
    def check_changelog_schema(ctx):
        """Every entry must pass schema validation."""
        from ..changelog.validate import check_schema

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _project, entries in all_contexts:
            passed, details = check_schema(entries)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return CheckResult("pass", "all entries valid")
        return CheckResult("fail", f"{len(all_details)} schema error(s)", details=all_details)

    @app.check("changelog-user-facing")
    def check_changelog_user_facing(ctx):
        """At least one entry must be user-facing."""
        from ..changelog.validate import check_has_user_facing

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

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
            return CheckResult("skip", "non-releasable project")

        if not any_failed:
            return CheckResult("pass", "has user-facing entries")
        return CheckResult("warn", "no user-facing entries", details=all_details)

    @app.check("changelog-batch-commits")
    def check_changelog_batch_commits(ctx):
        """No entry should have more commits than max_commits_per_entry."""
        from ..changelog.validate import check_batch_size_commits, _get_batch_limits_config

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

        batch_config = _get_batch_limits_config(ctx.config)

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _project, entries in all_contexts:
            passed, details = check_batch_size_commits(entries, batch_config, version="unreleased")
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return CheckResult("pass", "all entries within commit batch limit")
        return CheckResult("fail", f"{len(all_details)} entry(ies) exceed commit limit", details=all_details)

    @app.check("changelog-batch-entries")
    def check_changelog_batch_entries(ctx):
        """No commit should appear in more entries than max_entries_per_commit."""
        from ..changelog.validate import (
            check_batch_size_entries,
            _get_batch_limits_config,
            _read_all_versioned_entries,
        )

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return CheckResult("skip", "no .rlsbl/changes/ directory")

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
            return CheckResult("pass", "all commits within entry batch limit")
        return CheckResult("fail", f"{len(all_details)} commit(s) exceed entry limit", details=all_details)
