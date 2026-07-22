# First-class maintenance-release (hotfix) mode

## Context

A consumer project follows a "one release at the very end of a long
roadmap" model: main accumulates months of unreleased work. If a critical
bug lands in the currently-released version mid-roadmap, the fix must ship
from a branch cut off the OLD release tag while main has diverged far
ahead. No intermediate main state may reach a consumer.

## Problem

- The dev-branch release path (rlsbl/commands/release/validate.py:404-452)
  requires the release branch (main) to be an ancestor of HEAD
  (`git merge-base --is-ancestor`) and hard-fails with "cannot
  fast-forward" for a hotfix branch cut from an old tag (tested:
  tests/test_dev_branch_release.py:281-296, test_diverged_main_raises).
- A workaround exists: adding the hotfix branch to `release_branches` in
  .rlsbl/config.json takes the on-release-branch path
  (validate.py:381-402), which never touches main. Changelog range
  computation (git describe sees only ancestor tags — utils.py:155-182,
  changelog/resolve.py:80-90) and later main releases behave correctly.
- Two hazards the workaround leaves:
  1. CROSS-BRANCH TAG COLLISION: the next version is bumped from the
     branch's own target files, so main's later release can compute the
     same tag a hotfix already minted (e.g. both produce vX.Y.(Z+1));
     `git tag` then fails late in the flow. rlsbl neither detects nor
     prevents this.
  2. The `release_branches` edit is persistent config that must be kept
     off main and cleaned up manually.
- Note: `bump_type == "hotfix"` already exists (utils.py:624-625) but is
  purely a patch-increment with changelog exemptions — it has nothing to
  do with branch topology.

## Solutions

(a) `maintenance_branches` config key (glob patterns, e.g. "hotfix/*"):
matching branches release in place — skip the --is-ancestor gate and the
return-to-main ff-merge (commands/release/__init__.py:1156-1170) — plus a
TAG-COLLISION GUARD: before tagging, verify the computed tag does not
already exist on ANY ref (not just ancestors). Pros: declarative, no
per-incident config edits. Cons: new config surface.

(b) An explicit `--maintenance` flag on `release run` with the same
gate-skipping + collision guard. Pros: no persistent config;
per-invocation explicitness matches the mandatory-flags philosophy. Cons:
a flag that changes branch semantics per run.

(c) Both: the config key authorizes which branches MAY be
maintenance-released; the flag confirms intent per invocation. Most
aligned with the hard-constraints philosophy (an agent must both be
authorized and declare intent).

Independent of the chosen mode: the tag-collision guard is valuable on the
NORMAL release path too — refusing when the computed tag exists on any ref
costs nothing and catches the cross-branch collision from either side.

## Affected files

- rlsbl/commands/release/validate.py (branch gate, validate_branch_and_remote)
- rlsbl/commands/release/__init__.py (return-to-main ff-merge skip)
- rlsbl/prepush_utils.py (_get_release_branches)
- config schema + docs
- tests/test_dev_branch_release.py (the diverged-main case inverts under
  the new mode; red-green: release from a branch cut off an old tag while
  main is ahead)

## Effort

Small-medium. The on-release-branch path already does the right thing;
this formalizes access to it plus one new guard.
