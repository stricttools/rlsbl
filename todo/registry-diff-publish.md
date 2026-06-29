# Registry-diff publish mode

## Problem

When a version bump lands on main outside the `rlsbl release run` ceremony (e.g., an external contributor's PR bumps `version` in `pyproject.toml`), the local version sits ahead of the registry with no mechanism to publish it. Someone must retroactively create a release file, write changelog entries, and run the full release flow for a version decision that was already made in the PR.

`rlsbl status --registry` can detect this drift, but it's diagnostic only -- it doesn't act on it.

## Proposed solution

An opt-in publish mode where rlsbl detects packages with local versions ahead of their registry and publishes them directly, bypassing the release file and changelog ceremony.

### How it would work

- A CI workflow trigger (scaffolded, opt-in via config) or a local command like `rlsbl release sync` runs after merge to main.
- For each configured target, queries the registry (npm, PyPI, Go proxy, crates.io) using the existing `registry.py` infrastructure.
- If `local > published`, creates a tag, pushes, creates a GitHub Release, and triggers the publish workflow.
- If `local == published`, no-op.
- Changelog enforcement is skipped for registry-diff publishes -- there's no JSONL entry for externally-bumped versions and requiring one would defeat the purpose.

### Design questions

1. **Opt-in gate.** This must be explicitly enabled in config (e.g., `registry_sync = true`). It should never run by default -- it's a fundamentally different release model.

2. **Changelog bypass.** Registry-diff publishes skip changelog validation. The GitHub Release body could be auto-generated from the commit range between the previous tag and the new version commit, or it could simply say "Version bump detected from registry drift."

3. **CI vs. local.** This is most useful as a CI workflow that runs on push to main (like AG-UI's model). A local command is also useful for manual catch-up. Both should be supported.

4. **Monorepo scope.** In monorepos, the diff should be per-package -- only packages ahead of their registry get published, not the whole workspace.

5. **Interaction with PR mode.** If `release.mode = "pr"`, registry-diff publishes should probably bypass PR mode and publish directly, since the version bump was already reviewed in the original PR.

6. **Pre-release versions.** Registry-diff should respect pre-release semantics -- a local `1.0.0-alpha.0` ahead of published `0.9.0` should publish with `--prerelease` and the correct dist-tag.

## Tension with rlsbl's philosophy

This is implicit publishing based on detected state rather than declared intent. It conflicts with the explicit-declaration, file-driven design. The mitigation is making it strictly opt-in, clearly separated from the normal release flow, and documented as a different operational model for projects that accept external version bumps.

## Affected files

- `rlsbl/registry.py` -- already has query functions, needs a compare-and-act layer
- `rlsbl/commands/` -- new `release sync` command or equivalent
- `rlsbl/templates/shared/.github/workflows/` -- new scaffold template for CI trigger
- `rlsbl/commands/init_cmd.py` -- scaffold integration gated on config
- `rlsbl/commands/release/execute.py` -- publish path that skips changelog validation

## Effort

Medium-large. The registry query infrastructure exists. The main work is the new publish path that bypasses changelog, the CI template, and the monorepo per-package scoping.
