# Structural push prevention: the push-URL sentinel

## Context

The fleet rule is that releases are the only thing that ever pushes; manual
pushes are banned. Enforcement today is a pre-push hook plus written
instructions — a rule, not a mechanism. Agents keep finding new spellings the
ban did not enumerate, and every new spelling needs a new guardrail. Per the
standing principle (accumulating guardrails against one bug class call for a
structural change), the push capability itself should be removed from
day-to-day sessions rather than policed.

## Problem

Every managed repo's `origin` carries a working push URL, and any session can
`git push` to it. The hook is bypassable (`--no-verify`, stale hooks), and
banning command spellings one at a time is a losing game.

## Solution: neuter the push URL; the release flow supplies the real one

git keeps separate fetch and push URLs per remote. In every managed repo:

    git remote set-url --push origin push-disabled://use-rlsbl-release

Fetch is untouched. Any push — any flags, any refspec, hook or no hook —
fails immediately trying to resolve the sentinel, and the error text itself
teaches the remedy (the sentinel string appears in git's own error message).
There is nothing to bypass: it is not a check before the push, it is the
absence of a destination.

rlsbl then owns both halves:

1. **Setting and verifying the sentinel.** Scaffold sets it; a check (project
   tag, and verified during release preflight) errors when a managed repo's
   push URL is not the sentinel, so a hand-restored URL is caught at the next
   check run.
2. **Pushing by explicit URL.** Every push the release flow performs — the
   candidate push, the finalization push, tag pushes, scrub's force-push,
   mirror's force-with-lease, reconcile's re-pushes — supplies the real URL
   explicitly (resolved from the repo's fetch URL or gh) instead of relying
   on the remote's push URL. The push capability lives only inside the tool's
   own push steps.

## Honest limits

- A deliberate `git remote set-url --push` restores the capability — the
  sentinel converts accidental/creative evasion into unambiguous config
  rewriting, which is the point; it does not achieve absolute prevention.
- An explicit push to a spelled-out URL (or over SSH with an ambient key)
  bypasses the sentinel. The companion measure — read-only ambient
  credentials for sessions, with the write credential held only by this
  tool's own configuration — closes that side and is filed where the session
  environment is owned. When that companion ships, this tool must resolve its
  write credential from its own configuration, never from the session
  environment.

## Affected

- scaffold (sentinel setup), the check registry (the new verification check)
- every push site in release run/resume, scrub, mirror, reconcile
- docs describing the push model

## Effort

Small-medium: one sentinel constant, one check, and a URL-resolution helper
threaded through the existing push sites.
