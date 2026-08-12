# dep-floors check is vacuous in flat uv workspaces

## Problem

The `dep-floors` preflight compares each member's manifest floors against its
locked versions by reading `<project_root>/uv.lock`. In a flat uv workspace the
only lockfile lives at the repository root, so every member reports
`pypi: no uv.lock -- no locked versions to compare` and the check passes with
zero comparisons. A monorepo that declares `internal_dep_floors` believes the
floors are being policed; nothing is.

Observed live: a monorepo with `internal_dep_floors` declared in all its
releasable configs gets a clean PASS while its members' floors were verified
only by hand-driving the comparison against the root lock.

## Fix

When a member has no local `uv.lock`, walk up to the workspace root (the
directory whose `pyproject.toml` declares the uv workspace, or the monorepo
marker) and resolve the member's dependencies from the root lock. A member
absent from any reachable lock stays a hard error rather than a skip — the
current silent "no lock, no comparison" pass is the same silent-skip class the
check family exists to prevent.

## Affected

- `dep_floors.py` (`pypi_locked` or its caller).
- Every monorepo consumer that declares `internal_dep_floors` — all currently
  unprotected on the pypi side. Go and npm sides unaffected (their lock
  discovery differs).

## Effort

Small: lock-path resolution plus a red-green test with a flat-workspace
fixture.
