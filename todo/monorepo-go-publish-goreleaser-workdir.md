# Go publish in a monorepo never builds: goreleaser-action runs at the repo root

## Context

A monorepo Go member lives in a subdirectory (`go/`, `cmd/`, `backend/`, ...).
`rlsbl monorepo sync` inlines that member's `publish.yml` into the root
`Publish Router` and adds

```yaml
defaults:
  run:
    working-directory: go
```

to the job, plus it rewrites `go-version-file: go.mod` to
`go-version-file: go/go.mod` (`rewrite_version_file_inputs` in
`rlsbl/ci_yaml.py`, which knows `go-version-file`, `python-version-file`,
`node-version-file`).

## Problem

`defaults.run.working-directory` applies to `run:` steps only. It has no effect
on an action. `goreleaser/goreleaser-action` therefore executes at the
repository root, where there is no `.goreleaser.yml` (it lives in the member
directory, where scaffold puts it) and no main package. goreleaser logs

```
• could not find a configuration file, using defaults...
⨯ release failed after 0s
  │ build failed: build for <name> does not contain a main function
```

and the job fails. The release itself has already tagged, pushed and created
the GitHub Release by then, so what ships is a release whose GitHub Release
carries **no** archives and no `checksums.txt`.

That is not cosmetic when the same repo also publishes pypi/npm launcher
packages: the scaffolded first-run launcher shims download
`<name>_<version>_<os>_<arch>.tar.gz` + `checksums.txt` from exactly that
GitHub Release. The launcher of an already-published wheel/tarball then 404s
for the lifetime of that version. Recovering it costs a patch release (to get
a fixed workflow onto the default branch, since a `workflow_dispatch` resolves
the workflow file from the ref it is dispatched at) plus a re-dispatch at the
broken version's tag.

Nothing catches this before it ships: the release's own CI check never runs the
publish workflow, and `--no-watch` explicitly does not verify the publish
outcome.

## Reproduction

1. Monorepo, one member with a `go` target at path `go/`, `publish_mode: "ci"`.
2. `rlsbl monorepo sync`, then release the member.
3. `Publish Router` -> `<name>-go` job fails in the goreleaser step; the
   GitHub Release has zero assets.

Standalone Go repos are unaffected: there the module is at the repo root, so
the action's default workdir `.` is already correct. This is monorepo-only,
which is why it can sit undetected.

## Solutions

### A. Rewrite `workdir` at inline time (recommended)

Alongside `rewrite_version_file_inputs`, set `with.workdir: <project_path>` on
any step whose `uses` is `goreleaser/goreleaser-action` (matching the same way
the pinned-action helper matches it, so a version suffix does not defeat it).
Prefix an existing relative value rather than clobbering it, mirroring the
version-file rule.

- Pro: fixes every existing repo on the next `monorepo sync`, needs no scaffold
  re-run, keeps the per-project workflow correct standalone (`workdir` absent
  means `.`, which is right there).
- Pro: same shape as the existing rewrite, so there is one place where "inputs
  the runner resolves from the repo root, not from working-directory" is
  handled.
- Con: a hardcoded action name in the inliner. It already hardcodes three input
  names, so this is consistent, not new.

### B. Emit `workdir` from `templates/go/publish.yml.tpl`

The template cannot know the member path (it is rendered per project, but the
value has to be relative to the *repo root*, which differs standalone vs
monorepo), so this needs a template variable fed by the scaffold context, and
it only heals repos that re-scaffold. Weaker than A on both counts.

### C. Do both

A fixes existing repos, B makes the generated per-project file self-describing.
If B lands, A must still prefix rather than overwrite.

## Also worth checking (same class)

Every other action input the runner resolves from the repo root while the job
believes it is in a subdirectory. Candidates in the current templates:
`packages-dir` on `pypa/gh-action-pypi-publish` (already emitted as
`python/dist/`, so that one is handled), cache-dependency-path on the setup
actions, and any `path:` input on upload/download-artifact. A test that inlines
a fixture member and asserts every `with:` path input resolves from the root
would close the class instead of the instance.

## Affected files

- `rlsbl/ci_yaml.py` -- `rewrite_version_file_inputs` and its caller
- `rlsbl/templates/go/publish.yml.tpl` -- the goreleaser step
- `rlsbl/commands/monorepo/sync.py` -- inline path
- tests: a monorepo-inline test asserting `workdir` equals the member path

## Effort

Small: ~20 lines plus tests for A. The value is high — it is the difference
between a Go monorepo member publishing binaries and silently publishing none.
