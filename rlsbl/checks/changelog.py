"""Changelog checks (tag: changelog) validating JSONL entry schema, commit hash resolution, coverage, orphans, and batch size limits.

Checks: changelog-entry, changelog-hashes, changelog-range,
changelog-coverage, changelog-orphans, changelog-schema,
changelog-user-facing, changelog-batch-commits, changelog-batch-entries,
changelog-format-version-gate.
"""

import os

from ..release_record import releases_dir_for_changes_dir
from ._common import _resolve_version_and_tag, _get_all_changelog_contexts


def _changes_dirs_for_ctx(ctx):
    """Return the changes-dir(s) whose *.jsonl files this ctx owns.

    Derived from the same context resolution the other changelog checks use, so
    the format_version gate covers exactly the files that are validated. Returns
    an empty list when there is no changes dir.
    """
    return [changes_dir for changes_dir, _tag, _scope, _entries in _get_all_changelog_contexts(ctx)]


def _batch_limits_labels_for_ctx(ctx):
    """The config file each batch_limits setting of ``ctx.config`` came from.

    Resolves the releasable config directory the same way the check context
    loaded ``ctx.config`` (releasable base, per-package on top).
    """
    from ..config import batch_limits_labels
    from ..targets import resolve_releasable_config_dir_for_ctx

    return batch_limits_labels(
        str(ctx.project_root), resolve_releasable_config_dir_for_ctx(ctx),
    )


def _batch_limits_for_changes_dir(ctx, changes_dir):
    """``(batch_config, labels)`` governing the changelog in *changes_dir*.

    At the workspace root the batch checks walk every releasable's changelog,
    and each must meet its own releasable's limits, not the root context's.
    A releasable changelog the context was not built for is judged by the
    config its representative (first declared) member reads.
    """
    from ..changelog.validate import _get_batch_limits_config
    from ..config import batch_limits_labels, read_project_config
    from ..targets import resolve_releasable_config_dir_for_ctx
    from ..workspace import (
        get_releasable_changes_dir,
        get_releasable_dir,
        members_of,
    )

    ws_root = getattr(ctx, "workspace_root", None)
    if ws_root is not None:
        ws_root = str(ws_root)
        for rel in getattr(ctx, "releasables", None) or []:
            rel_changes = get_releasable_changes_dir(ws_root, rel.name)
            if os.path.realpath(rel_changes) != os.path.realpath(changes_dir):
                continue
            rel_dir = get_releasable_dir(ws_root, rel.name)
            own = resolve_releasable_config_dir_for_ctx(ctx)
            if own is not None and os.path.realpath(own) == os.path.realpath(rel_dir):
                break
            members = members_of(rel.name, ctx.projects)
            member_dir = (
                os.path.join(ws_root, members[0]["path"]) if members else ws_root
            )
            labels = batch_limits_labels(member_dir, rel_dir)
            config = read_project_config(member_dir, releasable_config_dir=rel_dir)
            return _get_batch_limits_config(config, labels), labels
    labels = _batch_limits_labels_for_ctx(ctx)
    return _get_batch_limits_config(ctx.config, labels), labels


def _changelog_label(changes_dir):
    """The releasable a releasable's changes directory belongs to, by name."""
    return os.path.basename(os.path.dirname(os.path.normpath(changes_dir)))


def _scope_is_releasable(scope):
    """Does any member in *scope* produce releases (and therefore a changelog)?

    A scope over a single ``releasable = false`` member has no changelog to
    validate; a releasable's member scope always has at least one.
    """
    from ..workspace import project_is_releasable

    return any(project_is_releasable(m) for m in scope.owned_members())


