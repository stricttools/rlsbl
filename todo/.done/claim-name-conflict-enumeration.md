# check-name/claim-name: enumerate ALL npm conflicts + normalization rule (Option B)

Successor to the (now `.done/`) claim-name-npm-moniker-conflict-name todo. Option A
(surface the concrete conflicting package name instead of the internal check tag) is
shipped and regression-tested in both output paths. This todo is the approved
follow-up: full enumeration.

## Problem

Both classifier paths already compute the COMPLETE list of conflicting packages and
then discard all but the first:

- `_check_single_name` (`rlsbl/commands/check.py:500`):
  `note = f"...collision with '{hard[0]}'..."` — `hard` is the full list from
  `_classify_variant_collisions` (`check.py:421`).
- The `_search_npm_similar` path (`check.py:516`): `conflicts[0]` — `conflicts` is
  the full list.

A user with several near-name packages sees only one conflict per run, and the
specific normalization rule that matched (npm strips `-`/`.`/`_` and lowercases
before comparing) is not stated explicitly.

## Approved scope

1. Store the full conflict list (not `[0]`) plus the normalization rule that matched
   on the result dict.
2. Both output paths (check-name verbose formatter, claim-name taken-branch) list
   every concrete conflict and state the rule ("npm strips punctuation: 'foo-bar',
   'foobar', 'foo.bar' share one moniker").
3. `--json` output gains a structured `conflicts` field (list of package names) plus
   the rule identifier, so scripts can consume it. This is a result-schema change —
   update JSON-consuming tests accordingly.
4. Decide ordering/capping in code review (alphabetical, no cap, unless the list is
   pathological — registry variant checks are already bounded by the insertion cap).

## Affected files

- `rlsbl/commands/check.py` (`_check_single_name`, `_format_single_result`,
  `_format_table_row` if conflict count belongs there)
- `rlsbl/commands/claim_name.py` (taken-branch message)
- `tests/test_check.py`, `tests/test_check_name_multi_target.py`,
  `tests/test_claim_name.py`

## Effort

Medium — half a day including the schema change and test updates.
