"""Release execution: version bump, commit, tag, push, GitHub Release creation, JSONL changelog finalization, and post-release hook invocation."""

import dataclasses
import fnmatch
import json
import os
import re
import shutil
import sys
import time

from ...ci_checks import RUN_ALL_REMEDY
from ...errors import RlsblError

from .release_state import (
    get_state_path,
    get_missing_steps,
    get_failed_steps,
    save_release_state,
    load_release_state,
    save_step,
    save_step_failure,
    clear_release_state,
)
from . import phase_a
from ... import effects


class ReleaseAbortError(Exception):
    """Raised when the release must abort (e.g., unexpected dirty files)."""


class RollbackClobberError(Exception):
    """Raised when rollback would destroy foreign commits or dirty files.

    This prevents ``git reset --hard`` from silently discarding work
    created by concurrent sessions sharing the same worktree.
    """


class ReleaseCIError(ReleaseAbortError):
    """Raised when CI does not go green on the pushed release candidate.

    Under main-as-candidate ordering this is a *normal* terminal state, not a
    corrupted one: the candidate commit is on the release branch, but no tag,
    no GitHub Release and no finalized changelog exist. The version is not
    burnt -- the fix lands forward on the same version and the release
    resumes.
    """


def _ci_red_message(*, version, tag, branch, candidate_sha, detail):
    """Remediation text for a red (or unreachable) CI verdict on the candidate.

    This replaces the pre-C4 guidance ("re-run CI to green on this exact
    commit"), which was unfollowable whenever the failure was in the code at
    the tagged commit -- the common case. Under main-as-candidate ordering the
    correct remedy is always the same and always available: fix forward on the
    release branch, same version, then resume.
    """
    return (
        f"CI did not pass on the release candidate for {version}.\n"
        f"  {detail}\n"
        f"  Candidate commit: {candidate_sha} (on origin/{branch})\n"
        f"\n"
        f"Nothing was tagged, released or finalized:\n"
        f"  - no {tag} tag exists (local or remote)\n"
        f"  - no GitHub Release exists\n"
        f"  - the changelog is still unreleased.jsonl -- {version} was not\n"
        f"    finalized, so there is no orphan version file to clean up\n"
        f"  - nothing reached any registry\n"
        f"\n"
        f"The version number is NOT burnt. Fix forward on {branch}:\n"
        f"  1. Fix the failure and commit on {branch}\n"
        f"     (record it with `rlsbl changelog add` as usual)\n"
        f"  2. rlsbl release resume\n"
        f"     -- re-pushes the new tip as the candidate, re-runs the CI gate,\n"
        f"        and completes the SAME version ({version}) when it is green.\n"
        f"\n"
        f"Do not start a new release at a higher version to escape a red CI, and\n"
        f"do not re-run CI on the same commit expecting a different answer: a\n"
        f"failure baked into the code fails identically every time."
    )


def _ci_not_run_message(*, version, tag, branch, candidate_sha, detail):
    """Remediation for a candidate whose CI never RAN for this project.

    Distinct from red and from timeout: CI concluded, and it concluded green
    -- for somebody else. The code at the candidate may be perfectly fine, so
    "fix the failure" is the wrong instruction. What the candidate lacks is a
    commit under this project's paths, which is what makes its CI job run at
    all, and what the publish gate will later demand evidence of.
    """
    return (
        f"CI never ran for this project on the release candidate for "
        f"{version}.\n"
        f"  {detail}\n"
        f"  Candidate commit: {candidate_sha} (on origin/{branch})\n"
        f"\n"
        f"Nothing was tagged, released or finalized:\n"
        f"  - no {tag} tag exists (local or remote)\n"
        f"  - no GitHub Release exists\n"
        f"  - the changelog is still unreleased.jsonl -- {version} was not\n"
        f"    finalized, so there is no orphan version file to clean up\n"
        f"  - nothing reached any registry\n"
        f"\n"
        f"This is NOT a CI failure and NOT a timeout: the workflow run went\n"
        f"green, but this project's own job inside it never ran, so nothing\n"
        f"was proven about the candidate. The publish gate applies the SAME\n"
        f"filter, so tagging here would create {tag} for a version that can\n"
        f"never publish.\n"
        f"\n"
        f"The version number is NOT burnt. Make the candidate contain a\n"
        f"commit this project's CI actually runs on:\n"
        f"  1. Commit a change under one of this project's paths on {branch}\n"
        f"     (its `watch` patterns feed the `filters:` block of\n"
        f"      .github/workflows/ci-router.yml -- record the commit with\n"
        f"      `rlsbl changelog add` as usual), or release this project\n"
        f"     together with the commits that already touch it.\n"
        f"  2. rlsbl release resume\n"
        f"     -- re-pushes the new tip as the candidate, re-runs the CI gate,\n"
        f"        and completes the SAME version ({version}) when this\n"
        f"        project's own CI goes green on it.\n"
        f"\n"
        f"Do not re-run CI on the same commit unchanged, expecting a different\n"
        f"answer: a paths filter that matched nothing matches nothing every\n"
        f"time. Re-running it with the filter SHORT-CIRCUITED is a different\n"
        f"thing entirely, and it is the right move when the candidate's\n"
        f"commits are honestly narrow -- see the remedy above.\n"
    )


def _ci_timeout_message(*, version, tag, branch, candidate_sha, detail):
    """Remediation text for a CI wait that ran OUT OF TIME, not out of luck.

    A timeout proves nothing about the candidate: the runs may still be in
    flight. Reporting it as a red verdict would send the operator to fix code
    that is very possibly fine, so it gets its own honest message.
    """
    return (
        f"The CI wait for {version} ran out of time before every run "
        f"concluded.\n"
        f"  {detail}\n"
        f"  Candidate commit: {candidate_sha} (on origin/{branch})\n"
        f"\n"
        f"This is NOT a CI failure: those runs may still be in progress. "
        f"Nothing was\n"
        f"proven about the candidate either way.\n"
        f"\n"
        f"Nothing was tagged, released or finalized:\n"
        f"  - no {tag} tag exists (local or remote)\n"
        f"  - no GitHub Release exists\n"
        f"  - the changelog is still unreleased.jsonl -- {version} was not\n"
        f"    finalized\n"
        f"  - nothing reached any registry\n"
        f"\n"
        f"The version number is NOT burnt. What to do:\n"
        f"  1. Check the runs: `rlsbl watch {candidate_sha[:12]}`\n"
        f"  2. When they are green: `rlsbl release resume`\n"
        f"     -- completes the SAME version ({version}).\n"
        f"  3. If they went red, fix forward on {branch} and resume; if they are\n"
        f"     merely slow, raise the budget with `--ci-timeout` or the\n"
        f"     `ci_timeout` config key.\n"
        f"\n"
        f"Do not start a new release at a higher version to escape a slow CI."
    )


# Wall-clock budget for the post-publish registry probe, as the delays BEFORE
# each attempt. A registry can take a few seconds to serve a version its
# publish API has already accepted, so a single immediate probe would report
# false absences; ~50s total is enough for npm/PyPI/the Go proxy to settle
# without turning the tail of a release into a wait. Module-level so tests can
# collapse it.
_PUBLICATION_PROBE_DELAYS = (0, 5, 15, 30)


def _probe_publication(resolved_targets, version, ctx, *, log, delays=None):
    """Probe every publishable target's registry for *version*.

    Returns ``(missing, checked)``: the registry names still not serving the
    version after the retry budget, and every name that was probeable at all.

    Targets whose ``publish_mode`` is ``none`` are skipped (nothing was meant
    to reach a registry), as are targets with no ``publication_probe``
    capability -- an unprobeable target yields no evidence either way, and
    inventing a verdict from its silence is the exact move this check exists
    to replace.

    ``delays`` defaults to :data:`_PUBLICATION_PROBE_DELAYS`, read at call time
    rather than bound as a default argument so a test that monkeypatches the
    module attribute actually collapses the budget for the wired-in call sites
    too (which pass no ``delays`` of their own).
    """
    from ...evidence_gate import EvidenceKind, RegistryProbeSource
    from ...targets import TARGETS

    if delays is None:
        delays = _PUBLICATION_PROBE_DELAYS

    probeable = []
    for rt in resolved_targets:
        if rt.publish_mode == "none":
            continue
        impl = TARGETS.get(rt.name)
        if impl is None:
            continue
        if "publication_probe" not in getattr(impl, "capabilities", frozenset()):
            continue
        probeable.append((impl, rt.path))
    if not probeable:
        return [], []

    source = RegistryProbeSource()
    checked = sorted({impl.name for impl, _ in probeable})
    pending = list(probeable)
    missing = []
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        still_pending = []
        missing = []
        for impl, path in pending:
            evidence = source.gather([impl], path, version, ctx)
            kind = evidence[0].kind if evidence else EvidenceKind.INCONCLUSIVE
            if kind == EvidenceKind.PUBLISHED:
                log(f"  {impl.name}: serving {version}")
            elif kind == EvidenceKind.INCONCLUSIVE:
                # The probe could not reach a verdict (no name, API error).
                # Reported, never converted into a pass or a failure.
                log(f"  {impl.name}: could not be probed ({evidence[0].message})")
            else:
                still_pending.append((impl, path))
                missing.append(impl.name)
        if not still_pending:
            return [], checked
        pending = still_pending
        if attempt < len(delays) - 1:
            log(
                f"  waiting for {', '.join(missing)} to serve {version} "
                f"(attempt {attempt + 1}/{len(delays)})"
            )
    return missing, checked


def _verify_publication(resolved_targets, version, tag, ctx, *, log,
                        delays=None):
    """Assert every publishable target's registry is serving *version*.

    A release verified its PROCESS -- CI green, tag pushed, publish workflow
    dispatched -- and then announced success. It never verified its OUTCOME, so
    a publish job that silently produced no artifact (a skipped matrix leg, a
    gate that refused, an upload that 4xx'd into a retry that never happened)
    ended as a green release with nothing on the registry. This is the outcome
    check: after CI has concluded, ask each registry whether the version it was
    supposed to publish is actually being served.

    Exits nonzero naming every registry that is not.

    **This runs on the ``--watch`` path only, deliberately.** The probe is
    meaningful exactly once CI has concluded, because CI is what runs the
    publish job. Under ``--no-watch`` the release returns while the publish
    workflow is still queued or running, so probing there would report every
    registry as missing the version and fail every release. That is an explicit
    mode choice, not a degradation: ``--watch`` verifies the outcome,
    ``--no-watch`` does not verify it and says so out loud
    (:func:`_announce_unverified_publication`).
    """
    log("\nVerifying publication...")
    missing, checked = _probe_publication(
        resolved_targets, version, ctx, log=log, delays=delays,
    )
    if not checked:
        log("  no probeable registry targets; nothing to verify")
        return
    if not missing:
        log(f"Publication verified on: {', '.join(checked)}")
        return
    print(
        f"\nError: {tag} is tagged and released, but "
        f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not "
        f"serving {version}.\n"
        f"CI concluded and the publish workflow ran, so this is not a CI "
        f"failure -- the artifact never reached the registry.\n"
        f"  Probed: {', '.join(checked)}\n"
        f"  Missing: {', '.join(missing)}\n"
        f"\n"
        f"The git tag and the GitHub Release exist, so nothing needs "
        f"re-releasing at a new version. Inspect the publish workflow run for "
        f"{tag}, fix the cause, and re-dispatch it with `rlsbl release "
        f"retry`. If the version must not ship at all, `rlsbl release yank "
        f"{version}`.",
        file=sys.stderr,
    )
    sys.exit(1)


def _verify_publication_members(specs, *, log, delays=None):
    """Batch form of :func:`_verify_publication`: one verdict per member.

    *specs* is an iterable of ``(label, resolved_targets, version, tag, ctx)``
    -- one entry per batch member, carrying that member's OWN resolved targets
    and its OWN new version. A batch publishes many packages from one candidate
    commit, and each lands on its own registry under its own version, so the
    outcome question is per member; there is no batch-wide "did it publish".

    Every member is probed before anything is decided, so one missing artifact
    never hides another: a batch that half-published is reported whole. Exits
    nonzero naming every (member, registry) pair still not serving its version.

    Like :func:`_verify_publication`, this belongs on the ``--watch`` path
    only -- see that function's docstring for why.
    """
    specs = list(specs)
    if not specs:
        return
    log("\nVerifying publication...")
    failures = []
    verified = []
    for label, resolved_targets, version, tag, ctx in specs:
        missing, checked = _probe_publication(
            resolved_targets, version, ctx,
            log=lambda line, _l=label: log(f"  [{_l}] {line.strip()}"),
            delays=delays,
        )
        if not checked:
            log(f"  {label}: no probeable registry targets; nothing to verify")
            continue
        if missing:
            failures.append((label, version, tag, missing, checked))
        else:
            verified.append(f"{label} {version} ({', '.join(checked)})")
    if verified:
        log("Publication verified: " + "; ".join(verified))
    if not failures:
        return
    lines = [
        "\nError: the batch is tagged and released, but "
        f"{len(failures)} member(s) never reached their registries.",
        "CI concluded and the publish workflow ran, so this is not a CI "
        "failure -- the artifacts never reached the registries.",
    ]
    for label, version, tag, missing, checked in failures:
        lines.append(
            f"  {label} {version} ({tag}): missing on {', '.join(missing)} "
            f"(probed {', '.join(checked)})"
        )
    lines.append(
        "\nEvery tag and GitHub Release above exists, so nothing needs "
        "re-releasing at a new version. Inspect each named member's publish "
        "workflow run, fix the cause, and re-dispatch it with `rlsbl release "
        "retry` from that member's directory. If a version must not ship at "
        "all, `rlsbl release yank <version>` there."
    )
    print("\n".join(lines), file=sys.stderr)
    sys.exit(1)


def _announce_unverified_publication(sha, log):
    """Say out loud that ``--no-watch`` left the publish outcome unverified.

    The registry probe runs only after the CI wait (see
    :func:`_verify_publication`), so a ``--no-watch`` release ends with the
    publish workflow still in flight and nothing having asked the registry
    whether the artifact arrived. That is a legitimate mode -- but it must not
    look like the verified one, so the difference is stated rather than left to
    the absence of a message.

    Goes to stderr so ``--quiet`` cannot swallow it, and names the command that
    resumes the verification the run skipped.
    """
    print(
        f"\nNOTICE: publish outcome NOT verified (--no-watch).\n"
        f"  The tag is pushed and the publish workflow will run in CI, but "
        f"this run did not wait for it, so nothing here establishes that the "
        f"artifact reached its registry. The post-publish registry probe runs "
        f"on the --watch path only: without the CI wait it would probe a "
        f"version the publish job has not attempted yet.\n"
        f"  Verify with: rlsbl watch {sha}\n"
        f"  That is CI's verdict. A green run means the publish job concluded; "
        f"confirm the version is actually being served before treating this "
        f"release as shipped.",
        file=sys.stderr,
    )
    log(f"Watch CI: rlsbl watch {sha}")


def _empty_candidate_window_message(*, version, tag, branch, candidate_sha,
                                    base_sha, patterns, changed, pushing):
    """Remediation for a candidate whose push cannot trigger this project's CI.

    Same terminal state as :func:`_ci_not_run_message` and the same remedy --
    but reached BEFORE the push and before the CI wait, from the diff alone,
    so the operator learns it in a second instead of after a full CI cycle.
    """
    listed = "\n".join(f"    {p}" for p in patterns) or "    (none)"
    seen = "\n".join(f"    {p}" for p in sorted(changed)[:10]) or "    (nothing)"
    if len(changed) > 10:
        seen += f"\n    ... and {len(changed) - 10} more"
    window = (
        f"the push about to happen ({base_sha[:12]}..{candidate_sha[:12]})"
        if pushing else
        f"the push that already published this commit "
        f"({base_sha[:12]}..{candidate_sha[:12]})"
    )
    return (
        f"the release candidate for {version} cannot trigger this project's "
        f"CI, so its gate could never pass.\n"
        f"  Candidate commit: {candidate_sha}\n"
        f"  Diff window: {window}\n"
        f"\n"
        f"The generated CI router filters each project's job by the paths a "
        f"push touched, and nothing in that window matches this project's "
        f"filters:\n"
        f"  Filters:\n{listed}\n"
        f"  Changed in the window:\n{seen}\n"
        f"\n"
        f"Its CI job would conclude `skipped`, and the publish gate refuses a "
        f"skipped check (correctly -- it proves nothing about the commit), so "
        f"{tag} would exist for a version that can never publish. Refused here "
        f"rather than after a full CI wait that can only end one way.\n"
        f"\n"
        f"Nothing was pushed, tagged, released or finalized -- the version "
        f"number is NOT burnt. Make the candidate contain a commit this "
        f"project's CI runs on:\n"
        f"  1. Commit a change under one of the paths above on {branch}\n"
        f"     (they are the project's `path` and `watch` patterns, which feed\n"
        f"      the `filters:` block of .github/workflows/ci-router.yml --\n"
        f"      record the commit with `rlsbl changelog add` as usual), or\n"
        f"     release this project together with the commits that touch it.\n"
        f"  2. rlsbl release resume\n"
        f"     -- re-pushes the new tip as the candidate and completes the SAME\n"
        f"        version ({version}).\n"
        f"\n"
        f"{RUN_ALL_REMEDY}"
    )


