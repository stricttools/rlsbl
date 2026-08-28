"""Release checks (tag: release): the refs, the branch, the CI credentials, and the follow-ups a recorded conversion still owes the outside world.

Checks: unpublished-refs, branch-sync, ci-publish-secrets, old-repo-archived,
go-deprecation-published.

Every check in this module reads something OUTSIDE the working tree -- the
remote's refs, the GitHub API, the Go module proxy -- which is why they carry
the ``release`` tag rather than ``project``: the offline tags (``project``,
``changelog``, ``quality``, ``prepush``) stay answerable with no network, and a
networked check placed in one of them would make an offline run fail for a
reason that has nothing to do with the repository.

All of them are fail-closed. A probe that cannot answer is a hard error, never
a pass: "we could not ask" is not evidence that a ref is pushed, a secret
exists, a repository is archived, or a module is deprecated.
"""

import os
import subprocess

from ._common import _resolve_release_identity

# One remedy for every finding this module reports. `rlsbl release reconcile`
# re-pushes the tags a rewrite moved and writes their GitHub Release documents
# in place (creating only the ones that are absent), driven by safegit's
# rewrite journal, and it is fail-closed: a divergence the journal does not
# explain is a hard error there rather than a force-push.
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
                        f"record the release flow wrote, and rlsbl rewrites one "
                        f"only through its own documented unlock paths, so the "
                        f"ref moved. {_REMEDY}"
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

    register_networked_release_checks(app)


def _lineage_paths(ctx):
    """Every lineage record this project can reach, newest home first.

    The three homes :func:`rlsbl.lineage.get_lineage_path` resolves: a
    standalone project's own record, the workspace-scoped record, and one per
    releasable. A monorepo carries facts in all three, and a conversion
    follow-up is owed whichever record recorded it.
    """
    from ..check_context import WorkspaceCheckContext
    from ..lineage import get_lineage_path, lineage_file_exists

    paths = [get_lineage_path(str(ctx.project_root))]
    if isinstance(ctx, WorkspaceCheckContext):
        root = str(ctx.workspace_root)
        paths.append(get_lineage_path(root, workspace=True))
        from ..workspace_types import get_releasable_dir

        for releasable in ctx.releasables or []:
            paths.append(get_lineage_path(
                root, releasable_dir=get_releasable_dir(root, releasable.name),
            ))
    seen = []
    for path in paths:
        if path not in seen and lineage_file_exists(path):
            seen.append(path)
    return seen


def _read_lineage(ctx, reporter):
    """``(events, error_outcome)`` -- the events, or a finalized failure.

    A malformed record is this check's finding, not a traceback: reading a
    record FOR USE is where :func:`rlsbl.lineage.read_events` raises, and a
    check that consumes one has to report it.
    """
    from ..lineage import LineageError, read_events

    events = []
    for path in _lineage_paths(ctx):
        try:
            events.extend(read_events(path))
        except LineageError as exc:
            reporter.error(
                f"{os.path.basename(path)} could not be read ({exc}), so the "
                f"conversions it records cannot be verified."
            )
            return None, reporter.found("the lineage record could not be read")
    return events, None


def _followup_outcome(verdict, reporter, *, passed):
    """Report a :class:`rlsbl.lineage_followup.FollowupVerdict`."""
    if verdict.skip_reason is not None:
        return reporter.skipped(verdict.skip_reason)
    if verdict.ok:
        return reporter.passed("; ".join(verdict.notes[:3]) or passed)
    for problem in verdict.problems:
        reporter.error(problem)
    return reporter.found(f"{len(verdict.problems)} finding(s)")


def register_networked_release_checks(app):
    """Register the probing release-tag checks on *app*."""

    @app.error_check("ci-publish-secrets")
    def check_ci_publish_secrets(ctx, reporter):
        """Every secret the CI publish pipelines authenticate with must exist.

        Without one the publish job dies with ``ENEEDAUTH`` -- after the release
        has already tagged, pushed and created the GitHub Release. Presence
        only: the value is not retrievable through the API and rlsbl never puts
        a credential on a pipe.

        WHICH secrets are owed is each pipeline's own declaration
        (``ci_secret_names``), not a pipeline type tested by name here: npm
        declares ``NPM_TOKEN``, maven-central declares its Central Portal
        credentials and GPG signing key, hex declares ``HEX_API_KEY``, pypi
        declares none because its workflow authenticates through OIDC trusted
        publishing, and a ``local: true`` pipeline declares none because it
        authenticates from the developer's own environment.
        """
        from ..ci_secrets import evaluate_ci_secret_presence
        from ..utils import get_github_repo

        verdict = evaluate_ci_secret_presence(
            ctx.config, get_github_repo(ctx.config),
        )
        if verdict.skip_reason is not None:
            return reporter.skipped(verdict.skip_reason)
        if verdict.ok:
            return reporter.passed(
                "; ".join(verdict.notes)
                or "every CI publish credential exists"
            )
        for problem in verdict.problems:
            reporter.error(problem)
        return reporter.found(f"{len(verdict.problems)} missing CI secret(s)")

    @app.error_check("old-repo-archived")
    def check_old_repo_archived(ctx, reporter):
        """A repository this one absorbed should be archived.

        Until it is, it keeps collecting issues, pull requests and clones for
        code that now lives here. rlsbl never archives it: the finding prints
        the `gh repo archive` command instead.
        """
        from ..lineage_followup import evaluate_old_repo_archived

        events, failure = _read_lineage(ctx, reporter)
        if failure is not None:
            return failure
        if not events:
            return reporter.skipped("this repository has no lineage record")
        return _followup_outcome(
            evaluate_old_repo_archived(events), reporter,
            passed="every absorbed source repository is archived",
        )

    @app.error_check("go-deprecation-published")
    def check_go_deprecation_published(ctx, reporter):
        """A superseded Go module path should serve a deprecation notice.

        A module path rlsbl recorded as moved is still published at the old
        path, and a consumer resolving it sees nothing about the move unless
        the old repository ships `// Deprecated:` in its `go.mod`. The finding
        prints the steps; rlsbl never commits into a retired repository.
        """
        from ..lineage_followup import evaluate_go_deprecation_published

        events, failure = _read_lineage(ctx, reporter)
        if failure is not None:
            return failure
        if not events:
            return reporter.skipped("this repository has no lineage record")
        return _followup_outcome(
            evaluate_go_deprecation_published(events), reporter,
            passed="every superseded module path is deprecated",
        )


def _same_commit(a, b):
    """Do two git object names denote the same commit, allowing abbreviation?

    An anchor may be recorded abbreviated (the schema accepts 7 to 40 hex
    characters) while a resolved ref is always full, so the comparison is by
    common prefix -- the same rule the ledger applies.
    """
    n = min(len(a), len(b))
    return n > 0 and a[:n] == b[:n]
