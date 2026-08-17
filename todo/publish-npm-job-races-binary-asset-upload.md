# Publish workflow: the npm job races the binary-uploading job

## Context

The scaffolded `publish.yml` runs one job per configured pipeline (`go`, `npm`,
`pypi`, ...), all of them fanning out in parallel from the `gate` job. Nothing
in the generated job graph declares an ordering between them.

That is correct when the pipelines are independent. It is wrong for a very
common shape: a project that ships a Go binary via goreleaser **and** an npm
wrapper package whose `postinstall` downloads that binary from the GitHub
Release the same workflow run is still producing.

## The failure, as observed

A release whose `go` job took 4m28s (a cold goreleaser build) and whose `npm`
job started at the same moment:

```
X npm in 9s
  ✓ Install gitleaks
  X Install dependencies
```

with:

```
> <wrapper-package>@X.Y.Z postinstall
> node install.js
Downloading ... v X.Y.Z
  https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/<name>_X.Y.Z_linux_amd64.tar.gz
Failed to install: Download failed: HTTP 404
npm error code 1
```

The `npm ci` step in the npm job triggers the wrapper's own `postinstall`, which
fetches the release asset. At that moment the `go` job has not finished
uploading anything, so the asset genuinely does not exist and the job fails.

It is a race, not a deterministic break: the previous release of the same
project passed because its `go` job happened to finish in 19s while the `npm`
job took 42s. That is exactly the kind of failure that will keep reappearing at
random and will keep being re-run by hand.

The same race also produces the earlier, quieter symptom during the release
itself:

```
Warning: package-lock.json sync failed: Command
'['npm', 'install', '--package-lock-only']' returned non-zero exit status 1.
```

`npm install --package-lock-only` still runs the wrapper's `postinstall`, which
tries to download the not-yet-published version. The lockfile ends up correct
by another path, so this one is only noise -- but it is the same root cause and
would be fixed by the same reasoning.

## Why this is rlsbl's problem

`publish.yml` is scaffold-managed (three-way merged on re-scaffold). A project
that hand-edits the job graph to add `needs:` gets a merge conflict on every
future scaffold, and the next project with the same shape hits the same race
from scratch. The dependency between "the job that uploads release assets" and
"the job that consumes them" is a fact about the pipeline set, which is
something rlsbl knows and the project does not have to restate.

## Options

1. **Order the jobs when both pipelines exist.** When the pipeline set contains
   a binary-producing target (`artifact: "binary"`) and an npm target, emit
   `needs: [gate, go]` on the npm job instead of `needs: [gate]`.
   - Pro: one line in the template, no new configuration, fixes the race
     completely, and costs nothing for projects that do not have both.
   - Con: serializes two jobs that are only *sometimes* dependent -- an npm
     package that does not download the binary waits for no reason. Adds
     wall-clock time to those releases.

2. **Declare the dependency in `.rlsbl/config.json`.** A pipeline gains an
   optional `depends_on` naming other pipelines, and the workflow generator
   turns it into `needs:`.
   - Pro: explicit, honest, and reusable for any other cross-pipeline ordering
     (an npm package that consumes a PyPI artifact, a wrapper that consumes two
     binaries).
   - Con: another key to declare, and a project that forgets it keeps the race.
     Silence is the wrong default here -- the failure mode is a red publish.

3. **Make the npm job not run the wrapper's postinstall.** Install with
   `npm ci --ignore-scripts` in the publish job, since publishing a package does
   not require executing its own install hook.
   - Pro: removes the dependency instead of ordering around it; the npm job
     stops caring when the binary lands, and the `package-lock-only` warning
     during the release goes away for the same reason.
   - Con: changes what the publish job verifies -- today the run incidentally
     proves the wrapper's download path works against the real release. That
     verification is worth keeping, but it belongs in a post-publish smoke check
     rather than in the middle of the publish.
   - This is the most correct option regardless of effort: combine it with a
     separate post-publish install check that runs after every artifact exists.

4. **Retry the download inside the wrapper.** Out of scope for rlsbl -- it is
   per-project code, and a retry loop would only paper over an ordering fault
   the workflow can state exactly.

## Affected files

- The `publish.yml` scaffold template and whatever renders its job list.
- The scaffold's stored base (a template change reaches existing projects
  through the three-way merge, so the diff should stay small and local to the
  job header).
- Any documentation describing the publish job graph.

## Effort

Small for option 1 or 3 (a template edit plus a scaffold-diff test). Medium for
option 2 (a new config key, its validation, and its rendering).