def _router_pattern_matches(path, pattern):
    """Does repo-relative *path* match one dorny/paths-filter pattern?

    The router emits two shapes: a directory globstar (``packages/core/**``,
    from the project's ``path``) and arbitrary globs (from ``watch``, plus the
    releasable's CHANGELOG artifact). The globstar is a prefix test -- picomatch
    matches direct children as well as nested ones -- and everything else goes
    through ``fnmatch``, the same approximation ``file_matches_project`` uses
    for watch globs.
    """
    if pattern.endswith("/**"):
        prefix = pattern[: -len("/**")].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, pattern)


def _release_router_patterns(monorepo_root, monorepo_name, releasable_name):
    """Router filter patterns for every project the release's tag publishes.

    The same set :func:`rlsbl.ci_checks.release_check_filters` builds for the
    CI gate: the releasing project, plus every member of its releasable in
    explicit mode. One tag publishes all of them, so every one of their CI
    jobs has to have run on the candidate.
    """
    from ...workspace import load_releasables, load_workspace, members_of
    from ..monorepo.sync import router_filter_patterns

    projects = load_workspace(str(monorepo_root))
    releasables = load_releasables(str(monorepo_root), projects)
    wanted = {monorepo_name}
    if releasable_name:
        wanted |= {m["name"] for m in members_of(releasable_name, projects)}

    patterns = []
    for project in projects:
        if project["name"] in wanted:
            patterns.extend(router_filter_patterns(project, releasables))
    return patterns


def _git_read(args, *, cwd):
    """Run a read-only git command, or return None when it cannot answer.

    Goes through ``effects.run`` rather than the release flow's late-bound
    ``run`` for the same reason :func:`head_sha` does: this is guard
    bookkeeping, and it must never consume a mock side effect or shift a call
    sequence in tests that stub the release's git calls.

    ``None`` means "git declined" -- typically a SHA the local clone does not
    have (a remote head that was never fetched). That is not evidence about
    the window either way, so the caller falls through to the CI gate, which
    remains the authority. A missing git binary or an internal bug raises.
    """
    result = effects.run(
        ["git"] + list(args), capture_output=True, text=True, cwd=cwd,
    )
    if effects.unsettled(result):
        # A preview past its first recorded mutation. The framework answers
        # observes with a stale carrier, which is the same "git declined" case
        # as any other: no evidence, and the caller falls through.
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _diff_names(base_sha, head_sha_, *, cwd):
    """Repo-relative paths changed between two commits, or None if unknowable."""
    out = _git_read(["--no-optional-locks", "diff", "--name-only", f"{base_sha}..{head_sha_}"], cwd=cwd)
    if out is None:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def _widened_window_base(state_path, *, cwd):
    """The parent of the earliest commit this release created, or None.

    Used when the candidate is ALREADY on the remote: no push is about to
    happen, so the CI run that will be examined is the one an earlier push
    triggered, and its own before-SHA is not knowable locally. The release's
    own commit trail is the best statement of what that push carried, so the
    window widens to start just before the version-bump commit.
    """
    state = load_release_state(state_path) or {}
    trail = [sha for sha in (state.get("release_commits") or []) if sha]
    for sha in trail:
        parent = _git_read(["rev-parse", f"{sha}^"], cwd=cwd)
        if parent:
            return parent.strip()
    return None


# Written onto the release state when the guard determines that this
# candidate's push window cannot trigger the project's CI and the release owes
# a ``run_all`` dispatch once the commit is on the remote. Names the SHA the
# dispatch must correlate to, so a dispatch is never made for a candidate other
# than the one the guard judged, and a crash between the push and the dispatch
# is repaired by a resume rather than silently skipped.
RUN_ALL_DISPATCH_KEY = "run_all_dispatch_for"


def _has_published_candidate(state_path):
    """Did an earlier attempt already push a candidate for this release?

    ``BRANCH_PUSHED`` is the only marker that says so, and it is the whole
    discriminator between the two empty-window shapes: a fresh release whose
    own version-bump commit somehow misses every filter (a configuration
    defect, refused), and a resume whose fix-forward is honestly narrow (the
    commit is owed a dispatch, not a refusal). The state's ``candidate_sha``
    cannot serve: the single-release path rewrites it with the NEW tip before
    the guard runs.
    """
    state = load_release_state(state_path) or {}
    return "BRANCH_PUSHED" in (state.get("completed_steps") or [])


def _owe_run_all_dispatch(state_path, candidate_sha):
    """Record that *candidate_sha* needs a ``run_all`` dispatch after its push."""
    state = load_release_state(state_path) or {}
    state[RUN_ALL_DISPATCH_KEY] = candidate_sha
    save_release_state(state_path, state)


def run_all_dispatch_owed(state_path, candidate_sha):
    """Is a ``run_all`` dispatch owed for exactly *candidate_sha*?"""
    state = load_release_state(state_path) or {}
    return bool(candidate_sha) and state.get(RUN_ALL_DISPATCH_KEY) == candidate_sha


def clear_run_all_dispatch(state_path):
    """Forget the owed dispatch: it has been made and correlated."""
    state = load_release_state(state_path) or {}
    if state.pop(RUN_ALL_DISPATCH_KEY, None) is not None:
        save_release_state(state_path, state)


def dispatch_owed_run_all(state_path, *, candidate_sha, branch, config=None,
                          log=None):
    """Make the owed ``run_all`` dispatch for *candidate_sha*, once.

    Called AFTER the candidate is on the remote -- the dispatch resolves a ref,
    so the commit has to be there for the run to be about it at all. That
    ordering is the whole point of the fix: the guard used to refuse before the
    push, which made its own prescribed remedy unreachable.
    """
    from ..watch import dispatch_run_all

    if not run_all_dispatch_owed(state_path, candidate_sha):
        return None
    entry = dispatch_run_all(branch, candidate_sha, config=config, log=log)
    clear_run_all_dispatch(state_path)
    return entry


def _guard_empty_candidate_window(*, candidate_sha, remote_head, needs_push,
                                  state_path, monorepo_root, monorepo_name,
                                  releasable_name, version, tag, branch,
                                  cwd, log):
    """Judge a candidate whose diff window cannot trigger this project's CI.

    The generated monorepo router gates each project's CI job on a
    dorny/paths-filter over the paths a push touched, computed against the
    push's own before-SHA. When the window matches none of a project's
    patterns its job concludes ``skipped``, the publish gate refuses the
    skipped check, and the release deadlocks -- after a full CI wait, which is
    the expensive part. The most common way to land there is a resume: the
    candidate was already pushed, the fix commit touches somebody else's
    paths, and the new window no longer contains the version bump at all.

    Two shapes, two answers:

    - **A resume owed a push, whose candidate was already published once**
      (``BRANCH_PUSHED`` recorded). The fix-forward is honestly narrow and
      widening it would be a lie in the history, so the release PUSHES the
      candidate and dispatches the router itself with ``run_all=true``, then
      gates on the dispatched run. Refusing here instead deadlocked the
      remedy: the dispatch resolves a ref, so it needs the very commit the
      refusal was withholding. The dispatch is recorded as owed on the state
      (:data:`RUN_ALL_DISPATCH_KEY`) and made by the caller after the push.
    - **Anything else** -- a fresh release whose own version-bump commit
      matches none of its filters -- is a configuration defect, and stays a
      hard error. Nothing is pushed, tagged or finalized, the state stays
      resumable, and the version is not burnt.

    A repository with no generated router on disk has nothing to dispatch, so
    the refusal stands there too.

    Only monorepo projects have a router, so a standalone repository (whose CI
    runs on every push) is not guarded. Neither is a branch with no remote
    head -- there is no before-SHA, hence no window to reason about.
    """
    if not (monorepo_root and monorepo_name):
        return None
    if not remote_head:
        return None

    patterns = _release_router_patterns(
        monorepo_root, monorepo_name, releasable_name,
    )
    if not patterns:
        return None

    base_sha = remote_head
    if not needs_push:
        # The candidate is already the remote head: the run that will be
        # examined came from the push that put it there.
        base_sha = _widened_window_base(state_path, cwd=cwd) or remote_head

    changed = _diff_names(base_sha, candidate_sha, cwd=cwd)
    if changed is None:
        # git declined the range (typically a remote head this clone never
        # fetched). Not evidence of an empty window -- but say so, because a
        # guard that quietly turns itself off is the shape this whole class of
        # bug hides in. The CI gate below remains the authority.
        print(
            f"rlsbl: could not compute the candidate's diff window "
            f"({base_sha[:12]}..{candidate_sha[:12]}); the router-filter "
            f"pre-check is skipped and the CI gate decides.",
            file=sys.stderr,
        )
        return None
    if any(
        _router_pattern_matches(path, pattern)
        for path in changed for pattern in patterns
    ):
        return None

    from ..watch import router_workflow_path

    if needs_push and _has_published_candidate(state_path) and (
        router_workflow_path(monorepo_root)
    ):
        _owe_run_all_dispatch(state_path, candidate_sha)
        log(
            f"The candidate's push window cannot trigger {monorepo_name}'s "
            f"router job, and its candidate was already published once: "
            f"pushing {candidate_sha[:12]} and dispatching the router with "
            f"run_all=true instead of refusing (nothing is waived -- every "
            f"member's real CI jobs run on this exact commit)"
        )
        return RUN_ALL_DISPATCH_KEY

    detail = _empty_candidate_window_message(
        version=version, tag=tag, branch=branch, candidate_sha=candidate_sha,
        base_sha=base_sha, patterns=patterns, changed=changed,
        pushing=needs_push,
    )
    save_step_failure(state_path, "CI_VERIFIED", "empty candidate window")
    raise ReleaseCIError(detail)


# The publish gate's only precise statement of which commit CI must be green
# on. Matched on its own line so a reconcile replaces rather than duplicates.
_CI_SHA_MARKER_RE = re.compile(r"^<!-- rlsbl-ci-sha: [0-9a-f]{40} -->\n?", re.M)


def _marker_reconcile_failure(tag, marker, what, exc):
    """The message for a CI-SHA marker that could not be read or written."""
    return (
        f"could not {what} the CI-SHA marker on the existing '{tag}' GitHub "
        f"Release ({exc}).\n"
        f"  Intended marker: {marker}\n"
        f"\n"
        f"The marker is the publish gate's only precise statement of which "
        f"commit CI proved green. Without it the gate falls back to "
        f"$GITHUB_SHA -- whatever commit the workflow happens to observe -- so "
        f"continuing would publish under a verdict nobody established. This is "
        f"refused rather than warned about.\n"
        f"\n"
        f"The release state has been preserved: fix the cause (gh auth, "
        f"network, release permissions) and run `rlsbl release resume` to "
        f"re-attempt the reconcile and the steps after it."
    )


class CiShaMarkerError(RlsblError):
    """Raised when the CI-SHA marker cannot be reconciled onto a Release.

    Fail-closed by design: a release whose gate marker is unknown must not
    proceed to the steps that act on the published Release.
    """


def _reconcile_ci_sha_marker(tag, marker, notes_file, *, config, log):
    """Ensure an ALREADY-EXISTING GitHub Release carries the CI-SHA marker.

    The marker used to be written only on the creation path, so a Release that
    pre-existed the notes write -- a resumed release, or one created out of
    band -- shipped without it and the publish gate fell back to
    ``$GITHUB_SHA``, gating on whatever commit the workflow happened to see.
    The marker is now written unconditionally: created with the Release, or
    edited into the existing body here.

    Idempotent: a body already carrying this exact marker is left untouched; a
    body carrying a DIFFERENT marker has it replaced, never duplicated.

    A read or write failure raises :class:`CiShaMarkerError`. It used to print
    a warning and return ``False`` -- a value the caller discarded -- so a
    release whose gate marker was missing or stale completed and exited 0. The
    caller turns the raise into a recorded step failure plus a nonzero,
    resumable exit, and every step after this one is skipped: the gate reading
    the marker is what decides whether the tag may publish at all, so acting
    further on a Release whose marker is unknown is exactly the move to refuse.
    """
    # Late-bound through the package namespace, like the rest of this module,
    # so mock.patch("rlsbl.commands.release.run_gh") is honored at call time.
    from . import run_gh

    try:
        body = run_gh(
            ["release", "view", tag, "--json", "body", "-q", ".body"],
            config=config,
        )
    except Exception as exc:
        raise CiShaMarkerError(
            _marker_reconcile_failure(tag, marker, "read", exc)
        ) from exc

    if marker in body:
        log(f"CI-SHA marker already present on {tag}")
        return True

    stripped = _CI_SHA_MARKER_RE.sub("", body).rstrip("\n")
    new_body = f"{stripped}\n\n{marker}\n"
    tmp = notes_file + ".reconcile"
    try:
        with effects.open_write(tmp, "w", encoding="utf-8") as f:
            f.write(new_body)
        run_gh(["release", "edit", tag, "--notes-file", tmp], config=config)
    except Exception as exc:
        raise CiShaMarkerError(
            _marker_reconcile_failure(tag, marker, "write", exc)
        ) from exc
    finally:
        if os.path.exists(tmp):
            effects.remove(tmp)
    log(f"Reconciled CI-SHA marker onto existing release {tag}")
    return True


def _track_release_commit(state_path, sha=None, cwd=None):
    """Record a release commit SHA in the state file.

    Called immediately after each ``commit_files()`` /
    ``commit_files_if_changed()`` invocation so the rollback guard can
    distinguish release-owned commits from foreign ones.

    Best-effort: failures are silently ignored. When tracking fails
    (e.g., in test environments without a real git repo), the rollback
    guard treats all commits as foreign and refuses rollback -- the
    safe default.

    If ``sha`` is not provided, reads HEAD via ``effects.run`` directly
    (bypasses the mock-patched ``run`` function used by the release
    flow, avoiding mock side-effect exhaustion in tests).
    """
    try:
        if sha is None:
            result = effects.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
                cwd=cwd,
            )
            sha = result.stdout.strip()
        else:
            sha = sha.strip()
        state = load_release_state(state_path)
        if state is None:
            state = {}
        commits = state.setdefault("release_commits", [])
        if sha not in commits:
            commits.append(sha)
        save_release_state(state_path, state)
    except Exception:
        pass  # Best-effort: never mask the original release error


def _guard_rollback(pre_release_sha, state_path, cwd=None):
    """Refuse rollback if foreign commits exist between pre_release_sha and HEAD.

    Compares commits between ``pre_release_sha`` and HEAD against the
    ``release_commits`` list persisted in the state file.  Any commit
    not in ``release_commits`` is a foreign commit (from a concurrent
    session).

    Dirty files (uncommitted modifications) are NOT checked because the
    release flow itself writes version-bump and changelog files before
    committing them -- those dirty files are the expected rollback
    target, not concurrent work.  Untracked files survive
    ``git reset --hard`` anyway.

    Uses ``effects.run`` directly (not the mock-patched ``run`` function)
    to avoid consuming mock side-effect entries in tests.

    Raises :class:`RollbackClobberError` with details and manual
    recovery instructions when rollback is unsafe.
    """
    state = load_release_state(state_path)
    release_commits = set((state or {}).get("release_commits", []))

    # Find all commits between pre_release_sha and HEAD
    try:
        result = effects.run(
            ["git", "rev-list", f"{pre_release_sha.strip()}..HEAD"],
            capture_output=True, text=True, check=True,
            cwd=cwd,
        )
        rev_list_output = result.stdout.strip()
    except Exception:
        # If rev-list fails (e.g. pre_release_sha is invalid), allow
        # the rollback -- the guard is best-effort, and blocking here
        # would leave the release in a worse state.
        return

    if rev_list_output:
        all_commits = [c.strip() for c in rev_list_output.splitlines() if c.strip()]
    else:
        all_commits = []

    foreign_commits = [c for c in all_commits if c not in release_commits]

    if not foreign_commits:
        return  # Safe to roll back

    parts = [
        "Rollback aborted: git reset --hard would destroy work from "
        "concurrent sessions.",
        f"\nForeign commits (not created by this release):",
    ]
    for fc in foreign_commits:
        parts.append(f"  {fc}")
    parts.append(
        "\nManual recovery:"
        f"\n  1. Inspect the commits above"
        f"\n  2. If safe, run: git reset --hard {pre_release_sha.strip()[:10]}"
        f"\n  3. Otherwise, cherry-pick or stash foreign work first"
    )
    raise RollbackClobberError("\n".join(parts))


def _is_push_timeout_exc(exc):
    """True when a push failure was a timeout.

    Both the candidate/branch push (via ``push_if_needed``) and the tag push
    surface timeouts as :class:`GitError` whose message contains "timed out"; a
    raw :class:`subprocess.TimeoutExpired` counts too (belt-and-braces in case
    conversion is bypassed).
    """
    import subprocess as _sp

    from ...errors import GitError

    if isinstance(exc, _sp.TimeoutExpired):
        return True
    return isinstance(exc, GitError) and "timed out" in str(exc).lower()


def _is_resumable_failure(exc, branch_pushed, candidate_push_attempted, completed):
    """Decide whether a mutating-phase failure must SKIP rollback.

    Rollback (``git reset --hard`` to the pre-release commit) is only safe
    while nothing has reached the remote. Three states forbid it:

    - the candidate push succeeded (``branch_pushed``),
    - a prior run already recorded ``BRANCH_PUSHED``,
    - the candidate push was attempted and TIMED OUT -- a timed-out push may
      still have landed, so resetting would diverge from published history.

    A non-timeout candidate-push failure (rejected ref, auth error) proves
    nothing landed, and still rolls back.
    """
    if branch_pushed or "BRANCH_PUSHED" in completed:
        return True
    return candidate_push_attempted and _is_push_timeout_exc(exc)


