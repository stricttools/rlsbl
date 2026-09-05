---
description: "Operational reference for AI agents working on rlsbl-managed projects."
---
# rlsbl

:-: var key="project.description"

Built in Python 3.11+ with ruamel-yaml, tomlkit, strictcli, and tree-sitter. Also distributed as an npm wrapper package. Current version: check `package.json`.

## Commands

:-: table-commands

## Release workflow

This project uses [rlsbl](https://github.com/smm-h/rlsbl) for release orchestration.

- Run `rlsbl release init` to scaffold `.rlsbl/releases/unreleased.toml`
- Edit the release file: set bump type (patch/minor/major), include/exclude targets
- Run `rlsbl release run --no-allow-dirty --watch --approve-consequential` to execute the release
- CI handles publishing automatically via the publish workflow
- Never publish manually -- always use `rlsbl release run`
- Use `rlsbl release run --dry-run` to preview without making changes
- `--dry-run`, `--approve-consequential`, `--quiet` and `--verbose` are framework-owned
  flags available on all commands. Every command is classified `read_only` or `mutating`,
  which decides whether `--dry-run` records its effects as a would-do log instead of
  performing them. Confirmation is separate: a command asks before it runs only if it
  declares itself `consequential`, and `--approve-consequential` skips that prompt. A few
  commands refuse `--dry-run` outright with a reason (`commit`, `release init`,
  `monorepo init`, `monorepo remove`, `monorepo release init`).
- Release-specific required flags: `--allow-dirty`/`--no-allow-dirty`, `--watch`/`--no-watch` (no defaults -- must choose explicitly)
- Every flag and positional argument declares its presence: `required`, `optional`, or a
  `default` value. On a `mutating` command a value default is a registration-time hard
  error -- absence must never resolve to a value the invocation did not state -- so
  rlsbl's opt-out booleans (`--auto-commit`, `--auto-tag`, `--user-facing`,
  `--validate-hashes`) declare `optional` and name their fallback in their own help.
  A flag that is simply omitted arrives at the handler AS absent, never as `""` or `0`.
- Exactly-one selections are choice flags, not mutex groups. `release scrub` uses them
  for the mode (`--pattern`/`--file`/`--recipe`) and for the commit range
  (`--from-commit`/`--entire-history`). `--replace` and `--mangle` sit inside the
  `--pattern` scope, so passing one under another mode names both sides.
- `changelog edit` is a declared sparse update of one `changelog-entry`: at least one of
  `--description`/`--type`/`--user-facing` is required, an unsupplied property is
  untouched, and `--unset-description`/`--unset-type` clear a field rather than writing
  an empty string to it. Deleting an entry is therefore `changelog remove`, a command of
  its own -- a removal writes no property, which the update declaration refuses -- and
  its `--id`/`--commits` are an exactly-one election, since a removal that deletes the
  wrong line cannot be corrected by re-running with a better flag.

## Who writes which ref namespace

Each namespace has ONE routine writer -- the flow that puts refs there in the
ordinary course of shipping -- plus a small, named set of repair and retraction
surfaces that exist to correct or withdraw what was already written. Reading
this is how you know whether something you are about to do belongs to you or to
a command:

