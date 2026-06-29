# Release flow gaps

Areas where rlsbl's release flow has friction, missing capabilities, or design limitations worth addressing.

## 1. One-shot release flow is not idempotent

The release flow is a single-pass operation. If it fails after pushing but before creating the GitHub Release, you can't re-run `rlsbl release run` -- the release file has been consumed, the tag exists, and the version is already bumped. The only recovery path is `rlsbl release undo` followed by a full re-run from scratch. `release retry` covers the narrow case where the GitHub Release exists but CI didn't fire, but there's no general-purpose "resume from where it failed" capability.

A detection-based approach -- where each step checks whether its work is already done before executing -- would allow safe re-execution without requiring a full rollback-then-retry cycle. This would mean: if the tag exists and points to HEAD, skip tagging; if the GitHub Release exists, skip creation; if the version is already bumped, skip bumping. The release file consumption model (one-shot TOML) is the main obstacle.

## 2. No pre-release or canary channel support

`bump_version()` always produces clean `X.Y.Z` versions, explicitly stripping pre-release suffixes. There is no way to:

- Publish `1.2.3-alpha.0` to npm's `alpha` dist-tag
- Push `1.2.3a0` (PEP 440 pre-release) to PyPI
- Generate canary versions with unique suffixes (timestamp, branch name, commit SHA)
- Select npm dist-tags (`--tag alpha`, `--tag canary`, `--tag next`)
- Support PEP 440 markers (`.dev`, `a`, `b`, `rc`)

Projects that need to ship preview builds for testing before committing to a stable release must bypass rlsbl entirely and publish manually, which defeats the purpose of the tool.

This would require:

- A `prerelease` bump type (alongside patch/minor/major/hotfix)
- A `--preid` or equivalent for the pre-release identifier (alpha, beta, rc, canary)
- PEP 440 conversion logic for Python targets
- Dist-tag selection for npm targets
- Decision on whether pre-releases create GitHub Releases and git tags (probably not for canary, yes for alpha/beta/rc)

## 3. No PR-based release flow

The release flow is purely imperative: edit TOML, run CLI, it commits/tags/pushes directly. There is no intermediate review step. For solo developers and AI agents this is appropriate, but for teams that want a review checkpoint before shipping, rlsbl offers nothing.

A PR-based mode would:

- Create a `release/next` branch with the version bump commit
- Open a PR against main with the changelog as the body
- Let CI run against the release branch before merging
- Trigger the actual publish when the PR merges

This is a different release model, not a replacement for the imperative one. Both could coexist via a flag or config option.

## 4. No incremental release stacking

Monorepo batch releases require declaring all packages upfront in the release file before running the command. There is no "add one more package to the current release" workflow. If you realize mid-day that another package should be included, you must edit the release file and start over.

An incremental stacking model would allow running a command like `rlsbl monorepo release add --package mylib --bump patch` repeatedly, accumulating packages into a pending release, then shipping everything with `rlsbl monorepo release run`. This maps to the pattern where a team decides over the course of a day which packages to include.

Alternatively, this could be the PR-based flow from item 3 applied to monorepos -- each `release add` pushes to a release branch and updates the PR.

## 5. No AI-generated changelog descriptions

Changelog descriptions are written entirely by the operator (human or agent) when running `rlsbl changelog add`. The release description in `unreleased.toml` is also manually written. There is no automatic summarization of what changed -- no diff-to-prose generation, no model call.

For projects with many small commits, writing good changelog descriptions is manual labor. An optional `--ai` flag on `rlsbl changelog add` could generate a description from the commit diff, using an LLM API. This should be strictly opt-in (no implicit AI calls) and fail-soft (if the API is unavailable, fall back to requiring manual input).

Similarly, `rlsbl release init` could optionally pre-fill the release `description` field by summarizing the unreleased changelog entries.

## 6. No registry-diffing detection mode

rlsbl requires explicit declaration of what to release. If someone bumps a version in a `pyproject.toml` via a manual PR (e.g., an external contributor), rlsbl won't notice or publish it. There is no "discover what changed relative to the registry" capability.

A `rlsbl detect` or `rlsbl status --registry` command could compare local versions against npm/PyPI/crates.io and report which packages have local versions ahead of published versions. This wouldn't replace the explicit release flow but would surface version bumps that happened outside rlsbl's control.

## 7. High ceremony for trivial releases

Releasing a patch fix requires: creating a TOML file (`rlsbl release init`), editing it to set bump type and description, then running `rlsbl release run`. The file-driven approach is deliberate (forces the operator to think), but for trivial patches where the ceremony feels disproportionate, there is no quick path.

A `rlsbl release run --bump patch --description "Fix X"` shortcut that skips the TOML file would reduce friction for simple cases. The TOML file would remain for complex releases (include/exclude targets, context, etc.).

## 8. Requires local environment for release

The release flow requires a local machine with: Python 3.11+, rlsbl installed, `gh` authenticated, git push access, and (for local pipelines) registry credentials. There is no way to trigger a release from a browser, a phone, or a CI-only environment. This limits who on a team can cut releases and makes it impossible to release from environments where the full toolchain isn't available.

## 9. Limited Python build backend support

rlsbl standardized on `uv` for all Python projects. There is no `poetry build` or `poetry publish` path. Projects using poetry as their build system must either migrate to uv or bypass rlsbl for publishing. The `uv build` command may work for some poetry projects (since uv can read poetry's pyproject.toml), but this is fragile and untested.

More broadly, rlsbl doesn't branch on the Python build backend (hatchling vs. setuptools vs. flit vs. maturin) at the build/publish invocation level -- it always calls `uv build` and relies on the backend being compatible. This works for most backends since `uv build` delegates to the declared build-system, but there may be edge cases (maturin, for example, has its own build flow).

## Effort estimates

| Item | Effort | Impact |
|------|--------|--------|
| 1. Idempotent release flow | Large -- requires rethinking the release file lifecycle and adding per-step done-checks | High -- eliminates the most stressful failure mode |
| 2. Pre-release / canary | Large -- touches version computation, tag format, dist-tag selection, publish pipelines, and GitHub Release policy | Medium -- only matters for projects with preview consumers |
| 3. PR-based release flow | Large -- new mode with branch management, PR creation, merge-triggered publish | Medium -- only matters for team workflows |
| 4. Incremental stacking | Medium -- needs a persistent "pending release" state that accumulates across invocations | Medium -- only matters for large monorepos |
| 5. AI-generated descriptions | Small -- API call on `changelog add`, optional flag, fail-soft | Low -- convenience, not correctness |
| 6. Registry-diffing detection | Medium -- needs per-registry query logic for npm, PyPI, crates.io, etc. | Low -- diagnostic, not a workflow change |
| 7. Quick bump shortcut | Small -- inline TOML generation from CLI flags | Medium -- removes friction for the most common case |
| 8. Remote release trigger | Medium -- would require a `workflow_dispatch`-based release mode | Low -- niche requirement |
| 9. Python build backends | Small -- poetry support is mostly wiring `poetry build`/`poetry publish` as an alternative pipeline | Low -- uv covers most cases |
