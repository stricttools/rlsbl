# test-suite check timeout: knob exists — fix discoverability, not the feature

## Correction notice (this todo was updated in place)

The original version of this todo requested a configurable timeout for the `test-suite` check, claiming it is hardcoded at ~120 s. That premise is WRONG: the knob already exists — `get_check_timeout` (`rlsbl/utils.py:231`) resolves it with precedence `RLSBL_CHECK_TIMEOUT` env var > `check_timeout` key in `.rlsbl/config.json` > default 120 s, with validation of bad values. The feature is done and shipped.

## The remaining, real gap: discoverability

The scenario that prompted the original filing still happened as described: a project whose race-enabled suite legitimately runs ~100 s intermittently failed the check under load, and the operator (an AI session that had just read the check output) concluded the timeout was not configurable. Nothing in the failure path mentions the knob:

1. The timeout failure message ("command timed out after 120s" or similar) does not name `check_timeout` / `RLSBL_CHECK_TIMEOUT` or hint that the budget is configurable. For an agent-first tool, the error message is the documentation that gets read — a one-line remediation hint ("declare a larger budget via check_timeout in .rlsbl/config.json if your suite legitimately needs it") would have prevented the misdiagnosis entirely.
2. Verify docs coverage: if the key is absent or thin in the config documentation / selfdoc output, add it (what it does, precedence, that it is a declared budget rather than a bypass — the check still hard-fails on real hangs).

## Affected files

- The test-suite check's timeout-failure message construction (wherever the "timed out after Ns" string is emitted)
- Config docs for `check_timeout` / `RLSBL_CHECK_TIMEOUT`

## Effort estimate

Trivial-to-small: one error-message string with the remediation hint, plus a docs check. A regression test asserting the hint appears in the timeout failure output would lock it in.
