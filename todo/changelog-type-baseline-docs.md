# State the changelog type baseline explicitly: types are relative to the last release tag

## Context

The changelog system classifies entries as breaking/feature/fix, with
the user-focused test "would a user who upgrades read this and think
'that affects me'?". An upgrading user comes from the last released
version, so the test is inherently release-relative — but the
documentation never states that baseline explicitly.

## Problem

Without a stated baseline, a writer can classify tree-relatively:
marking an entry breaking because it refuses input that worked earlier
in the same unreleased development cycle, even when the surface being
restricted has never shipped in any release. The generated release
notes then pair "Breaking: X refused" with the feature entry
introducing X in the same version — noise that dilutes the breaking
entries an upgrading user actually must act on. This ambiguity produced
real misclassifications during a consumer project's pre-release review.

## Solutions

1. **Docs clarification (recommended).** One or two sentences in the
   changelog documentation's entry-type definitions: types are relative
   to the last release tag — an entry is breaking only if it refuses or
   changes input or behavior that worked in the most recently released
   version; restrictions on surfaces introduced within the same
   unreleased cycle belong inside those features' own entries, stated
   in the entry text. Optionally mirror the sentence in
   `changelog add`'s `--type` help.
   - Pros: fleet-wide single authority for what the types mean;
     minutes of work.
   - Cons: none.
2. **Also render the baseline in generated output** (e.g. breaking
   sections annotated with the version they break from).
   - Pros: extra precision for readers.
   - Cons: cosmetic; touches the generator for little gain.
3. **Heuristic enforcement check** (flag breaking entries whose commits
   only touch files created since the last tag).
   - Pros: mechanical pressure.
   - Cons: the semantic fact ("does this entry refuse input that worked
     at the last tag?") is not mechanically checkable; a
     frequently-wrong warning trains agents to ignore warnings.
     Rejected.

## Affected files

- The changelog documentation section defining entry types
- Optionally the `--type` flag help text

## Effort

Minutes — a sentence or two of documentation.
