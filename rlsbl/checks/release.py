"""Release checks (tag: release) verifying that every released version's refs exist -- locally and on the remote, at the commit the release anchored -- and that the branch is in sync.

Checks: unpublished-refs, branch-sync.
"""

import subprocess

from ._common import _resolve_release_identity

# One remedy for every finding this module reports. `rlsbl release reconcile`
# re-pushes the tags a rewrite moved and recreates their GitHub Releases from
# safegit's rewrite journal, and it is fail-closed: a divergence the journal
# does not explain is a hard error there rather than a force-push.
_REMEDY = "Repair the release metadata with `rlsbl release reconcile`."

# Reading the whole tag namespace on both sides costs a fixed number of
# processes and exactly ONE network round trip, so neither half of this check
# is windowed: a project with 200 archived releases is probed as cheaply as one
# with two. A per-version `ls-remote` would have forced a recent-versions bound
# and left every older release unchecked.
_LOCAL_TIMEOUT = 30
_REMOTE_TIMEOUT = 60


def register_release_checks(app):
    """Register release-tag checks on *app*."""

    @app.error_check("unpublished-refs")
    def check_unpublished_refs(ctx, reporter):
        """Every released version's refs exist locally and on origin, at its anchor."""
        from ..errors import RlsblError
        from ..release_file import (
            archived_release_path,
            list_archived_versions,
            read_release_file,
        )
        from ..utils import local_tag_commits, remote_is_configured, remote_tag_commits

        resolved = _resolve_release_identity(ctx)
        if resolved is None:
            return reporter.skipped("no release target detected")
        target, ref_ctx, releases_dir = resolved

        versions = list_archived_versions(releases_dir)
        if not versions:
            return reporter.skipped("the release ledger records no release")

        root = str(ctx.project_root)
        local = local_tag_commits(cwd=root, timeout=_LOCAL_TIMEOUT)
        if not local.conclusive:
            # Fail-closed: an unreadable local namespace is not evidence that
            # the tags are fine, and reading it as such is how a broken
            # checkout would report a clean bill of health.
            reporter.error(
                f"the local tag namespace could not be read ({local.error}), so "
                f"no released version's refs could be verified. {_REMEDY}"
            )
            return reporter.found("the local tag namespace could not be read")

        has_remote = remote_is_configured(cwd=root)
        remote = None
        if has_remote:
            remote = remote_tag_commits(cwd=root, timeout=_REMOTE_TIMEOUT)
            if not remote.conclusive:
                reporter.error(
                    f"the tag namespace of origin could not be read "
                    f"({remote.error}), so no released version's refs could be "
                    f"verified against it. An unanswered probe is not an "
                    f"answer. {_REMEDY}"
                )
                return reporter.found("origin's tag namespace could not be read")

        missing_local = 0
        missing_remote = 0
        wrong_commit = 0
        unrecoverable = 0
        # Versions whose ref set could not be derived at all -- an unreadable
        # archive, or a target that could not name the refs. Counted, because
        # the terminal decision below is the ONLY thing that decides pass or
        # fail: a reported problem that increments nothing reaches `passed()`,
        # which refuses to finalize a pass over reported problems and takes the
        # whole check run down with a ValueError instead of failing this check.
        underivable = 0

        for version in versions:
            try:
                archive = read_release_file(archived_release_path(releases_dir, version))
            except (RlsblError, OSError) as exc:
                underivable += 1
                reporter.error(
                    f"{version}: its release archive could not be read ({exc}), "
                    f"so the refs it should own are unknown."
                )
                continue
            anchor = None if archive.unanchorable else archive.candidate_sha

            try:
                expected = target.expected_refs(version, ref_ctx)
            except RlsblError as exc:
                underivable += 1
                reporter.error(
                    f"{version}: the refs this version owns could not be "
                    f"derived ({exc})."
                )
                continue

            for ref in expected.tags:
                local_commit = local.commits.get(ref)
                if local_commit is None:
                    if anchor is None:
                        # The archive records this version as UNANCHORABLE:
                        # its commit is permanently unrecoverable, so there is
                        # nothing to recreate the ref at and no repair to name.
                        # Counted and surfaced rather than passed over silently,
                        # but not reported as a fixable error -- a check that
                        # can never go green is a check people stop reading.
                        unrecoverable += 1
                        continue
                    missing_local += 1
                    reporter.error(
                        f"{version}: the ref {ref} is recorded as released but "
                        f"does not exist locally. {_REMEDY}"
                    )
                    continue

                if anchor and not _same_commit(local_commit, anchor):
                    wrong_commit += 1
                    reporter.error(
                        f"{version}: the ref {ref} points at {local_commit} but "
                        f"the release anchored {anchor}. The archive is the "
                        f"record of what shipped and is read-only from the "
                        f"instant it was written, so the ref moved. {_REMEDY}"
                    )

                if remote is None:
                    continue
                remote_commit = remote.commits.get(ref)
                if remote_commit is None:
                    missing_remote += 1
                    reporter.error(
                        f"{version}: the ref {ref} exists locally but not on "
                        f"origin, so the release is not published where "
                        f"consumers resolve it. {_REMEDY}"
                    )

        if not (missing_local or missing_remote or wrong_commit or underivable):
            scope = (
                f"{len(versions)} released version(s)"
                if has_remote else
                f"{len(versions)} released version(s), locally (no origin remote)"
            )
            if unrecoverable:
                scope += (
                    f"; {unrecoverable} ref(s) absent for versions recorded "
                    f"unanchorable, which have no commit to recreate them at"
                )
            return reporter.passed(f"every ref exists for {scope}")

        summary = (
            f"{missing_local} ref(s) missing locally, "
            f"{missing_remote} missing on origin, "
            f"{wrong_commit} at the wrong commit"
        )
        if underivable:
            summary += (
                f", {underivable} release archive(s) whose ref set could not "
                f"be derived"
            )
        return reporter.found(summary)

    @app.error_check("branch-sync")
    def check_branch_sync(ctx, reporter):
        """Local branch must be in sync with origin."""
        from ..utils import get_current_branch, run

        root_str = str(ctx.project_root)
        branch = get_current_branch(cwd=root_str)
        try:
            output = run("git", ["rev-list", "--left-right", "--count",
                                  f"origin/{branch}...HEAD"], cwd=root_str)
        except subprocess.CalledProcessError:
            return reporter.skipped(f"no remote tracking for {branch}")

        parts = output.split("\t")
        if len(parts) != 2:
            reporter.error(f"unexpected rev-list output: {output}")
            return reporter.found(f"unexpected rev-list output: {output}")

        behind, ahead = int(parts[0]), int(parts[1])
        if behind == 0 and ahead == 0:
            return reporter.passed(f"up to date with origin/{branch}")
        if behind == 0 and ahead > 0:
            reporter.warn(f"{ahead} commit(s) ahead of origin/{branch}")
            return reporter.found(f"{ahead} commit(s) ahead of origin/{branch}")
        if behind > 0 and ahead == 0:
            reporter.error(f"{behind} commit(s) behind origin/{branch}")
            return reporter.found(f"{behind} commit(s) behind origin/{branch}")
        reporter.error(f"{behind} behind, {ahead} ahead of origin/{branch}")
        return reporter.found(f"{behind} behind, {ahead} ahead of origin/{branch}")


def _same_commit(a, b):
    """Do two git object names denote the same commit, allowing abbreviation?

    An anchor may be recorded abbreviated (the schema accepts 7 to 40 hex
    characters) while a resolved ref is always full, so the comparison is by
    common prefix -- the same rule the ledger applies.
    """
    n = min(len(a), len(b))
    return n > 0 and a[:n] == b[:n]
