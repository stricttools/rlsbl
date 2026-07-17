# Print a one-line success summary for preflight checks during release

## Context

During `rlsbl release run`, the preflight step runs `run_checks(tag_expr="preflight")`, which covers built-in checks (tests, lint) plus any `external_checks` configured in `.rlsbl/config.json`. The release flow prints check results only on failure: the caller iterates results and emits `FAIL <name>: <message>` lines solely when the exit code is non-zero (`rlsbl/commands/release/__init__.py`, around lines 941-960). On success it prints nothing — there is not even a "running preflight checks" line in the non-customized-hook branch.

## Problem

A consumer project wired an external preflight check and then could not tell from the release log whether the check had executed at all. The release succeeded silently, and confirming that the gate was actually armed required reading rlsbl source code to trace the check-selection path. For a tool whose primary consumers are AI agents, an invisible guardrail is indistinguishable from a missing one: agents (and humans) will burn time re-verifying that gates fire, or worse, wrongly conclude the gate is dead and add redundant manual check runs.

This does not require failure-style verbosity. A single line is enough to prove execution.

## Solution

After the preflight `run_checks` call succeeds, print one summary line, e.g.:

```
Preflight: 4 checks passed (test-suite, mypy-strict, ...)
```

Options:

1. **Count plus names (recommended).** One line listing check names. Pros: positively confirms each configured check ran (an agent can grep for its check's name); still one line. Cons: line can get long with many checks — acceptable, truncation is not needed at realistic check counts.
2. **Count only** (`Preflight: 4 checks passed`). Pros: shortest. Cons: does not confirm that a *specific* external check was among the four; the original confusion would only be partially resolved.
3. **Verbose per-check lines on success.** Pros: maximal visibility. Cons: noise in every release log; failure output stops standing out. Not recommended.

The same consideration may apply to other silent-on-success `run_checks` call sites in the release flow (e.g., changelog validation), but preflight is the one that includes user-configured external checks, so it is where invisibility hurts most.

## Affected files

- `rlsbl/commands/release/__init__.py` — the preflight branch that calls `run_checks(tag_expr="preflight")` and currently prints only failures (around lines 900-960; both the customized-hook and non-customized branches if both run checks)
- Tests covering release output (add an assertion that the summary line appears on a successful preflight)

## Effort

Small: a few lines plus a test. The check names are already present on the returned results.
