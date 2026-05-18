# Post-release workflow gap

## Problem

Some work can only happen after a release succeeds (e.g., publishing an npm wrapper that delegates to `uvx <package>` — it needs the PyPI package to actually exist before it makes sense to publish). Currently rlsbl has no concept of a "post-release commit phase" that creates commits, covers them in changelog, and immediately cuts a follow-up release.

The result: the user does their post-release work, then has to remember to `rlsbl release patch` instead of `git push`. If they push manually (violating the "never push manually" rule), unreleased commits sit on main between the tag and HEAD with no guard or warning.

## Possible solutions

1. **`rlsbl release --chain`**: after the release completes, prompt for additional commits (or detect them) and immediately cut a patch release. Automates the "commit npm wrapper, release patch" flow.

2. **`rlsbl status` warning**: when unreleased commits exist on main after the last tag, `rlsbl status` should surface a prominent warning: "N commits ahead of v0.1.0 — run `rlsbl release` or investigate."

3. **Post-release hook with commit support**: the existing `post-release.sh` hook runs after the release but its commits aren't covered by the release's changelog. If the hook creates commits, rlsbl could auto-detect them and offer to cut a follow-up patch.

4. **Documentation only**: document the pattern ("if you need post-release work, commit it and immediately `rlsbl release patch`") without tool support.

## Context

Observed in a private project: PyPI package released via rlsbl, then an npm wrapper package (which delegates to `uvx a private project`) was created and needed publishing. The npm commit happened post-release and was manually pushed, violating the managed-push rule.
