# changelog edit should support commit-list and batch-exclusion modification

## Context

`rlsbl changelog edit` modifies an existing entry's type, description,
and user-facing fields, selected by `--id` or `--commits`. It cannot
modify an entry's COMMIT LIST, and a batch exclusion can only be
created implicitly by `changelog add --allow-batch` for a new entry —
there is no way to create or update an exclusion for an existing
entry. A consumer needing either operation must hand-edit
`.rlsbl/changes/unreleased.jsonl` and `.rlsbl/config.json` — tool-owned
files the fleet philosophy says should never need hand edits.

## Why this is needed: a case study

A consumer project ran a long unreleased development cycle — several
hundred commits across multiple sequential work campaigns, all riding
one eventual release under the release-once-at-the-end principle. The
sequence that forces the missing operations:

1. Midway through the cycle a release looked imminent, so a full
   changelog pass covered every unreleased commit — features described
   as they then stood. The release was postponed and a further round
   of work was added; this happened TWICE.
2. The later rounds changed some already-described behaviors (a flag
   renamed before ever shipping; refusals added and lifted). Nothing
   had been released, so the honest changelog describes only the FINAL
   state — the existing entries must be EDITED, never paired with
   correction entries that would narrate intermediate states no user
   ever had. The text half of this works today (`changelog edit`).
3. The later rounds' commits that CONTINUE an already-entried feature
   (for example, reshaping a payload member of a feature that has
   never shipped) belong in that feature's existing entry's commit
   list. The only tool-shaped alternative is a content-free sibling
   entry holding the commits — which fragments the notes and misleads
   any reader of the JSONL, because the entry describing the behavior
   carries none of its implementation commits.
4. Appending commits can push an entry past `max_commits_per_entry`,
   so the batch exclusion must be created or updated in the same
   operation — also unsupported.
5. Result: the consumer explicitly sanctioned careful hand edits —
   line-count-preserving (the exclusion list keys entries by
   version+line, so any inserted or deleted line invalidates it),
   dry-run previewed, occurrence-asserted, diff-reviewed, and
   validated with `rlsbl check --tag changelog` afterwards. This was
   the SECOND such sanction in that project: an earlier campaign had
   to hand-repair a writer's own line-shift error because no entry
   deletion or commit-list edit exists.

The point: this is not a convenience gap. Under release-once with
possible postponements, edit debt on existing entries is structural —
and the commit-list half of that debt has no tool path, so the
"never hand-edit tool-owned files" rule gets exceptions in exactly the
place the tool exists to make exceptions unnecessary.

## Solutions

1. **`changelog edit --add-commits <shas>` / `--remove-commits <shas>`**
   with the same validation `changelog add` performs (hash resolution,
   unreleased-range membership, monorepo scope), plus batch-exclusion
   creation/update behind an explicit `--allow-batch`-style flag with a
   mandatory reason.
   - Pros: closes both operations inside the tool with existing
     validation; hand edits become unnecessary.
   - Cons: `edit`'s surface grows.
2. **A dedicated `changelog amend-commits` command.**
   - Pros: keeps `edit` field-only.
   - Cons: a second command for one entry-mutation concept.
3. **Status quo plus a documented hand-edit procedure.**
   - Pros: no work.
   - Cons: a documented hand edit of a tool-owned file is a standing
     contradiction of the no-escape-hatch philosophy.

Related design note: the exclusion list's version+line keying is what
makes hand edits hazardous at all; if this work touches the exclusion
machinery, keying exclusions by entry id instead of line number would
remove the hazard class entirely.

## Affected surface

- The changelog edit command (or a sibling) and its validation
- The batch-limits machinery and exclusion storage

## Effort

Small to moderate — the validation building blocks exist in
`changelog add`.
