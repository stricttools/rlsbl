# Release-engine defect batch (campaign findings, 2026-08)

All reproduced live during the 2026-08 fix campaign; each independent, each needs red-green.

1. **`changelog generate` is destructive on archive-less versions**: silently drops released
   descriptions from CHANGELOG.md AND the read-only per-version .md, then auto-commits.
   Guard: refuse when any released version lacks releases/v{X}.toml; add a backfill command
   reading batch-*.toml as the offline source (111 archives were hand-backfilled fleet-wide).
2. **`release run --dry-run` false-aborts on released projects**: post-mutation observes
   return Unsettled → tag_exists_locally False → _abort_on_destroyed_tag fires with a
   confidently wrong diagnosis.
3. **Check-run name masking**: a sibling job hand-named like a matrix leg
   ("test (integration)") can satisfy skipped-supersession in the CI wait AND the publish
   gate. Pin or narrow.
4. **custom_assets defeat goreleaser idempotency**: pre-uploaded assets ⇒ count ≥1 ⇒
   goreleaser skipped ⇒ a release shipped ZERO platform binaries while Publish reported
   green.
5. **`release retry` scaffolds an unusable retry.toml** (ref="" hard error; the file is not
   written on the first run).
6. **`monorepo sync --dry-run` aborts** on an unsettled go-list observe.
7. **CI-source discovery gaps**: members with no CI source only WARN (two members shipped
   with zero CI coverage); root-member workflows not named ci.yml are invisible to
   discovery; `check`/`status` unusable from a path="." member (publish_mode abort); no
   check that every sub-package with a publish pipeline commits its lockfile.

## Effort

Small-to-medium per item.