| Namespace | Routine writer | Never written by |
| --- | --- | --- |
| `origin` branch heads | Releases -- `rlsbl release run` pushes the untagged candidate and, after CI, the finalization commits. There is no dev-branch push path. | Anything else. There is no `rlsbl push`, and the pre-push hook refuses a manual push to a release branch. (`rlsbl release undo` also pushes the branch -- it is a retraction surface, see below.) |
| `origin` tags, and the GitHub Releases attached to them | The release's own tag step. `rlsbl release reconcile` repairs them when a rewrite or a partial release left them wrong; both compose the Release through the one publication module, so the notes and the `rlsbl-ci-sha` marker are identical either way. | Hand-created tags. A released tag is never MOVED by anything -- the repair surfaces below re-push, delete or rewrite a Release body, never relocate a shipped tag. |
| A subtree mirror's `main` | The mirror reconciler's converge -- `rlsbl monorepo mirror <project>`, and the release's mirror step, which calls the same code. Force-with-lease is its routine write, because the mirror is a derived artifact. | Anything that authors on the mirror. A commit the reconciler cannot account for is a contract violation and it refuses. |
| A subtree mirror's tags, and their GitHub Releases | The mirror publication module, driven by the release's mirror step or by `rlsbl monorepo mirror` materializing a version the mirror is missing. | The mirror's own CI. A mirror's scaffold renders no publish workflow, and any publish workflow that reaches the mirror another way is swept on the next convergence. |
| Rewritten history on any of the above | `rlsbl release scrub` (which wraps `safegit scrub`), the one sanctioned rewrite. It force-pushes, remaps the changelog hashes, re-points the tags and rewrites each tag's GitHub Release document in one pass -- in place, never delete-then-create. | A bare `git push --force`. A rewrite performed outside it leaves the release record, the tags and the Releases stale; `rlsbl release reconcile` is what heals that. |

**The sanctioned repair and retraction surfaces.** These write the same
namespaces on purpose. The list is complete; a write from anywhere else is not
rlsbl's:

| Command | What it writes |
| --- | --- |
| `rlsbl release undo` | Deletes the GitHub Release, deletes the tag (remote and local), reverts the version-bump commit and pushes the branch. |
| `rlsbl release reconcile` | Re-pushes the tags an out-of-band rewrite moved and writes their GitHub Release documents in place, creating only the ones origin lacks. Fail-closed: a divergence no record explains is a hard error. |
| `rlsbl release scrub` | The rewrite itself: force-push, tag re-pointing, GitHub Release documents rewritten in place. |
| `rlsbl release edit` | Re-syncs one Release's notes from CHANGELOG.md. |
| `rlsbl release deprecate` / `rlsbl release yank` | Rewrites a Release's body and sets its pre-release flag; `yank` also performs the registry's own removal. |
| `rlsbl changelog amend` / `rlsbl changelog edit` / `rlsbl changelog remove` | Rewrites a released version's JSONL and re-syncs that version's GitHub Release notes. |
| `rlsbl monorepo rename-releasable` | Pushes one boundary alias tag at the renamed releasable's current version. |

## Release pipeline order

During `rlsbl release run`, the validation and build steps run in this order:

1. Pre-checks hook (`.rlsbl/hooks/pre-checks.sh`)
2. Strictcli schema dump (`--dump-schema`, for projects using strictcli)
3. Selfdoc gen (`selfdoc gen --no-auto-commit`, regenerates docs from source)
4. Selfdoc check (verifies generated docs are up-to-date)
5. Built-in tests (`uv run pytest`, `go test`, `npm test`) -- skipped if pre-release hook is customized
6. Built-in lint (library projects only) -- skipped if pre-release hook is customized
7. Pre-release hook (`.rlsbl/hooks/pre-release.sh`)

**Hooks override:** When the pre-release hook has been customized (content differs from any known scaffold template version), built-in tests and lint are skipped. The hook is expected to handle testing and linting itself.

## Conventions

- No tokens or secrets in command-line arguments (use env vars or config files)
- All file writes to shared state should be atomic (write to tmp, then rename)
- External calls (APIs, CLI tools) must have timeouts and graceful fallbacks
- Use `rlsbl dev install` for local editable installs (picks the right tool per target)
- CI runs smoke tests on every push; manual testing for UI/UX changes

## Configuration

`.rlsbl/config.json` holds per-project settings. Key fields:

- `pipelines` -- publish pipelines with mandatory `type` and `local` fields. See [docs/pipelines.md](pipelines.md).
- `publish_mode` (enum, required) -- `"ci"` publishes via CI pipelines; `"none"` suppresses publishing to public registries. No default. (Replaces the former `private` boolean.)
- `release_branches` -- branch names that trigger manual-release-push warnings.
- `batch_limits` -- limits for changelog batch size checks. See [docs/changelog.md](changelog.md).

For details on checks, CI customization, pre-push hook, validation cache, monorepo support, and asset uploads, see the corresponding pages in [docs/](index.md).
