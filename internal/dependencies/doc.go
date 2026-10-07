// Package dependencies reads what a project declares about its dependencies
// and what its lockfiles resolved: pyproject.toml and uv.lock, package.json
// and package-lock.json, go.mod and go.sum. It answers:
//
//   - which uv.lock resolves a manifest, through the one locator of the uv
//     workspace that claims a directory (LocateUvLock, FindUvWorkspaceRoot);
//   - which dependencies a pyproject.toml takes from a local checkout, and
//     the edits that turn them into registry constraints, for the PyPI build
//     and for `rewrite uv-path-sources` (Pyproject);
//   - whether each ecosystem-internal dependency declares a floor at what the
//     lock resolved (EvaluateFloors);
//   - whether each lockfile still resolves the manifest beside it
//     (EvaluateLocks);
//   - whether every committed strictspec validator declares a generated-code
//     format the strictspec runtime reads (EvaluateGeneratedFormat);
//   - which local checkouts a project's environment runs on instead of
//     registry wheels, and whether each overlay is still installed
//     (ActiveOverlays, LoadSentinel, InspectInstalled, ClassifyOverlay).
//
// Everything here reads committed files and the local environment: nothing
// runs a package manager and nothing touches the network. A file that exists
// and cannot be read is an error naming it, never an absence.
package dependencies
