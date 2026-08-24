# dev sync cannot reach a workspace-root dev-sources file in monorepos

## Context

`rlsbl dev sync` overlays local editable checkouts onto a project's locked
environment, driven by `dev-sources.toml.local-only` at the project root.
In monorepo mode, `rlsbl dev sync` refuses to run at the workspace root
("must run inside a sub-project") — but when run inside a sub-project it
looks for `dev-sources.toml.local-only` IN THAT SUB-PROJECT's directory.

## Problem

A monorepo whose lockfile lives at the workspace root (a uv workspace with
one shared `uv.lock`) naturally keeps its `dev-sources.toml.local-only` at
the workspace root too — the overlays apply to the one shared environment.
That file is then unreachable through the prescribed command entirely:

- at the root: "must run inside a sub-project" refusal;
- inside a sub-project: "no dev-sources.toml.local-only found" (it only
  looks in the sub-project directory).

The declared overlays are silently unusable, and the manual equivalent
(`uv sync --inexact --no-install-package <pkg>` then
`uv pip install -e <path>`) has to be reconstructed by hand — observed in
practice on a workspace-rooted monorepo whose declared overlay could not be
applied by any `dev sync` invocation.

## Solutions

1. **Teach monorepo `dev sync` to read the workspace root's dev-sources
   file** when the workspace uses a single root lockfile — run the overlay
   against the root environment (which is what the file describes). The
   sub-project refusal stays for commands that genuinely need a member
   context; dev sync's real subject is the environment the lock defines.
   Pros: the declared file works where it naturally lives. Cons: needs a
   rule for monorepos where members keep their own locks (fall back to the
   current per-member behavior there).
2. **Per-member dev-sources with root lookup fallback:** inside a
   sub-project, look in the member dir first, then the workspace root.
   Pros: minimal change; both layouts work. Cons: two lookup locations to
   document.
3. **Refuse louder, do nothing else:** the root refusal message names the
   limitation ("workspace-root dev-sources files are not supported; move
   the file into the member"). Honest, but moving the file misdescribes a
   shared-lock workspace where overlays are root-environment facts.

Option 2 is the smallest change that makes both layouts work; option 1 is
the most correct model of what the file describes.

## Affected files

- the dev sync command's dev-sources discovery and its monorepo-mode
  refusals
- `dev-sources` documentation

## Effort

Small.