def head_sha(cwd=None):
    """Return HEAD's SHA, or None when it cannot be resolved.

    Uses ``effects.run`` directly rather than the release flow's ``run``: this
    is bookkeeping for the drift guard, and it must never consume a mock side
    effect (or shift a call sequence) in tests that stub the release's git
    calls. Same rationale as :func:`_track_release_commit`.
    """
    try:
        result = effects.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, cwd=cwd,
        )
    except Exception:
        return None
    if effects.unsettled(result):
        # A preview past its first recorded mutation: the framework answers
        # observes with a stale carrier, and reading a field off one truncates
        # the preview (deliberately -- the carrier derives from BaseException
        # so no ``except Exception`` can swallow it). "Cannot be resolved" is
        # the honest answer, and it is the one this function already promises.
        return None
    return result.stdout.strip() or None


class ForeignCommitError(RlsblError):
    """Raised when commits outside the release's own trail rode into it.

    The forward twin of :class:`RollbackClobberError`: that guard refuses to
    DESTROY a concurrent session's commits, this one refuses to SHIP them.
    """


class UnverifiedCandidateError(RlsblError):
    """Raised when the CI-verified commit a resume must tag cannot be proven.

    A resume that skips the candidate push and the CI gate does so because a
    prior attempt already recorded ``CI_VERIFIED``.  The tag it then creates is
    stamped "(CI-verified)", and the publish gate believes that claim, so the
    SHA it lands on must be the commit CI actually judged.  When the recorded
    candidate is missing, unresolvable, or not in the current history, the
    claim cannot be made honestly and the release stops -- it never falls back
    to HEAD, which is exactly how an untested commit once got tagged, released
    and handed to the publish gate.
    """


def guard_foreign_commits(pin_sha, trail, cwd=None, *, phase):
    """Refuse to continue if commits in ``pin_sha..HEAD`` are not in *trail*.

    A release pins HEAD when it starts and records every commit it creates in
    a trail. Anything in the pin range that is not in the trail arrived from
    somewhere else -- a concurrent session sharing the worktree, an editor's
    auto-commit, a hook. Releasing it would ship unreviewed work under this
    version's changelog, and the range is recomputed at run time, so a ride-in
    between two attempts used to join the release silently.

    Fail-closed and by name: the error lists every foreign SHA with its
    subject, so the operator can decide whether to include the work (record it
    in the changelog and re-run) or move it aside. Nothing is rolled back.

    ``phase`` names the checkpoint in the error text (entry / candidate push /
    CI gate / final push). Uses ``effects.run`` directly, not the mock-patched
    ``run``, so the guard is never starved of a mock side effect in tests.

    The batch orchestrator uses this directly with a workspace-level pin and
    the union of its members' trails; :func:`_guard_foreign_commits` is the
    single-release wrapper that reads the trail out of a state file.
    """
    if not pin_sha:
        return

    trail = set(trail or ())

    try:
        result = effects.run(
            ["git", "rev-list", f"{pin_sha.strip()}..HEAD"],
            capture_output=True, text=True, check=True, cwd=cwd,
        )
    except Exception:
        # An unresolvable pin cannot prove drift either way; the rollback
        # guard takes the same stance on an unusable range.
        return
    if effects.unsettled(result):
        # A preview, past its first recorded mutation: the framework answers
        # observes with a stale carrier rather than a fact. Same stance as an
        # unresolvable range -- no evidence is not evidence of drift -- and
        # there is nothing to protect anyway, since a preview creates no commit
        # a ride-in could be confused with.
        return

    commits = [c.strip() for c in result.stdout.splitlines() if c.strip()]
    foreign = [c for c in commits if c not in trail]
    if not foreign:
        return

    lines = [
        f"Release aborted at the {phase}: commits that this release did not "
        f"create appeared on the branch after it started.",
        "",
        "Foreign commits (not part of this release):",
    ]
    for sha in foreign:
        try:
            subject = effects.run(
                ["git", "log", "-1", "--format=%s", sha],
                capture_output=True, text=True, check=True, cwd=cwd,
            ).stdout.strip()
        except Exception:
            subject = "(subject unavailable)"
        lines.append(f"  {sha[:12]}  {subject}")
    lines.extend([
        "",
        f"The release range is computed from the branch at run time, so these "
        f"would ship under this version without ever being reviewed as part "
        f"of it. Pinned at {pin_sha.strip()[:12]}.",
        "",
        "Resolve one way or the other, then re-run:",
        "  - to include them: record them with `rlsbl changelog add` and start "
        "a fresh release",
        "  - to exclude them: move them off this branch (commit them on a "
        "branch of their own) first",
    ])
    raise ForeignCommitError("\n".join(lines))


def require_recorded_candidate(state_path, cwd=None, *, version):
    """The CI-verified commit this release is sealed to, or a hard error.

    Called only where the executor SKIPS the candidate push and the CI gate
    because an earlier attempt already recorded ``CI_VERIFIED``.  The tag that
    follows is stamped "(CI-verified)" and the publish gate trusts that claim,
    so the SHA must be the commit CI actually judged.

    Three ways the claim can fail, all hard errors and none of them a fallback
    to HEAD:

    * no ``candidate_sha`` in the state file (the state was written by a
      version that did not record it, or was hand-edited),
    * a ``candidate_sha`` this repository cannot resolve (history rewritten
      under the release),
    * a ``candidate_sha`` that is not an ancestor of HEAD, which means the
      branch no longer contains the verified commit at all.

    Falling back to HEAD here is precisely how a commit with zero CI runs was
    tagged, released, and refused by the publish gate.
    """
    state = load_release_state(state_path) or {}
    recorded = (state.get("candidate_sha") or "").strip()
    remedy = (
        f"\n\nThe release cannot honestly claim CI verification for "
        f"{version}. Either roll back with `rlsbl release undo` and release "
        f"again, or push the intended commit and let a fresh "
        f"`rlsbl release run` gate it."
    )
    if not recorded:
        raise UnverifiedCandidateError(
            f"the release state records CI_VERIFIED but carries no "
            f"candidate_sha, so the commit CI verified is unknown."
            + remedy
        )
    try:
        resolved = effects.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{recorded}^{{commit}}"],
            capture_output=True, text=True, check=True, cwd=cwd,
        ).stdout.strip()
    except Exception:
        resolved = ""
    if not resolved:
        raise UnverifiedCandidateError(
            f"the CI-verified candidate recorded for {version} "
            f"({recorded[:12]}) does not resolve in this repository."
            + remedy
        )
    ancestry = effects.run(
        ["git", "merge-base", "--is-ancestor", resolved, "HEAD"],
        capture_output=True, text=True, cwd=cwd,
    )
    if ancestry.returncode != 0:
        raise UnverifiedCandidateError(
            f"the CI-verified candidate recorded for {version} "
            f"({resolved[:12]}) is not an ancestor of HEAD, so the current "
            f"branch does not contain the commit CI verified."
            + remedy
        )
    return resolved


def _guard_foreign_commits(pin_sha, state_path, cwd=None, *, phase):
    """Single-release wrapper: the trail comes from the state file."""
    state = load_release_state(state_path)
    guard_foreign_commits(
        pin_sha,
        (state or {}).get("release_commits", []),
        cwd=cwd,
        phase=phase,
    )


def _changelog_files_for_commit(project_dir, git_root, *, releasable_cfg_dir,
                                monorepo_root):
    """The generated CHANGELOG.md paths a release commits, git-root-relative.

    One derivation, two callers: the Phase-A plan builder (for the release
    commit) and the changelog-finalization step (for the finalize commit).
    """
    from ...changelog.home import get_changelog_home, get_workspace_changelog_path

    files = []
    canonical = get_changelog_home(project_dir, releasable_dir=releasable_cfg_dir)
    if os.path.exists(canonical):
        files.append(_rel_to_git_root(canonical, git_root))
    if releasable_cfg_dir and monorepo_root:
        root_changelog = get_workspace_changelog_path(str(monorepo_root))
        if os.path.exists(root_changelog):
            files.append(_rel_to_git_root(root_changelog, git_root))
    return files


