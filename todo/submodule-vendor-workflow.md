# Submodule vendor workflow for forked dependencies

## Problem

Projects sometimes need to fork an upstream dependency, apply patches, PR them upstream, and periodically rebase to pick up upstream changes. This is a common pattern for:
- Vendoring a tool that needs project-specific fixes
- Contributing to upstream while benefiting from fixes immediately
- Managing dependencies where the upstream is a solo maintainer (bus factor risk)

## Desired workflow

`rlsbl vendor add <upstream-url> <local-path>` should:
1. Fork the upstream repo (via `gh repo fork`)
2. Add the fork as a git submodule at the specified path
3. Add the path to the monorepo's workspace config (pnpm-workspace.yaml, etc.) if applicable
4. Track the upstream remote for rebasing

`rlsbl vendor sync <local-path>` should:
1. Fetch upstream changes
2. Rebase local patches onto upstream
3. Report conflicts if any
4. Push to the fork

`rlsbl vendor status` should:
1. Show all vendored submodules
2. How many local patches ahead of upstream
3. Whether upstream has new commits to rebase onto

## Context

Triggered by a monorepo needing to fork an upstream dependency as a submodule, apply fixes, PR upstream, and rebase periodically. The pattern is general enough to be an rlsbl feature.

## Affected projects

Any rlsbl-managed monorepo that vendors forked dependencies.

## Prior art

The following documents the manual steps and friction points observed during a real submodule fork workflow.

### Manual steps performed

1. Forked the upstream project via `gh repo fork`.
2. Added the fork as a git submodule in the host monorepo: `git submodule add <fork-url> <path>`.
3. Navigated into the submodule directory (a separate git repository nested inside the host).
4. Installed the forked dependency's own dependencies via `npm install`. The submodule has its own `package.json` independent of the host workspace.
5. Added the upstream remote for future rebasing: `git remote add upstream <upstream-url>`.
6. Made patches to the source code inside the submodule.
7. Committed inside the submodule (its own git history, separate from the host repo).
8. Pushed the submodule commits to the fork remote.
9. Back in the host repo: staged the updated submodule reference (the new commit hash) and committed.
10. For future maintenance: `git fetch upstream && git rebase upstream/main` inside the submodule to pick up upstream changes, then repeat steps 8-9.

### Friction points

- **Host commit tooling cannot operate inside submodules.** The host project's commit tooling (designed for the main repo's object store) failed when attempting to commit inside the submodule — `hash-object` could not resolve the submodule path as a file. Raw `git commit` was required inside the submodule directory. This is a fundamental git limitation: submodules are separate repositories, so any tooling that wraps git for the host repo does not apply inside them.

- **Dependency installation is a separate manual step.** The host workspace's package manager does not manage submodule dependencies. After adding the submodule, the developer must navigate into it and run `npm install` (or the appropriate package manager) separately. This is easy to forget, especially for contributors unfamiliar with the submodule setup.

- **Moving a submodule requires manual `.gitmodules` editing.** When the submodule was relocated to a different path within the host repo, `.gitmodules` needed a manual edit to update both the `path` and the submodule section name. Git does not provide a high-level command for this — `git mv` handles the filesystem move but the `.gitmodules` entry can get out of sync, requiring hand-editing.

- **Host workspace config needs a separate entry.** The host monorepo's workspace configuration (used for release tracking and other tooling) needed a distinct entry for the forked dependency. This was not automatically inferred from the submodule's presence — it had to be added manually and kept in sync if the submodule path changed.

- **No automated upstream drift detection.** There was no built-in way to check whether the fork had fallen behind upstream or had unrebased local patches. The developer had to manually `git fetch upstream` inside the submodule and compare refs. Over time, this leads to forks silently drifting, making eventual rebases larger and more conflict-prone.
