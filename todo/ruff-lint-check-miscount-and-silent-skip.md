# ruff-lint check: violation miscount (~10x) and silent skip when ruff is missing

Two defects in the built-in `ruff-lint` check (`rlsbl/checks/quality.py`).

## Item 1: counts output lines, not violations

### Context

The check runs `ruff check <project_root> --quiet` with ruff's default (full) output format, which emits a multi-line block per violation (location line, source snippet, caret line, help line). The check then counts non-empty lines of output and reports that number as the issue count (`rlsbl/checks/quality.py:81-85`).

### Problem

The reported count is inflated roughly 10x. Measured on a consumer project: the check reported 537 "issues" where `--output-format=concise` shows exactly 50 violations. Consumers (human or agent) triaging the check output massively overestimate the remediation surface — in the observed case, a "major lint debt" signal was actually a small, mostly auto-fixable set.

### Solution

Run ruff with a machine-stable output format and count violations, not lines:

1. **`--output-format=concise`** (one line per violation) and count lines. Pros: minimal change. Cons: still text-parsing; concise format could evolve.
2. **`--output-format=json` and parse** (recommended). Pros: exact count, plus rule codes become available for a richer message (e.g., top rules by count, fixable count). Cons: slightly more code.

Red-green: add a test with a fixture file containing a known number of violations spanning multi-line output, assert the reported count equals the violation count.

## Item 2: missing ruff silently skips the check

### Context

The ruff binary is resolved via `require_tool("ruff", fatal=False)` (`rlsbl/utils.py:30-52`); when ruff is absent from PATH, the check is skipped rather than failed.

### Problem

A registered check that silently does not run is silent degradation: `rlsbl check --all` can show a green/quiet result on a machine where the tool is simply not installed, and the same project fails elsewhere. This contradicts the ecosystem's hard-errors-over-warnings principle. (Contrast: `external_checks` validates command binary existence as a hard error at registration.)

### Solution

Options:

1. **Hard error when ruff is missing** (most correct, matches external_checks behavior). Cons: makes ruff a de facto required tool for every project on machines running `rlsbl check --all`; may need a per-project opt-out in config for projects that genuinely do not lint — but an explicit config declaration, not a silent skip.
2. **Loud SKIP result** — the check reports a visible "SKIPPED: ruff not installed" line with non-pass status in summaries. Weaker, but eliminates the silence while keeping ruff optional.

Also worth noting for triage: the tool is invoked as bare `ruff` from PATH, so the version actually used is whatever the machine has (observed: a machine with ruff below rlsbl's own `ruff>=0.15.20` dev pin). If version matters for rule behavior, consider a minimum-version check at invocation.

## Affected files

- `rlsbl/checks/quality.py` (invocation, counting, missing-tool handling)
- `rlsbl/utils.py` (`require_tool` semantics, if item 2 changes them)
- Tests for the quality checks

## Effort

Item 1: small (invocation + parsing + red-green test). Item 2: small mechanically; the main work is deciding required-vs-loud-skip policy.