def _bump_selfdoc_version_content(project_dir, new_version):
    """The selfdoc.json content a version bump would produce, or None.

    Pure: reads and derives, writes nothing.  The Phase-A plan builder calls
    this so the write itself becomes a data-only plan step (a path and its
    finished bytes) that the executor issues without re-deriving anything.
    """
    config_path = os.path.join(project_dir, "selfdoc.json")
    if not os.path.exists(config_path):
        return None

    with open(config_path, "r", encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    data["version"] = new_version
    versions = data.get("versions")
    if versions and isinstance(versions, list):
        versions[-1]["version"] = new_version

    # Detect indent from existing file
    indent = 2
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = len(line) - len(stripped)
            break

    return json.dumps(data, indent=indent, ensure_ascii=False) + "\n"


def _git_toplevel(cwd=None):
    """The git work-tree root for *cwd*.

    Asks git first (mockable through the release flow's ``run``, which is what
    tests stub). When git cannot answer -- a preview past its first recorded
    mutation replies with a stale carrier -- the answer is derived from the
    filesystem instead, by walking up for a ``.git`` entry. That walk is pure:
    no subprocess, no observe, and correct for worktrees too (whose ``.git`` is
    a file rather than a directory).
    """
    from . import run

    try:
        out = run("git", ["rev-parse", "--show-toplevel"], cwd=cwd)
        if not effects.unsettled(out) and out.strip():
            return out.strip()
    except Exception:
        pass
    current = os.path.realpath(cwd or ".")
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent


def _rel_to_git_root(path, git_root):
    """Normalize path; make relative to git root if absolute."""
    n = os.path.normpath(path)
    if os.path.isabs(n):
        return os.path.relpath(n, git_root)
    return n


def resolve_target_paths(project_dir=".", releasable_config_dir=None):
    """Build a dict mapping target names to their resolved paths.

    Resolution goes through :func:`rlsbl.member_context.resolve_member_context`,
    which reads the merged config "targets" (supporting both plain strings and
    dicts with "name"/"path", with releasable-level inheritance when
    ``releasable_config_dir`` is given) and falls back to auto-detection.

    Returns dict[str, str] mapping target name -> resolved directory path.
    """
    from ...member_context import resolve_member_context

    member = resolve_member_context(
        project_dir, releasable_config_dir=releasable_config_dir,
    )
    return member.target_paths


def resolve_release_targets(primary, flags, project_dir=".", *, config,
                            releasable_config_dir=None):
    """Compute the effective set of secondary targets for this release.

    Reads the baseline from config "release_targets" list.
    If absent, falls back to auto-detect (all targets that detect("."),
    with releasable-level inheritance when ``releasable_config_dir`` is
    given). Entries can be plain strings or dicts with "name" and
    optional "path".

    The primary target is always excluded from the secondary set
    (it's handled separately by the main release flow).

    Returns a dict mapping target name -> resolved directory path.
    """
    from . import TARGETS as ALL_TARGETS, _parse_target_entry, ConfigError

    configured = config.get("release_targets")

    # Build baseline: dict of name -> path
    if configured is not None:
        baseline = {}
        for entry in configured:
            try:
                te = _parse_target_entry(entry, project_dir)
            except (ConfigError, TypeError):
                # Unparseable entry -- skip
                continue
            if te.name in ALL_TARGETS:
                baseline[te.name] = te.path
    else:
        # Auto-detect: use detect_targets which handles config and fallback
        baseline = resolve_target_paths(
            project_dir, releasable_config_dir=releasable_config_dir,
        )

    # Never include the primary target in the secondary set
    baseline.pop(primary, None)

    return baseline


# Lockfile specs: (lockfile, tool_name, sync_cmd, guard_file)
# guard_file: if set, the spec only applies when this file exists in the same directory.
# This distinguishes e.g. go.sum (per-module) from go.work.sum (workspace root only).
_LOCKFILE_SPECS = [
    ("uv.lock", "uv", ["uv", "lock"], None),
    ("package-lock.json", "npm", ["npm", "install", "--package-lock-only"], None),
    ("go.sum", "go", ["go", "mod", "tidy"], None),
    ("go.work.sum", "go", ["go", "work", "sync"], "go.work"),
    ("gradle.lockfile", "gradle", ["./gradlew", "dependencies", "--write-locks"], None),
]

_LOCKFILE_SYNC_TIMEOUT = 30


def _target_lockfile_syncs(target_paths, log, specs=None):
    """Which lockfile syncs a release owes, and what each one runs.

    Pure: every question that decides whether a sync is owed -- does the
    lockfile exist, is its guard file present, is the tool on PATH, is the
    lockfile gitignored -- is asked here, before the release records or
    performs anything.  The old inline version asked the gitignore question
    with a ``git check-ignore`` observe issued AFTER its own sync, which under
    a preview is a question asked after a recorded mutation: the framework
    answers with a stale carrier and the preview truncates on the reply.

    *specs* narrows the ecosystems considered (defaults to every entry of
    :data:`_LOCKFILE_SPECS`).  The dev_node refresh passes the uv spec alone:
    such a project is not a release target, and the only lockfile the bump can
    stale there is the one recording the bumped sibling.

    Returns a list of dicts the plan carries verbatim::

        {"cwd": ..., "cmd": [...], "lockfile": ..., "lockfile_path": ...,
         "timeout": ...}
    """
    from . import effects as _effects

    syncs = []
    for _target_name, t_path in target_paths.items():
        for lockfile, tool_name, sync_cmd, guard_file in (
            specs if specs is not None else _LOCKFILE_SPECS
        ):
            if guard_file and not os.path.exists(os.path.join(t_path, guard_file)):
                continue
            lockfile_path = os.path.join(t_path, lockfile)
            if not os.path.exists(lockfile_path):
                continue
            if sync_cmd[0].startswith("./"):
                wrapper_path = os.path.join(t_path, sync_cmd[0][2:])
                if not os.path.exists(wrapper_path):
                    log(f"Warning: {sync_cmd[0]} not found in {t_path}, skipping {lockfile} sync")
                    continue
            elif shutil.which(tool_name) is None:
                log(f"Warning: {tool_name} not found on PATH, skipping {lockfile} sync")
                continue
            norm_path = os.path.normpath(lockfile_path)
            ignored = False
            try:
                probe = _effects.run(
                    ["git", "check-ignore", "-q", norm_path],
                    cwd=t_path, capture_output=True,
                )
                # An unanswerable probe (a preview past its first recorded
                # mutation) is not evidence that the lockfile is ignored, and
                # treating it as such would silently drop the sync from the
                # preview. Exit 0 means the file IS ignored.
                ignored = (
                    not _effects.unsettled(probe) and probe.returncode == 0
                )
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("git check-ignore failed for lockfile", e)
            if ignored:
                log(f"Lockfile is gitignored, skipping: {lockfile}")
                continue
            syncs.append({
                "cwd": t_path,
                "cmd": list(sync_cmd),
                "lockfile": lockfile,
                "lockfile_path": norm_path,
                "timeout": _LOCKFILE_SYNC_TIMEOUT,
            })
    return syncs


# The uv entry of :data:`_LOCKFILE_SPECS`, by name rather than by index, so a
# reordering of the list cannot silently repoint the dev_node refresh.
_UV_LOCKFILE_SPEC = [s for s in _LOCKFILE_SPECS if s[0] == "uv.lock"]


def _uv_lock_path_sources(lock_path):
    """Directories a ``uv.lock`` resolves PATH sources into, absolute.

    uv records a path dependency as ``source = { editable = "../sibling" }``
    (editable install) or ``source = { directory = "../sibling" }``, relative to
    the lock's own directory. Both record the sibling's version in the same
    place and go stale on the same event, so both are read.

    An unreadable or unparseable lock yields nothing: it is not evidence that a
    refresh is owed, and the lock-pin failure it would otherwise mask surfaces
    at CI as it does today.
    """
    import tomllib

    try:
        with open(lock_path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return []
    base = os.path.dirname(os.path.abspath(lock_path))
    found = []
    for package in data.get("package") or []:
        source = package.get("source")
        if not isinstance(source, dict):
            continue
        for key in ("editable", "directory"):
            rel = source.get(key)
            if isinstance(rel, str) and rel:
                found.append(os.path.normpath(os.path.join(base, rel)))
    return found


def _devnode_lock_syncs(monorepo_root, bumped_dirs, log):
    """``uv lock`` syncs owed by non-releasable projects this bump stales.

    A workspace project that is never released -- a ``dev_node``, or one with
    ``releasable = false`` -- can install a releasable sibling through an
    editable uv path source, which makes its ``uv.lock`` record that sibling's
    version. The version bump is the moment that lock goes stale, and the only
    moment the release can refresh it as part of the candidate commit.

    Left to CI, the staleness is found by the dev_node's own lock-pin test
    AFTER the candidate is pushed, and the fix-forward for it touches only the
    dev_node's path -- so every releasable's path-filtered job then concludes
    skipped on the resumed candidate. The whole wedge starts here.

    Only projects the release does NOT bump are considered: a releasable
    sibling re-locks itself on its own release, and writing into its tree here
    would put another releasable's files in this release's commit.

    Returns the same sync dicts :func:`_target_lockfile_syncs` produces.
    """
    from ...workspace import load_workspace
    from ...workspace_types import project_is_releasable

    if not monorepo_root or not bumped_dirs:
        return []
    try:
        projects = load_workspace(str(monorepo_root))
    except Exception as exc:
        from ...utils import warn_exception

        warn_exception("could not read the workspace for dev_node locks", exc)
        return []

    wanted = {os.path.normpath(os.path.abspath(d)) for d in bumped_dirs}
    syncs = []
    for project in projects:
        if project_is_releasable(project):
            continue
        path = project["path"]
        project_dir = os.path.join(str(monorepo_root), path)
        lock_path = os.path.join(project_dir, "uv.lock")
        if not os.path.exists(lock_path):
            continue
        if not any(src in wanted for src in _uv_lock_path_sources(lock_path)):
            continue
        owed = _target_lockfile_syncs(
            {"dev_node": project_dir}, log, specs=_UV_LOCKFILE_SPEC,
        )
        if owed:
            log(
                f"Refreshing {path}/uv.lock: it locks a bumped sibling as an "
                f"editable path source"
            )
        syncs.extend(owed)
    return syncs


def _sync_lockfiles(target_paths, files_to_commit, log):
    """Re-sync lockfiles after version bumps so they stay consistent.

    For each known lockfile found in a target directory, runs the
    corresponding sync command. If the lockfile is modified, its path
    is appended to files_to_commit so it is included in the release
    commit and not flagged by the unexpected-files guard.

    Missing tools and sync failures are warnings, not errors.
    """
    import shutil

    # Late-bound through the package namespace so tests can patch
    # rlsbl.commands.release.effects (and subprocess, for its exception types).
    from . import effects as _effects
    from . import subprocess

    for _target_name, t_path in target_paths.items():
        for lockfile, tool_name, sync_cmd, guard_file in _LOCKFILE_SPECS:
            if guard_file and not os.path.exists(os.path.join(t_path, guard_file)):
                continue
            lockfile_path = os.path.join(t_path, lockfile)
            if not os.path.exists(lockfile_path):
                continue

            # For commands using a project-local wrapper (e.g. ./gradlew),
            # check if the wrapper exists in the target directory instead of
            # looking for the tool on PATH.
            if sync_cmd[0].startswith("./"):
                wrapper_path = os.path.join(t_path, sync_cmd[0][2:])
                if not os.path.exists(wrapper_path):
                    log(f"Warning: {sync_cmd[0]} not found in {t_path}, skipping {lockfile} sync")
                    continue
            elif shutil.which(tool_name) is None:
                log(f"Warning: {tool_name} not found on PATH, skipping {lockfile} sync")
                continue

            # Record mtime before sync
            try:
                mtime_before = os.stat(lockfile_path).st_mtime_ns
            except OSError:
                mtime_before = None

            try:
                _effects.run(
                    sync_cmd,
                    cwd=t_path,
                    timeout=_LOCKFILE_SYNC_TIMEOUT,
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                log(f"Warning: {lockfile} sync failed: {e}")
                continue

            # Check if lockfile was modified
            try:
                mtime_after = os.stat(lockfile_path).st_mtime_ns
            except OSError:
                continue

            if mtime_before != mtime_after:
                norm_path = os.path.normpath(lockfile_path)
                # Skip gitignored lockfiles -- git add would fail
                try:
                    result = _effects.run(
                        ["git", "check-ignore", "-q", norm_path],
                        cwd=t_path,
                        capture_output=True,
                    )
                    if result.returncode == 0:
                        # exit 0 means the file IS ignored
                        log(f"Lockfile updated but gitignored, skipping: {lockfile}")
                        continue
                except Exception as e:
                    from ...utils import warn_exception
                    warn_exception("git check-ignore failed for lockfile", e)
                if norm_path not in files_to_commit:
                    files_to_commit.append(norm_path)
                    log(f"Lockfile updated: {lockfile}")


def _publish_standalone_pipelines(
    ctx, target_paths, primary_path, new_version, state_path, log
):
    """Publish every configured pipeline for a standalone / implicit-mode release.

    Each pipeline publishes from its own linked target's path
    (``target_paths[pipeline.target]``) so multi-target projects whose
    targets live in distinct subdirectories publish the right artifacts.
    Targetless (``target is None``) deploy pipelines fall back to
    *primary_path* (the registry/root path).

    Resume support: ``published_targets`` in the release state tracks
    completed pipelines so a re-run skips them. On failure, partial progress
    is persisted and a ``PostReleaseError`` is raised.
    """
    # Resolve load_pipelines through the package __init__ so that
    # mock.patch("rlsbl.commands.release.load_pipelines") is honoured
    # (mirrors the resolution used inside _execute_release).
    from . import load_pipelines
    from ...errors import PostReleaseError

    existing_state = load_release_state(state_path) or {}
    already_published = set(existing_state.get("published_targets", []))
    published = list(already_published)

    release_pipelines = load_pipelines(ctx.config)
    for pl_name, pl in release_pipelines.items():
        if pl_name in already_published:
            log(f"  Pipeline '{pl_name}': skipped (already published)")
            continue
        # Defensive read (mirrors PublishPipeline.publication_probe): the
        # target link is set by load_pipelines but may be absent on a
        # targetless deploy pipeline -- fall back to the primary/root path.
        pl_target = getattr(pl, "target", None)
        pl_path = target_paths.get(pl_target, primary_path)
        try:
            pl.publish(pl_path, new_version, ctx=ctx)
        except Exception as e:
            partial = load_release_state(state_path) or {}
            partial["published_targets"] = published
            save_release_state(state_path, partial)
            save_step_failure(
                state_path, "PIPELINES_PUBLISHED",
                f"pipeline '{pl_name}': {e}",
            )
            raise PostReleaseError(
                f"pipeline '{pl_name}' publish failed: {e}. "
                f"Release state has been preserved; fix the issue and "
                f"run `rlsbl release resume` to re-attempt the publish."
            ) from e
        published.append(pl_name)

    final_state = load_release_state(state_path) or {}
    final_state["published_targets"] = published
    save_release_state(state_path, final_state)
    return published


def archive_blog_body(releases_dir, version):
    """Archive unreleased.md to v{version}.md during release finalization.

    ``releases_dir`` is the resolved releases directory (member
    ``.rlsbl/releases/``, or the releasable's ``releases/`` dir in
    explicit releasable mode).

    Returns the archived path if the file existed, None otherwise.
    """
    blog_body_src = os.path.join(releases_dir, "unreleased.md")
    blog_body_dst = os.path.join(releases_dir, f"v{version}.md")
    if os.path.exists(blog_body_src):
        effects.rename(blog_body_src, blog_body_dst)
        effects.chmod(blog_body_dst, 0o444)
        return blog_body_dst
    return None


def collect_companion_tags(member_package_paths, workspace_root, version,
                           primary_tag, releasable_config_dir=None):
    """Collect companion tags from all publishing member packages.

    Iterates member packages in an explicit releasable, detects their
    targets, and collects companion tags (e.g. Go module proxy tags).

    Guards:
    - Only meaningful in explicit releasable mode (caller checks).
    - Skips companion creation if the primary tag already contains a
      ``/v`` pattern (Go-compatible), to avoid duplicate tags.
    - Skips publish-suppressed packages (same logic as
      _sync_member_package_versions_plan, including releasable-level config
      inheritance).

    Args:
        member_package_paths: workspace-relative paths for member packages.
        workspace_root: absolute path to the monorepo root.
        version: version string being released.
        primary_tag: the primary release tag string.
        releasable_config_dir: optional path to the releasable's state
            directory for config inheritance.

    Returns:
        List of companion tag strings (deduplicated, excluding the primary tag).
    """
    from . import TARGETS
    from ...member_context import resolve_member_context

    # If the primary tag is already Go-compatible (contains /v), skip
    # companion creation to avoid duplicates.
    if "/v" in primary_tag:
        return []

    seen = set()
    result = []
    for pkg_path in member_package_paths:
        abs_pkg = os.path.join(str(workspace_root), pkg_path)
        if not os.path.isdir(abs_pkg):
            continue

        # A broken member config is a hard error, mirroring
        # _sync_member_package_versions_plan: version sync and companion-tag
        # collection must agree on the member set, so this must never
        # silently skip a member the sync path would abort on. (The undo
        # flow wraps its collect_companion_tags call in its own try/except
        # and degrades gracefully there.)
        member = resolve_member_context(
            abs_pkg, releasable_config_dir=releasable_config_dir,
        )

        # Skip publish-suppressed packages (publish_mode == "none")
        if member.publish_mode == "none":
            continue

        entries = member.targets
        if not entries:
            continue

        for entry in entries:
            tgt = TARGETS.get(entry.name)
            if not tgt:
                continue
            for ctag in tgt.companion_tags(entry.name, version, path=pkg_path):
                if ctag not in seen and ctag != primary_tag:
                    seen.add(ctag)
                    result.append(ctag)

    return result


def _sync_member_package_versions_plan(
    member_package_paths, monorepo_root, new_version, git_root,
    exclude_path=None, releasable_config_dir=None,
):
    """Which member packages a releasable version bump has to write, and where.

    Pure: resolves each member's config and targets, validates that a declared
    target's manifest exists, and returns one entry per (member, target) pair::

        {"package_path": ..., "target": ..., "path": ..., "files": [...]}

    ``files`` are git-root-relative predictions from the target's declared
    version file -- a lower bound the executor widens with whatever the
    target's writer actually reports touching.
    """
    from . import TARGETS
    from ...member_context import resolve_member_context

    plan = []
    for pkg_path in member_package_paths:
        if exclude_path and pkg_path == exclude_path:
            continue
        abs_pkg = os.path.join(str(monorepo_root), pkg_path)
        if not os.path.isdir(abs_pkg):
            continue
        member = resolve_member_context(
            abs_pkg, releasable_config_dir=releasable_config_dir,
        )
        if member.publish_mode == "none":
            continue
        for entry in member.targets or ():
            tgt = TARGETS.get(entry.name)
            if not tgt:
                continue
            if not tgt.check_project_exists(entry.path):
                from ...errors import ConfigError
                raise ConfigError(
                    f"member '{pkg_path}' declares target '{entry.name}' but "
                    f"its manifest does not exist at {entry.path}. "
                    f"Cannot sync version."
                )
            vfile = tgt.version_file(entry.path)
            plan.append({
                "package_path": pkg_path,
                "target": entry.name,
                "path": entry.path,
                "files": (
                    [_rel_to_git_root(
                        os.path.join(entry.path, vfile), git_root,
                    )] if vfile else []
                ),
            })
    return plan


def _target_paths_from_resolved(resolved_targets) -> dict:
    """Map target name -> directory path from a resolved-targets list.

    Deduplicates by target name (keeps the first occurrence for each name,
    matching the order of ``resolved_targets``). A target served by multiple
    pipelines appears once.
    """
    paths: dict[str, str] = {}
    for rt in resolved_targets:
        if rt.name not in paths:
            paths[rt.name] = rt.path
    return paths


@dataclasses.dataclass
class ReleaseState:
    """All state needed by _run_release_mutating, grouped logically.

    ``resolved_targets`` is the canonical list of publishable
    (target, pipeline) pairs (see :class:`ResolvedTarget`); exactly one of
    them is marked ``primary``. The mutating flow derives the registry name,
    the registry-target instance (``TARGETS[registry]``), the primary path,
    the full target-path map, and the secondary-target map from this list
    (see :attr:`primary` and :func:`_target_paths_from_resolved`). No legacy
    scalar identity/path fields are stored.
    """

    # Identity / version
    new_version: str
    current_version: str
    bump_type: str | None
    tag: str
    branch: str

    # Canonical per-(target, pipeline) records; exactly one is primary.
    resolved_targets: list

    # Paths
    lock_dir: str = ".rlsbl"
    changes_dir: str | None = None  # resolved changes dir (releasable or per-project)

    # Monorepo
    monorepo_name: str | None = None
    monorepo_project_path: str | None = None

    # Releasable (explicit mode) -- None in implicit mode
    releasable_name: str | None = None
    member_package_paths: list[str] | None = None
    releasable_tag_format: str | None = None

    # Metadata
    changelog_entry: str | None = None
    commit_msg: str | None = None
    description: str = ""
    context: str = ""

    # State
    pre_existing_dirty: set | None = None
    hook_generated: set | None = None
    # HEAD pinned at the top of the release ENTRY, before any mutation
    # (including the pre-mutating selfdoc auto-commit). Everything in
    # pin_sha..HEAD must be in the release's own commit trail.
    pin_sha: str | None = None
    # The git work-tree root, resolved by the entry point BEFORE the preflight
    # hooks run. It has to come from up there: a preview records the hooks
    # rather than running them, and after the first recorded mutation every
    # observe -- including ``git rev-parse --show-toplevel`` -- answers with
    # the framework's stale carrier. None means "resolve it here" (direct
    # callers in tests).
    git_root: str | None = None
    # Commits this release created BEFORE the state file existed (the selfdoc
    # auto-commit). They seed the state file's release_commits trail.
    prior_release_commits: list[str] = dataclasses.field(default_factory=list)
    companion_tags: list[str] = dataclasses.field(default_factory=list)
    completed_steps: list[str] = dataclasses.field(default_factory=list)

    # Release config fields (persisted in state file for resume)
    include: list[str] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)
    preid: str = ""
    blog: bool = False

    # Control
    flags: dict = dataclasses.field(default_factory=dict)
    quiet: bool = False
    log: object = None  # callable
    ctx: object = None  # ProjectContext

    @property
    def primary(self):
        """The primary :class:`ResolvedTarget` (the ``include[0]`` record).

        The release flow requires exactly one primary record; a resolved list
        with none marked is a programming error (the primary name must be
        threaded through from the release file).
        """
        for rt in self.resolved_targets:
            if rt.primary:
                return rt
        raise ReleaseAbortError(
            "resolved_targets has no primary record; the release flow requires "
            "exactly one primary target (from the release file's include[0])."
        )


def _refuse_phase_b_in_preview(flags):
    """Structural backstop at the one door a preview must never open.

    It used to guard the whole mutating phase, because a preview had no
    business anywhere inside it: ``release resume --dry-run`` once walked
    straight through and committed, tagged, pushed to the release branch,
    created a GitHub Release and dispatched the publish workflows.

    Phase A is now previewable BY CONSTRUCTION -- every mutation in it is an
    effect, so a preview records it and nothing reaches the disk or the remote
    (``tests/test_release_phase_a_seam.py`` pins that, including the fact that
    no ``["git"]`` observe prefix exists that could let a push execute). What
    stays un-previewable is Phase B, which waits on CI and then publishes. So
    the backstop moved down to exactly that door: reaching it with ``--dry-run``
    set means the seam return above was bypassed, which is a bug.
    """
    if (flags or {}).get("dry-run", False):
        raise RlsblError(
            "internal error: the CI gate and the publishing half of the "
            "release were entered with --dry-run set. Every release entry "
            "point must return at the Phase-A/Phase-B seam before this point "
            "-- reaching here would tag, push and publish for real. This is a "
            "bug in rlsbl; please report the command that produced it."
        )


def _run_release_mutating(state: ReleaseState):
    """Inner release logic that runs under the advisory lock (mutating phase)."""
    # Preview mode reaches here on purpose: Phase A is issued through the plan
    # executor, whose effects a preview RECORDS rather than performs, and the
    # run returns at the Phase-A/Phase-B seam. The backstop that used to refuse
    # this whole function now guards that seam instead
    # (:func:`_refuse_phase_b_in_preview`).
    _previewing = bool((state.flags or {}).get("dry-run", False))
    # Late-bound through the package namespace, exactly like the ``run`` family
    # below, so this backstop and the entry point's own check consult the SAME
    # effects module -- including when a test patches it.
    from . import effects as _effects_ns
    if _previewing and not _effects_ns.previewing():
        # --dry-run without an effects handle to record onto: every mutation
        # below would EXECUTE. The entry points stop at the plan summary in
        # that case (the library path), so reaching here means one of them
        # did not -- and the difference between "recorded" and "performed" is
        # the whole release.
        raise RlsblError(
            "internal error: the mutating release phase was entered with "
            "--dry-run set but no effects handle bound, so its effects would "
            "execute instead of being recorded. This is a bug in rlsbl; "
            "please report the command that produced it."
        )
    # Unpack frequently-used state into locals for readability and to preserve
    # the existing closure/reference patterns (commit_msg, primary_path, and
    # target_paths are conditionally reassigned below).
    # Derive identity/paths from the canonical resolved-targets list. The
    # primary record supplies the registry name and primary path; the full
    # target-path map (and thus the secondary map) come from the whole list.
    # The registry-target INSTANCE (TARGETS[registry]) is resolved after the
    # late-bound import block below.
    primary_rt = state.primary
    registry = primary_rt.name
    primary_path = primary_rt.path
    target_paths = _target_paths_from_resolved(state.resolved_targets)
    secondary_targets = {k: v for k, v in target_paths.items() if k != registry}
    flags = state.flags
    quiet = state.quiet
    log = state.log
    new_version = state.new_version
    current_version = state.current_version
    bump_type = state.bump_type
    tag = state.tag
    branch = state.branch
    changelog_entry = state.changelog_entry
    monorepo_name = state.monorepo_name
    monorepo_project_path = state.monorepo_project_path
    releasable_name = state.releasable_name
    member_package_paths = state.member_package_paths
    releasable_tag_format_str = state.releasable_tag_format
    commit_msg = state.commit_msg
    lock_dir = state.lock_dir
    # ``pre_existing_dirty`` and ``hook_generated`` are read off ``state`` by
    # the Phase-A plan builder, which owns everything they feed (the commit's
    # file list and the concurrent-change guard's expected set).
    description = state.description
    context = state.context
    ctx = state.ctx

    # Late-bound imports from the package namespace for mock.patch compatibility.
    # All rlsbl-internal names are resolved through __init__.py so that
    # mock.patch("rlsbl.commands.release.X") is picked up at call time.
    from . import (
        run,
        run_gh,
        run_gh_unscoped,
        push_if_needed,
        commit_files,
        commit_files_if_changed,
        has_staged_or_modified,
        get_push_timeout,
        get_hook_timeout,
        get_ci_timeout,
        wait_for_ci_green,
        CIWaitError,
        ProjectCINotRunError,
        release_check_filters,
        CI_GREEN,
        CI_NOT_CONFIGURED,
        CI_TIMEOUT,
        get_current_branch,
        should_tag,
        tag_exists_locally,
        resolve_tag_push_plan,
        TARGETS,
        load_pipelines,
        load_workspace,
        read_deploy_config,
        deploy_target,
        ensure_github_topic,
        changes_dir_exists,
        finalize_version,
        generate_version_file,
        get_changes_dir,
        validate_subtree_remote_ssh_host,
        _cleanup_release_artifacts,
        upload_release_assets,
        _print_stale_dep_advisory,
        parse_porcelain_paths,
        ReleaseValidationError,
        HookError,
        _read_release_metadata_full,
    )

    # Registry-target instance for the primary target (needs TARGETS, imported
    # just above). 'reg' and 'target' were always the same object.
    target = TARGETS[registry]
    reg = target

    project_root = ctx.project_root
    monorepo_root = ctx.workspace_root
    project_dir = str(project_root)

    # Releasable state dir for member config/target inheritance (explicit mode)
    _releasable_cfg_dir = None
    if releasable_name and monorepo_root:
        from ...workspace import get_releasable_dir
        _releasable_cfg_dir = get_releasable_dir(str(monorepo_root), releasable_name)

    # In releasable mode, the representative member is subject to the same
    # publish_mode-awareness as every other member (resolve_member_context with
    # releasable-level inheritance): a publish-suppressed representative's manifests
    # are never version-bumped or keyword-tagged. The releasable version
    # file remains the source of truth and is always updated.
    _rep_is_private = False
    if _releasable_cfg_dir is not None:
        from ...member_context import resolve_member_context
        _rep_is_private = resolve_member_context(
            project_dir, releasable_config_dir=_releasable_cfg_dir,
        ).publish_mode == "none"

    # Snapshot dirty files BEFORE any version-bump writes. This captures
    # everything dirtied by prior stages (generate_changelog, hooks, lint,
    # --allow-dirty pre-existing files, etc.). Only files that become dirty
    # AFTER this point — i.e. during the version bump — are candidates for
    # the "unexpected modified files" abort.
    #
    # A preview has no such snapshot to take: the stages above RECORDED their
    # mutations instead of making them, so git would report the tree as it was
    # before the release started -- which is exactly ``pre_existing_dirty``.
    # (It would not even report that: an observe issued after a recorded
    # mutation answers with the framework's stale carrier.) The guard that
    # consumes the snapshot does not run in a preview either, because there is
    # nothing written for it to judge.
    if _previewing:
        baseline_dirty = set(state.pre_existing_dirty or ())
    else:
        baseline_output = run("git", ["--no-optional-locks", "status", "--porcelain"])
        baseline_dirty = parse_porcelain_paths(baseline_output) if baseline_output else set()

    if commit_msg is None:
        commit_msg = tag

    # git status --porcelain outputs paths relative to the repo root.
    # Compute the repo root so vpath can produce matching relative paths.
    # The caller resolves it before the preflight hooks and hands it over,
    # because by the time control reaches here a preview has already recorded
    # a mutation or two -- and after the first of those, every observe (this
    # one included) answers with the framework's stale carrier.
    _git_root = state.git_root or _git_toplevel(project_dir)

    def vpath(filename):
        """Join filename with project_dir, return relative to git root."""
        return _rel_to_git_root(os.path.join(project_dir, filename), _git_root)

    def target_vpath(t_path, filename):
        """Join filename with a target's resolved path, return relative to git root."""
        return _rel_to_git_root(os.path.join(t_path, filename), _git_root)

    # Pre-compute expected version files for the confirmation prompt display.
    # The actual files_to_commit list is built from write_version() return
    # values below, which may include additional files (e.g. __init__.py).
    version_file = reg.version_file(primary_path)
    preview_files = []
    if version_file:
        preview_files.append(target_vpath(primary_path, version_file))
    for t_name, t_path in target_paths.items():
        if t_name == registry:
            continue
        other_reg = TARGETS.get(t_name)
        if other_reg and other_reg.check_project_exists(t_path):
            other_file = other_reg.version_file(t_path)
            if other_file:
                preview_files.append(target_vpath(t_path, other_file))

    # Announcement, not a gate.  `release run` declares itself
    # `consequential`, so strictcli confirms once before dispatch and
    # --approve-consequential skips that one prompt; a second prompt here
    # asked the same question in different words and needed its own
    # non-interactive error text.  What the old prompt SHOWED is kept, because
    # the framework prompt cannot know the resolved version, tag or files.
    bump_label = f" ({bump_type})" if bump_type else ""
    print(f"\nReleasing {new_version}{bump_label} on {branch}")
    print(f"  Tag: {tag}")
    if preview_files:
        print(f"  Files: {', '.join(preview_files)}")
    else:
        print("  Files: (none -- version is the git tag)")
    if should_tag(flags, ctx.config):
        print("  Will add 'rlsbl' keyword to project manifests")

    # Capture HEAD before any version-bump writes so we can roll back on failure.
    # This must happen before write_version() so that git reset --hard reverts
    # the uncommitted version-bumped files if the release aborts.
    #
    # A preview has nothing to roll back -- it performs no write -- so it takes
    # the release's own entry pin rather than issuing an observe that a
    # recorded mutation has already made stale.
    pre_release_sha = (
        (state.pin_sha or "") if _previewing
        else run("git", ["rev-parse", "HEAD"])
    )

    # Write release state file at the start of the mutating phase.
    # This persists if the release fails mid-way, enabling future resume.
    # On a resume, an existing state file may already contain completed_steps
    # from a prior run. Preserve those so per-step guards can skip them.
    # Releasable releases keep their state under the releasable's own dir
    # (.rlsbl-monorepo/releasables/<name>/releases/), never under the
    # representative member's .rlsbl/.
    _state_path = get_state_path(project_dir, releasable_dir=_releasable_cfg_dir)
    _existing_state = load_release_state(_state_path)
    _prior_completed = (
        _existing_state.get("completed_steps", [])
        if _existing_state is not None
        else list(state.completed_steps)
    )
    _prior_failed = (
        _existing_state.get("failed_steps", {})
        if _existing_state is not None
        else {}
    )
    # Seed the trail with the commits the release already made before the
    # state file existed (the pre-mutating selfdoc auto-commit), preserving
    # anything a prior attempt recorded.
    _prior_trail = list(
        (_existing_state or {}).get("release_commits", [])
    )
    for _sha in state.prior_release_commits or []:
        if _sha and _sha not in _prior_trail:
            _prior_trail.append(_sha)
    # Start from what a prior attempt recorded, THEN overwrite this attempt's
    # own fields. Rebuilding from a bare literal instead silently erased every
    # key the literal did not happen to mention -- ``candidate_sha`` above all,
    # which is the only record of the commit CI verified. A resume therefore
    # forgot its verified candidate the moment it entered this function, and
    # the reader below fell back to "whatever HEAD is now"; an untested commit
    # got tagged "(CI-verified)" and handed to the publish gate. Merging keeps
    # that class closed for any future in-flight key as well.
    _state_dict = dict(_existing_state or {})
    _state_dict.update({
        "new_version": new_version,
        "tag": tag,
        "branch": branch,
        "pre_release_sha": pre_release_sha,
        "pin_sha": state.pin_sha or pre_release_sha,
        "release_commits": _prior_trail,
        "bump_type": bump_type,
        "registry": registry,
        "completed_steps": list(_prior_completed),
        "failed_steps": dict(_prior_failed),
        "companion_tags": [],
        "monorepo_name": monorepo_name,
        "releasable_name": releasable_name,
        "commit_msg": commit_msg,
        "description": description,
        "context": context,
        "include": list(state.include),
        "exclude": list(state.exclude),
        "preid": state.preid,
        "blog": state.blog,
    })
    save_release_state(_state_path, _state_dict)
    # Refuse-on-drift, checkpoint 1 of 4 (mutating entry). Re-checked before
    # the candidate push, immediately after the CI gate (the long window, and
    # the last moment before anything irreversible), and before the final
    # push.
    _pin_sha = _state_dict["pin_sha"]
    _guard_foreign_commits(
        _pin_sha, _state_path, cwd=_git_root, phase="mutating entry",
    )
    # Load completed_steps to check which steps are already done (empty on
    # fresh start; populated when resuming from a prior failed attempt).
    _completed = set(_state_dict.get("completed_steps", []))

    # Track whether the candidate push succeeded. Once commits are on the
    # remote, a local `git reset --hard` would create divergent state.
    # Set to True after push_if_needed() returns successfully.
    branch_pushed = False
    # True once the candidate push has been ATTEMPTED. A push that timed out
    # may still have landed on the remote, so a timeout at this point must not
    # trigger `git reset --hard` (that is exactly how divergent local/remote
    # state was created before). A non-timeout push failure proves nothing
    # landed and still rolls back.
    candidate_push_attempted = False
    # The commit CI verified: the tag target, the CI-SHA release-notes marker,
    # and the post-release watch all address it explicitly. Assigned inside the
    # mutating try block once the candidate is pushed.
    verified_sha = None

    def _handle_resumable_push_failure(_exc) -> bool:
        """Classify a post-candidate-push failure as RESUMABLE (no rollback).

        Once the release candidate is on the remote, a local rollback would
        diverge from published history, so the entire rollback family is
        skipped — no clobber guard, no ``git reset --hard``, no tag deletion,
        no artifact cleanup, no state clearing. The failure is recorded and
        ``rlsbl release resume`` re-attempts from the failed step via
        idempotent guards.

        When the release is already TAGGED, the outstanding work is the tag
        push: a best-effort retry is attempted first (transient stalls often
        clear on retry); a recovered tag push marks PUSHED complete so resume
        picks up at GITHUB_RELEASE.

        Returns True when the outstanding tag push recovered on retry (PUSHED
        marked complete): the caller then falls through into the remaining
        post-push steps (GITHUB_RELEASE, ...) instead of aborting. Returns
        False when the push is still outstanding and the release must stop
        with resume guidance.
        """
        _timed_out = _is_push_timeout_exc(_exc)
        _retry_timeout = get_push_timeout(ctx.config, override=flags.get("push-timeout"))
        _recovered = False
        _tagged = "TAGGED" in _completed
        if branch_pushed and _tagged:
            # Branch commits are already on the remote; only the tag push
            # is outstanding. Retry it a couple of times before giving up.
            # Use the same commit-aware plan as the main PUSHED path: if a
            # prior attempt already landed every tag at the matching commit
            # (partial success then a stall), the plan reports no push needed
            # and the retry cleanly marks PUSHED complete.
            _all_tags = [tag] + list(state.companion_tags)
            for _attempt in range(2):
                time.sleep(1)
                try:
                    if resolve_tag_push_plan(_all_tags):
                        run(
                            "git",
                            ["push", "--no-verify", "origin", tag] + list(state.companion_tags),
                            timeout=_retry_timeout,
                        )
                    log(f"Tag push succeeded on retry {_attempt + 1}")
                    save_step(_state_path, "PUSHED")
                    _completed.add("PUSHED")
                    _recovered = True
                    break
                except Exception:
                    pass
        if _recovered:
            # The outstanding tag push cleared on retry; the release is fully
            # pushed. Signal the caller to continue with the post-push steps
            # rather than aborting with resume guidance.
            return True
        # The failing step is the first canonical mutating step that has not
        # completed. Derived rather than hardcoded so every failure point
        # between the candidate push and the GitHub Release records itself.
        from .release_state import MUTATING_STEPS as _MUTATING_STEPS
        _failed_step = next(
            (s for s in _MUTATING_STEPS if s not in _completed), "PUSHED",
        )
        # Record the failed step (fatal + resumable). This does NOT gate
        # resume-skip; resume re-attempts the step via its own idempotent
        # guards.
        save_step_failure(
            _state_path, _failed_step, str(_exc) or _exc.__class__.__name__,
        )
        if hasattr(_exc, "stderr") and _exc.stderr:
            print(f"Command error: {_exc.stderr.strip()}", file=sys.stderr)
        if _tagged:
            print(
                f"Error: push failed after the release was tagged ({tag}). "
                f"Local state is intact and fully resumable — nothing was rolled "
                f"back; the tag and finalized changelog files are preserved.",
                file=sys.stderr,
            )
        elif _failed_step == "BRANCH_PUSHED":
            print(
                f"Error: the candidate push to origin/{branch} timed out, so "
                f"whether it landed is unknown. Nothing was rolled back (a "
                f"reset could diverge from a push that did land) and nothing "
                f"was tagged, released or finalized — {new_version} is not "
                f"burnt.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: the release failed after the candidate was pushed to "
                f"origin/{branch}. Nothing was rolled back (the candidate is "
                f"published) and nothing was tagged, released or finalized — "
                f"{new_version} is not burnt.",
                file=sys.stderr,
            )
        if _timed_out:
            print(
                f"The push timed out (limit: {_retry_timeout}s). Raise the "
                f"timeout and resume:\n"
                f"  rlsbl release resume --push-timeout 900\n"
                f"(or set push_timeout in .rlsbl/config.json)",
                file=sys.stderr,
            )
        else:
            print(
                "Fix the issue and resume:\n  rlsbl release resume",
                file=sys.stderr,
            )
        return False

    def _warn_rollback_residuals():
        """Postcondition check: warn if the working tree is not clean after a
        pre-TAGGED rollback.

        A correct rollback (``git reset --hard`` + orphan-artifact cleanup)
        must leave the working tree byte-identical to the pre-release HEAD.
        If ``git status --porcelain`` still reports changes, something was not
        fully reverted (e.g. a residual generated file). This is a warning,
        not a fatal error -- the original release failure is the primary
        signal -- but it names every leftover path so manual cleanup is
        possible before retrying.

        Transient release-machinery files that are not rollback residuals are
        excluded: the advisory lock file (``.rlsbl/lock``, released by the
        caller's ``finally`` after this handler) and the in-progress state
        file (already removed by ``clear_release_state`` above, but excluded
        defensively).
        """
        try:
            residual = run("git", ["--no-optional-locks", "status", "--porcelain"]).strip()
        except Exception:
            return
        if not residual:
            return
        # Transient paths that are not rollback residuals, as absolute paths.
        _transient_abs = {
            os.path.abspath(os.path.join(project_dir, lock_dir, "lock")),
            os.path.abspath(_state_path),
        }
        leftover_paths = []
        for _p in parse_porcelain_paths(residual):
            _abs = os.path.abspath(os.path.join(_git_root, _p.rstrip("/")))
            if _abs in _transient_abs:
                continue
            leftover_paths.append(_p)
        if not leftover_paths:
            return
        print(
            "Warning: rollback left residual working-tree changes; "
            "may need manual cleanup before retrying:",
            file=sys.stderr,
        )
        for _p in sorted(leftover_paths):
            print(f"  {_p}", file=sys.stderr)

    # Everything from version-bump writes through commit/tag/push is wrapped
    # in a single try block so that any failure (including ReleaseAbortError
    # from the unexpected-files check) triggers rollback of version-bumped
    # files via git reset --hard.
    try:
        # ---- Phase A: version bump through candidate push ----
        #
        # Built first, then issued. :func:`~rlsbl.commands.release.phase_a.
        # build_phase_a_plan` does every read this half of the release needs --
        # the manifests, the working tree, the branch tip, the remote head, the
        # state file -- and returns typed steps whose payloads are plain data;
        # :func:`~rlsbl.commands.release.phase_a.execute_phase_a_plan` issues
        # them and asks nothing. That is what lets a preview record Phase A end
        # to end: with no observe left after the first recorded mutation, there
        # is nothing for the framework's stale carrier to truncate.
        #
        # Rollback stays here, in the caller: a failing step raises into the
        # handlers below (reset to the pre-release pin plus orphan cleanup).
        # It is never a plan step, and a preview -- which executes nothing --
        # needs none.
        _phase_a_inputs = phase_a.BuildInputs(
            state=state, ctx=ctx, log=log,
            project_dir=project_dir, git_root=_git_root,
            monorepo_root=monorepo_root,
            releasable_cfg_dir=_releasable_cfg_dir,
            rep_is_private=_rep_is_private,
            registry=registry, primary_path=primary_path,
            target_paths=target_paths, secondary_targets=secondary_targets,
            state_path=_state_path, completed=_completed, pin_sha=_pin_sha,
            baseline_dirty=baseline_dirty, commit_msg=commit_msg,
            lock_dir=lock_dir,
        )

        # The batch orchestrator runs ONE CI gate for the whole batch: every
        # member's candidate is pushed first (ci-defer), the batch tip is
        # verified once, and each member is then resumed with that verified
        # SHA. Such a resume must not re-push or re-gate its own candidate --
        # so it skips Phase A entirely and adopts the recorded candidate.
        _batch_verified = flags.get("ci-verified-sha")
        files_to_commit = []
        # Both operands of the seam's preview render are defined BEFORE the
        # branch below, because a preview reaches the render on either path.
        # ``None`` is the declared shape for "Phase A issued nothing": the
        # branch that skips it did so because the candidate is already pushed
        # and verified, and the render says exactly that instead of printing an
        # empty plan table that would read as "nothing to do".
        _phase_a_plan = None
        if "CI_VERIFIED" in _completed or (
            _batch_verified and "BRANCH_PUSHED" in _completed
        ):
            # This branch SKIPS the gate, so the verified commit can only come
            # from the record an earlier attempt left. Fail-closed: there is no
            # fallback to HEAD, because "the current tip" is not evidence that
            # CI ran on anything.
            branch_pushed = True
            if "CI_VERIFIED" in _completed:
                candidate_sha = require_recorded_candidate(
                    _state_path, cwd=_git_root, version=new_version,
                )
                log(
                    f"Skipping candidate push and CI gate (already verified "
                    f"on {candidate_sha[:12]})"
                )
            else:
                # Batch pass 2: the orchestrator gated the batch tip and passes
                # it in explicitly, so IT is the verified commit.
                candidate_sha = str(_batch_verified).strip()
                log(
                    "CI gate satisfied by the batch orchestrator "
                    f"({str(_batch_verified)[:12]})"
                )
                save_step(_state_path, "CI_VERIFIED")
                _completed.add("CI_VERIFIED")
        else:
            _phase_a_plan = phase_a.build_phase_a_plan(_phase_a_inputs)
            files_to_commit = _phase_a_plan.files_to_commit
            # Recorded before the push is issued: a push that times out may
            # still have landed, and the resumable-failure classifier below
            # must not roll back over a candidate that reached the remote.
            candidate_push_attempted = any(
                s.kind == phase_a.PUSH_CANDIDATE for s in _phase_a_plan.steps
            )
            candidate_sha = phase_a.execute_phase_a_plan(
                _phase_a_plan, _phase_a_inputs, preview=_previewing,
            )
            if _phase_a_plan.defers_push:
                # Batch mode, pass 1: COMMIT ONLY -- the candidate stays local.
                # The orchestrator publishes the whole batch in ONE push once
                # every member has committed, and gates that single commit.
                #
                # Pushing per member instead gave each push a one-project diff,
                # and the generated CI router filters paths against the push's
                # own before-SHA: on the commit every tag then pointed at, all
                # the OTHER members' CI jobs concluded `skipped`. Their publish
                # gates refuse a skipped check (correctly -- it proves nothing),
                # so tags and GitHub Releases existed for versions that never
                # reached their registries, with no re-run that could ever go
                # green. Two real batch releases half-published that way.
                log("Deferring the candidate push and CI gate to the batch "
                    "orchestrator")
                return
            branch_pushed = True

        # ---- The Phase-A / Phase-B seam ----
        #
        # Everything above is knowable now. Everything below waits on a verdict
        # only CI can give, so a preview stops here: it renders what Phase B
        # WOULD do as a declared plan, under a boundary line saying so, and
        # returns without issuing any of it.
        if _previewing:
            from .validate import print_release_preview

            print_release_preview(
                log, _phase_a_plan, state,
                registry=registry, files_to_commit=files_to_commit,
            )
            return
        _refuse_phase_b_in_preview(flags)

        # ---- Phase B: the CI gate ----
        #
        # The candidate is on the release branch, untagged, and the
        # repository's own CI is judging it. Only a green verdict unlocks the
        # irreversible half of the release (changelog finalization, tag, GitHub
        # Release, registry publish). A red verdict leaves NO tag, NO GitHub
        # Release and NO finalized changelog behind, so the version is not
        # burnt and the fix lands forward at the same version.
        if "CI_VERIFIED" not in _completed:
            if flags.get("ci-verified-sha"):
                log(
                    "CI gate satisfied by the batch orchestrator "
                    f"({str(flags['ci-verified-sha'])[:12]})"
                )
            else:
                # The candidate is on the remote now. If the window guard
                # judged that this push cannot trigger the project's router
                # job -- an honestly narrow fix-forward on a resume -- the
                # dispatch it recorded as owed is made HERE, before the gate,
                # so the gate has a run that actually exercises this project
                # to read. Recorded on the state rather than kept in memory:
                # a crash between the push and the dispatch is then repaired
                # by a resume instead of walking into a skipped-check refusal.
                dispatch_owed_run_all(
                    _state_path, candidate_sha=candidate_sha, branch=branch,
                    config=ctx.config, log=log,
                )
                _ci_timeout = get_ci_timeout(
                    ctx.config, override=flags.get("ci-timeout"),
                )
                try:
                    # The publish gate's own check-run filter for exactly the
                    # project(s) this tag will publish. Resolved here, applied
                    # inside the wait: one predicate, both gates.
                    _check_filters = release_check_filters(
                        config=ctx.config,
                        registry=registry,
                        project_dir=project_dir,
                        workspace_root=monorepo_root,
                        monorepo_name=state.monorepo_name,
                        releasable_name=releasable_name,
                    )
                    _verdict, _ci_results = wait_for_ci_green(
                        candidate_sha, timeout=_ci_timeout,
                        check_filters=_check_filters, log=log,
                        config=ctx.config, repo_root=_git_root,
                    )
                except ProjectCINotRunError as _pn:
                    save_step_failure(_state_path, "CI_VERIFIED", str(_pn))
                    raise ReleaseCIError(_ci_not_run_message(
                        version=new_version, tag=tag, branch=branch,
                        candidate_sha=candidate_sha, detail=str(_pn),
                    ))
                except CIWaitError as _cw:
                    save_step_failure(_state_path, "CI_VERIFIED", str(_cw))
                    raise ReleaseCIError(_ci_red_message(
                        version=new_version, tag=tag, branch=branch,
                        candidate_sha=candidate_sha, detail=str(_cw),
                    ))
                if _verdict == CI_NOT_CONFIGURED:
                    # Deliberately unconditional and on stderr: this notice is
                    # the operator's only signal that the release shipped with
                    # NO CI gate, and --quiet must not be able to swallow it.
                    print(
                        "rlsbl: no push-triggered CI workflow is configured in "
                        ".github/workflows; proceeding without a CI gate.",
                        file=sys.stderr,
                    )
                elif _verdict == CI_TIMEOUT:
                    _unresolved = ", ".join(
                        r["name"] for r in _ci_results if r.get("timed_out")
                    ) or "unknown workflow"
                    _detail = (
                        f"Unresolved workflow(s) after {_ci_timeout}s: "
                        f"{_unresolved}"
                    )
                    save_step_failure(_state_path, "CI_VERIFIED", _detail)
                    raise ReleaseCIError(_ci_timeout_message(
                        version=new_version, tag=tag, branch=branch,
                        candidate_sha=candidate_sha, detail=_detail,
                    ))
                elif _verdict != CI_GREEN:
                    _red = ", ".join(
                        r["name"] for r in _ci_results
                        if not r["passed"] and not r.get("timed_out")
                    ) or "unknown workflow"
                    _detail = f"Failing workflow(s): {_red}"
                    save_step_failure(_state_path, "CI_VERIFIED", _detail)
                    raise ReleaseCIError(_ci_red_message(
                        version=new_version, tag=tag, branch=branch,
                        candidate_sha=candidate_sha, detail=_detail,
                    ))
                else:
                    log(f"CI is green on {candidate_sha[:12]}")

            save_step(_state_path, "CI_VERIFIED")
            _completed.add("CI_VERIFIED")

        # Refuse-on-drift, checkpoint 3 of 4: the CI wait is a minutes-long
        # window, and it sits immediately before the irreversible half of the
        # release. A commit that landed while we waited must abort HERE --
        # before any finalization or tag -- not after.
        _guard_foreign_commits(
            _pin_sha, _state_path, cwd=_git_root, phase="CI gate",
        )

        # The commit the tag, the CI-SHA marker and the publish gate all
        # address. In batch mode the orchestrator verifies the batch tip and
        # passes it here, so every member tag points at a CI-green commit.
        verified_sha = (flags.get("ci-verified-sha") or candidate_sha or "").strip()
        if not verified_sha:
            raise UnverifiedCandidateError(
                f"no CI-verified commit could be established for "
                f"{new_version}; refusing to tag."
            )
        # Persist it as THE candidate, so a later resume of this same release
        # reads back exactly the commit that was tagged rather than deriving a
        # new one. Every path into the tag step now agrees on one SHA.
        _vs_state = load_release_state(_state_path) or {}
        if _vs_state.get("candidate_sha") != verified_sha:
            _vs_state["candidate_sha"] = verified_sha
            save_release_state(_state_path, _vs_state)

        # Finalize JSONL changelog: rename unreleased.jsonl to x.y.z.jsonl.
        # CHANGELOG.md already has the correct "## X.Y.Z" heading because the
        # earlier generate_changelog() call (above acquire_lock) was passed
        # version_override=new_version, so no regeneration is needed here.
        # That holds for EVERY bump type: generate_changelog() emits the
        # version_override section whenever an override is given, including
        # when unreleased.jsonl is empty -- which is what an infra release is.
        # (Before that invariant held, infra releases finalized a CHANGELOG.md
        # with no section for their own version, since nothing regenerates it
        # after this point.)
        #
        # In explicit releasable mode, the changes dir lives at the releasable
        # level, and the tag glob uses the releasable's tag format. The
        # resolved changes_dir is passed via state.changes_dir.
        changes_dir = state.changes_dir
        if changes_dir is None and changes_dir_exists(project_dir):
            changes_dir = get_changes_dir(project_dir)

        # CHANGELOG_FINALIZED guard: skip if {version}.jsonl already exists
        # and unreleased.jsonl is empty (indicating finalization already ran).
        _changelog_already_finalized = False
        if "CHANGELOG_FINALIZED" in _completed:
            _changelog_already_finalized = True
            log("Skipping changelog finalization (already done)")
        elif changes_dir and os.path.isdir(changes_dir):
            _versioned_jsonl = os.path.join(changes_dir, f"{new_version}.jsonl")
            _unreleased_jsonl = os.path.join(changes_dir, "unreleased.jsonl")
            if os.path.exists(_versioned_jsonl):
                _unreleased_empty = (
                    not os.path.exists(_unreleased_jsonl)
                    or os.path.getsize(_unreleased_jsonl) == 0
                )
                if _unreleased_empty:
                    _changelog_already_finalized = True
                    save_step(_state_path, "CHANGELOG_FINALIZED")
                    _completed.add("CHANGELOG_FINALIZED")
                    log("Skipping changelog finalization (version JSONL already exists)")

        if not _changelog_already_finalized and changes_dir and os.path.isdir(changes_dir):
            if releasable_name and releasable_tag_format_str:
                from .validate import _releasable_tag_glob
                tag_glob = _releasable_tag_glob(releasable_tag_format_str, releasable_name)
            elif monorepo_name:
                tag_glob = target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path)
            else:
                tag_glob = None

            finalize_version(changes_dir, new_version, tag_glob=tag_glob)
            # Pass release metadata so the new version's .md matches what a
            # future backfill from the archived v{version}.toml would produce
            # (the archived toml is stripped on read, so strip here too).
            generate_version_file(
                changes_dir, new_version,
                description=(description or "").strip(),
                context=(context or "").strip(),
                bump_type=bump_type,
            )
            log(f"Finalized JSONL changelog for {new_version}")
            # Commit the finalized JSONL file and the new empty unreleased.jsonl
            jsonl_finalized = _rel_to_git_root(os.path.join(changes_dir, f"{new_version}.jsonl"), _git_root)
            jsonl_unreleased = _rel_to_git_root(os.path.join(changes_dir, "unreleased.jsonl"), _git_root)
            # The generated CHANGELOG.md files, resolved the same way the
            # Phase-A plan resolved them for the release commit.
            _changelog_commit_files = _changelog_files_for_commit(
                project_dir, _git_root,
                releasable_cfg_dir=_releasable_cfg_dir,
                monorepo_root=monorepo_root,
            )
            finalize_files = [jsonl_finalized, jsonl_unreleased, *_changelog_commit_files]
            # Also commit the generated per-version .md file if it exists
            jsonl_md = _rel_to_git_root(os.path.join(changes_dir, f"{new_version}.md"), _git_root)
            if os.path.exists(jsonl_md):
                finalize_files.append(jsonl_md)
            # generate_changelog() (run before the mutating phase) backfills
            # description/context from archived release files into OLDER
            # per-version .md files. Include any it actually modified so the
            # release leaves a clean working tree. Only files git reports as
            # changed are added (passing unchanged files to safegit may error).
            md_status = run("git", ["--no-optional-locks", "status", "--porcelain", "--", changes_dir])
            if md_status:
                for md_path in sorted(parse_porcelain_paths(md_status)):
                    if md_path.endswith(".md") and md_path not in finalize_files:
                        finalize_files.append(md_path)
            commit_files(f"chore: finalize changelog for {new_version}", finalize_files, cwd=_git_root)
            _track_release_commit(_state_path)
            log(f"Committed finalized changelog files")
            save_step(_state_path, "CHANGELOG_FINALIZED")
            _completed.add("CHANGELOG_FINALIZED")
        elif not changes_dir or not os.path.isdir(changes_dir or ""):
            log("No .rlsbl/changes/ directory; skipping changelog finalization")
            # Not applicable: mark so completeness is provable at the epilogue.
            save_step(_state_path, "CHANGELOG_FINALIZED")
            _completed.add("CHANGELOG_FINALIZED")

        # Clean stale batch_limits exclusions that referenced unreleased.jsonl.
        # In releasable mode, `changelog add --allow-batch` writes exclusions
        # to the RELEASABLE-level config.json, so clean that one.
        from ...config import clean_stale_exclusions
        if _releasable_cfg_dir is not None:
            config_path = os.path.join(_releasable_cfg_dir, "config.json")
        else:
            config_path = os.path.join(project_dir, ".rlsbl", "config.json")
        if os.path.exists(config_path):
            removed = clean_stale_exclusions(config_path)
            if removed:
                config_rel = _rel_to_git_root(config_path, _git_root)
                commit_files(
                    f"chore: clean {removed} stale batch exclusion(s) from config.json",
                    [config_rel],
                    cwd=_git_root,
                )
                _track_release_commit(_state_path)
                log(f"Cleaned {removed} stale batch exclusion(s) from config.json")

        # Finalize release file: rename unreleased.toml to vX.Y.Z.toml
        # RELEASE_FILE_FINALIZED guard: skip if vX.Y.Z.toml exists and
        # unreleased.toml doesn't (indicating finalization already ran).
        # Releasable releases keep the release file (and its archive) under
        # the releasable's own releases dir, never the member's .rlsbl/.
        from ...release_file import get_release_file_path
        release_file_path = get_release_file_path(
            project_dir, releasable_dir=_releasable_cfg_dir,
        )
        _release_file_already_finalized = False
        if "RELEASE_FILE_FINALIZED" in _completed:
            _release_file_already_finalized = True
            log("Skipping release file finalization (already done)")
        else:
            releases_dir_rf = os.path.dirname(release_file_path)
            versioned_release_check = os.path.join(releases_dir_rf, f"v{new_version}.toml")
            if os.path.exists(versioned_release_check) and not os.path.exists(release_file_path):
                _release_file_already_finalized = True
                save_step(_state_path, "RELEASE_FILE_FINALIZED")
                _completed.add("RELEASE_FILE_FINALIZED")
                log("Skipping release file finalization (already archived)")

        # A release either HAS a release file to archive (standalone and
        # releasable releases) or carries its metadata inline (batch members,
        # whose description/context/bump come from the workspace-level batch
        # TOML). Both must end with the same archived v{version}.toml, because
        # that archive is the only thing later changelog regenerations read the
        # description and context back out of -- a batch-released version used
        # to lose both on the next regeneration.
        _synthesize_archive = (
            not _release_file_already_finalized
            and not os.path.exists(release_file_path)
            and bool((description or "").strip())
            and bool(bump_type)
        )
        if not _release_file_already_finalized and (
            os.path.exists(release_file_path) or _synthesize_archive
        ):
            releases_dir = os.path.dirname(release_file_path)
            versioned_release = os.path.join(releases_dir, f"v{new_version}.toml")
            release_finalize_files = [
                _rel_to_git_root(versioned_release, _git_root),
            ]
            if _synthesize_archive:
                from ...release_file import write_archived_release_file
                write_archived_release_file(
                    releases_dir, new_version,
                    bump=bump_type,
                    include=list(state.include) or [registry],
                    exclude=list(state.exclude),
                    description=(description or "").strip(),
                    context=(context or "").strip(),
                    preid=state.preid,
                    blog=state.blog,
                )
            else:
                effects.rename(release_file_path, versioned_release)
                effects.chmod(versioned_release, 0o444)
                release_finalize_files.append(
                    _rel_to_git_root(release_file_path, _git_root)
                )
                # Archive blog body file if it exists (unreleased.md -> v{version}.md)
                blog_body_dst = archive_blog_body(releases_dir, new_version)
                if blog_body_dst:
                    release_finalize_files.append(_rel_to_git_root(blog_body_dst, _git_root))
            commit_files(f"chore: finalize release file for {new_version}", release_finalize_files, cwd=_git_root)
            _track_release_commit(_state_path)
            log(f"Finalized release file for {new_version}")

            # Now that v{version}.toml is archived, regenerate the per-version
            # .md so its content is derived from _read_release_metadata() rather
            # than the direct params passed earlier. This keeps the .md
            # consistent with what future generate_changelog() calls produce.
            changes_dir_regen = state.changes_dir or (get_changes_dir(project_dir) if changes_dir_exists(project_dir) else None)
            if changes_dir_regen and os.path.isdir(changes_dir_regen):
                ver_desc, ver_ctx, ver_bump = _read_release_metadata_full(
                    project_dir, new_version, releases_dir=releases_dir,
                )
                generate_version_file(
                    changes_dir_regen, new_version,
                    description=ver_desc, context=ver_ctx,
                    bump_type=ver_bump or None,
                )
                md_regen_path = os.path.join(changes_dir_regen, f"{new_version}.md")
                md_regen_rel = _rel_to_git_root(md_regen_path, _git_root)
                if has_staged_or_modified([md_regen_rel], cwd=_git_root):
                    commit_files(
                        f"chore: regenerate {new_version}.md from archived release metadata",
                        [md_regen_rel],
                        cwd=_git_root,
                    )
                    _track_release_commit(_state_path)
            save_step(_state_path, "RELEASE_FILE_FINALIZED")
            _completed.add("RELEASE_FILE_FINALIZED")

        if "RELEASE_FILE_FINALIZED" not in _completed:
            # No release file to archive (e.g. imperative invocation).
            # Mark so completeness is provable at the epilogue.
            save_step(_state_path, "RELEASE_FILE_FINALIZED")
            _completed.add("RELEASE_FILE_FINALIZED")

        # TAGGED guard: the tag is created on the CI-VERIFIED commit, which is
        # an ancestor of HEAD (the finalization commits land on top of it).
        _tag_already_exists = False
        if "TAGGED" in _completed:
            _tag_already_exists = True
            log("Skipping tag creation (already done)")
        else:
            _existing_tag = tag_exists_locally(tag)
            if _existing_tag:
                # Tag exists -- verify it points at the verified commit
                _tag_sha = run("git", ["rev-parse", f"refs/tags/{tag}^{{}}"]).strip()
                if _tag_sha == verified_sha:
                    _tag_already_exists = True
                    save_step(_state_path, "TAGGED")
                    _completed.add("TAGGED")
                    log("Skipping tag creation (tag already at the verified commit)")

        if not _tag_already_exists:
            # Create local git tag on the commit CI verified
            run("git", ["tag", tag, verified_sha])
            log(f"Tagged: {tag} -> {verified_sha[:12]} (CI-verified)")

            # Create companion tags (e.g. Go module proxy tags in releasable mode)
            if member_package_paths is not None:
                _companion_list = collect_companion_tags(
                    member_package_paths, monorepo_root, new_version, tag,
                    releasable_config_dir=_releasable_cfg_dir,
                )
                for ctag in _companion_list:
                    run("git", ["tag", ctag, verified_sha])
                    state.companion_tags.append(ctag)
                    log(f"Created Go companion tag: {ctag}")

            save_step(_state_path, "TAGGED")
            _completed.add("TAGGED")

        # PUSHED guard: publish the finalization commits (SHA-addressed, so a
        # ride-in that landed on the local branch is never swept along) and the
        # tags. The candidate itself is already on the remote.
        _push_already_done = False
        if "PUSHED" in _completed:
            _push_already_done = True
            branch_pushed = True
            log("Skipping push (already done)")
        else:
            # Check if branch push is needed via a LIVE remote comparison
            # (git ls-remote) rather than the local origin/<branch> tracking
            # ref. The tracking ref can be stale when the last fetch predates a
            # concurrent push, which would wrongly skip the push and leave the
            # branch behind the remote. This aligns with the tag_exists_on_remote
            # check just below, which is also a live ls-remote query.
            push_timeout = get_push_timeout(
                ctx.config, override=flags.get("push-timeout"),
            )
            _local_head = run("git", ["rev-parse", "HEAD"]).strip()
            _branch_needs_push = True
            try:
                _ls_out = run(
                    "git", ["ls-remote", "origin", f"refs/heads/{branch}"]
                ).strip()
                _remote_head = _ls_out.split()[0] if _ls_out else None
                if _remote_head and _local_head == _remote_head:
                    _branch_needs_push = False
                    branch_pushed = True
                    log("Skipping branch push (remote already at local HEAD)")
            except Exception:
                pass  # Remote branch might not exist yet

            _guard_foreign_commits(
                _pin_sha, _state_path, cwd=_git_root, phase="final push",
            )
            if _branch_needs_push:
                push_if_needed(
                    branch, config=ctx.config, cwd=project_dir, sha=_local_head,
                )
                branch_pushed = True

            # Check if tag push is needed. Commit-aware: verify EVERY release
            # tag (primary + companions) that is already on the remote points
            # at the same commit as the local tag. A divergent remote tag or an
            # inconclusive remote probe is a hard error (resolve_tag_push_plan);
            # an all-present-matching state is an idempotent skip; a mixed
            # (some absent) state pushes all tags, and git no-ops the identical
            # refs. This replaces the old bare tag_exists_on_remote skip that
            # silently swallowed ls-remote failures and pushed regardless of the
            # remote tag's commit.
            _all_tags = [tag] + list(state.companion_tags)
            if resolve_tag_push_plan(_all_tags):
                import subprocess as _subprocess
                try:
                    run("git", ["push", "--no-verify", "origin", tag] + state.companion_tags, timeout=push_timeout)
                except _subprocess.TimeoutExpired as _e:
                    from ...errors import GitError
                    raise GitError(
                        f"Tag push timed out after {push_timeout}s — remote "
                        f"state may be inconsistent. Check with: "
                        f"git ls-remote --tags origin {tag}"
                    ) from _e
            else:
                log(
                    "Skipping tag push (all release tags already on remote at "
                    "matching commits)"
                )

            log(f"Pushed to origin/{branch}")
            save_step(_state_path, "PUSHED")
            _completed.add("PUSHED")
    except ForeignCommitError as e:
        # A concurrent session's commits rode onto the branch mid-release.
        # Never roll back: those commits are exactly what must be preserved
        # (the rollback guard refuses the same thing from the other side).
        # Nothing was tagged or released, so the version survives.
        print(f"Error: {e}", file=sys.stderr)
        raise
    except ReleaseCIError as e:
        # CI did not pass on the pushed candidate. The candidate commit is on
        # the remote, so there is nothing to roll back; and no tag, GitHub
        # Release or finalized changelog was created, so there is nothing to
        # clean up. State is preserved for `rlsbl release resume`.
        print(f"Error: {e}", file=sys.stderr)
        raise
    except ReleaseAbortError as e:
        if _is_resumable_failure(e, branch_pushed, candidate_push_attempted,
                                 _completed):
            # Post-candidate-push failure: canonical resumable state. Preserve
            # everything and record a failed marker instead of rolling back
            # (which would diverge from the already-published candidate).
            # A recovered tag-push retry falls through into the post-push
            # steps below; otherwise re-raise to abort with resume guidance.
            if not _handle_resumable_push_failure(e):
                raise
            # Recovered: the retry cleared the outstanding push. Fall through
            # (no rollback) so the post-push steps below the try/except run.
        else:
            # Pre-push failure -- safe to roll back locally,
            # but only if no foreign commits or dirty files would be destroyed.
            _guard_rollback(pre_release_sha, _state_path)
            run("git", ["reset", "--hard", pre_release_sha])
            # State file is useless after local rollback -- clean it up.
            from ...release_file import get_releases_dir as _get_releases_dir
            _cleanup_release_artifacts(
                project_dir, new_version, changes_dir=state.changes_dir,
                releases_dir=_get_releases_dir(project_dir, releasable_dir=_releasable_cfg_dir),
            )
            clear_release_state(_state_path)
            print(str(e), file=sys.stderr)
            print(
                f"Local state has been rolled back to {pre_release_sha[:10]}.",
                file=sys.stderr,
            )
            _warn_rollback_residuals()
            raise
    except Exception as e:
        if _is_resumable_failure(e, branch_pushed, candidate_push_attempted,
                                 _completed):
            # Post-candidate-push failure (push failed / timed out): canonical
            # resumable state. Preserve everything and record a failed
            # marker instead of rolling back. A recovered tag-push
            # retry falls through into the post-push steps below; otherwise
            # re-raise to abort with resume guidance.
            if not _handle_resumable_push_failure(e):
                raise
            # Recovered: the retry cleared the outstanding push. Fall through
            # (no rollback) so the post-push steps below the try/except run.
        else:
            # Pre-push failure -- safe to roll back locally,
            # but only if no foreign commits or dirty files would be destroyed.
            _guard_rollback(pre_release_sha, _state_path)
            # Delete tag (may not exist yet) and reset commits so the working
            # tree looks like it did before the release attempt.
            try:
                run("git", ["tag", "-d", tag])
            except Exception:
                pass
            # Clean up companion tags (best-effort)
            for ctag in state.companion_tags:
                try:
                    run("git", ["tag", "-d", ctag])
                except Exception:
                    pass
            run("git", ["reset", "--hard", pre_release_sha])
            # State file is useless after local rollback -- clean it up.
            from ...release_file import get_releases_dir as _get_releases_dir
            _cleanup_release_artifacts(
                project_dir, new_version, changes_dir=state.changes_dir,
                releases_dir=_get_releases_dir(project_dir, releasable_dir=_releasable_cfg_dir),
            )
            clear_release_state(_state_path)
            if hasattr(e, 'stderr') and e.stderr:
                print(f"Command error: {e.stderr.strip()}", file=sys.stderr)
            print(
                f"Error: release failed. Local state has been rolled back to {pre_release_sha[:10]}.",
                file=sys.stderr,
            )
            print(
                "No push happened (the failure occurred before the release "
                "candidate was pushed), so nothing on the remote needs fixing. "
                "Address the error above and re-run:\n"
                "  rlsbl release run",
                file=sys.stderr,
            )
            _warn_rollback_residuals()
            raise

    # The CI-verified commit: the tag target, the publish gate's subject, and
    # the SHA the post-release watch follows. It is stable across post-release
    # hooks that create further commits. It is normally already resolved above;
    # a recovered post-push failure can fall through here without it, and the
    # state file's record (or, failing that, the tag's own commit -- the tag is
    # only ever created FROM the verified SHA) supplies it. Never HEAD: this
    # SHA becomes the publish gate's rlsbl-ci-sha marker, and a wrong marker
    # points the gate at a commit CI never judged.
    if not verified_sha:
        verified_sha = (
            (load_release_state(_state_path) or {}).get("candidate_sha") or ""
        ).strip()
    if not verified_sha:
        try:
            verified_sha = run(
                "git", ["rev-parse", f"refs/tags/{tag}^{{}}"],
            ).strip()
        except Exception:
            verified_sha = ""
    if not verified_sha:
        raise UnverifiedCandidateError(
            f"the CI-verified commit for {new_version} could not be "
            f"established after the mutating phase, so the GitHub Release "
            f"would carry no (or a wrong) rlsbl-ci-sha marker and the publish "
            f"gate would judge the wrong commit. Run `rlsbl release resume` "
            f"again once {tag} exists locally, or roll back with "
            f"`rlsbl release undo`."
        )
    pushed_sha = verified_sha

    # GITHUB_RELEASE guard: skip if the release already exists
    # Create GitHub Release using a temp notes file
    # Notes file cleanup is deferred until after subtree publishing (which reuses it)
    notes_file = f".rlsbl-notes-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    release_created = True
    _gh_release_already_exists = False
    if "GITHUB_RELEASE" in _completed:
        _gh_release_already_exists = True
        log("Skipping GitHub Release creation (already done)")
    else:
        try:
            run_gh(["release", "view", tag], config=ctx.config)
            _gh_release_already_exists = True
            save_step(_state_path, "GITHUB_RELEASE")
            _completed.add("GITHUB_RELEASE")
            log(f"Skipping GitHub Release creation (release {tag} already exists)")
        except Exception:
            pass  # Release doesn't exist yet -- proceed with creation

    # The machine-parseable CI-SHA marker tells the publish gate exactly which
    # commit CI ran on: the CI-verified candidate, which is also the tag's
    # commit. Under main-as-candidate ordering CI has ALREADY concluded green
    # on it before this Release exists, so the gate confirms rather than waits.
    # The notes file is written UNCONDITIONALLY -- the subtree mirror release
    # reuses it, and a pre-existing Release gets the marker reconciled in below.
    _ci_sha = pushed_sha.strip()
    _ci_marker = f"<!-- rlsbl-ci-sha: {_ci_sha} -->"
    notes_body = (changelog_entry or "").rstrip("\n")
    notes_body = f"{notes_body}\n\n{_ci_marker}\n"
    with effects.open_write(writing_file, "w", encoding="utf-8") as f:
        f.write(notes_body)
    effects.rename(writing_file, notes_file)

    try:
        if _gh_release_already_exists:
            try:
                _reconcile_ci_sha_marker(
                    tag, _ci_marker, notes_file, config=ctx.config, log=log,
                )
            except CiShaMarkerError as e:
                from ...errors import PostReleaseError
                save_step_failure(_state_path, "GITHUB_RELEASE", str(e))
                raise PostReleaseError(str(e)) from e
        else:
            # Retry gh release create with race-condition detection.
            # GitHub API can return an error even when the release was actually created,
            # so after each failure we check whether the release exists before retrying.
            gh_release_args = ["release", "create", tag, "--title", tag, "--notes-file", notes_file]
            # Mark pre-release versions as GitHub pre-releases
            if "-" in new_version:
                gh_release_args.append("--prerelease")
            gh_release_succeeded = False
            for attempt in range(2):
                try:
                    run_gh(gh_release_args, config=ctx.config)
                    gh_release_succeeded = True
                    log(f"Created GitHub Release: {tag}")
                    break
                except Exception as e:
                    if hasattr(e, 'stderr') and e.stderr:
                        print(f"Command error: {e.stderr.strip()}", file=sys.stderr)
                    # Check if the release was created despite the error (race condition)
                    try:
                        run_gh(["release", "view", tag], config=ctx.config)
                        gh_release_succeeded = True
                        log(f"GitHub Release created (confirmed via view): {tag}")
                        break
                    except Exception:
                        pass  # Release truly doesn't exist; retry or fail

            if gh_release_succeeded:
                save_step(_state_path, "GITHUB_RELEASE")
                _completed.add("GITHUB_RELEASE")
            else:
                release_created = False
                save_step_failure(
                    _state_path, "GITHUB_RELEASE",
                    f"GitHub Release creation failed for {tag}",
                )
                # Point at the resolved changes dir (releasable dir in
                # releasable mode, .rlsbl/changes/ otherwise), relative to
                # the CWD the release ran from so the hint is pasteable.
                _notes_base = changes_dir or os.path.join(project_dir, ".rlsbl", "changes")
                notes_path = os.path.relpath(os.path.join(_notes_base, f"{new_version}.md"))
                print(
                    f"Error: GitHub Release creation failed for {tag}. "
                    f"The tag and commit are on the remote.\n"
                    f"  To create the release: gh release create {tag} --title {tag} --notes-file {notes_path}\n"
                    f"  To roll back: rlsbl release undo",
                    file=sys.stderr,
                )

        # Subtree publishing for monorepo projects with subtree_remote configured
        if release_created and monorepo_name and monorepo_project_path:
            try:
                projects = load_workspace(monorepo_root)
                proj_dict = None
                for p in projects:
                    if p["name"] == monorepo_name:
                        proj_dict = p
                        break
                subtree_remote = proj_dict.get("subtree_remote") if proj_dict else None
            except Exception as e:
                from ...utils import warn_exception
                warn_exception("could not load workspace for subtree publishing", e)
                subtree_remote = None

            if subtree_remote:
                validate_subtree_remote_ssh_host(subtree_remote, str(ctx.project_root))
                plain_tag = target.tag_format(new_version)
                log(f"Publishing subtree to {subtree_remote}...")
                _subtree_pushed = False
                try:
                    run("git", ["subtree", "split", f"--prefix={monorepo_project_path}", "-b", "_rlsbl-subtree-tmp"])
                    run("git", ["push", "--no-verify", subtree_remote, f"_rlsbl-subtree-tmp:refs/tags/{plain_tag}"])
                    run("git", ["push", "--no-verify", subtree_remote, "_rlsbl-subtree-tmp:refs/heads/main"])
                    log(f"Subtree published: {plain_tag} -> {subtree_remote}")
                    _subtree_pushed = True
                except Exception as e:
                    print(f"Warning: subtree push failed: {e}", file=sys.stderr)
                    save_step_failure(
                        _state_path, "SUBTREE_PUBLISHED",
                        f"subtree push to {subtree_remote} failed: {e}",
                    )
                finally:
                    try:
                        run("git", ["branch", "-D", "_rlsbl-subtree-tmp"])
                    except Exception:
                        pass
                if _subtree_pushed:
                    save_step(_state_path, "SUBTREE_PUBLISHED")
                    _completed.add("SUBTREE_PUBLISHED")

                # Create GitHub Release on the mirror repo. Non-fatal in the
                # sense that the primary release is already published and
                # nothing is rolled back -- the failure marker still makes the
                # run exit nonzero and stay resumable (see the epilogue).
                try:
                    # Unscoped: --repo names the mirror explicitly, so this
                    # must NOT inherit the current project's GH_REPO.
                    run_gh_unscoped(["release", "create", plain_tag,
                                     "--repo", subtree_remote,
                                     "--title", plain_tag,
                                     "--notes-file", notes_file])
                    log(f"Created mirror GitHub Release: {plain_tag} on {subtree_remote}")
                except Exception as e:
                    print(f"Warning: mirror GitHub Release failed: {e}", file=sys.stderr)
                    save_step_failure(
                        _state_path, "MIRROR_RELEASED",
                        f"mirror GitHub Release {plain_tag} on "
                        f"{subtree_remote} failed: {e}",
                    )
                else:
                    save_step(_state_path, "MIRROR_RELEASED")
                    _completed.add("MIRROR_RELEASED")
    finally:
        # Clean up temp files after both main and mirror releases
        for tmp in (notes_file, writing_file):
            if os.path.exists(tmp):
                effects.remove(tmp)

    # Subtree/mirror mark-up: the two steps above only run for a monorepo
    # project that declares a subtree_remote. Everywhere else they are
    # trivially done, and the completeness check below demands a marker for
    # every canonical step. An existing marker (success OR failure) is never
    # overwritten -- a resume re-attempts the failed push above and clears its
    # own marker on success.
    _subtree_state = load_release_state(_state_path) or {}
    _subtree_marked = (set(_subtree_state.get("completed_steps") or [])
                       | set(_subtree_state.get("failed_steps") or {}))
    for _mirror_step in ("SUBTREE_PUBLISHED", "MIRROR_RELEASED"):
        if _mirror_step not in _subtree_marked:
            save_step(_state_path, _mirror_step)
            _completed.add(_mirror_step)

    # ---- Post-release phase ----
    # Every step below is tracked in the state file. Success markers gate
    # resume-skip; failure markers feed the completion epilogue. Asset upload
    # and pipeline publish failures are FATAL (they abort right here, state
    # preserved, resumable); deploy / post-hooks / snapshot failures are
    # non-fatal -- the release is not rolled back and the remaining steps
    # still run -- but their markers make the epilogue exit nonzero and keep
    # the state file, so no failed step is ever reported as success.

    # Upload release assets for pipelines with assets/custom_assets config.
    # In releasable mode, iterate each publishing member (publish_mode != "none") and
    # upload assets from each member's directory with member-prefixed names.
    if release_created:
        if "ASSETS_UPLOADED" in _completed:
            log("Skipping asset upload (already done)")
        else:
            try:
                if releasable_name and member_package_paths and monorepo_root:
                    from ...member_context import resolve_member_context as _rmc_asset
                    from .publish import _upload_assets_for_config

                    for _a_pkg_path in member_package_paths:
                        _a_abs_pkg = os.path.join(str(monorepo_root), _a_pkg_path)
                        if not os.path.isdir(_a_abs_pkg):
                            continue
                        _a_member = _rmc_asset(
                            _a_abs_pkg, releasable_config_dir=_releasable_cfg_dir,
                        )
                        if _a_member.publish_mode == "none":
                            continue
                        # Use the member name (last path component) as prefix
                        _a_member_name = os.path.basename(_a_pkg_path.rstrip("/"))
                        _upload_assets_for_config(
                            tag, new_version, log, flags,
                            _a_member.config, _a_abs_pkg, ctx,
                            member_name=_a_member_name,
                        )
                else:
                    upload_release_assets(tag, new_version, log, flags, ctx=ctx)
            except (ReleaseValidationError, HookError) as e:
                from ...errors import PostReleaseError
                save_step_failure(_state_path, "ASSETS_UPLOADED", str(e))
                raise PostReleaseError(str(e)) from e
            save_step(_state_path, "ASSETS_UPLOADED")
            _completed.add("ASSETS_UPLOADED")

    # Publish step: skip when publish_mode is "none" (suppressed -- no
    # registry publishing). Publish failures are FATAL: for `local: true`
    # pipelines this IS the publish, so downgrading a failure to a warning
    # would silently ship a release that was never published.
    from ...config import suppresses_publish
    is_private = suppresses_publish(ctx.config)
    if "PIPELINES_PUBLISHED" in _completed:
        log("Skipping pipeline publish (already done)")
    else:
        if not is_private:
            if releasable_name and member_package_paths and monorepo_root:
                # Per-member publish: each publishing member with pipelines
                # publishes from its own directory at the shared version.
                # Resume support: state tracks published_members list so
                # completed members are skipped on retry.
                _existing_state_pub = load_release_state(_state_path) or {}
                _already_published = set(
                    _existing_state_pub.get("published_members", [])
                )
                _published_members = list(_already_published)

                from ...member_context import resolve_member_context as _resolve_mc

                for pkg_path in member_package_paths:
                    abs_pkg = os.path.join(str(monorepo_root), pkg_path)
                    if not os.path.isdir(abs_pkg):
                        continue

                    member = _resolve_mc(
                        abs_pkg, releasable_config_dir=_releasable_cfg_dir,
                    )

                    if member.publish_mode == "none":
                        log(f"  {pkg_path}: skipped (publish_mode none)")
                        continue

                    member_pipelines = load_pipelines(member.config)
                    if not member_pipelines:
                        log(f"  {pkg_path}: no pipelines, not published")
                        continue

                    if pkg_path in _already_published:
                        log(f"  {pkg_path}: skipped (already published)")
                        continue

                    for pl_name, pl in member_pipelines.items():
                        try:
                            pl.publish(abs_pkg, new_version, ctx=ctx)
                        except Exception as e:
                            from ...errors import PostReleaseError
                            # Save partial progress so resume can skip
                            # already-published members.
                            _pub_state = load_release_state(_state_path) or {}
                            _pub_state["published_members"] = _published_members
                            save_release_state(_state_path, _pub_state)
                            save_step_failure(
                                _state_path, "PIPELINES_PUBLISHED",
                                f"member '{pkg_path}' pipeline '{pl_name}': {e}",
                            )
                            raise PostReleaseError(
                                f"member '{pkg_path}' pipeline '{pl_name}' "
                                f"publish failed: {e}. "
                                f"Release state has been preserved; fix the "
                                f"issue and run `rlsbl release resume` to "
                                f"re-attempt the publish."
                            ) from e

                    _published_members.append(pkg_path)
                    log(f"  {pkg_path}: published ({', '.join(member_pipelines)})")

                # Persist final published_members list in state
                _pub_state_final = load_release_state(_state_path) or {}
                _pub_state_final["published_members"] = _published_members
                save_release_state(_state_path, _pub_state_final)
            else:
                # Standalone / implicit mode: each pipeline publishes from its
                # own linked target's path (multi-target subdir support), with
                # resume tracking. See _publish_standalone_pipelines.
                _publish_standalone_pipelines(
                    ctx, target_paths, primary_path, new_version,
                    _state_path, log,
                )

        save_step(_state_path, "PIPELINES_PUBLISHED")
        _completed.add("PIPELINES_PUBLISHED")

    # Deploy phase (after publish, before post-release hook). Non-fatal:
    # a failure is recorded as a failure marker and named in the summary.
    if "DEPLOYED" in _completed:
        log("Skipping deploy (already done)")
    else:
        _deploy_failure = None
        deploy_targets, deploy_errors = read_deploy_config(ctx.config)
        if deploy_targets and not deploy_errors:
            current_branch = get_current_branch(cwd=project_dir)
            for target_config in deploy_targets:
                print(f"\nDeploying to {target_config['name']}...")
                result = deploy_target(target_config, current_branch)
                if result.success:
                    print(f"  Deploy to {result.target_name}: {result.message}")
                else:
                    print(f"  Deploy to {result.target_name} FAILED: {result.message}", file=sys.stderr)
                    if result.rolled_back:
                        print("  Rollback was executed.", file=sys.stderr)
                    print(f"  Retry with: rlsbl deploy {result.target_name}", file=sys.stderr)
                    _deploy_failure = f"deploy to {result.target_name} failed: {result.message}"
                    break  # Stop at first failure
        elif deploy_errors:
            print("Warning: deploy config has errors, skipping deploy:", file=sys.stderr)
            for err in deploy_errors:
                print(f"  {err}", file=sys.stderr)
            _deploy_failure = "deploy config errors: " + "; ".join(deploy_errors)
        # If no deploy targets configured, the step is trivially done.
        if _deploy_failure is not None:
            save_step_failure(_state_path, "DEPLOYED", _deploy_failure)
        else:
            save_step(_state_path, "DEPLOYED")
            _completed.add("DEPLOYED")

    # Ecosystem tagging: add GitHub topic after release is created
    if should_tag(flags, ctx.config):
        ensure_github_topic(quiet=quiet)

    # Run post-release hook if present (non-fatal: release is already complete)
    _use_releasable_hooks = releasable_name and monorepo_root and member_package_paths
    hook_timeout = get_hook_timeout(ctx.config, override=flags.get("hook-timeout"))
    _post_hook_error = None

    if "POST_HOOKS_RUN" in _completed:
        log("Skipping post-release hooks (already done)")
    elif _use_releasable_hooks:
        # Multi-level post-release: releasable first, then per-package
        from .hooks import build_hook_env, run_releasable_hooks
        from ...workspace import members_of, get_releasable_dir
        from . import read_json_config

        _ws_projects = load_workspace(str(monorepo_root))
        _member_projs = members_of(releasable_name, _ws_projects)
        _member_tuples = []
        for mp in _member_projs:
            mp_name = mp.name if hasattr(mp, "name") else mp["name"]
            mp_path = mp.path if hasattr(mp, "path") else mp["path"]
            mp_dir = os.path.join(str(monorepo_root), mp_path)
            _member_tuples.append((mp_name, mp_dir))

        # Load releasable-level config and per-package configs for hook dispatch
        _rel_cfg_dir = get_releasable_dir(str(monorepo_root), releasable_name)
        _releasable_config = read_json_config(os.path.join(_rel_cfg_dir, "config.json"))
        _package_configs = {}
        for _mp_name, _mp_dir in _member_tuples:
            _pkg_cfg = read_json_config(os.path.join(_mp_dir, ".rlsbl", "config.json"))
            if _pkg_cfg:
                _package_configs[_mp_name] = _pkg_cfg

        hook_env = build_hook_env(
            os.environ.copy(),
            new_version,
            bump_type=bump_type or "",
            prev_version=current_version or "",
            description=description or "",
        )

        try:
            run_releasable_hooks(
                "post-release", monorepo_root, releasable_name,
                _member_tuples, hook_env, hook_timeout, log,
                project_dir=project_dir,
                releasable_config=_releasable_config,
                package_configs=_package_configs,
            )
        except Exception as e:
            # Post-release hooks are non-fatal
            print(f"Warning: post-release hook failed: {e}", file=sys.stderr)
            _post_hook_error = str(e)
    else:
        from .hooks import build_hook_env, run_release_hook

        post_release_script = os.path.join(project_dir, ".rlsbl", "hooks", "post-release.sh")
        hook_env = build_hook_env(
            os.environ.copy(),
            new_version,
            bump_type=bump_type or "",
            prev_version=current_version or "",
            description=description or "",
        )
        log("Running post-release hook...")
        try:
            run_release_hook(
                "post-release", post_release_script, project_dir,
                hook_env, hook_timeout, config=ctx.config,
            )
        except Exception as e:
            # Post-release hooks are non-fatal
            print(f"Warning: post-release hook failed: {e}", file=sys.stderr)
            _post_hook_error = str(e)

    if "POST_HOOKS_RUN" not in _completed:
        if _post_hook_error is not None:
            save_step_failure(_state_path, "POST_HOOKS_RUN", _post_hook_error)
        else:
            save_step(_state_path, "POST_HOOKS_RUN")
            _completed.add("POST_HOOKS_RUN")

    # Post-tag snapshot fallback: the normal path regenerates the snapshot
    # BEFORE tagging (see the pre-tag SNAPSHOT_REGENERATED block). This block
    # only fires when that slot was forfeited -- i.e. an old-ordering state
    # file (written before the snapshot moved ahead of TAGGED) is resumed
    # after the tag was already pushed. Regenerating here is post-hoc and
    # therefore non-fatal (the release is already on the remote; it cannot be
    # rolled back), so a failure is recorded and named rather than aborting.
    if "SNAPSHOT_REGENERATED" in _completed:
        pass  # already regenerated pre-tag (normal path) or in a prior resume
    elif monorepo_name:
        try:
            from ...snapshot import generate_snapshot, write_snapshot
            from ...workspace_graph import WorkspaceGraph

            projects = load_workspace(monorepo_root)
            graph = WorkspaceGraph(monorepo_root, projects)
            snapshot = generate_snapshot(monorepo_root, projects, graph)
            rel_path = write_snapshot(monorepo_root, snapshot)
            did_commit = commit_files_if_changed("snapshot", [rel_path], skip_message="Snapshot unchanged.", autogenerated=True, cwd=monorepo_root)
            if did_commit:
                _track_release_commit(_state_path)
            log(f"Regenerated monorepo snapshot (post-hoc): {rel_path}")
        except Exception as e:
            print(f"Warning: snapshot regeneration failed: {e}", file=sys.stderr)
            save_step_failure(_state_path, "SNAPSHOT_REGENERATED", str(e))
        else:
            save_step(_state_path, "SNAPSHOT_REGENERATED")
            _completed.add("SNAPSHOT_REGENERATED")
    else:
        # Not a monorepo: nothing to regenerate, the step is trivially done.
        save_step(_state_path, "SNAPSHOT_REGENERATED")
        _completed.add("SNAPSHOT_REGENERATED")

    # Advisory: constraint propagation
    if monorepo_name:
        _print_stale_dep_advisory(monorepo_name, new_version, monorepo_root=monorepo_root)

    # If GitHub Release creation failed, preserve the state file for resume
    # and raise PostReleaseError BEFORE clearing state.
    if not release_created:
        from ...errors import PostReleaseError
        raise PostReleaseError(f"GitHub Release creation failed for {tag}")

    # Single decision point for the whole non-fatal family (deploy, post
    # hooks, subtree push, mirror release, post-hoc snapshot). "Non-fatal"
    # means the release is not rolled back and stays resumable -- it never
    # meant "exit 0". Any failure marker at this point is a step that ran and
    # failed, so the run reports it through its exit code and KEEPS the state
    # file: `rlsbl release resume` re-attempts the failed steps, and a resume
    # that succeeds clears the markers (save_step drops them) and falls
    # through to the success epilogue below.
    _final_state = load_release_state(_state_path) or {}
    _failed_final = get_failed_steps(_final_state)
    if _failed_final:
        print(
            f"\nRelease {new_version} completed with failed steps:",
            file=sys.stderr,
        )
        for _step, _msg in _failed_final.items():
            print(f"  {_step}: {_msg}", file=sys.stderr)
        from ...errors import PostReleaseError
        raise PostReleaseError(
            f"release {new_version} finished with {len(_failed_final)} failed "
            f"step(s): {', '.join(sorted(_failed_final))}. The release state "
            f"has been preserved; fix the cause and run "
            f"`rlsbl release resume` to re-attempt them."
        )

    # Provable completeness: every canonical step must carry a success or
    # failure marker before the state file is cleared. A missing marker
    # here is an internal bug (a step ran without recording itself).
    _missing_final = get_missing_steps(_final_state)
    if _missing_final:
        raise RuntimeError(
            "internal error: release reached the success epilogue with "
            f"unmarked steps: {', '.join(_missing_final)}"
        )

    # Success epilogue: clear state and announce BEFORE watch, because
    # watch_run_cmd() calls sys.exit() and would skip cleanup.
    clear_release_state(_state_path)

    log(f"\nRelease {new_version} complete!")

    # Watch CI or print hint (uses SHA captured before post-release hooks).
    # In batch-mode, the batch orchestrator handles watch after all packages
    # are released, so skip both the watch call and the hint here.
    # Dry-run returns earlier (no push happens), but guard defensively.
    if not flags.get("dry-run", False) and not flags.get("batch-mode", False):
        if flags.get("watch"):
            log(f"Watching CI for {pushed_sha}...")
            from ..watch import run_cmd as watch_run_cmd
            # watch_run_cmd exits with CI's verdict. Catch it so the outcome
            # check below can run: a green CI means the publish workflow
            # concluded, which is exactly when asking the registry is
            # meaningful. A red one is answered already -- reporting missing
            # artifacts on top of a failed publish adds nothing.
            _watch_code = 0
            try:
                watch_run_cmd(None, [pushed_sha], {})
            except SystemExit as _we:
                _watch_code = _we.code or 0
            if _watch_code:
                sys.exit(_watch_code)
            if not is_private:
                _verify_publication(
                    state.resolved_targets, new_version, tag, ctx, log=log,
                )
        else:
            _announce_unverified_publication(pushed_sha, log)
