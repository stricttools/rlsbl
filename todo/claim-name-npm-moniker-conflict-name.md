# claim-name / check-name (npm): conflict error reports the CHECK name, not the conflicting package

## Context

`rlsbl claim-name <name> --target npm` runs `check-name` first, and if the name
appears taken it aborts before publishing. For npm, "taken" can mean one of two
things:

1. A package with the *exact* name is registered.
2. The name **collides under npm's moniker normalization** -- npm strips all
   `-`, `.`, and `_` characters and lowercases before comparing, so `foo-bar`,
   `foobar`, and `foo.bar` are all the same "moniker" and only one can exist.

The collision path already computes the concrete conflicting package name(s) and
stores them in a human-readable `note` field on the result dict (in
`_check_single_name`):

- `result["note"] = f"moniker collision with '{hard[0]}' (npm strips punctuation)"`
- `result["note"] = f"moniker conflict with '{conflicts[0]}' (npm strips punctuation)"`

while `result["reason"]` is set to the internal machine tag `"moniker"`.

## Problem

Two related defects surface the wrong information to the user:

1. **`claim-name` prints the internal check name, not the conflict.** In the
   `claim-name` command, the "taken" branch reports
   `result.get("reason", "unknown reason")`, producing:

   > `Name '<name>' appears taken on npm: moniker.`

   The trailing word `moniker` is the *name of the similarity check that
   matched*, not the actual conflicting npm package (a dash/dot/underscore
   variant of the requested name whose moniker normalization collides). The user
   cannot tell WHAT conflicts or WHY. The concrete conflicting package name lives
   in `result["note"]`, which `claim-name` never reads.

2. **`check-name` (npm) also drops the note.** In the verbose formatter, the
   npm branch prints the reason *explanation* line but -- unlike the pypi,
   crates, and go branches -- never prints `result.get("note")`. So even a plain
   `rlsbl check-name <name> --target npm` tells the user the name is taken "by
   moniker" without naming the concrete package that collides, even though the
   information was computed and is sitting in the result dict.

Net effect: both the claim path and the check path withhold the one piece of
information the user needs (the concrete conflicting package name and the
normalization rule that matched), despite that information already being
computed.

## Solutions

### Option A -- Surface `note` in both output paths (minimal, most correct for the reported bug)

- In `claim-name`, when status is `taken`, prefer `result.get("note")` over the
  bare `reason` tag when composing the "appears taken" message (fall back to
  `reason` only when `note` is absent, e.g. `reason == "registered"`).
- In the `check-name` npm branch of the verbose formatter, add the same
  `if result.get("note"): print(f"  Note: {result['note']}")` block that the
  pypi/crates/go branches already have.

Pros:
- Fixes the exact reported symptom in both commands with a couple of lines.
- Reuses information already computed -- no new registry calls.
- Brings the npm formatter branch into parity with the other registries
  (removes an inconsistency that is itself a latent bug).

Cons:
- `note` currently reports only the *first* conflicting package (`hard[0]` /
  `conflicts[0]`); does not enumerate all conflicts (see Option B).

### Option B -- Report all conflicts plus the normalization rule (fuller fix)

- Change `_check_single_name` to store the full list of conflicting package
  names (not just `[0]`) and the specific normalization rule that matched
  (e.g. "npm strips dashes/dots/underscores"), then have both output paths list
  every concrete conflict.
- Structured (`--json`) output should include the conflict list as a field so
  scripts can consume it.

Pros:
- Most informative; a user with several near-name packages sees all of them.
- The normalization rule is stated explicitly, answering "why".

Cons:
- Larger change touching the result schema and any JSON consumers.
- The moniker/`_search_npm_similar` path can return several conflicts; deciding
  ordering/capping adds minor complexity.

### Option C -- Do both: Option A now, Option B as a follow-up

Land the parity fix (A) so no code path hides an already-computed conflict name,
then expand to full enumeration (B). Recommended if enumeration is wanted but
the immediate priority is to stop reporting the internal check tag as if it were
a package name.

## Affected files

- `rlsbl/commands/claim_name.py` -- `run_cmd`, the `status == "taken"` branch
  that prints `f"Name '{name}' appears taken on {target}: {reason}."` using
  `result.get("reason", ...)`.
- `rlsbl/commands/check.py`:
  - `_check_single_name` -- sets `result["reason"] = "moniker"` and the
    descriptive `result["note"]` for the npm hard-collision and
    `_search_npm_similar` paths (the two `note` assignments).
  - `_format_single_result` -- the `registry == "npm"` branch, which prints the
    reason explanation but omits the `result.get("note")` print that the pypi,
    crates, and go branches include.
  - Supporting helpers for context: `_search_npm_similar`, `get_npm_variants`,
    `_classify_variant_collisions`, and `normalize_npm`
    (in `rlsbl/targets/utils.py`).
- `tests/test_check.py`, `tests/test_check_name_multi_target.py` -- add
  regression coverage: assert that a moniker/normalization collision surfaces the
  concrete conflicting package name (not the bare check tag) in both `check-name`
  and `claim-name` output.

## Effort estimate

- Option A: small -- ~1 hour including a red-green regression test in each
  output path.
- Option B: medium -- half a day including result-schema change and JSON
  consumer updates.
- Option C: small + medium, staged.
