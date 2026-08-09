# The cloudflare-pages pipeline: fate, and the post-tag failure window it exposed

## Context

`cloudflare-pages` is one of the built-in pipeline types. It is `local: true`
and publishes by shelling out to `selfdoc deploy`, which reads a `deploy`
section from the consuming project's `selfdoc.json` and makes a Cloudflare Pages
deployment live (or force-pushes `gh-pages`).

It is actively maintained: a recent release repaired it after `selfdoc deploy`
became consequential, which had made the pipeline's bare invocation hard-error
on the release runner's non-interactive stdin. The pipeline now passes
`--approve-consequential`.

## What happened

A consumer project released with a `docs-deploy` pipeline declared as
`{"type": "cloudflare-pages", "target": null, "local": true}` while its
`selfdoc.json` had no `deploy` key at all. The release ran to completion
through version bump, commit, tag, push and GitHub Release, and then failed in
the publish step with:

```
No 'deploy' section in selfdoc.json. Add a deploy provider configuration.
Error: pipeline 'docs-deploy' publish failed
```

The tag and the GitHub Release were already public. The release was left in the
resumable state with an instruction to fix and re-run.

Nothing had verified the pipeline's provider configuration beforehand. The one
pre-mutation pipeline validator checks only `required_env_vars()`, which passed
because the Cloudflare credentials were present — the missing piece was the
`selfdoc.json` section, which rlsbl never looks at.

## Problem 1: the pipeline looks superseded, but that is not confirmed

Evidence gathered:

- Every project examined that deploys documentation does so from its
  `post-release.sh` hook via `selfblog assembly push`, gated on an `assembly`
  key in `selfdoc.json`. None of them declares a `docs-deploy` pipeline.
- Seven projects carry a `deploy` section in `selfdoc.json` with provider
  `cloudflare-pages` and **no** corresponding pipeline anywhere. That
  combination has apparently never been configured together.
- So the ecosystem appears to have moved from per-project Cloudflare Pages
  deploys to a centralized documentation assembly, leaving the deploy sections
  as vestigial config from before that migration.

What this evidence does **not** settle: assembly aggregates documentation into a
shared site, whereas the pipeline publishes a standalone per-project site. If
standalone per-project sites are still wanted, the pipeline is the only path to
them and should stay. That is a product question, not a code question.

## Problem 2: the publish loop ignores the release file's include/exclude

`_publish_standalone_pipelines` (`rlsbl/commands/release/execute.py:1329`)
iterates every pipeline declared in `.rlsbl/config.json`. The release file's
`include` / `exclude` lists are not consulted. In the failure above the release
file said `include = ["npm", "pypi"]` and the docs pipeline ran regardless.

This is independent of whatever is decided about cloudflare-pages, and it is
the reason a pipeline nobody asked for in that release was able to break it.

## Problem 3: any local pipeline can fail after the tag is public

The structural hole is wider than one pipeline type. Any `local: true` pipeline
whose provider configuration is unverified until publish time can abort a
release after the tag, push and GitHub Release already exist. Registry publishes
inherently need the tag and cannot move earlier; deploy-type pipelines have no
such dependency and currently run in the same phase anyway.

## Undecided

### A. Does the cloudflare-pages pipeline survive?

| Option | Pros | Cons |
|---|---|---|
| Delete the pipeline type, remove the seven vestigial `deploy` sections | Zero adoption is strong revealed preference; removes a whole class of post-tag failure; less code | Entrenches the assembly hook as the only path, and a failing post-release hook is non-fatal, so a broken docs deploy becomes silent |
| Keep it, add provider preflight (see B) | Preserves standalone per-project sites; a pipeline failure is loud where a hook failure is silent | Maintains a feature with no current users |
| Keep it, and refuse a half-configured state at scaffold time | Stops the broken combination being written again | Does not help the repos already in that state; catches it earlier than useful |

Deciding this needs one input the code cannot supply: whether standalone
per-project documentation sites are still wanted.

### B. Preflight validation only, or preflight plus phase ordering?

| | Preflight only | Preflight + phase ordering |
|---|---|---|
| Core idea | Each pipeline verifies its provider config before any mutation | Same, plus pipelines declare whether they need the tag; position-free ones run before tagging |
| Catches misconfiguration | Yes, before the tag | Yes, before the tag |
| Catches runtime failure (outage, expired token, transient error) | No — config can be valid and the deploy still fail post-tag | Yes for pre-tag pipelines; nothing is public when they fail |
| Post-tag failure window | Narrowed | Closed for pipelines that do not need the tag |
| Change surface | One method on the pipeline interface, extending the existing `required_env_vars` loop | The above plus a phase property on every pipeline type and a reordering of the release flow |
| Risk | Low — only refuses earlier | Moderate — ordering affects every release, and ordering bugs surface only during real releases |
| Testability | Assert a misconfigured pipeline refuses before mutation | Needs tests that drive a full release flow to prove ordering |
| Effort | Small | Roughly double, mostly the flow restructure and its tests |

Note the coupling: if cloudflare-pages is deleted, no position-free pipelines
remain and phase ordering has nothing to order, which makes preflight-only the
obvious answer. Decide A first.

Either option needs a small addition on the selfdoc side: a read-only probe so
rlsbl can ask whether a deploy provider is configured, rather than parsing
`selfdoc.json` itself and duplicating knowledge of its schema.

## Affected files

- `rlsbl/pipelines/cloudflare_pages.py` — the pipeline implementation
- `rlsbl/commands/release/execute.py:1329` — `_publish_standalone_pipelines`,
  the loop that ignores include/exclude
- `rlsbl/commands/release/validate.py` — the pre-mutation validator that today
  checks only `required_env_vars()`
- `rlsbl/config.py` — `validate_pipelines_config`, which has no cloudflare branch
- `docs/pipelines.md` — documents neither the `selfdoc.json` `deploy`
  requirement nor the built-output requirement
- Consuming projects carrying a vestigial `deploy` section, if A is to delete

## Effort

Problem 2 (include/exclude) is small and independent — worth fixing regardless.
Decision A is a product call plus a small deletion or a documentation pass.
Decision B is small for preflight-only, roughly double for phase ordering.
