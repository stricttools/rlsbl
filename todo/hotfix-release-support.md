# Hotfix release support (comprehensive)

Supersedes todo/.obsolete/maintenance-release-mode.md, whose premises were
partially wrong: it treated the branch-gate skip and a tag-collision guard
as the whole feature ("small-medium"). Investigation showed the crux is
elsewhere — CI/publish gating — and corrected the collision claim. This
todo is the consolidated, corrected scope.

## Context

A consumer project follows a "one release at the very end of a long
roadmap" model: main accumulates months of unreleased work. A critical bug
in the currently-released version must ship as a hotfix from a branch cut
off the OLD release tag, while main has diverged far ahead and must never
reach a registry mid-roadmap.

## Corrected findings (from investigation)

1. **CI/publish gate blocker (the crux, missed by the original todo).**
   Scaffolded CI templates trigger only on `push: branches: [main]` — no
   tag trigger, no other branches. A hotfix released from a maintenance
   branch creates the tag and GitHub Release, but the publish gate polls
   check-runs for the release SHA, finds zero (CI never ran on that
   branch), and hard-fails after the grace window. **The hotfix never
   reaches the registry.** The feature is inert for CI-published targets
   without a CI-scaffold change. (Consumer-side interim workaround exists
   — hand-edit the hotfix branch's ci.yml to add its branch to the push
   trigger; edits survive scaffold merge — but that is per-incident
   paperwork, not a fix.)
2. **Changelog omission on main.** After a hotfix, main's CHANGELOG.md
   silently omits the hotfix version FOREVER unless the hotfix branch's
   changelog-finalize commit is merged/cherry-picked back. Nothing prompts
   this today.
3. **Tag collision: partially refuted, guard still warranted.** The
   computed-tag-exists check DOES fire pre-mutation today (an incidental
   `git fetch` brings the hotfix tag down first), but it is local-only and
   generic — and it is backstopped by a **silent skip of the tag-push at
   execute time**, which is the real anti-pattern (silent degradation).
4. **Branch gate (unchanged from original).** The dev-branch release path
   (rlsbl/commands/release/validate.py:404-452) requires main to be an
   ancestor of HEAD and hard-fails for a branch cut from an old tag
   (tests/test_dev_branch_release.py:281-296). The on-release-branch path
   (validate.py:381-402) never touches main, so adding the hotfix branch
   to `release_branches` works as a config workaround — with persistent
   config edits and no intent declaration.
5. **Monorepo batch releases can't even honor `release_branches` today**
   (validate_branch_and_remote called without config) — hotfix support
   there is a separate, larger problem.

## Scope — everything needed

### A. Maintenance-release mode (settled: option c)

Config authorizes + flag confirms intent: a `maintenance_branches` config
key (glob patterns) declares which branches MAY be maintenance-released,
and an explicit flag on `release run` confirms it per invocation. Matching
branches release in place: skip the --is-ancestor gate and the
return-to-main ff-merge (commands/release/__init__.py:1156-1170). Malformed
config hard-errors. Trust the glob (no ancestry assertion on the branch —
the tag-collision guard is the safety net). Standalone-only first cut;
monorepo batch flow explicitly out of scope (finding 5).

### B. CI trigger for the hotfix SHA (the crux — decide first)

The release SHA must have check-runs for the publish gate to pass.
Options: (i) scaffold CI to also trigger on `maintenance_branches` globs
(creates a config↔CI sync surface that can silently drift); (ii) add
`push: tags: ['v*']` to CI templates (robust, always the exact release
commit; cost: a duplicate CI run per normal release, fleet-wide); (iii)
require pushing the branch pre-release with a branch trigger. Investigator
leaned (ii) with (i) as fallback; a consumer's stated preference is also
(ii) — the sync-drift class of (i) is exactly what this tooling exists to
kill, and one duplicate CI run per release is cheap. Whichever option:
this is a fleet-wide CI semantics change and needs its own red-green
coverage (gate passes on a maintenance-branch release).

### C. Tag-collision guard as a hard error (both paths)

Before tagging, verify the computed tag does not exist on ANY ref, local
AND remote (explicit ls-remote, not incidental fetch), on the maintenance
path AND the normal path. Kill the silent tag-push skip at execute time —
replace with a hard error. No bypass flags.

### D. Post-hotfix reconciliation checklist

After a maintenance release, print an explicit merge-back checklist: the
cherry-pick command for the changelog-finalize commit (fixes finding 2),
the fix-commit forward-port, and a note that main's version files must
never compute a tag the hotfix already minted (the consumer-side rule "the
final roadmap release bumps minor/major, never patch" complements this).
Checklist only — no automated cross-branch mutation (worktree-safety).

## Open decisions (with recommendations)

- B's mechanism: (ii) tags trigger, with (i) as fallback — medium
  confidence, fleet-wide impact, decide before implementation.
- Whether the duplicate-CI-run cost of (ii) warrants a scaffold knob to
  opt out per project (lean no: knobs are escape hatches).

## Affected files

- rlsbl/commands/release/validate.py (branch gate; collision guard)
- rlsbl/commands/release/__init__.py (return-to-main skip; tag-push
  execute path with the silent skip; reconciliation checklist output)
- rlsbl/prepush_utils.py (_get_release_branches / maintenance_branches)
- CI workflow templates + scaffold three-way merge bases (trigger change)
- publish gate template (no change expected — it gates on check-runs for
  the release SHA, which B makes exist)
- config schema + docs
- tests/test_dev_branch_release.py (diverged-main case inverts under the
  mode) + new: end-to-end maintenance release incl. gate pass, collision
  hard error, silent-skip removal red-green

## Effort

Medium-large (revised from the original "small-medium"): A and C are the
small half; B is a fleet-wide CI semantics change with scaffold-template,
merge-base, and gate interactions; D is small.
