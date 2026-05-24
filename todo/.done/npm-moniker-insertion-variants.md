# npm moniker collision: insertion variants

## Problem

`rlsbl check-name pgspec --target npm` reported "available" but `npm publish` rejected it as too similar to existing `pg-spec`.

npm normalizes names by stripping `[-._]` and lowercasing before comparing. Both `pgspec` and `pg-spec` normalize to `pgspec`, so npm considers them identical.

## Root cause

`check.py` generates variants by swapping/stripping separators from the candidate name (`get_npm_variants()`). When the candidate has no separators (like `pgspec`), no useful variants are generated. The search API fallback (`_search_npm_similar()`) queries npm search but is unreliable.

## Investigation findings

- **npm search API fails completely:** `registry.npmjs.org/-/v1/search?text=pgspec&size=20` returns 0 results. The search is text-based, not normalization-based, so `pgspec` and `pg-spec` are unrelated queries.
- **npm suggestions endpoint doesn't exist:** `/-/v1/search/suggestions` returns 404. The `npmjs.com/search/suggestions` endpoint returns 403.
- **Increasing search `size` parameter wouldn't help:** Even with size=250, the search API ranks by relevance/popularity, not moniker similarity.
- **All three separators matter:** npm's official blog confirms dashes, dots, AND underscores are all stripped during collision checks. `react-native`, `reactnative`, `react_native`, and `react.native` all collide.
- **PyPI is unaffected:** PyPI normalizes by replacing runs of `.-_` with a single dash (PEP 503), preserving separator positions. `pgspec` and `pg-spec` are legitimately different packages on PyPI.

## Fix

**Bounded insertion:** For separator-free npm candidates, generate variants by inserting each separator (`-`, `.`, `_`) at every valid position, then `npm view` each.

For `pgspec` (6 chars): 5 positions x 3 separators = 15 `npm view` calls (~3s total).
For a 12-char name: 11 x 3 = 33 calls (~7s total).

Modify `get_npm_variants()` to detect when the candidate has no separators and generate insertion variants.

## Affected code

- `rlsbl/commands/check.py`, `get_npm_variants()` (lines ~184-197)

## Severity

High -- false positive on name availability leads to wasted release + project rename.
