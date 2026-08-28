# dep-floors: PASS line shows only the Go note, hiding that the PyPI comparison ran

## Problem

When the `dep-floors` check passes, its PyPI path emits no per-target note on success —
silence is the pass. The Go path always emits its "require lines ARE the declared
minimums ... nothing to enforce" note. Net effect: a passing run's PASS line shows only
the Go note, and reads as if only Go was evaluated, in exactly the place someone looks
to confirm the pyproject-floor-vs-uv.lock comparison actually ran.

Observed on a project declaring `internal_dep_floors` with a Python dependency: the
comparison provably executed (a negative probe with a hypothetically-ahead lock produced
the real error), but nothing on the success path says so.

## Solutions

- **Emit a per-target success note** (e.g. `pypi: <pkg> floor x.y.z, lock resolves x.y.z`
  per policed package). Pros: the PASS line becomes evidence rather than absence of
  evidence; symmetric with Go. Cons: mildly longer output on multi-package configs.
- **One aggregate note** (`pypi: N package(s) at or above their floors`). Pros: compact.
  Cons: less useful when someone wants the resolved versions at a glance.
- Leave as-is: the check is correct, only the reporting misleads.

## Affected

The dep-floors check's PyPI (and npm, if it shares the shape) success path.

## Effort

Minutes.
