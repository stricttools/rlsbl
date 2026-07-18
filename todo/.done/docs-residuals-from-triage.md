# Three small docs additions (residuals from 2026-07-17 todo triage)

Three shipped features whose code + tests are complete but whose human-facing docs
are missing one entry each. All three parent todos were verified fixed and moved to
`.done/`; these lines are the only remaining slivers.

## 1. changelog.md — document the .md/.jsonl permission asymmetry

`docs/changelog.md` (~line 31, the directory-layout section) labels `<version>.jsonl`
as "read-only (chmod 444), immutable" but describes `<version>.md` only as "Generated
markdown". Add one clarifying line: the `.md` files are writable and regenerated in
place on every release (in contrast to the immutable 444 `.jsonl`), and a deliberately
locked `.md` is handled gracefully (the atomic mode-preserving writer compares content
first and skips identical regeneration). The asymmetry is already documented in the
`rlsbl/changelog/generate.py` module docstring — the docs page just needs to match.

## 2. configuration.md — add the check_timeout row

`docs/configuration.md` (~line 181) documents `RLSBL_PUSH_TIMEOUT`/`push_timeout` in
the env-var/config table but omits the check-timeout knob entirely. Add a row:
`RLSBL_CHECK_TIMEOUT` env var / `check_timeout` config key, project scope, "check
subprocess timeout in seconds, default 120; precedence env > config > default; a
declared budget, not a bypass — the check still hard-fails on real hangs" (per
`get_check_timeout`, `rlsbl/utils.py:231`).

## 3. configuration.md — add the test.pypi.markers section

The per-target test config (`{"test": {"pypi": {"markers": "not integration"}}}`,
validated by `validate_test_config` in `rlsbl/config.py:505`) is documented only via
the selfdoc-rendered docstring. Add a short prose subsection to `docs/configuration.md`:
what it does, that an absent section means run everything (byte-identical to prior
behavior), and that it is a selection filter, not a gate bypass.

## Effort

Trivial — three doc edits, no code. A regression test is not needed (the features
themselves are already tested); this is pure discoverability.
