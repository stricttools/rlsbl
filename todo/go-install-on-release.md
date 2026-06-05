# Auto `go install` on release for Go projects

## Problem

After `rlsbl release run` for a Go project, the installed binary in `~/go/bin/` is stale. The release tags and pushes new code, but the locally installed binary still points to the old version. This was discovered when saferm shipped v0.3.0 with a symlink bug fix, but the running binary was still v0.2.1 from May 29.

Go has no editable install mode (unlike Python's `pip install -e`). Every source change requires `go install .` to rebuild and copy the binary.

## Proposed solution

Add a `go_install_on_release` boolean to `.rlsbl/config.json` (opt-in, default false). When true and the project target is `go`, `rlsbl release run` automatically runs `go install .` after the version bump commit, before or after the GitHub Release step.

This ensures the locally installed binary always matches the latest release without manual intervention.

## Scope

- Only applies to Go projects (target = "go" in config)
- Only runs `go install .` (standard Go toolchain, no custom build steps)
- Opt-in per project via config flag
- Runs locally (not in CI — CI doesn't install to ~/go/bin/)
- Should happen after the version bump so the installed binary reports the correct version
