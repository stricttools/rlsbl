# Two self-release/resume edges from 0.112.0's own release

Both observed while rlsbl released itself under the new engine (first
self-application), 2026-08-07. Neither affects normal consumer releases.

## 1. Resume re-stamps `.rlsbl/version` when the running tool's version changed mid-flight

Phase A plans a WRITE_MARKER step stamping the RUNNING rlsbl's `__version__`
into `.rlsbl/version` and lists it in the release commit. When rlsbl releases
ITSELF from an editable install, the version bump rewrites `rlsbl/__init__.py`
under the running process; on `release resume` the plan builder re-reads
`__version__` as the NEW version and rewrites the marker even though
VERSION_BUMPED is complete and the commit step is skipped — a real diff
written after the commit meant to carry it, leaving the tree dirty
post-release (poised to block the next `--no-allow-dirty`). Only reachable
when the tool's own version changes mid-flight, i.e. self-release + resume.

Fix directions: pin the marker value into the PLAN at build time on the first
run (resume replays the planned value, not the live `__version__`); or make
WRITE_MARKER skip when its producing commit step is already complete. The
first is more in the spirit of the seam (the builder reads once; the executor
replays data).

The stray marker from 0.112.0 was committed via `rlsbl commit` (`a9c7d911`,
changelog-exempt path) and rides the next release's push.

## 2. The red-CI abort message recommends a command the flag contract refuses

The abort text names bare `rlsbl release resume` as the remedy, but resume
requires the explicit `--watch`/`--no-watch` choice (mandatory-flags
philosophy — correct), so the recommended command exits 1 with a flag error.
Fix: the remedy text names the full invocation
(`rlsbl release resume --no-allow-dirty --watch --approve-consequential` or
the minimal required set). One-line message fix + test asserting the remedy
text parses/executes past flag validation.

## Effort

Small: (1) is a plan-payload change + a resume test with a mutated
`__version__`; (2) is a message fix + test.
