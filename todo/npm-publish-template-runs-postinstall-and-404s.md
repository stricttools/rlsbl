# npm publish template runs the package's postinstall, breaking binary-wrapper publishes

Sibling of `npm-lockfile-sync-runs-lifecycle-scripts.md` — same defect class
(`npm` invoked without `--ignore-scripts` where scripts are not wanted), a
different file, and this one **fails the publish** rather than merely warning.

## Problem

The npm publish template's install step is:

```yaml
      - name: Install dependencies
        # Full install (devDependencies included): `npm publish` runs the
        # package's prepack script, which for a TypeScript package compiles
        # with the dev toolchain. A bare checkout has none of it.
        run: npm ci
```

`npm ci` runs the package's own `postinstall`. For a **binary-wrapper** npm
package — the shape rlsbl itself scaffolds for Go CLIs, where `install.js`
downloads `<tool>_<version>_<os>_<arch>` from the GitHub Release named in
`package.json` — that postinstall asks for an asset that does not exist yet.
The `go`/goreleaser job is uploading it **in parallel** in the same workflow;
neither job waits for the other.

Observed on a real release:

```
> saferemove@X.Y.Z postinstall
> node install.js
Downloading <tool> vX.Y.Z for linux/amd64...
Failed to download <tool>: HTTP 404: .../releases/download/vX.Y.Z/<tool>_X.Y.Z_linux_amd64.tar.gz
npm error code 1
##[error]Process completed with exit code 1.
```

Result: `gate` success, `go` success, `pypi` success, **`npm` failure** — the
npm package silently misses the release while everything else ships. rlsbl's
watcher reports `[Publish] FAILED` and retries once; the retry usually passes
only because the goreleaser job has finished by then and the 404 has become a
200. So the outcome is decided by a race, and the "fix" is that the publish job
downloads a binary it has no use for.

## Why this is newly biting

Two template changes compose into it:

1. the `npm ci` step (added for TypeScript packages, whose `prepack` needs the
   dev toolchain), and
2. the requirement that `package-lock.json` be committed so `npm ci` can run.

Neither is wrong on its own. Together, on a wrapper package with zero
dependencies and a downloading postinstall, they turn "install nothing" into
"reach across the network for an artifact this workflow is still producing".

## Fix

```yaml
        run: npm ci --ignore-scripts
```

`--ignore-scripts` still installs devDependencies; it only suppresses lifecycle
scripts. Preparing a package for publication does not include running its
install hooks.

**Caveat to decide, not to assume:** a TypeScript package may depend on
packages whose own `postinstall` fetches a platform binary (esbuild, swc,
sharp, better-sqlite3). `--ignore-scripts` suppresses those too, and if the
`prepack` build shells out to one of them the publish breaks in the other
direction. Options:

- **A.** `--ignore-scripts` unconditionally. Simplest; risks the dep-postinstall
  case above. Probably fine today (check whether any rlsbl-managed npm package
  has a script-requiring dependency) but it is an assumption, not a fact.
- **B.** Branch on the pipeline shape: wrapper/launcher pipelines get
  `--ignore-scripts`, TypeScript library pipelines keep the full `npm ci`.
  rlsbl already knows which it scaffolded.
- **C.** Order the jobs: make `npm` `needs: [gate, go]` so the GitHub Release
  assets exist before the wrapper's postinstall runs. Fixes the 404 without
  touching script semantics, but serializes the publish and leaves the job
  downloading a binary for no reason.

Recommendation: **B** — it is the only option that is correct for both package
shapes without an assumption about anyone's dependency tree. **A** is an
acceptable interim only if a sweep confirms no managed npm package needs
dependency install scripts.

## Affected files

- the npm publish job in the npm-family publish workflow template(s) under
  `rlsbl/templates/`
- template render assertions covering the npm job's install step
- consumers must re-scaffold to pick it up; at least one repo has the fix
  applied locally and will three-way merge

## Effort

Under an hour for A; half a day for B including the pipeline-shape plumbing and
render assertions.
