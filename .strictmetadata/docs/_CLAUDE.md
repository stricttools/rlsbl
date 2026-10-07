+++
description = "Operational reference for AI agents working on rlsbl and on rlsbl-managed projects."
+++
# rlsbl

:-: var key="project.description"

Built in Go on strictcli (commands, effects, checks), strictspec (generated validators, options, the lifecycle-and-license library), and go-toml-edit. One binary, `cmd/rlsbl`; the packages are under `internal/`. Current version: check `VERSION`.

## Commands

:-: table-commands

## Where rlsbl's records live

Everything rlsbl owns is under `.strictmetadata/` ([on-disk layout](.strictmetadata/docs/on-disk-layout.md)):

- `.strictmetadata/releasables/releasables.toml`: the declarations (layout, release branches, releasables, members, targets, pipelines, hooks, checks). See [declarations](.strictmetadata/docs/declarations.md).
- `.strictmetadata/changelog/<releasable>/`: `unreleased.jsonl` and each released version's read-only `<version>.jsonl`.
- `.strictmetadata/releases/<releasable>/`: the release file `unreleased.toml`, the archives `v<version>.toml`, and a workspace releasable's `version`.
- `.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml`: each releasable's lifecycle, license, and identities. See [lifecycle and license](.strictmetadata/docs/lifecycle-and-license.md).
- `.strictmetadata/options/`: options entries, each setting an option's value (softening or switching off a check, or switching on an adoption) for the repository or one member, with its reason.
- `.strictmetadata/.release-state/`: run state, ignored by git.

`.rlsbl/`, `.rlsbl-monorepo/`, and `<member>/.rlsbl/` are the old layout: rlsbl reads none of them, `releasable-residue` reports what remains, and `rlsbl monorepo cleanup` removes it.

## Release workflow

- Run `rlsbl release init` to write `.strictmetadata/releases/<releasable>/unreleased.toml`, then set its `bump` (`patch`, `minor`, `major`, or `infra`) and `description`.
- Run `rlsbl release run --watch --approve-consequential`. It runs in the release checkout (`.git/rlsbl/release-checkout`), a detached checkout of the branch tip: an uncommitted change to a path the release writes refuses it by name, every other uncommitted change is listed and left alone, and the branch advances only by compare-and-swap from where the release started.
- The commit is pushed untagged and tagged only after the repository's own CI passed on it. After a red verdict, commit the fix on the release branch, add its changelog entry, and run `rlsbl release resume --watch`; never start a new release to get past it.
- `rlsbl release abandon` records a stopped attempt's version as never released.
- CI publishes from the publish workflow the GitHub Release starts. Never publish by hand.
- `--watch`/`--no-watch` is required on `release run`, `release resume`, `release retry`, and `monorepo release run`, with no default.
- `--dry-run`, `--approve-consequential`, `--quiet`, and `--verbose` are framework flags on every command. Every command is `read_only` or `mutating`, which decides whether `--dry-run` records its effects as a would-do log instead of performing them. A command asks for confirmation only when it declares itself consequential, and `--approve-consequential` gives it. A command that cannot be previewed refuses `--dry-run`, and its refusal says why.
- Every flag and argument declares its presence: required, optional, or a default. A mutating command declares no value default, so an opt-out boolean such as `--auto-commit` is optional and names its fallback in its help; an omitted flag reaches the handler as absent.
- A selection of one of several flags is a choice flag. `release scrub` elects its mode (`--pattern`, `--file`, or `--recipe`) and its range (`--from-commit` or `--entire-history`); `--replace` and `--mangle` exist only under `--pattern`. `changelog edit` and `changelog remove` elect `--id` or `--commits`.
- `changelog edit` is a sparse update: at least one of `--description`, `--type`, `--user-facing`, `--unset-description`, `--unset-type`; a field not named is untouched. Deleting an entry is `changelog remove`.

## Release pipeline order

In the release checkout, before the version is written:

1. the `pre_checks` hooks of the releasable and its members;
2. the strictcli schema dump (`<app> help --json` written to `.strictmetadata/.cli-schema/schema.json`), for a strictcli program;
3. `selfdoc gen --no-auto-commit --version-override <version>`, then `selfdoc check`, then the selfdoc commit, for a project using selfdoc;
4. the changelog validation and the preflight checks (`rlsbl check --hook pre-release`, the `preflight` tag), every one of them, always;
5. the built-in tests of each member (`go test`, `npm test`, `uv run python -P -m pytest`), unless the member or its releasable declares a `pre_release` hook, which replaces them and nothing else;
6. the `pre_release` hooks.

Then the release's steps, from the version bump to the post-release hooks, each recorded in `.strictmetadata/.release-state/<releasable>/in-progress.toml`; [the release workflow](.strictmetadata/docs/release-workflow.md#the-release-steps) tables them.

## Who writes which ref namespace

| Namespace | Routine writer | Never written by |
| --- | --- | --- |
| `origin` branch heads | Releases: `rlsbl release run --watch` pushes the untagged candidate and, after CI, the finalization commits. | Anything else. There is no push command, and the pre-push hook refuses a manual push to a release branch. (`rlsbl release undo` also pushes the branch, as a retraction.) |
| `origin` tags and their GitHub Releases | The release's tag and Release steps. `rlsbl release reconcile` repairs them from the record. | Hand-made tags. A shipped tag is never moved, except to follow its commit through a rewrite the records explain. |
| Rewritten history | `rlsbl release scrub` (and `rlsbl transition declassify`), through safegit: force-push, re-pointed tags, and each Release rewritten in place. | A bare `git push --force`; after an out-of-band rewrite, `rlsbl release reconcile` and `rlsbl changelog remap --from-journal` repair the records. |
| A fork's inherited tags, `refs/tags-of/<host>/<owner>/<repo>/<tag>` | `rlsbl upstream adopt-tags`. | Anything else. |
| A fork's upstream branch, `refs/upstream/<host>/<owner>/<repo>/<branch>`, here only | The operator's `git fetch --no-tags`. | rlsbl. |

The repair and retraction commands that write those namespaces on purpose are tabled in [the release workflow](.strictmetadata/docs/release-workflow.md#who-writes-which-ref-namespace). A write from anywhere else is not rlsbl's.

## Conventions

- rlsbl commits through safegit and deletes through saferm; a missing one is an error, never a fallback.
- Every writing command goes through strictcli's effects handle, so `--dry-run` records it; the `effects-bypass` check refuses a bypass.
- No tokens or secrets in command-line arguments: they reach programs through the environment or stdin.
- Registry lookups use the package-level listings only (npm's package document, PyPI's project document, the Go proxy's `@v/list`); no request names one version, which could burn it.
- A question rlsbl cannot answer is an error, never read as absence or as a pass.
- Use `rlsbl dev install` for local installs and `rlsbl dev sync` for editable overlays of sibling checkouts.
- Generated files (`CHANGELOG.md`, the routers, the schema dump, the support matrix, the options registry) are regenerated, never edited, and committed with the `Autogenerated: true` trailer (`rlsbl commit`).

## Source layout

- `cmd/rlsbl`: `main`, which builds the app through `internal/cli`.
- `internal/cli`: every command's registration, flags, help, and payload.
- `internal/checks/checks.toml`: the checks registry; `internal/options/registry.toml` is generated from it by `internal/options/gen`.
- `internal/targets/support-matrix.json`: the per-target facts, generated by `internal/targets/gen`.
- `internal/cli/layering_test.go` refuses an import that points up the package order.