def register_changelog_checks(app):
    """Register changelog-tag checks on *app*."""

    @app.error_check("changelog-format-version-gate")
    def check_changelog_format_version_gate(ctx, reporter):
        """Every changelog line must carry a supported format_version.

        Scans unreleased.jsonl and every finalized x.y.z.jsonl in the changes
        dir(s), routing each line through the strictspec per-line gate. A line
        lacking (or carrying an unsupported) ``format_version`` is reported,
        naming the file and the fix. Whether a finding blocks is this check's
        option, ``rlsbl:changelog-format-version-gate``: at ``warn`` the check
        still runs and reports, at ``off`` it does not run.
        """
        import os

        from ..changelog.files import list_versioned_files
        from ..changelog.schema import parse_jsonl
        from ..errors import ChangelogError

        dirs = _changes_dirs_for_ctx(ctx)
        if not dirs:
            return reporter.skipped("no .rlsbl/changes/ directory")

        details = []
        for changes_dir in dirs:
            files = []
            unreleased = os.path.join(changes_dir, "unreleased.jsonl")
            if os.path.isfile(unreleased):
                files.append(unreleased)
            files.extend(path for _v, path in list_versioned_files(changes_dir))
            for filepath in files:
                try:
                    parse_jsonl(filepath, enforce_format_version=True)
                except ChangelogError as exc:
                    details.append(f"{filepath}: {exc}")

        if not details:
            return reporter.passed("all changelog lines carry format_version")
        for detail in details:
            reporter.error(detail)
        return reporter.found(f"{len(details)} changelog file(s) failed the format_version gate")

    @app.error_check("changelog-entry")
    def check_changelog_entry(ctx, reporter):
        """CHANGELOG.md must have an entry for the current version."""
        from ..utils import extract_changelog_entry
        from ..changelog.home import get_changelog_home
        from ..check_context import WorkspaceCheckContext
        from ..workspace import (
            get_releasable_dir,
            resolve_project,
            resolve_releasable_for_project,
        )

        version, _tag = _resolve_version_and_tag(ctx)
        if not version:
            return reporter.skipped("no version detected")

        # The canonical CHANGELOG.md location comes from the single home
        # resolver: the releasable dir when the project belongs to one,
        # project root otherwise.
        releasable_dir = None
        if isinstance(ctx, WorkspaceCheckContext) and ctx.workspace_root is not None:
            ws_root = str(ctx.workspace_root)
            if getattr(ctx, "releasables", None):
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
        from ..changelog.validate import check_hashes_resolve

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for _changes_dir, _tag_glob, _scope, entries in all_contexts:
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
        from ..changelog.validate import check_in_range

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for changes_dir, tag_glob, scope, entries in all_contexts:
            passed, details = check_in_range(
                entries, releases_dir_for_changes_dir(changes_dir), tag_glob,
                scope=scope,
            )
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
        from ..changelog.validate import check_coverage

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        # Several changelogs (the workspace root with no --releasable) report
        # side by side, so each line names the changelog it is about: one
        # commit uncovered in two releasables is two findings, not one line
        # printed twice.
        labelled = len(all_contexts) > 1
        all_details = []  # (is_informational, text)
        all_passed = True
        checked_any = False
        for changes_dir, tag_glob, scope, entries in all_contexts:
            if scope is not None and not _scope_is_releasable(scope):
                continue

            checked_any = True
            passed, details = check_coverage(
                entries, releases_dir_for_changes_dir(changes_dir), tag_glob,
                scope=scope,
            )
            if not passed:
                all_passed = False
                prefix = f"{_changelog_label(changes_dir)}: " if labelled else ""
                all_details.extend(
                    (d.startswith("skipped "), prefix + d) for d in details
                )

        if not checked_any:
            return reporter.skipped("non-releasable project")

        if all_passed:
            return reporter.passed("all unreleased commits covered")
        # Informational "skipped N ..." lines do not count as failures.
        fail_count = sum(1 for info, _d in all_details if not info)
        for info, detail in all_details:
            if info:
                reporter.warn(detail)
            else:
                reporter.error(detail)
        return reporter.found(f"{fail_count} uncovered commit(s)")

    @app.error_check("changelog-orphans")
    def check_changelog_orphans(ctx, reporter):
        """No entry should have ALL hashes unresolvable (stale/rebased)."""
        from ..changelog.validate import check_no_orphans

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for changes_dir, tag_glob, scope, entries in all_contexts:
            passed, details = check_no_orphans(
                entries, releases_dir_for_changes_dir(changes_dir), tag_glob,
                scope=scope,
            )
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
        for _changes_dir, _tag_glob, _scope, entries in all_contexts:
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
        from ..changelog.validate import check_has_user_facing

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        any_failed = False
        checked_any = False
        for _changes_dir, _tag_glob, scope, entries in all_contexts:
            if scope is not None and not _scope_is_releasable(scope):
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
        return reporter.found('no user-facing entries (use bump = "infra" for infrastructure-only releases)')

    @app.error_check("changelog-batch-commits")
    def check_changelog_batch_commits(ctx, reporter):
        """No entry should have more commits than max_commits_per_entry."""
        from ..changelog.validate import check_batch_size_commits

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for changes_dir, _tag_glob, _scope, entries in all_contexts:
            batch_config, labels = _batch_limits_for_changes_dir(ctx, changes_dir)
            passed, details = check_batch_size_commits(entries, batch_config, labels, version="unreleased")
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
        from ..changelog.validate import (
            check_batch_size_entries,
            _read_all_versioned_entries,
        )

        all_contexts = _get_all_changelog_contexts(ctx)
        if not all_contexts:
            return reporter.skipped("no .rlsbl/changes/ directory")

        all_details = []
        all_passed = True
        for changes_dir, _tag_glob, _scope, _entries in all_contexts:
            batch_config, labels = _batch_limits_for_changes_dir(ctx, changes_dir)
            entries_by_version = _read_all_versioned_entries(changes_dir)
            passed, details = check_batch_size_entries(entries_by_version, batch_config, labels)
            if not passed:
                all_passed = False
                all_details.extend(details)

        if all_passed:
            return reporter.passed("all commits within entry batch limit")
        for detail in all_details:
            reporter.error(detail)
        return reporter.found(f"{len(all_details)} commit(s) exceed entry limit")
